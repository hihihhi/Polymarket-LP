# Polymarket LP portfolio metrics spec

This is the metric stack for a managed-risk LP/reward-farming strategy. The key principle is to measure **reward income per unit of unpaired inventory risk**, not just gross rewards.

## 1. Market-selection metrics

| Metric | Meaning | Use |
|---|---|---|
| `reward_daily` | Daily reward pool for the market | Bigger pool increases capacity |
| `min_incentive_size` | Minimum order size to score rewards | Determines capital needed per quote |
| `max_incentive_spread` | Farthest quote distance from midpoint that can score | Wider is safer and better |
| `market_competitiveness` | Competition indicator from rewards endpoint, if available | Lower is better |
| `reward_density` | `reward_daily / required_quote_notional` | First ranking metric |
| `reward_per_competitor` | `reward_daily / estimated_competitor_score` | Detects under-farmed markets |
| `volatility_1h/6h/24h` | Midpoint movement | Lower is safer |
| `jump_risk_score` | Frequency of 5c+ or 10c+ jumps | Avoid high jump-risk markets |
| `time_to_resolution` | Hours/days until resolution | Avoid near-resolution unless proven |
| `catalyst_window` | Upcoming game/news/deadline/oracle event | Reduce or disable quoting |
| `category` / `cluster` | Politics/sports/crypto/etc. | Enforce correlation caps |

## 2. Reward-scoring metrics

| Metric | Meaning | Use |
|---|---|---|
| `our_score` | Approximate reward score from quote size and midpoint distance | Drives reward share |
| `competitor_score` | Estimated total score from other LPs | Needed for reward share |
| `reward_share` | `our_score / (our_score + competitor_score)` | Expected share of pool |
| `expected_reward_usdc` | `reward_daily * time_fraction * reward_share` | Gross reward estimate |
| `reward_capture_rate` | Actual/expected reward | Validate scoring model |
| `quote_eligibility_rate` | % of time orders meet spread+size rules | Must be high |
| `two_sided_score_ratio` | min(side score) / max(side score) | Measures balanced quoting |

## 3. Execution / fill-quality metrics

| Metric | Meaning | Use |
|---|---|---|
| `fill_rate` | Filled shares / posted shares | Too high may mean adverse selection |
| `one_sided_fill_rate` | One-sided fills / all fills | Core LP risk metric |
| `pair_completion_ratio` | Paired fills / (paired + one-sided fills) | Higher is safer |
| `avg_unpaired_minutes` | Average time inventory stays naked | Keep low |
| `p95_unpaired_minutes` | Tail inventory duration | Shows stuck-inventory risk |
| `adverse_selection_bps` | Price move after fill against our side | Must be tracked by market/category |
| `cancel_latency_ms` | Request-to-confirm cancel latency | Critical for avoiding toxic fills |
| `stale_quote_fill_rate` | Fills after signal/cancel should have happened | Kill-switch metric |
| `post_only_reject_rate` | Invalid/crossing order attempts | Bot health |

## 4. Inventory-risk metrics

| Metric | Meaning | Use |
|---|---|---|
| `open_inventory_notional` | Current naked inventory | Hard-capped |
| `max_open_inventory_notional` | Worst naked exposure | Main MDD driver |
| `inventory_by_market` | Naked inventory per market | Per-market kill switch |
| `inventory_by_cluster` | Naked inventory per correlated group | Prevent correlated drawdown |
| `inventory_delta` | YES value minus NO value, normalized | Directional exposure |
| `inventory_var_95` | 95% simulated inventory loss | Portfolio risk budget |
| `inventory_cvar_95` | Average loss beyond VaR | Tail-risk budget |
| `forced_exit_loss` | Loss from flattening inventory | Shows cost of staying safe |

## 5. P&L metrics

| Metric | Meaning | Use |
|---|---|---|
| `reward_pnl` | Liquidity rewards | Base income |
| `pair_spread_pnl` | Locked spread from complete YES+NO pairs | Low-risk trading P&L |
| `rebate_pnl` | Maker rebates if filled | Add separately, never blend |
| `inventory_exit_pnl` | P&L from exiting naked fills | Main loss source |
| `fees_slippage` | Fees/slippage/failed fills | Execution drag |
| `net_pnl` | Sum of all components | Final result |
| `reward_to_loss_ratio` | rewards / inventory losses | Must be > 1.5, ideally > 3 |
| `pnl_per_dollar_deployed` | Net P&L / active capital | Capital efficiency |
| `pnl_per_unit_inventory_risk` | Net P&L / avg naked inventory | The key strategy metric |

## 6. Drawdown / portfolio metrics

| Metric | Meaning | Use |
|---|---|---|
| `realized_equity` | Capital + closed P&L + rewards | Conservative accounting |
| `mark_to_market_equity` | Realized equity + inventory MTM | True risk accounting |
| `max_drawdown_realized` | MDD on closed equity | Accounting MDD |
| `max_drawdown_mtm` | MDD including open inventory | Actual MDD |
| `daily_sharpe` | Annualized Sharpe on daily returns | Stable enough after 30+ days |
| `daily_sortino` | Downside-risk-adjusted returns | Better than Sharpe for LP |
| `profit_factor` | Gross profit / gross loss | Must be > 1.3, ideally > 2 |
| `bad_day_p95` | 95th percentile daily loss | Set daily stop |
| `bad_week_p95` | 95th percentile weekly loss | Set weekly stop |
| `recovery_factor` | net profit / max drawdown | Higher is better |

## 7. Capacity metrics

| Metric | Meaning | Use |
|---|---|---|
| `max_safe_order_size` | Size before fill risk dominates reward | Scale ceiling |
| `reward_share_saturation` | How reward share changes with more size | Detects diminishing returns |
| `capital_at_work` | Open-order notional | Cash deployment |
| `capital_locked_in_inventory` | Cash locked in unpaired tokens | Liquidity risk |
| `market_capacity_usdc` | Max daily deployable size per market | Sizing cap |
| `portfolio_capacity_usdc` | Sum of capped market capacities | Max bankroll this strat can handle |

## 8. Acceptance thresholds before live scaling

For a $2,000 bankroll, do not scale unless the live/paper backtest shows:

| Metric | Minimum acceptable |
|---|---:|
| Sample size | 2,000+ quote intervals or 500+ live/paper fills |
| Net P&L | Positive after conservative exits |
| Reward-to-loss ratio | > 1.5; > 3 preferred |
| Pair completion ratio | > 60%; > 75 preferred |
| Max MTM drawdown | < 20–25% |
| Worst day | Better than -6% |
| Profit factor | > 1.3; > 2 preferred |
| Daily Sortino | > 1.5 after 30+ days |
| Stale-fill rate | < 2% |
| API/cancel failure incidents | zero before scaling |

## 9. Backtest levels

| Level | Data needed | Trust level |
|---|---|---|
| L0 synthetic stress | generated mids/rewards | sanity only |
| L1 price-history proxy | midpoint/price history + reward configs | weak |
| L2 trade-tape proxy | trades + price history + reward configs | medium for fill risk |
| L3 full orderbook replay | WebSocket L2 book snapshots + trades + rewards | strong |
| L4 paper trading | your actual quotes/cancels/fills + rewards | strongest pre-live |
| L5 tiny live | real fills/rewards with $200–$500 | production proof |

Do not trust any LP backtest that lacks a mark-to-market inventory curve and one-sided-fill accounting.
