"""Prints reports as plain-text or markdown tables."""

import math

from allocations.checks import OK, SEVERITY, DependencyRow, Move, Report, Rule, pct


def full(report: Report, markdown: bool) -> str:
    """Rules, dependencies, details and missing data."""
    out = _Out(report, markdown)
    out.header("allocation check")
    out.heading("Rules")
    out.table(
        ["#", "Rule", "Value", "Limit", "Status"],
        [
            [
                str(r.number),
                r.description + _worst(r.worst),
                out.value(r, r.value),
                out.limit(r, r.limit),
                r.status,
            ]
            for r in report.rules
        ],
    )
    out.heading("Dependencies")
    out.table(
        ["Dependency", "Exposure (% of TVL)", "Loss rate", "Loss if it breaks (% of junior)", "Status"],
        [
            [
                d.name,
                out.share(d.exposure, report.tvl),
                _rates(d.loss_rates),
                out.share(d.loss, report.junior),
                _dependency_status(d),
            ]
            for d in sorted(report.dependencies, key=lambda d: -d.loss)
        ],
    )
    _details(out)
    _missing(out)
    return out.text()


def caps(report: Report, markdown: bool, header: bool = True) -> str:
    """How much each strategy can hold under rules 1 and 2."""
    out = _Out(report, markdown)
    if header:
        out.header("caps")
    out.heading("Caps")
    out.paragraph(
        "The most each strategy could hold under rules 1 and 2 if everything else stays the same (same TVL, same junior)."
    )
    rows = []
    for c in sorted(report.caps, key=lambda c: c.cap - c.strategy.amount):
        if math.isinf(c.cap):  # no dependency limits it
            rows.append([c.strategy.id, out.amount(c.strategy.amount), "no limit", "-", "-"])
        else:
            room = f"{c.cap - c.strategy.amount:+,.{out.decimals}f}"
            rows.append(
                [
                    c.strategy.id,
                    out.amount(c.strategy.amount),
                    out.amount(c.cap),
                    room,
                    f"rule {c.rule}, {c.dependency}",
                ]
            )
    out.table(["Strategy", "Amount", "Cap", "Room", "Set by"], rows, right=(1, 2, 3))
    return out.text()


def moved(before: Report, after: Report, moves: list[Move], markdown: bool) -> str:
    """Every rule before -> after the moves."""
    out = _Out(after, markdown)
    out.header("what if")
    out.heading("Moves")
    out.paragraph(
        "Moving " + ", then ".join(f"{out.amount(m.amount)} from {m.source} to {m.target}" for m in moves) + "."
    )
    touched = dict.fromkeys(end for m in moves for end in (m.source, m.target))
    out.table(
        ["Strategy", "Before", "After"],
        [[name, out.amount(_held(before, name)), out.amount(_held(after, name))] for name in touched],
        right=(1, 2),
    )
    out.heading("Rules")
    rows = []
    for b, a in zip(before.rules, after.rules):
        limit = (
            out.limit(a, a.limit)
            if _same(b.limit, a.limit)
            else f"{a.sign} {out.value(b, b.limit)} → {out.value(a, a.limit)}"
        )
        rows.append(
            [
                str(a.number),
                a.description + _worst(a.worst, b.worst),
                out.value(b, b.value),
                out.value(a, a.value),
                limit,
                _change(b.status, a.status),
            ]
        )
    out.table(["#", "Rule", "Before", "After", "Limit", "Status"], rows)
    out.heading("Dependencies that changed")
    old = {d.name: d for d in before.dependencies}
    out.table(
        ["Dependency", "Exposure (% of TVL)", "Loss if it breaks (% of junior)", "Status"],
        [
            [
                d.name,
                f"{out.share(old[d.name].exposure, before.tvl)} → {out.share(d.exposure, after.tvl)}",
                f"{out.share(old[d.name].loss, before.junior)} → {out.share(d.loss, after.junior)}",
                _change(_dependency_worst(old[d.name]), _dependency_worst(d)),
            ]
            for d in sorted(after.dependencies, key=lambda d: -d.loss)
            if not (_same(old[d.name].exposure, d.exposure) and _same(old[d.name].loss, d.loss))
        ],
    )
    return out.text()


def _details(out: "_Out") -> None:
    r, numbers = out.report, out.report.numbers
    out.heading("Worst-case scenarios (rule 3)")
    out.table(
        ["Scenario", "Loss (% of junior)", "Picked"],
        [
            [
                s.name,
                out.share(s.loss, r.junior),
                ", ".join(f"biggest {kind}: {name}" for kind, name in s.picked.items()) or "-",
            ]
            for s in r.scenarios
        ],
    )
    out.heading("Reliable yield (rules 4 and 11)")
    rows = [[kind, out.amount(amount), out.amount(yearly)] for kind, (amount, yearly) in r.reliable.items()]
    rows.append(
        [
            "total",
            out.amount(sum(a for a, _ in r.reliable.values())),
            out.amount(sum(y for _, y in r.reliable.values())),
        ]
    )
    out.table(["Yield type", "Amount", "Reliable yield a year"], rows, right=(1, 2))
    cover = numbers.reliable_yield_min_cover
    notes = [f"Yield we owe: {out.amount(r.owed)} a year. Rule 4 needs {cover:g}x that: {out.amount(cover * r.owed)}."]
    if r.break_even_low is not None and r.break_even_low <= 0:
        notes.append("Break-even 90-day low: none needed, locked and steady yield already pass rule 4.")
    elif r.break_even_low is not None:
        notes.append(
            f"Break-even 90-day low: the variable strategies need to average {r.break_even_low:.2%} for rule 4 to pass."
        )
    if r.max_fixed_target is not None and r.vault.fixed.target is not None:
        notes.append(f"Max fixed target: {r.max_fixed_target:.2%} (now {r.vault.fixed.target:.2%}).")
    out.notes(notes)


def _missing(out: "_Out") -> None:
    r, numbers = out.report, out.report.numbers
    found: dict[str, dict[str, set[int]]] = {}
    for rule in r.rules:
        for m in rule.missing:
            found.setdefault(m.owner, {}).setdefault(m.field, set()).add(rule.number)
    out.heading("Missing data")
    if not found:
        out.paragraph("None.")
        return
    owners = [s.id for s in r.vault.strategies if s.id in found]
    if "vault" in found:
        owners.append("vault")
    out.table(
        ["Strategy or vault", "Missing (rules it affects)"],
        [
            [owner, ", ".join(f"{name} ({_rule_list(rules)})" for name, rules in found[owner].items())]
            for owner in owners
        ],
    )
    out.paragraph(
        f"Missing inputs count as 0, except a missing maturity (assumed after the {numbers.locked_in_window_days:g}-day window)"
        f" and top_fixed_holders (only the {pct(numbers.cash_24h_min)} of fixed tranche part is used)."
    )


class _Out:
    """Collects output. The same calls print plain text or markdown."""

    def __init__(self, report: Report, markdown: bool) -> None:
        self.report = report
        self.markdown = markdown
        self.decimals = 0 if report.tvl >= 100_000 else 2  # whole numbers for USD-sized books, decimals for ETH
        self.lines: list[str] = []
        self.after_heading = False

    def text(self) -> str:
        return "\n".join(self.lines).strip("\n")

    def header(self, title: str) -> None:
        r = self.report
        self.heading(f"{r.vault.name} {title} · {r.today}", level=1)
        self.paragraph(
            f"TVL {self.amount(r.tvl)} {r.vault.asset} · junior {self.amount(r.junior)} ({_share(r.junior, r.tvl)} of TVL)"
        )
        if not r.tranches_add_up:
            self.paragraph(f"Warning: the tranche sizes add up to {self.amount(r.tranche_sizes)}, not the TVL.")

    def heading(self, text: str, level: int = 2) -> None:
        self.lines += ["", f"{'#' * level} {text}" if self.markdown else text]
        self.after_heading = True

    def paragraph(self, text: str) -> None:
        self._block([text])

    def notes(self, lines: list[str]) -> None:
        self._block([f"- {line}" for line in lines] if self.markdown else lines)

    def table(self, headers: list[str], rows: list[list[str]], right: tuple[int, ...] = ()) -> None:
        if not rows:
            self.paragraph("None.")
        elif self.markdown:
            align = ["---:" if i in right else "---" for i in range(len(headers))]
            self._block([_markdown_row(cells) for cells in (headers, align, *rows)])
        else:
            widths = [max(map(len, column)) for column in zip(headers, *rows)]

            def line(cells: list[str]) -> str:
                return "  ".join(
                    c.rjust(w) if i in right else c.ljust(w) for i, (c, w) in enumerate(zip(cells, widths))
                ).rstrip()

            self._block([line(headers), line(["-" * w for w in widths]), *map(line, rows)])

    def amount(self, x: float) -> str:
        return f"{x:,.{self.decimals}f}"

    def share(self, x: float, whole: float) -> str:
        return f"{self.amount(x)} ({_share(x, whole)})"

    def value(self, rule: Rule, x: float | None) -> str:
        if x is None:
            return "-"
        if math.isinf(x):  # e.g. rule 10's limit for a strategy with no dependencies
            return "no limit"
        whole = self._whole(rule)
        if whole:
            return self.share(x, whole[0])
        if rule.unit == "share":
            return f"{x:.1%}"
        if rule.unit == "rate":
            return f"{x:.2%}"
        if rule.unit == "days":
            return f"{x:g} days"
        return self.amount(x)

    def limit(self, rule: Rule, x: float | None) -> str:
        if x is None:
            return "-"
        if math.isinf(x):
            return "no limit"
        whole = self._whole(rule)
        if whole:
            return f"{rule.sign} {self.amount(x)} ({_share(x, whole[0])} of {whole[1]})"
        return f"{rule.sign} {self.value(rule, x)}"

    def _whole(self, rule: Rule) -> tuple[float, str] | None:
        """What the rule's amounts are a share of."""
        r = self.report
        return {
            "junior": (r.junior, "junior"),
            "tvl": (r.tvl, "TVL"),
            "fixed": (r.vault.fixed.size, "fixed tranche"),
        }.get(rule.unit)

    def _block(self, lines: list[str]) -> None:
        if self.markdown or not self.after_heading:
            self.lines.append("")
        self.lines += lines
        self.after_heading = False


def _markdown_row(cells: list[str]) -> str:
    return "| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |"


def _share(x: float, whole: float) -> str:
    return f"{x / whole:.1%}" if whole else "-"


def _rates(rates: list[float]) -> str:
    return pct(rates[0]) if len(rates) == 1 else f"{pct(rates[0])} to {pct(rates[-1])}"


def _dependency_worst(d: DependencyRow) -> str:
    return max(d.rule_1, d.rule_2, key=SEVERITY.__getitem__)


def _dependency_status(d: DependencyRow) -> str:
    worst = _dependency_worst(d)
    rules = [n for n, status in ((1, d.rule_1), (2, d.rule_2)) if status == worst]
    return worst if worst == OK else f"{worst} ({_rule_list(rules)})"


def _rule_list(numbers: list[int] | set[int]) -> str:
    ordered = sorted(numbers)
    return ("rules " if len(ordered) > 1 else "rule ") + ", ".join(map(str, ordered))


def _worst(worst: list[str], before: list[str] | None = None) -> str:
    """The worst dependency, scenario or strategy, shown after the rule's description."""
    if before is None or before == worst:
        return f" ({', '.join(worst)})" if worst else ""
    return f" ({', '.join(before) or '-'} → {', '.join(worst) or '-'})"


def _change(before: str, after: str) -> str:
    if before == after:
        return after
    return f"{before} → {after} ({'worse' if SEVERITY[after] > SEVERITY[before] else 'better'})"


def _held(report: Report, name: str) -> float:
    return report.vault.idle if name == "idle" else next(s.amount for s in report.vault.strategies if s.id == name)


def _same(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is b
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
