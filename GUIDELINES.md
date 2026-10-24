# Allocation Guidelines: yTranche

## Principle
Junior capital is the risk budget.

The product breaks in three ways:
- a loss event (hack, depeg, bad debt)
- a yield shortfall (we earn less than the targets, and the difference is paid out of the junior's money)
- a forced sale (withdrawals are bigger than our cash, so we sell something at a loss)

The junior pays for all three. So every limit below is a % of the junior, and when the junior gets smaller, the limits get smaller too.

## Terms
- **Fixed tranche**: the senior tranche.
- **Junior**: the levered and equity tranches together. Don't count junior withdrawals already in cooldown, since that money is leaving.
- **Dependency**: anything that can break and hit our money: a protocol, a collateral or its issuer, an oracle, a curator, a chain or bridge. The vault's own asset (USDC or ETH) and Ethereum mainnet don't count. A strategy counts toward every dependency it uses (e.g. PT-siUSD looped on Morpho counts toward Pendle, infiniFi, Morpho and the oracle).
- **Loops**: count the full looped position, not just what we put in (e.g. 10M looped 3x counts as 30M). The loss can't be more than what we put in.
- **Loss if it breaks**: the % of our money in a dependency we'd lose if it fails. Comes from the risk score (table below). If we can't get out within 24h, add the discount we'd take selling in a hurry.
- **Reliable yield**: locked-in yield (PTs held to maturity, fixed-rate loans) and steady yield (savings rates like sUSDS, ETH staking) count in full. Variable lending rates count at their lowest level in the last 90 days. Points and incentives count as 0.
- **Yield we owe**: what the whole vault has to earn to pay all tranche targets. Example: if the fixed tranche is 80% of TVL with a 5% target, the vault has to earn 4% (plus the levered tranche's share, if it has a target).

## Rules

**Losses**

1. If any one dependency breaks, we lose at most 25% of the junior.
2. No more than 30% of TVL in any one dependency, no matter how good its risk score is.
3. In our worst-case scenario (see below), we lose at most 75% of the junior.

**Earning enough**

4. Reliable yield is at least 1.2x the yield we owe.
5. At least 25% of the yield we owe over the next 60 days is already locked in.

**Cash for withdrawals**

6. At least 10% of the fixed tranche can be withdrawn in a single transaction.
7. Within 24h we can pay out the bigger of: our 3 biggest fixed-tranche holders, or 25% of the fixed tranche. Plus all junior withdrawals due in the next 7 days.

**Junior size and cooldown**

8. The junior is at least 15% of TVL.
9. The junior cooldown (14 days) is longer than the time we need to exit our least liquid positions, and longer than the time it takes for a loss to show up in reports.

**Process**

10. New strategies start at 25% of what rule 1 allows, for the first 4 weeks.
11. Tranche targets are set so rule 4 passes at today's reliable yield. Reviewed monthly.

**Rule 1 example:** junior = 20M, so any one dependency breaking can cost us at most 5M. If it would lose 100% when it breaks, we can put at most 5M in it. If it would lose 20%, at most 25M. If the junior drops to 10M, all these limits halve.

### Risk score → loss if it breaks
| Risk score | Loss if it breaks |
|---|---|
| [1] | [10%] |
| [2] | [25%] |
| [3] | [50%] |
| [4] | [75%] |
| [5] | [100%] |

### What counts as cash (rules 6 and 7)
| Position | In one transaction | Within 24h |
|---|---|---|
| Idle, or instantly redeemable 1:1 on the same chain (USDC, WETH, sUSDS) | 100% | 100% |
| Lending on the same chain (Aave, Morpho) | 50% of what we could withdraw right now | same |
| Anything on another chain | 0% | counted like the other rows, after bridging |
| Things we'd have to sell on a DEX (LSTs/LRTs, PTs, other stables) | 0% | only what we could sell losing at most 0.5% |
| Anything with a cooldown, queue or lock | 0% | 0% |

### Worst-case scenarios (rule 3)
- **USD:** the biggest stablecoin we're exposed to drops 20% + bad debt in the lending markets that use it + PT prices drop 3% while we're selling
- **ETH:** the biggest LST/LRT we hold drops 10% vs ETH + validator exit queue longer than 30 days + WETH borrow rate above staking yield for 30 days

## Before any allocation change
1. Get the risk score and list every dependency the strategy uses.
2. Check rules 1 to 3 with the change included.
3. Check rules 4 to 7. If it's a new strategy, size it with rule 10.

## When we break a limit
| Level | When | What we do |
|---|---|---|
| Warning | we're close to a limit (within 10% of it) | no new moves that get us closer; mention it in the weekly check |
| Breach | a limit is broken | fix it within 7 days (48 hours for rules 6 and 7) |
| Breach of rule 8 | the junior is below its minimum | also pause fixed-tranche deposits until it's fixed |

A breach is fixed either by reducing the position or by the junior growing back.

## Weekly check
1. Junior as % of TVL
2. Biggest single-dependency loss as % of the junior
3. Worst-case scenario loss as % of the junior
4. Reliable yield vs yield we owe, and how much of it is locked in
5. Cash available (one transaction and 24h) vs what rules 6 and 7 need

Plus a table with every dependency: how much we have in it, its loss if it breaks, and that loss as % of the junior.

Limits are recalculated whenever the junior changes by more than 5%.
