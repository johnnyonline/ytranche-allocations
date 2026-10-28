# ytranche-allocations

Allocation Guidelines for yTranche.

Checks a yTranche vault (yvUSD, yvETH) against the [allocation guidelines](GUIDELINES.md). It runs the 12 rules on the vault's strategies, answers "what if I move X from A to B?", and shows how much each strategy can hold. Every limit is a % of the junior (the levered and equity tranches together).

v1 reads everything from the YAML files in [`config/`](config/). Nothing is read onchain yet.

When a rule changes in [`GUIDELINES.md`](GUIDELINES.md), update [`config/rules.yaml`](config/rules.yaml) to match.

## Installation

Only Docker is needed.

```bash
git clone https://github.com/johnnyonline/ytranche-allocations.git
cd ytranche-allocations
docker build -t ytranche-allocations .   # again after changing dependencies
```

## Usage

```shell
# Full report: rules, dependencies, details and missing data.
# The -v mount runs it on your working copy, so config and code edits need no rebuild.
docker run --rm -v "$PWD:/app" ytranche-allocations yvusd

# Shorter: alias it once
alias allocations='docker run --rm -v "$PWD:/app" ytranche-allocations'

# How much each strategy can hold
allocations yvusd --caps

# What if we move 1M from morpho_og to pt_susds? Shows every rule before -> after
allocations yvusd --move 1M morpho_og pt_susds

# Moves can be chained, and idle works as FROM or TO
allocations yvusd --move 500K susds idle --move 400K idle pt_usd3

# Markdown tables, for pasting into HackMD or chat (works with every command)
allocations yvusd --markdown

# Pretend today is another date (affects maturities and rule 10)
allocations yvusd --date 2026-11-01
```

- `--move AMOUNT FROM TO` takes amounts like `500K`, `1.5M`, `2B` or plain numbers, in the vault's asset. FROM and TO are strategy ids or `idle`. It never edits the config.
- `--caps` with `--move` shows the caps after the moves.
- Exit code is 1 if any rule is a Breach (after the moves, with `--move`), 0 otherwise, and 2 for a bad config or bad arguments.

Statuses:
- **OK**
- **Warning**: within `warning_margin` (10%) of the limit.
- **Breach**: over the limit.
- **Needs data**: the rule fails with missing inputs counted as 0, but it could pass once they're filled in.
- **n/a**: nothing to check, e.g. no new strategies for rule 10.

## Config

```
config/
  rules.yaml          # every number from GUIDELINES.md
  dependencies.yaml   # risk score and type of each dependency, shared by all vaults
  vaults/yvusd.yaml   # one file per vault: tranches, strategies and scenarios
```

Use `null` for inputs we don't know yet. The report lists every null that affected a result under "Missing data". Unknown fields are an error, so a typo like `leverge: 4` fails loudly instead of being ignored.

### Change a rule number

Edit [`config/rules.yaml`](config/rules.yaml). No rule number is hard-coded.

### Add a dependency

Add a line to [`config/dependencies.yaml`](config/dependencies.yaml):

```yaml
ethena: {risk_score: 3, type: stablecoin, note: USDe / sUSDe}
```

- `risk_score` is 1 to 5. It picks the loss if it breaks from `loss_if_it_breaks` in `rules.yaml`.
- `type` is `protocol`, `stablecoin`, `chain` or `collateral`.

### Add a strategy

Add an entry under `strategies:` in the vault's file:

```yaml
  - id: pt_susde                 # used by --move; must be unique, and not "idle"
    name: sUSDe Pendle PT Maxi
    amount: 500000               # what the vault has in it
    apy: 0.081                   # current, including incentives
    yield: locked                # locked | steady | variable | incentive
    maturity: 2027-02-25         # locked only
    cash: dex                    # instant | lending | dex | other_chain | locked
    sellable_24h: null           # dex only: what we'd get back within 24h losing at most dex_max_loss
    added: 2026-10-20            # optional: rule 10 applies for new_strategy_weeks after this
    tags: [pt]
    dependencies:
      - pendle                                   # just a name: weight 1, the risk score's loss rate
      - {name: ethena, weight: 1, loss: 0.6}     # loss overrides the risk score's rate for this strategy
```

`weight` is the share of the strategy exposed to the dependency. For a collateral we lend against (rather than hold or sit in), add `role: collateral`, e.g. `{name: ethena, weight: 0.4, role: collateral}` on a lending strategy.

| Field | Needed | Notes |
|---|---|---|
| `id`, `name`, `amount`, `apy`, `yield`, `cash`, `dependencies` | always | `apy` can be null |
| `apy_90d_low` | `yield: variable` | excluding incentives |
| `maturity` | `yield: locked` | YYYY-MM-DD |
| `withdrawable_now` | `cash: lending` | |
| `sellable_24h` | `cash: dex` | |
| `after_bridge` | `cash: other_chain` | `instant`, `lending` or `dex`. Then give that type's field (`withdrawable_now` or `sellable_24h`). |
| `exit_days` | `cash: locked` | Optional otherwise. Defaults: instant 0, lending 1, dex 1, other_chain 2. |
| `incentive_apy` | optional | the part of `apy` that is points or incentives. Default 0. |
| `leverage`, `exit_discount` | optional | defaults 1 and 0 |
| `added`, `tags` | optional | |

A field marked "needed" must be there. If it's a number or a date, it can be `null` while unknown. `after_bridge` always needs a value.

### Add a scenario

Add an entry under `scenarios:` in the vault's file. Each shock hits either a dependency or a tag:

```yaml
  - name: USDe drops 15%
    shocks:
      - {dependency: ethena, loss: 0.15, collateral_loss: 0.03}  # collateral_loss: for role: collateral entries; leave it out to use loss
      - {dependency: biggest_collateral, loss: 0.10}             # biggest_<type>: the dependency of that type with the most exposure
      - {tag: pt, loss: 0.03}
```

### Add a vault

Copy `config/vaults/yvusd.yaml` to e.g. `config/vaults/yveth.yaml`, fill it in, and run `python -m allocations yveth`. Amounts are in the vault's own asset.

## Code

- [`allocations/config.py`](allocations/config.py): loads and validates the config. It's the only place that reads data, so onchain reads (balances, tranche sizes, the junior cooldown queue, 90-day lows, ...) can replace config values here one at a time.
- [`allocations/checks.py`](allocations/checks.py): the math (rules 1 to 12, caps, `--move`).
- [`allocations/report.py`](allocations/report.py): plain-text and markdown output.
- [`allocations/main.py`](allocations/main.py): the command line.

## Assumptions

These are decisions [the guidelines](GUIDELINES.md) don't spell out.

**Config and missing data**
- A field marked "needed" in the strategy table must be present, so a forgotten field is an error, not a silent 0. Write `null` when the value is unknown.
- A tranche without a `target` (or with `target: null`) owes nothing. Rule 11 is n/a if the fixed tranche has no target. Rule 5 is n/a if nothing is owed.
- Needs data only replaces a Breach, and only when the missing inputs could close the gap. `withdrawable_now` and `sellable_24h` count at most the strategy's amount, so a large enough gap stays a Breach. A Warning stays a Warning even if inputs are missing.
- Missing inputs on a strategy holding 0 aren't reported, since they can't change anything.

**How rules are reported**
- Rules 1, 2, 3 and 10 are checked per dependency, scenario or strategy. Each shows its worst one: the worst status first, then the highest value ÷ limit. Ties are listed together.
- Statuses rank n/a < OK < Warning < Needs data < Breach. `--move` uses this order to say "better" or "worse".

**Specific rules**
- Rule 3:
  - A dependency shock with no `collateral_loss` hits `role: collateral` entries at `loss`, which is the cautious choice. Scenario numbers are assumptions rather than inputs, so `null` isn't allowed in a shock.
  - `biggest_<type>` works for any dependency type, not just stablecoin (e.g. `biggest_collateral` for yvETH's biggest LST/LRT). On a tie it takes the first dependency in `dependencies.yaml`. If nothing of that type is held, the shock hits nothing.
- Rule 5: a maturity in the past counts as 0 days. Days are counted from `--date`.
- Rule 7: `sellable_24h` counts at most the strategy's amount, just as `withdrawable_now` does.
- Rule 9:
  - "Longer than" is strict: a cooldown equal to the longest exit is a Breach.
  - Strategies holding 0 have nothing to exit, so they're skipped.
  - A missing `exit_days` on a locked strategy counts as 0 and is listed as missing.
- Rule 10: "less than 4 weeks ago" means fewer than 28 days before `--date`. A strategy's rule 1 cap is the smallest across its dependencies, the same as in `--caps`.
- Rule 12:
  - How much of a strategy counts toward "risk N or riskier" is the sum of the weights of its dependencies scored N or more, capped at 100%. With the default weight of 1, the strategy's riskiest dependency decides.
  - Loops count at full size (amount × leverage), like rule 2, so a group can be over 100% of TVL.
  - Idle and strategies with no dependencies aren't in any group. `null` in `risk_group_max_share` means no cap.

**Moves, caps and display**
- `--move`:
  - It keeps `withdrawable_now` and `sellable_24h` as they are, because they're market conditions, but caps them at the new amount.
  - Moves run in order, and each one checks that FROM holds enough at that point.
- Caps hold TVL and junior fixed. In other words, the extra money is assumed to come from somewhere that doesn't share the strategy's dependencies or risk groups. A strategy with no dependencies shows "no limit".
- A cap is the lowest that rules 1, 2 and 12 allow. For rule 12, the group's room is divided by leverage × the strategy's share of the group.
- In the dependency table, the loss rate is the rate used: the risk score's rate (or an entry's `loss` override) plus the strategy's `exit_discount`. A range is shown when strategies differ. The status is the worse of rules 1 and 2.
- The 1% tolerance for "the tranche sizes add up to TVL" is a constant in `checks.py`. It isn't one of the guidelines' numbers, so it's not in `rules.yaml`.
- Amounts print as whole numbers when TVL is 100,000 or more, and with 2 decimals below that (for ETH-sized books).

## Code Style

Format and lint code with ruff:
```bash
# Format code
docker run --rm -v "$PWD:/app" --entrypoint ruff ytranche-allocations format .

# Lint code (also fixes what it can, since pyproject.toml sets fix = true)
docker run --rm -v "$PWD:/app" --entrypoint ruff ytranche-allocations check .
```

Type checking with mypy:
```bash
docker run --rm -v "$PWD:/app" --entrypoint mypy ytranche-allocations allocations
```
