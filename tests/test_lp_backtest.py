from __future__ import annotations

import pandas as pd

from polymarket_lp.lp_backtest import LPConfig, handle_fill, make_synthetic_snapshots, quote_for_row, simulate_lp


def test_quote_enforces_pair_safety() -> None:
    row = pd.Series(
        {
            "yes_mid": 0.55,
            "no_mid": 0.46,
            "max_incentive_spread": 0.04,
            "min_incentive_size": 20,
            "reward_daily": 100,
        }
    )
    q = quote_for_row(row, LPConfig(quote_size_shares=25, quote_offset=0.01, safety_margin=0.02))
    assert q["pair_cost"] <= 0.98 + 1e-9


def test_handle_fill_pairs_opposite_inventory() -> None:
    cfg = LPConfig()
    ts = pd.Timestamp("2026-01-01", tz="UTC")
    events = []
    inventory, *_ = handle_fill(
        inventory=[],
        events=events,
        timestamp=ts,
        condition_id="m1",
        side="YES",
        price=0.47,
        size=10,
        cluster="test",
        cfg=cfg,
    )
    assert len(inventory) == 1
    inventory, pair_pnl, exit_pnl, paired_shares, opened_shares, _ = handle_fill(
        inventory=inventory,
        events=events,
        timestamp=ts,
        condition_id="m1",
        side="NO",
        price=0.50,
        size=10,
        cluster="test",
        cfg=cfg,
    )
    assert len(inventory) == 0
    assert round(pair_pnl, 6) == 0.30
    assert exit_pnl == 0
    assert paired_shares == 10
    assert opened_shares == 0


def test_synthetic_backtest_outputs_core_metrics() -> None:
    snapshots = make_synthetic_snapshots(seed=1, days=2, n_markets=6)
    events, equity, summary = simulate_lp(snapshots, LPConfig(initial_capital=2000, quote_offset=0.025))
    assert not events.empty
    assert not equity.empty
    assert not summary.empty
    for col in [
        "total_pnl_usdc",
        "max_drawdown_mtm_pct",
        "reward_to_trading_loss_ratio",
        "pair_completion_ratio_shares",
        "max_open_inventory_notional",
    ]:
        assert col in summary.columns
