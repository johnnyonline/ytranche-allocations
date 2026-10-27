"""The allocation rules. All the math lives here; every number comes from config."""

import copy
import datetime as dt
import math
from dataclasses import dataclass, field

from allocations.config import DEFAULT_EXIT_DAYS, YIELD_TYPES, Dependency, Rules, Scenario, Strategy, Use, Vault

OK, WARNING, NEEDS_DATA, BREACH, NA = "OK", "Warning", "Needs data", "Breach", "n/a"
SEVERITY = {NA: 0, OK: 1, WARNING: 2, NEEDS_DATA: 3, BREACH: 4}
TRANCHE_TOLERANCE = 0.01  # warn if the tranche sizes are further than this from TVL


class MoveError(Exception):
    pass


@dataclass
class Missing:
    """A null input that went into a rule, counted as 0."""

    owner: str  # strategy id, or "vault"
    field: str
    gain: float = 0.0  # the most a real value could add to the rule's value (0: it could only make things worse)


@dataclass
class Rule:
    number: int
    description: str
    unit: str  # how value and limit read: junior | tvl | fixed | amount | share | rate | days
    sign: str  # what OK means: value ≤, ≥ or > limit
    value: float | None = None
    limit: float | None = None
    status: str = NA
    worst: list[str] = field(default_factory=list)  # rules 1, 2, 3, 10 and 12: the worst item they check
    missing: list[Missing] = field(default_factory=list)


@dataclass
class Link:
    """One strategy's exposure to one of its dependencies."""

    strategy: Strategy
    use: Use
    exposure: float
    loss_rate: float
    loss: float


@dataclass
class DependencyRow:
    name: str
    exposure: float
    loss: float
    loss_rates: list[float]
    rule_1: str
    rule_2: str


@dataclass
class GroupRow:
    """Rule 12: everything at risk score `score` or riskier."""

    score: int
    shares: dict[str, float]  # strategy id -> how much of it counts toward the group
    exposure: float
    limit: float
    status: str


@dataclass
class ScenarioRow:
    name: str
    loss: float
    picked: dict[str, str]  # dependency type -> the dependency biggest_<type> picked


@dataclass
class CapRow:
    strategy: Strategy
    cap: float
    rule: int | None  # what sets the cap
    dependency: str | None  # or the risk group, for rule 12


@dataclass
class Move:
    amount: float
    source: str  # strategy id or idle
    target: str


@dataclass
class Report:
    numbers: Rules
    vault: Vault
    today: dt.date
    tvl: float
    junior: float
    owed: float
    tranche_sizes: float
    rules: list[Rule]
    dependencies: list[DependencyRow]
    groups: list[GroupRow]
    scenarios: list[ScenarioRow]
    reliable: dict[str, tuple[float, float]]  # yield type -> (amount, reliable yield a year)
    break_even_low: float | None
    max_fixed_target: float | None
    caps: list[CapRow]

    @property
    def tranches_add_up(self) -> bool:
        return abs(self.tranche_sizes - self.tvl) <= TRANCHE_TOLERANCE * self.tvl


def run(numbers: Rules, dependencies: dict[str, Dependency], vault: Vault, today: dt.date) -> Report:
    """Runs every rule on the vault's book."""
    strategies, margin, fixed = vault.strategies, numbers.warning_margin, vault.fixed
    tranches = (fixed, vault.levered, vault.equity)
    tvl = vault.idle + sum(s.amount for s in strategies)
    junior = vault.levered.size + vault.equity.size - (vault.junior_withdrawals_in_cooldown or 0)
    owed = sum(t.size * t.target for t in tranches if t.target is not None)
    junior_missing = []  # counting it as 0 can only make the junior look bigger
    if vault.junior_withdrawals_in_cooldown is None:
        junior_missing.append(Missing("vault", "junior_withdrawals_in_cooldown"))

    # Rules 1 and 2: one dependency breaking
    links = [_link(s, use, numbers, dependencies) for s in strategies for use in s.dependencies]
    limit_1 = numbers.one_dependency_max_loss * junior
    limit_2 = numbers.one_dependency_max_share * tvl
    rows = _dependency_rows(links, dependencies, limit_1, limit_2, margin)
    rule_1 = _worst(
        Rule(1, "Loss if one dependency breaks", "junior", "≤", missing=list(junior_missing)),
        [(row.name, row.loss, limit_1, row.rule_1) for row in rows],
    )
    rule_2 = _worst(
        Rule(2, "TVL in one dependency", "tvl", "≤"),
        [(row.name, row.exposure, limit_2, row.rule_2) for row in rows],
    )

    # Rule 3: worst-case scenarios
    limit_3 = numbers.worst_case_max_loss * junior
    scenarios = [_scenario_loss(scenario, strategies, links, rows, dependencies) for scenario in vault.scenarios]
    rule_3 = _worst(
        Rule(3, "Worst-case scenario loss", "junior", "≤", missing=list(junior_missing)),
        [(row.name, row.loss, limit_3, _at_most(row.loss, limit_3, margin)) for row in scenarios],
    )

    # Rule 4: reliable yield vs yield we owe
    cover = numbers.reliable_yield_min_cover
    reliable, missing_4 = _reliable_yield(strategies, vault.idle)
    reliable_total = sum(y for _, y in reliable.values())
    rule_4 = _check(
        Rule(4, f"Reliable yield vs {cover:g}x yield we owe", "amount", "≥", missing=missing_4),
        reliable_total,
        cover * owed,
        margin,
    )
    variable_amount = reliable["variable"][0]
    not_variable = reliable["locked"][1] + reliable["steady"][1]
    break_even_low = (cover * owed - not_variable) / variable_amount if variable_amount else None

    # Rule 5: how much of the yield we owe over the window is locked in
    window = numbers.locked_in_window_days
    locked_in, missing_5 = _locked_in(strategies, today, window)
    rule_5 = Rule(5, f"Yield we owe over the next {window:g} days, locked in", "share", "≥")
    if owed:
        rule_5.missing = missing_5
        rule_5 = _check(rule_5, locked_in / (owed * window / 365), numbers.locked_in_min_share, margin)

    # Rules 6 and 7: cash for withdrawals
    cash_now, missing_6 = _cash(strategies, vault.idle, numbers.lending_haircut, within_24h=False)
    rule_6 = _check(
        Rule(6, "Cash in one transaction", "fixed", "≥", missing=missing_6),
        cash_now,
        numbers.cash_one_tx_min * fixed.size,
        margin,
    )
    cash_24h, missing_7 = _cash(strategies, vault.idle, numbers.lending_haircut, within_24h=True)
    if vault.top_fixed_holders is None:  # only the share part is used
        missing_7.append(Missing("vault", "top_fixed_holders"))
    if vault.junior_withdrawals_due_7d is None:
        missing_7.append(Missing("vault", "junior_withdrawals_due_7d"))
    fixed_exits = max(vault.top_fixed_holders or 0, numbers.cash_24h_min * fixed.size)
    junior_exits = vault.junior_withdrawals_due_7d or 0
    rule_7 = _check(
        Rule(7, "Cash within 24h", "amount", "≥", missing=missing_7), cash_24h, fixed_exits + junior_exits, margin
    )

    # Rule 8: junior size
    rule_8 = Rule(8, "Junior as % of TVL", "share", "≥")
    if tvl:
        rule_8.missing = list(junior_missing)
        rule_8 = _check(rule_8, junior / tvl, numbers.junior_min_share, margin)

    # Rule 9: junior cooldown
    rule_9 = _cooldown(vault, margin)

    # Rule 10: new strategies start small
    new = [s for s in strategies if s.added is not None and (today - s.added).days < 7 * numbers.new_strategy_weeks]
    new_limits = [numbers.new_strategy_share * _cap(s, links, rows, limit_1, None)[0] for s in new]
    rule_10 = _worst(
        Rule(
            10,
            f"New strategies vs {pct(numbers.new_strategy_share)} of their rule 1 cap",
            "amount",
            "≤",
            missing=list(junior_missing) if new else [],
        ),
        [(s.id, s.amount, limit, _at_most(s.amount, limit, margin)) for s, limit in zip(new, new_limits)],
    )

    # Rule 11: the highest fixed target rule 4 allows
    rule_11 = Rule(11, "Fixed tranche target vs max fixed target", "rate", "≤")
    max_fixed_target = None
    if fixed.target is not None and fixed.size:
        rule_11.missing = list(missing_4)
        others_owed = owed - fixed.size * fixed.target
        max_fixed_target = (reliable_total / cover - others_owed) / fixed.size
        rule_11 = _check(rule_11, fixed.target, max_fixed_target, margin)
        if rule_4.status == NEEDS_DATA:
            rule_11.status = NEEDS_DATA

    # Rule 12: TVL at each risk score or riskier
    groups = _group_rows(strategies, dependencies, numbers.risk_group_max_share, tvl, margin)
    rule_12 = _worst(
        Rule(12, "TVL at a risk score or riskier", "tvl", "≤"),
        [(f"risk {g.score}+", g.exposure, g.limit, g.status) for g in groups],
    )

    return Report(
        numbers=numbers,
        vault=vault,
        today=today,
        tvl=tvl,
        junior=junior,
        owed=owed,
        tranche_sizes=sum(t.size for t in tranches),
        rules=[rule_1, rule_2, rule_3, rule_4, rule_5, rule_6, rule_7, rule_8, rule_9, rule_10, rule_11, rule_12],
        dependencies=rows,
        groups=groups,
        scenarios=scenarios,
        reliable=reliable,
        break_even_low=break_even_low,
        max_fixed_target=max_fixed_target,
        caps=[CapRow(s, *_cap(s, links, rows, limit_1, limit_2, groups)) for s in strategies],
    )


def move(vault: Vault, moves: list[Move]) -> Vault:
    """A copy of the vault with the moves applied, in order."""
    after = copy.deepcopy(vault)
    by_id = {s.id: s for s in after.strategies}
    for m in moves:
        for end in (m.source, m.target):
            if end != "idle" and end not in by_id:
                raise MoveError(f"unknown strategy id '{end}' (known: idle, {', '.join(by_id)})")
        if m.source == m.target:
            raise MoveError(f"can't move from {m.source} to itself")
        held = after.idle if m.source == "idle" else by_id[m.source].amount
        if m.amount > held and not math.isclose(m.amount, held, rel_tol=1e-9):
            raise MoveError(f"{m.source} holds {_plain(held)}, can't move {_plain(m.amount)} out of it")
        amount = min(m.amount, held)  # moving "all of it" leaves exactly 0
        if m.source == "idle":
            after.idle -= amount
        else:
            by_id[m.source].amount -= amount
        if m.target == "idle":
            after.idle += amount
        else:
            by_id[m.target].amount += amount
    return after


def _link(s: Strategy, use: Use, numbers: Rules, dependencies: dict[str, Dependency]) -> Link:
    rate = use.loss if use.loss is not None else numbers.loss_if_it_breaks[dependencies[use.name].risk_score]
    rate = min(rate + s.exit_discount, 1.0)
    exposure = s.amount * s.leverage * use.weight
    loss = min(exposure * rate, s.amount * use.weight)  # a loop can't lose more than we put in
    return Link(s, use, exposure, rate, loss)


def _dependency_rows(
    links: list[Link], dependencies: dict[str, Dependency], limit_1: float, limit_2: float, margin: float
) -> list[DependencyRow]:
    rows = []
    for name in dependencies:
        mine = [link for link in links if link.use.name == name]
        if mine:
            exposure, loss = sum(link.exposure for link in mine), sum(link.loss for link in mine)
            rates = sorted({link.loss_rate for link in mine})
            rows.append(
                DependencyRow(
                    name, exposure, loss, rates, _at_most(loss, limit_1, margin), _at_most(exposure, limit_2, margin)
                )
            )
    return rows


def _group_rows(
    strategies: list[Strategy],
    dependencies: dict[str, Dependency],
    caps: dict[int, float | None],
    tvl: float,
    margin: float,
) -> list[GroupRow]:
    """Rule 12. A strategy counts toward "risk N or riskier" by the weights of its dependencies scored N or more."""
    rows = []
    for score, cap in caps.items():
        if cap is None:  # no cap
            continue
        shares: dict[str, float] = {}
        for s in strategies:
            share = min(sum(use.weight for use in s.dependencies if dependencies[use.name].risk_score >= score), 1.0)
            if share > 0:
                shares[s.id] = share
        if shares:
            exposure = sum(s.amount * s.leverage * shares[s.id] for s in strategies if s.id in shares)
            limit = cap * tvl
            rows.append(GroupRow(score, shares, exposure, limit, _at_most(exposure, limit, margin)))
    return rows


def _scenario_loss(
    scenario: Scenario,
    strategies: list[Strategy],
    links: list[Link],
    rows: list[DependencyRow],
    dependencies: dict[str, Dependency],
) -> ScenarioRow:
    hits = {s.id: 0.0 for s in strategies}
    picked = {}
    for shock in scenario.shocks:
        if shock.dependency is None:
            for s in strategies:
                if shock.tag in s.tags:
                    hits[s.id] += shock.loss * s.amount * s.leverage
            continue
        name = shock.dependency
        if name.startswith("biggest_"):
            kind = name.removeprefix("biggest_")
            candidates = [row for row in rows if dependencies[row.name].type == kind]
            if not candidates:
                picked[kind] = "none"
                continue
            name = picked[kind] = max(candidates, key=lambda row: row.exposure).name
        for link in links:
            if link.use.name == name:
                rate = shock.loss
                if link.use.role == "collateral" and shock.collateral_loss is not None:
                    rate = shock.collateral_loss
                hits[link.strategy.id] += rate * link.exposure
    return ScenarioRow(scenario.name, sum(min(hits[s.id], s.amount) for s in strategies), picked)


def _reliable_yield(strategies: list[Strategy], idle: float) -> tuple[dict[str, tuple[float, float]], list[Missing]]:
    """Reliable yield a year, by yield type: locked and steady at apy minus incentives, variable at its 90-day low."""
    amounts = dict.fromkeys(YIELD_TYPES, 0.0)
    yields = dict.fromkeys(YIELD_TYPES, 0.0)
    missing: list[Missing] = []
    for s in strategies:
        if s.amount <= 0:  # nothing it could add, so nothing missing either
            continue
        amounts[s.yield_type] += s.amount
        if s.yield_type in ("locked", "steady"):
            if s.apy is None:
                missing.append(Missing(s.id, "apy", math.inf))
            if s.incentive_apy is None:
                missing.append(Missing(s.id, "incentive_apy"))
            yields[s.yield_type] += s.amount * ((s.apy or 0) - (s.incentive_apy or 0))
        elif s.yield_type == "variable":
            if s.apy_90d_low is None:
                missing.append(Missing(s.id, "apy_90d_low", math.inf))
            yields["variable"] += s.amount * (s.apy_90d_low or 0)
    reliable = {kind: (amounts[kind], yields[kind]) for kind in YIELD_TYPES}
    reliable["idle"] = (idle, 0.0)
    return reliable, missing


def _locked_in(strategies: list[Strategy], today: dt.date, window: float) -> tuple[float, list[Missing]]:
    """Yield locked in over the window: amount × apy × days to maturity (at most the window) ÷ 365."""
    total = 0.0
    missing: list[Missing] = []
    for s in strategies:
        if s.yield_type != "locked" or s.amount <= 0:
            continue
        if s.maturity is None:  # assume it falls after the window
            missing.append(Missing(s.id, "maturity"))
            days = window
        else:
            days = max(0, min((s.maturity - today).days, window))
        if s.apy is None and days > 0:
            missing.append(Missing(s.id, "apy", math.inf))
        total += s.amount * (s.apy or 0) * days / 365
    return total, missing


def _cash(strategies: list[Strategy], idle: float, haircut: float, within_24h: bool) -> tuple[float, list[Missing]]:
    """Cash we could pay out in one transaction (rule 6) or within 24h (rule 7)."""
    total = idle
    missing: list[Missing] = []
    for s in strategies:
        if s.amount <= 0:
            continue
        kind = s.cash
        if within_24h and kind == "other_chain":
            kind = s.after_bridge or kind
        if kind == "instant":
            total += s.amount
        elif kind == "lending":
            if s.withdrawable_now is None:
                missing.append(Missing(s.id, "withdrawable_now", haircut * s.amount))
            total += haircut * min(s.withdrawable_now or 0, s.amount)
        elif kind == "dex" and within_24h:
            if s.sellable_24h is None:
                missing.append(Missing(s.id, "sellable_24h", s.amount))
            total += min(s.sellable_24h or 0, s.amount)
    return total, missing


def _cooldown(vault: Vault, margin: float) -> Rule:
    """Rule 9: the junior cooldown must be longer than the slowest exit and the time a loss takes to show up."""
    rule = Rule(9, "Junior cooldown vs slowest exit and loss reporting", "days", ">")
    if vault.loss_report_days is None:
        rule.missing.append(Missing("vault", "loss_report_days"))
    days = [vault.loss_report_days or 0.0]
    for s in vault.strategies:
        if s.amount <= 0:  # nothing to exit
            continue
        if s.exit_days is None and s.cash == "locked":
            rule.missing.append(Missing(s.id, "exit_days"))
        days.append(s.exit_days if s.exit_days is not None else DEFAULT_EXIT_DAYS.get(s.cash, 0.0))
    if vault.junior_cooldown_days is None:
        rule.missing.append(Missing("vault", "junior_cooldown_days", math.inf))
    return _check(rule, vault.junior_cooldown_days or 0.0, max(days), margin)


def _cap(
    s: Strategy,
    links: list[Link],
    rows: list[DependencyRow],
    limit_1: float,
    limit_2: float | None,
    groups: list[GroupRow] | None = None,
) -> tuple[float, int | None, str | None]:
    """The most the strategy could hold under rule 1 (and rules 2 and 12 if given) if everything else stays the same."""
    totals = {row.name: row for row in rows}
    options: list[tuple[int, float, float, str]] = []  # (rule, room left, room used per unit held, what sets it)
    for link in links:
        if link.strategy is not s:
            continue
        row, weight = totals[link.use.name], link.use.weight
        options.append(
            (1, limit_1 - (row.loss - link.loss), min(s.leverage * weight * link.loss_rate, weight), link.use.name)
        )
        if limit_2 is not None:
            options.append((2, limit_2 - (row.exposure - link.exposure), s.leverage * weight, link.use.name))
    for group in groups or []:
        share = group.shares.get(s.id, 0.0)
        room = group.limit - (group.exposure - s.amount * s.leverage * share)
        options.append((12, room, s.leverage * share, f"risk {group.score}+"))
    best: tuple[float, int | None, str | None] = (math.inf, None, None)
    for rule, room, per_unit, by in options:
        if per_unit > 0 and room / per_unit < best[0]:
            best = (room / per_unit, rule, by)
    return max(best[0], 0.0), best[1], best[2]


def _at_most(value: float, limit: float, margin: float) -> str:
    if value > limit:
        return BREACH
    return WARNING if value > (1 - margin) * limit else OK


def _check(rule: Rule, value: float, limit: float, margin: float) -> Rule:
    """Sets the rule's value, limit and status."""
    rule.value, rule.limit = value, limit
    if rule.sign == "≤":
        rule.status = _at_most(value, limit, margin)
    else:  # ≥, or > for strictly more
        short = value <= limit if rule.sign == ">" else value < limit
        rule.status = BREACH if short else WARNING if value < (1 + margin) * limit else OK
    return _needs_data(rule)


def _worst(rule: Rule, items: list[tuple[str, float, float, str]]) -> Rule:
    """For rules checked per dependency, scenario, strategy or risk group: (label, value, limit, status) of each."""
    if not items:
        return rule

    def badness(item: tuple[str, float, float, str]) -> tuple[int, float, float]:
        _, value, limit, status = item
        share = value / limit if limit > 0 else math.inf if value > 0 else 0.0  # 0 of a 0 limit is nothing used
        return SEVERITY[status], share, value

    top = max(items, key=badness)
    _, rule.value, rule.limit, rule.status = top
    rule.worst = [item[0] for item in items if item[1:] == top[1:]]
    return _needs_data(rule)


def _needs_data(rule: Rule) -> Rule:
    """A failing rule is Needs data if its missing inputs, filled in, could fix it.

    Only minimum rules (≥, >) have inputs like that: a missing input in rules 1, 2, 3 and 10 could only make them worse.
    """
    if rule.status != BREACH or rule.sign == "≤" or rule.value is None or rule.limit is None:
        return rule
    best = rule.value + sum(m.gain for m in rule.missing)
    if best > rule.limit or (rule.sign == "≥" and best == rule.limit):
        rule.status = NEEDS_DATA
    return rule


def pct(x: float) -> str:
    """A config share as a percentage: 0.25 -> 25%, 0.005 -> 0.5%."""
    return f"{x * 100:g}%"


def _plain(x: float) -> str:
    return f"{x:,.2f}".removesuffix(".00")
