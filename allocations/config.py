"""Loads the YAML config into plain dataclasses.

This is the only place that reads data. To swap a config value for an onchain read later,
fill it in here and nothing else has to change.
"""

import datetime as dt
import math
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

YIELD_TYPES = ("locked", "steady", "variable", "incentive")
CASH_TYPES = ("instant", "lending", "dex", "other_chain", "locked")
BRIDGED_CASH_TYPES = ("instant", "lending", "dex")
DEPENDENCY_TYPES = ("protocol", "stablecoin", "chain", "collateral")
BIGGEST = tuple(f"biggest_{kind}" for kind in DEPENDENCY_TYPES)  # scenario shorthands, e.g. biggest_stablecoin
DEFAULT_EXIT_DAYS = {"instant": 0.0, "lending": 1.0, "dex": 1.0, "other_chain": 2.0}

STRATEGY_FIELDS = (
    "id",
    "name",
    "amount",
    "apy",
    "incentive_apy",
    "yield",
    "apy_90d_low",
    "maturity",
    "leverage",
    "exit_discount",
    "cash",
    "withdrawable_now",
    "sellable_24h",
    "after_bridge",
    "exit_days",
    "added",
    "tags",
    "dependencies",
)
VAULT_FIELDS = (
    "name",
    "asset",
    "idle",
    "tranches",
    "junior_withdrawals_in_cooldown",
    "junior_withdrawals_due_7d",
    "top_fixed_holders",
    "junior_cooldown_days",
    "loss_report_days",
    "strategies",
    "scenarios",
)

REQUIRED: Any = object()


class ConfigError(Exception):
    pass


@dataclass
class Rules:
    one_dependency_max_loss: float
    one_dependency_max_share: float
    worst_case_max_loss: float
    reliable_yield_min_cover: float
    locked_in_min_share: float
    locked_in_window_days: float
    cash_one_tx_min: float
    cash_24h_min: float
    junior_min_share: float
    new_strategy_share: float
    new_strategy_weeks: float
    warning_margin: float
    lending_haircut: float
    dex_max_loss: float
    loss_if_it_breaks: dict[int, float]
    risk_group_max_share: dict[int, float | None]  # None: no cap


@dataclass
class Dependency:
    name: str
    risk_score: int
    type: str
    note: str | None


@dataclass
class Use:
    """One entry in a strategy's dependencies."""

    name: str
    weight: float
    loss: float | None  # overrides the risk score's loss if it breaks
    role: str | None  # "collateral": a collateral we lend against


@dataclass
class Strategy:
    id: str
    name: str
    amount: float
    apy: float | None
    incentive_apy: float | None
    yield_type: str
    apy_90d_low: float | None
    maturity: dt.date | None
    leverage: float
    exit_discount: float
    cash: str
    withdrawable_now: float | None
    sellable_24h: float | None
    after_bridge: str | None
    exit_days: float | None
    added: dt.date | None
    tags: list[str]
    dependencies: list[Use]


@dataclass
class Tranche:
    size: float
    target: float | None


@dataclass
class Shock:
    dependency: str | None  # a dependency name or one of BIGGEST
    tag: str | None
    loss: float
    collateral_loss: float | None


@dataclass
class Scenario:
    name: str
    shocks: list[Shock]


@dataclass
class Vault:
    name: str
    asset: str
    idle: float
    fixed: Tranche
    levered: Tranche
    equity: Tranche
    junior_withdrawals_in_cooldown: float | None
    junior_withdrawals_due_7d: float | None
    top_fixed_holders: float | None
    junior_cooldown_days: float | None
    loss_report_days: float | None
    strategies: list[Strategy]
    scenarios: list[Scenario]


@dataclass
class Config:
    rules: Rules
    dependencies: dict[str, Dependency]
    vault: Vault


def load(vault: str) -> Config:
    """Reads rules.yaml, dependencies.yaml and vaults/<vault>.yaml."""
    path = CONFIG_DIR / "vaults" / f"{vault.lower()}.yaml"
    if not path.is_file():
        known = ", ".join(sorted(p.stem for p in path.parent.glob("*.yaml")))
        raise ConfigError(f"unknown vault '{vault}' (known: {known})")
    rules = _rules(_read(CONFIG_DIR / "rules.yaml"))
    dependencies = _dependencies(_read(CONFIG_DIR / "dependencies.yaml"))
    return Config(rules, dependencies, _vault(_read(path), path.name, dependencies))


def _read(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text())
    except OSError as e:
        raise ConfigError(f"can't read {path}: {e.strerror}") from None
    except (yaml.YAMLError, ValueError) as e:  # ValueError: a date like 2026-02-30
        raise ConfigError(f"{path.name}: {e}") from None


def _rules(data: Any) -> Rules:
    names = tuple(f.name for f in fields(Rules))
    f = _Fields(data, "rules.yaml", names)
    losses = {
        score: _not_negative(loss, f"rules.yaml: loss_if_it_breaks {score}")
        for score, loss in _by_score(f, "loss_if_it_breaks").items()
    }
    group_caps = {
        score: None if cap is None else _not_negative(cap, f"rules.yaml: risk_group_max_share {score}")
        for score, cap in _by_score(f, "risk_group_max_share").items()
    }
    numbers = {name: f.number(name) for name in names if name not in ("loss_if_it_breaks", "risk_group_max_share")}
    for name, value in numbers.items():
        if value < 0 or (value == 0 and name in ("locked_in_window_days", "reliable_yield_min_cover")):
            raise ConfigError(f"rules.yaml: {name} can't be {value:g}")
    return Rules(**numbers, loss_if_it_breaks=losses, risk_group_max_share=group_caps)


def _by_score(f: "_Fields", key: str) -> dict[int, Any]:
    """A rules.yaml table with one entry for each risk score 1 to 5, in order."""
    table = f.get(key)
    if not isinstance(table, dict) or set(table) != {1, 2, 3, 4, 5}:
        raise ConfigError(f"rules.yaml: {key} needs one entry for each risk score 1 to 5")
    return {score: table[score] for score in sorted(table)}


def _dependencies(data: Any) -> dict[str, Dependency]:
    if not isinstance(data, dict):
        raise ConfigError("dependencies.yaml: expected one entry per dependency")
    dependencies = {}
    for key, entry in data.items():
        name = str(key)
        f = _Fields(entry, f"dependencies.yaml '{name}'", ("risk_score", "type", "note"))
        score = f.number("risk_score")
        if score not in (1, 2, 3, 4, 5):
            raise ConfigError(f"{f.where}: risk_score must be 1 to 5, got {score:g}")
        if name.startswith("biggest_"):
            raise ConfigError(f"{f.where}: names starting with biggest_ are kept for scenarios")
        note = f.get("note", None)
        dependencies[name] = Dependency(name, int(score), f.choice("type", DEPENDENCY_TYPES), note and str(note))
    return dependencies


def _vault(data: Any, where: str, dependencies: dict[str, Dependency]) -> Vault:
    f = _Fields(data, where, VAULT_FIELDS)
    tranches = _Fields(f.get("tranches"), f"{where} tranches", ("fixed", "levered", "equity"))
    fixed, levered, equity = (_tranche(tranches.get(k), f"{where} {k} tranche") for k in ("fixed", "levered", "equity"))
    strategies = [_strategy(s, where, dependencies) for s in f.items("strategies")]
    ids = [s.id for s in strategies]
    for sid in ids:
        if sid == "idle" or ids.count(sid) > 1:
            raise ConfigError(f"{where}: strategy id '{sid}' is {'kept for idle' if sid == 'idle' else 'used twice'}")
    return Vault(
        name=f.text("name"),
        asset=f.text("asset"),
        idle=f.number("idle"),
        fixed=fixed,
        levered=levered,
        equity=equity,
        junior_withdrawals_in_cooldown=f.maybe("junior_withdrawals_in_cooldown"),
        junior_withdrawals_due_7d=f.maybe("junior_withdrawals_due_7d"),
        top_fixed_holders=f.maybe("top_fixed_holders"),
        junior_cooldown_days=f.maybe("junior_cooldown_days"),
        loss_report_days=f.maybe("loss_report_days"),
        strategies=strategies,
        scenarios=[_scenario(s, where, dependencies) for s in f.items("scenarios")],
    )


def _tranche(data: Any, where: str) -> Tranche:
    f = _Fields(data, where, ("size", "target"))
    return Tranche(f.number("size"), f.maybe("target", None))


def _strategy(data: Any, where: str, dependencies: dict[str, Dependency]) -> Strategy:
    f = _Fields(data, _named(f"{where} strategy", data, "id"), STRATEGY_FIELDS)
    yield_type = f.choice("yield", YIELD_TYPES)
    cash = f.choice("cash", CASH_TYPES)
    after_bridge = f.choice("after_bridge", BRIDGED_CASH_TYPES) if cash == "other_chain" else None
    counts_as = after_bridge or cash
    uses = [_use(entry, f.where, dependencies) for entry in f.items("dependencies")]
    names = [use.name for use in uses]
    for name in names:
        if names.count(name) > 1:
            raise ConfigError(f"{f.where}: dependency '{name}' is listed twice")
    return Strategy(
        id=f.text("id"),
        name=f.text("name"),
        amount=f.number("amount"),
        apy=f.maybe("apy"),
        incentive_apy=f.maybe("incentive_apy", 0.0),
        yield_type=yield_type,
        apy_90d_low=f.maybe("apy_90d_low", REQUIRED if yield_type == "variable" else None),
        maturity=f.date("maturity", REQUIRED if yield_type == "locked" else None),
        leverage=f.number("leverage", 1.0),
        exit_discount=f.number("exit_discount", 0.0),
        cash=cash,
        withdrawable_now=f.maybe("withdrawable_now", REQUIRED if counts_as == "lending" else None),
        sellable_24h=f.maybe("sellable_24h", REQUIRED if counts_as == "dex" else None),
        after_bridge=after_bridge,
        exit_days=f.maybe("exit_days", REQUIRED if cash == "locked" else None),
        added=f.date("added", None),
        tags=[str(tag) for tag in f.items("tags", [])],
        dependencies=uses,
    )


def _use(data: Any, where: str, dependencies: dict[str, Dependency]) -> Use:
    entry = {"name": data} if isinstance(data, str) else data  # a plain name is short for {name: ...}
    f = _Fields(entry, f"{where} dependency", ("name", "weight", "loss", "role"))
    name = f.text("name")
    if name not in dependencies:
        raise ConfigError(f"{where}: unknown dependency '{name}' (add it to dependencies.yaml)")
    role = f.get("role", None)
    if role not in (None, "collateral"):
        raise ConfigError(f"{where}: dependency '{name}' role must be collateral, got {role!r}")
    return Use(name, f.number("weight", 1.0), f.maybe("loss", None), role)


def _scenario(data: Any, where: str, dependencies: dict[str, Dependency]) -> Scenario:
    f = _Fields(data, _named(f"{where} scenario", data, "name"), ("name", "shocks"))
    shocks = []
    for entry in f.items("shocks"):
        shock = _Fields(entry, f"{f.where} shock", ("dependency", "tag", "loss", "collateral_loss"))
        dependency, tag = shock.get("dependency", None), shock.get("tag", None)
        if (dependency is None) == (tag is None):
            raise ConfigError(f"{shock.where}: needs either a dependency or a tag")
        if not isinstance(dependency or tag, str):
            raise ConfigError(f"{shock.where}: dependency or tag must be a name, got {dependency or tag!r}")
        if "collateral_loss" in shock.data and shock.data["collateral_loss"] is None:
            raise ConfigError(f"{shock.where}: collateral_loss can't be null (leave it out to use loss)")
        if dependency is not None and dependency not in dependencies and dependency not in BIGGEST:
            raise ConfigError(
                f"{shock.where}: unknown dependency '{dependency}' (use one from dependencies.yaml or {', '.join(BIGGEST)})"
            )
        shocks.append(Shock(dependency, tag and str(tag), shock.number("loss"), shock.maybe("collateral_loss", None)))
    return Scenario(f.text("name"), shocks)


def _named(where: str, data: Any, key: str) -> str:
    """Where an entry is, by its id or name when it has one: for error messages."""
    return f"{where} '{data[key]}'" if isinstance(data, dict) and data.get(key) else where


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ConfigError(f"{where} must be a number, got {value!r}")
    return float(value)


def _not_negative(value: Any, where: str) -> float:
    number = _number(value, where)
    if number < 0:
        raise ConfigError(f"{where} can't be {number:g}")
    return number


class _Fields:
    """One YAML mapping. Errors say where the problem is."""

    def __init__(self, data: Any, where: str, known: tuple[str, ...]) -> None:
        if not isinstance(data, dict):
            raise ConfigError(f"{where}: expected a mapping, got {data!r}")
        unknown = [str(key) for key in data if key not in known]
        if unknown:
            raise ConfigError(f"{where}: unknown field {', '.join(unknown)}")
        self.data = data
        self.where = where

    def get(self, key: str, default: Any = REQUIRED) -> Any:
        if key in self.data:
            return self.data[key]
        if default is REQUIRED:
            raise ConfigError(f"{self.where}: missing required field '{key}'")
        return default

    def maybe(self, key: str, default: Any = REQUIRED) -> float | None:
        """A number, or None when it's null (unknown)."""
        value = self.get(key, default)
        return None if value is None else _number(value, f"{self.where}: '{key}'")

    def number(self, key: str, default: Any = REQUIRED) -> float:
        value = self.maybe(key, default)
        if value is None:
            raise ConfigError(f"{self.where}: '{key}' can't be null")
        return value

    def text(self, key: str) -> str:
        value = self.get(key)
        if not isinstance(value, str) or not value:
            raise ConfigError(f"{self.where}: '{key}' must be text, got {value!r}")
        return value

    def choice(self, key: str, options: tuple[str, ...]) -> str:
        value = self.get(key)
        if value not in options:
            raise ConfigError(f"{self.where}: '{key}' must be one of {' | '.join(options)}, got {value!r}")
        return str(value)

    def date(self, key: str, default: Any = REQUIRED) -> dt.date | None:
        value = self.get(key, default)
        if isinstance(value, dt.datetime):
            return value.date()
        if value is None or isinstance(value, dt.date):
            return value
        try:
            return dt.date.fromisoformat(str(value))
        except ValueError:
            raise ConfigError(f"{self.where}: '{key}' must be a date (YYYY-MM-DD), got {value!r}") from None

    def items(self, key: str, default: Any = REQUIRED) -> list[Any]:
        value = self.get(key, default)
        if not isinstance(value, list):
            raise ConfigError(f"{self.where}: '{key}' must be a list, got {value!r}")
        return value
