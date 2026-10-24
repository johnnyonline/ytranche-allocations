import argparse
import datetime as dt
import sys
from decimal import Decimal, InvalidOperation

from allocations import checks, config, report


def run() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m allocations", description="Check a yTranche vault against the allocation guidelines."
    )
    parser.add_argument("vault", help="a vault in config/vaults, e.g. yvusd")
    parser.add_argument(
        "--move",
        nargs=3,
        action="append",
        default=[],
        metavar=("AMOUNT", "FROM", "TO"),
        help="what if we move AMOUNT (e.g. 500K, 1.5M) from FROM to TO (strategy ids or idle); can be repeated",
    )
    parser.add_argument("--caps", action="store_true", help="show how much each strategy can hold")
    parser.add_argument("--markdown", action="store_true", help="print markdown tables, for HackMD or chat")
    parser.add_argument("--date", type=_date, default=dt.date.today(), help="pretend today is this date (YYYY-MM-DD)")
    args = parser.parse_args()

    try:
        cfg = config.load(args.vault)
        moves = [checks.Move(_amount(amount), source, target) for amount, source, target in args.move]
        before = checks.run(cfg.rules, cfg.dependencies, cfg.vault, args.date)
        after = checks.run(cfg.rules, cfg.dependencies, checks.move(cfg.vault, moves), args.date) if moves else before
    except (config.ConfigError, checks.MoveError) as e:
        parser.exit(2, f"error: {e}\n")

    if moves:
        print(report.moved(before, after, moves, args.markdown))
    if args.caps:
        print(("\n" if moves else "") + report.caps(after, args.markdown, header=not moves))
    if not moves and not args.caps:
        print(report.full(before, args.markdown))
    sys.exit(1 if any(rule.status == checks.BREACH for rule in after.rules) else 0)


def _amount(text: str) -> float:
    """500K, 1.5M, 2B or a plain number."""
    clean = text.strip().upper().replace(",", "").replace("_", "")
    multiplier = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(clean[-1:], 1)
    try:
        value = Decimal(clean[:-1] if multiplier != 1 else clean) * multiplier  # exact, so 2.007M is 2,007,000
    except InvalidOperation:
        raise checks.MoveError(f"can't read amount '{text}' (use e.g. 500K, 1.5M or 250000)") from None
    if not value.is_finite() or value <= 0:
        raise checks.MoveError(f"amount must be a positive number, got '{text}'")
    return float(value)


def _date(text: str) -> dt.date:
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{text}' isn't a date (use YYYY-MM-DD)") from None
