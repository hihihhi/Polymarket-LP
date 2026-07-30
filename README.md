# Polymarket-LP

## One sentence
A research toolkit for testing a managed-risk Polymarket liquidity-provider reward strategy without placing live orders.

## Result

**Synthetic result only — no live-fill proof.** The strategy and data-generation arguments in the command below produced `total_pnl_usdc = 122.63378371068302` in two deterministic reruns with separate output directories. Source: `docs/lp_synthetic_seed7_summary.csv:2`. Sample status: synthetic stress test; IS/OOS classification **unknown**; live status **unknown**.

![Seed-7 synthetic equity path](docs/lp_synthetic_seed7_equity.svg)

## How it works
- The backtest ranks markets by estimated reward density.
- Volatility and jump gates determine quote eligibility.
- Synthetic execution simulates two-sided quotes, midpoint-cross fill risk, and YES/NO inventory pairing.
- Realized and mark-to-market accounting track inventory exits and drawdown.
- Paper replay writes intended quotes from public snapshots only; it does not sign, submit, or cancel orders.

## The interesting decision
The strategy optimizes reward income per unit of unpaired inventory risk rather than gross rewards. It favors wide eligible quotes and hard inventory caps, accepting that the reported profile still has `0%` pair completion (`docs/RESULTS.md:51`) and therefore needs OOS validation before any capital is at risk.

## Run it
```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e .[dev]
python scripts/lp_backtest.py --synthetic --seed 7 --synthetic-days 14 --synthetic-markets 30 --quote-size 800 --quote-offset 0.035 --excluded-categories "" --active-capital-limit 1900 --max-unpaired-per-market 800 --max-total-unpaired 1200 --max-cluster-unpaired 600 --exit-loss-cents 0.05 --max-unpaired-minutes 90 --max-recent-vol 0.006 --max-recent-jump 0.025 --vol-quote-multiplier 0.5 --out-dir data/processed/income_density_wide
pytest -q
```

For snapshot replay, use `python scripts/lp_backtest.py --snapshots data/raw/orderbook_snapshots.csv --out-dir data/processed/real_replay` after collecting point-in-time public inputs.

## Status
Active research/backtest scaffold. Live orders are intentionally unsupported; OOS results, actual fills, cancels, reward payouts, and live execution evidence are **unknown**. Do not treat this repository as trading advice or a proven live strategy.
