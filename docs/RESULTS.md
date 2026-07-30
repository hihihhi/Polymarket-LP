# Current reproducible synthetic result

This is an L0 synthetic stress test, not evidence of live profitability. Its IS/OOS classification is **unknown** and its live status is **unknown**.

The strategy and data-generation arguments in the `README.md` command were run twice against the publication worktree, using separate output directories. Both runs produced byte-identical `lp_summary.csv` files with SHA-256 `fd8178302e8f3143da4911c003c771d042a54a28c44538fc16c751079ad76181`.

The unrounded output row is committed at `docs/lp_synthetic_seed7_summary.csv:2`. Selected fields, copied verbatim from that row:

| Metric field | Value |
|---|---:|
| `total_reward_usdc` | `163.42548863585822` |
| `total_inventory_exit_pnl_usdc` | `-40.79170492517545` |
| `total_pnl_usdc` | `122.63378371068302` |
| `return_on_initial_capital` | `0.06131689185534151` |
| `max_drawdown_mtm_usdc` | `-65.50954640844861` |
| `max_drawdown_mtm_pct` | `-0.03106791138596573` |
| `reward_to_trading_loss_ratio` | `4.006341214117696` |
| `pair_completion_ratio_shares` | `0.0` |
| `max_open_inventory_notional` | `228.91051310793222` |
| `avg_open_inventory_notional` | `0.17032032225292573` |
| `max_active_order_notional` | `1488.0` |

No annualization, monthly extrapolation, or qualitative performance label is applied.
