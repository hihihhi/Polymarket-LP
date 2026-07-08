from __future__ import annotations

import argparse
import json

import pandas as pd
import pytest

from polymarket_lp.lp_backtest import (
    LPConfig,
    depth_capped_quote_size,
    filter_snapshots_for_strategy,
    handle_fill,
    inventory_capped_quote_size,
    make_synthetic_snapshots,
    partial_rescue_residual_capped_quote_size,
    quote_for_row,
    rescue_quote_for_inventory,
    simulate_lp,
)
from polymarket_lp.paper import (
    LiveSnapshotConfig,
    PaperAnalysisConfig,
    _parse_book_bid_ask,
    analyze_paper_quotes,
    build_paper_quotes,
    collect_live_reward_snapshots,
    run_live_paper_loop,
    run_paper_analysis_to_files,
)
from polymarket_lp.target import TargetMonitorConfig, target_monitor_from_summary
from polymarket_lp.telemetry import ExecutionTelemetryConfig, audit_execution_telemetry
from polymarket_lp.deployment_gate import DeploymentReadinessConfig, evaluate_deployment_readiness
from polymarket_lp.capital_risk import (
    CapitalRiskStressConfig,
    config_from_lp_manifest,
    evaluate_capital_risk_stress,
)
from polymarket_lp.allocation import AllocationSelectionConfig, select_allocation
from polymarket_lp.proof import ObjectiveProofConfig, evaluate_objective_proof
from polymarket_lp.sustainability import SustainabilityStressConfig, evaluate_sustainability_stress
from polymarket_lp.risk_governor import RiskGovernorConfig, evaluate_risk_governor
from polymarket_lp.completion import evaluate_completion_audit
from polymarket_lp.governed_config import apply_risk_governor_to_lp_config
from polymarket_lp.hedge import HedgeFeasibilityConfig, evaluate_hedge_feasibility
from polymarket_lp.rescue_stress import RescueStressConfig, evaluate_rescue_stress
from polymarket_lp.depth_gate import DepthReadinessConfig, evaluate_depth_readiness
from polymarket_lp.candidate_leaderboard import CandidateEvidence, build_candidate_leaderboard
from polymarket_lp.drawdown_guard import DrawdownGuardConfig, evaluate_drawdown_guard, lp_config_from_manifest
from scripts.paper_replay import make_lp_config
from scripts.launch_live_paper_candidate import LaunchCandidateConfig, write_launch_artifacts
from scripts.refresh_candidate_leaderboard import (
    _input_freshness_metrics,
    _input_staleness_error,
    _pending_capital,
    _pending_drawdown,
    _pending_gate,
    _safe_name,
    _split_named_path,
)
from scripts.update_target_status import _bootstrap_target_from_quotes, _capture_stress_grid, _json_safe
from scripts.target_config_grid import SelectionConfig, _candidate_row
import scripts.partial_rescue_config_grid as partial_rescue_grid
from scripts.rolling_target_windows import _time_windows


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


def test_parse_clob_book_keeps_best_size_not_first_row() -> None:
    bid, ask, bid_size, ask_size = _parse_book_bid_ask(
        {
            "bids": [
                {"price": "0.10", "size": "50"},
                {"price": "0.25", "size": "12.5"},
                {"price": "0.25", "size": "7.5"},
            ],
            "asks": [
                {"price": "0.90", "size": "100"},
                {"price": "0.75", "size": "4"},
            ],
        }
    )
    assert bid == 0.25
    assert ask == 0.75
    assert bid_size == 20.0
    assert ask_size == 4.0


def test_paper_quotes_preserve_clob_depth_fields() -> None:
    snapshots = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-01-01T00:00:00Z"),
                "condition_id": "m1",
                "category": "economics",
                "cluster": "economics",
                "yes_mid": 0.52,
                "no_mid": 0.48,
                "reward_daily": 100.0,
                "max_incentive_spread": 0.05,
                "min_incentive_size": 10.0,
                "market_competitiveness": 0.1,
                "yes_best_bid": 0.51,
                "yes_best_ask": 0.53,
                "no_best_bid": 0.47,
                "no_best_ask": 0.49,
                "yes_best_bid_size": 123.0,
                "yes_best_ask_size": 45.0,
                "no_best_bid_size": 67.0,
                "no_best_ask_size": 89.0,
            }
        ]
    )
    quotes = build_paper_quotes(snapshots, LPConfig(quote_size_shares=10, active_capital_limit=1000))
    assert len(quotes) == 2
    assert set(["yes_best_bid_size", "yes_best_ask_size", "no_best_bid_size", "no_best_ask_size"]).issubset(
        quotes.columns
    )
    assert set(["yes_best_bid", "yes_best_ask", "no_best_bid", "no_best_ask"]).issubset(quotes.columns)
    assert quotes["yes_best_bid_size"].iloc[0] == 123.0
    assert quotes["yes_best_ask"].iloc[0] == 0.53


def test_depth_cap_quote_size_uses_smaller_opposite_rescue_ask_depth() -> None:
    row = pd.Series(
        {
            "yes_mid": 0.52,
            "no_mid": 0.48,
            "max_incentive_spread": 0.05,
            "min_incentive_size": 10.0,
            "reward_daily": 100.0,
            "yes_best_ask_size": 80.0,
            "no_best_ask_size": 30.0,
        }
    )
    cfg = LPConfig(quote_size_shares=100, depth_cap_quote_size=True, depth_quote_size_fraction=0.9)
    assert depth_capped_quote_size(row, cfg) == 27.0
    q = quote_for_row(row, cfg)
    assert q["eligible"]
    assert q["quote_size"] == 27.0


def test_depth_cap_quote_size_drops_quotes_below_minimum() -> None:
    row = pd.Series(
        {
            "yes_mid": 0.52,
            "no_mid": 0.48,
            "max_incentive_spread": 0.05,
            "min_incentive_size": 50.0,
            "reward_daily": 100.0,
            "yes_best_ask_size": 80.0,
            "no_best_ask_size": 30.0,
        }
    )
    cfg = LPConfig(quote_size_shares=100, depth_cap_quote_size=True)
    q = quote_for_row(row, cfg)
    assert q["quote_size"] == 30.0
    assert not q["eligible"]


def test_partial_rescue_residual_cap_allows_bounded_extra_size() -> None:
    row = pd.Series(
        {
            "yes_mid": 0.42,
            "no_mid": 0.58,
            "max_incentive_spread": 0.05,
            "min_incentive_size": 10.0,
            "reward_daily": 100.0,
            "yes_best_ask_size": 50.0,
            "no_best_ask_size": 25.0,
        }
    )
    cfg = LPConfig(
        quote_size_shares=300,
        quote_offset=0.02,
        partial_rescue_max_residual_loss_usdc=30.0,
    )
    capped = partial_rescue_residual_capped_quote_size(
        row,
        cfg,
        base_size=300,
        yes_bid=0.40,
        no_bid=0.56,
    )
    assert round(capped, 6) == round(min(25 + 30 / 0.40, 50 + 30 / 0.56), 6)
    q = quote_for_row(row, cfg)
    assert q["eligible"]
    assert float(q["quote_size"]) > 25
    assert float(q["quote_size"]) < 300


def test_partial_rescue_residual_cap_requires_book_depth() -> None:
    row = pd.Series(
        {
            "yes_mid": 0.42,
            "no_mid": 0.58,
            "max_incentive_spread": 0.05,
            "min_incentive_size": 10.0,
            "reward_daily": 100.0,
        }
    )
    cfg = LPConfig(quote_size_shares=300, partial_rescue_max_residual_loss_usdc=30.0)
    q = quote_for_row(row, cfg)
    assert q["quote_size"] == 0.0
    assert not q["eligible"]


def test_quote_size_respects_per_market_inventory_budget_before_reward_scoring() -> None:
    row = pd.Series(
        {
            "yes_mid": 0.62,
            "no_mid": 0.38,
            "max_incentive_spread": 0.05,
            "min_incentive_size": 10.0,
            "reward_daily": 100.0,
        }
    )
    cfg = LPConfig(
        quote_size_shares=500,
        quote_offset=0.02,
        safety_margin=0.015,
        max_unpaired_per_market=25.0,
        min_depth_capped_quote_size_shares=1.0,
    )
    capped = inventory_capped_quote_size(cfg, base_size=500, yes_bid=0.60, no_bid=0.36)
    assert capped == pytest.approx(25.0 / 0.60)
    q = quote_for_row(row, cfg)
    assert q["eligible"]
    assert float(q["quote_size"]) == pytest.approx(25.0 / float(q["yes_bid"]))
    assert float(q["quote_size"]) * max(float(q["yes_bid"]), float(q["no_bid"])) <= 25.0 + 1e-9


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


def test_rescue_quote_locks_pair_edge_before_rewards() -> None:
    row = pd.Series({"yes_mid": 0.45, "no_mid": 0.55})
    rescue = rescue_quote_for_inventory(
        row=row,
        inventory_side="YES",
        worst_entry_price=0.45,
        size=10,
        cfg=LPConfig(rescue_min_pair_edge_per_share=0.01, rescue_quote_offset=0.005),
    )
    assert rescue["eligible"]
    assert rescue["side"] == "NO"
    assert rescue["bid_price"] <= 0.54
    assert rescue["pair_edge_per_share"] >= 0.01 - 1e-12


def test_simulate_lp_uses_rescue_quote_to_complete_one_sided_inventory() -> None:
    snapshots = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-01-01T00:00:00Z"),
                "condition_id": "m1",
                "reward_daily": 100.0,
                "max_incentive_spread": 0.06,
                "min_incentive_size": 1.0,
                "yes_mid": 0.50,
                "no_mid": 0.50,
                "category": "economics",
                "cluster": "economics",
            },
            {
                "timestamp": pd.Timestamp("2026-01-01T00:05:00Z"),
                "condition_id": "m1",
                "reward_daily": 100.0,
                "max_incentive_spread": 0.06,
                "min_incentive_size": 1.0,
                "yes_mid": 0.44,
                "no_mid": 0.56,
                "category": "economics",
                "cluster": "economics",
            },
            {
                "timestamp": pd.Timestamp("2026-01-01T00:10:00Z"),
                "condition_id": "m1",
                "reward_daily": 100.0,
                "max_incentive_spread": 0.06,
                "min_incentive_size": 1.0,
                "yes_mid": 0.47,
                "no_mid": 0.53,
                "category": "economics",
                "cluster": "economics",
            },
        ]
    )
    cfg = LPConfig(
        quote_size_shares=10,
        quote_offset=0.05,
        safety_margin=0.01,
        rescue_min_pair_edge_per_share=0.01,
        rescue_quote_offset=0.005,
        exit_loss_cents=1.0,
        max_unpaired_minutes=999,
    )
    events, _, summary = simulate_lp(snapshots, cfg)
    assert "RESCUE_QUOTE" in set(events["event"])
    merged = events[events["event"].eq("PAIR_MERGED")]
    assert not merged.empty
    assert float(merged["pair_cost"].max()) <= 0.99 + 1e-12
    assert float(summary.iloc[0]["total_pair_spread_pnl_usdc"]) >= 0.10 - 1e-12
    assert int(summary.iloc[0]["rescue_quote_count"]) >= 1
    assert float(summary.iloc[0]["pair_completion_ratio_of_opened_shares"]) == 1.0


def test_rescue_stress_passes_when_opposite_price_can_lock_pair_edge() -> None:
    quotes = pd.DataFrame(
        [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "condition_id": "m1",
                "side": "YES",
                "bid_price": 0.45,
                "size_shares": 10,
                "quote_offset": 0.05,
                "cluster": "economics",
            },
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "condition_id": "m1",
                "side": "NO",
                "bid_price": 0.50,
                "size_shares": 10,
                "quote_offset": 0.05,
                "cluster": "economics",
            },
        ]
    )
    result = evaluate_rescue_stress(
        quotes,
        RescueStressConfig(
            rescue_min_pair_edge_per_share=0.01,
            rescue_quote_offset=0.005,
            min_price_feasible_rate=1.0,
        ),
    )
    assert result["status"] == "rescue_stress_passed"
    assert result["metrics"]["price_feasible_rate"] == 1.0
    assert result["metrics"]["latest_blocked_loss_to_zero"] == 0.0


def test_rescue_stress_blocks_unrescueable_one_sided_loss() -> None:
    quotes = pd.DataFrame(
        [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "condition_id": "m1",
                "side": "YES",
                "bid_price": 0.995,
                "size_shares": 100,
                "quote_offset": 0.0,
            },
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "condition_id": "m1",
                "side": "NO",
                "bid_price": 0.001,
                "size_shares": 100,
                "quote_offset": 0.0,
            },
        ]
    )
    result = evaluate_rescue_stress(
        quotes,
        RescueStressConfig(
            initial_capital=2000,
            rescue_min_pair_edge_per_share=0.01,
            max_latest_blocked_loss_fraction=0.01,
        ),
    )
    assert result["status"] == "rescue_stress_failed"
    assert result["metrics"]["latest_blocked_loss_to_zero"] >= 99.5
    assert not result["gates"]["latest_blocked_loss_gate_passed"]


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


def test_default_filter_excludes_sports_and_crypto() -> None:
    snapshots = make_synthetic_snapshots(seed=2, days=1, n_markets=6)
    filtered = filter_snapshots_for_strategy(snapshots, LPConfig())
    assert not set(filtered["category"].str.lower()).intersection({"sports", "crypto"})
    assert len(filtered) < len(snapshots)


def test_default_filter_excludes_sports_and_crypto_subcategories() -> None:
    snapshots = pd.DataFrame(
        [
            {"timestamp": "2026-01-01T00:00:00Z", "condition_id": "nba", "category": "nba", "cluster": "nba", "reward_daily": 100, "max_incentive_spread": 0.05, "min_incentive_size": 20, "yes_mid": 0.5},
            {"timestamp": "2026-01-01T00:00:00Z", "condition_id": "btc", "category": "markets", "cluster": "bitcoin", "tags": "btc,crypto", "reward_daily": 100, "max_incentive_spread": 0.05, "min_incentive_size": 20, "yes_mid": 0.5},
            {"timestamp": "2026-01-01T00:00:00Z", "condition_id": "fed", "category": "economic-policy", "cluster": "economic-policy", "reward_daily": 100, "max_incentive_spread": 0.05, "min_incentive_size": 20, "yes_mid": 0.5},
        ]
    )
    filtered = filter_snapshots_for_strategy(snapshots, LPConfig())
    assert filtered["condition_id"].tolist() == ["fed"]


def test_build_paper_quotes_emits_two_sided_intents() -> None:
    snapshots = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-01-01T00:00:00Z"),
                "condition_id": "m1",
                "market_id": "market-1",
                "question": "Will this test pass?",
                "category": "politics",
                "cluster": "politics",
                "yes_mid": 0.52,
                "no_mid": 0.48,
                "reward_daily": 100.0,
                "max_incentive_spread": 0.05,
                "min_incentive_size": 20.0,
                "market_competitiveness": 0.2,
            }
        ]
    )
    quotes = build_paper_quotes(
        snapshots,
        LPConfig(
            quote_size_shares=25,
            quote_offset=0.02,
            active_capital_limit=100,
            min_reward_density_per_day=0,
        ),
    )
    assert list(quotes["side"]) == ["YES", "NO"]
    assert quotes["active_order_notional_pair"].max() <= 100
    assert quotes["pair_cost"].max() <= 1 - LPConfig().safety_margin


def test_collect_live_reward_snapshots_parses_gamma_fixture() -> None:
    def fake_get_json(url: str, params: dict[str, object] | None, timeout: float) -> object:
        assert url.endswith("/events")
        return [
            {
                "id": "event-1",
                "category": "Politics",
                "tags": [{"slug": "election"}],
                "markets": [
                    {
                        "id": "market-1",
                        "conditionId": "condition-1",
                        "question": "Will fixture parse?",
                        "clobRewards": [{"rewardsDailyRate": "12.5"}],
                        "rewardsMaxSpread": "4",
                        "rewardsMinSize": "20",
                        "acceptingOrders": True,
                        "enableOrderBook": True,
                        "clobTokenIds": '["yes-token","no-token"]',
                        "bestBid": "0.48",
                        "bestAsk": "0.52",
                        "lastTradePrice": "0.50",
                        "spread": "0.02",
                        "liquidityClob": "1000",
                        "volume24hrClob": "500",
                        "outcomes": '["Yes","No"]',
                    }
                ],
            }
        ]

    snapshots = collect_live_reward_snapshots(
        LiveSnapshotConfig(max_events=1),
        now=pd.Timestamp("2026-01-01T00:00:00Z"),
        get_json=fake_get_json,
    )
    assert len(snapshots) == 1
    row = snapshots.iloc[0]
    assert row["condition_id"] == "condition-1"
    assert row["reward_daily"] == 12.5
    assert row["max_incentive_spread"] == 0.04
    assert row["quote_data_quality"] == "gamma_best_bid_ask"


def test_run_live_paper_loop_writes_manifest_without_orders(tmp_path) -> None:
    def fake_get_json(url: str, params: dict[str, object] | None, timeout: float) -> object:
        return [
            {
                "id": "event-1",
                "category": "Politics",
                "markets": [
                    {
                        "id": "market-1",
                        "conditionId": "condition-1",
                        "question": "Will manifest write?",
                        "clobRewards": [{"rewardsDailyRate": "100"}],
                        "rewardsMaxSpread": "5",
                        "rewardsMinSize": "20",
                        "acceptingOrders": True,
                        "enableOrderBook": True,
                        "bestBid": "0.49",
                        "bestAsk": "0.51",
                        "lastTradePrice": "0.50",
                        "spread": "0.02",
                    }
                ],
            }
        ]

    manifest = run_live_paper_loop(
        snapshot_path=tmp_path / "snapshots.csv",
        quotes_path=tmp_path / "quotes.csv",
        manifest_path=tmp_path / "manifest.json",
        lp_config=LPConfig(quote_size_shares=25, quote_offset=0.02, active_capital_limit=100),
        snapshot_config=LiveSnapshotConfig(max_events=1),
        iterations=1,
        interval_seconds=0,
        get_json=fake_get_json,
    )
    assert manifest["iterations_completed"] == 1
    assert manifest["paper_quote_rows"] == 2
    assert "paper only" in manifest["safety"]
    assert (tmp_path / "snapshots.csv").exists()
    assert (tmp_path / "quotes.csv").exists()


def test_run_live_paper_loop_refuses_existing_output_lock(tmp_path) -> None:
    lock = tmp_path / "manifest.json.lock"
    lock.write_text("already running", encoding="utf-8")

    with pytest.raises(RuntimeError, match="live-paper output lock exists"):
        run_live_paper_loop(
            snapshot_path=tmp_path / "snapshots.csv",
            quotes_path=tmp_path / "quotes.csv",
            manifest_path=tmp_path / "manifest.json",
            lp_config=LPConfig(quote_size_shares=25, quote_offset=0.02, active_capital_limit=100),
            snapshot_config=LiveSnapshotConfig(max_events=1),
            iterations=1,
            interval_seconds=0,
            get_json=lambda *_: [],
        )

    assert lock.read_text(encoding="utf-8") == "already running"
    assert not (tmp_path / "snapshots.csv").exists()
    assert not (tmp_path / "quotes.csv").exists()


def test_analyze_paper_quotes_uses_next_snapshot_fill_proxy() -> None:
    snapshots = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-01-01T00:00:00Z"),
                "condition_id": "m1",
                "yes_mid": 0.52,
                "no_mid": 0.48,
                "reward_daily": 100,
                "max_incentive_spread": 0.05,
                "min_incentive_size": 20,
                "cluster": "politics",
            },
            {
                "timestamp": pd.Timestamp("2026-01-01T00:05:00Z"),
                "condition_id": "m1",
                "yes_mid": 0.49,
                "no_mid": 0.51,
                "reward_daily": 100,
                "max_incentive_spread": 0.05,
                "min_incentive_size": 20,
                "cluster": "politics",
            },
        ]
    )
    quotes = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-01-01T00:00:00Z"),
                "condition_id": "m1",
                "side": "YES",
                "bid_price": 0.50,
                "size_shares": 25,
                "active_order_notional_pair": 24,
                "reward_density_per_day": 0.1,
                "cluster": "politics",
            },
            {
                "timestamp": pd.Timestamp("2026-01-01T00:00:00Z"),
                "condition_id": "m1",
                "side": "NO",
                "bid_price": 0.46,
                "size_shares": 25,
                "active_order_notional_pair": 24,
                "reward_density_per_day": 0.1,
                "cluster": "politics",
            },
        ]
    )
    per_quote, summary = analyze_paper_quotes(snapshots, quotes, PaperAnalysisConfig(stale_mid_change=0.025))
    yes = per_quote[per_quote["side"].eq("YES")].iloc[0]
    no = per_quote[per_quote["side"].eq("NO")].iloc[0]
    assert bool(yes["would_fill"])
    assert not bool(no["would_fill"])
    assert bool(yes["stale_fill"])
    assert summary["would_fill_rows"] == 1
    assert summary["stale_fill_rows"] == 1
    assert summary["estimated_mark_to_next_pnl_if_all_fills_usdc"] < 0


def test_analyze_paper_quotes_excludes_latest_right_censored_pending_from_quality_rate() -> None:
    snapshots = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-01-01T00:00:00Z"),
                "condition_id": "m1",
                "yes_mid": 0.52,
                "no_mid": 0.48,
                "reward_daily": 100,
                "max_incentive_spread": 0.05,
                "min_incentive_size": 20,
            },
            {
                "timestamp": pd.Timestamp("2026-01-01T00:05:00Z"),
                "condition_id": "m1",
                "yes_mid": 0.52,
                "no_mid": 0.48,
                "reward_daily": 100,
                "max_incentive_spread": 0.05,
                "min_incentive_size": 20,
            },
        ]
    )
    quotes = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-01-01T00:00:00Z"),
                "condition_id": "m1",
                "side": "YES",
                "bid_price": 0.50,
                "size_shares": 25,
                "active_order_notional_pair": 24,
                "reward_density_per_day": 0.1,
            },
            {
                "timestamp": pd.Timestamp("2026-01-01T00:00:00Z"),
                "condition_id": "m1",
                "side": "NO",
                "bid_price": 0.46,
                "size_shares": 25,
                "active_order_notional_pair": 24,
                "reward_density_per_day": 0.1,
            },
            {
                "timestamp": pd.Timestamp("2026-01-01T00:05:00Z"),
                "condition_id": "m1",
                "side": "YES",
                "bid_price": 0.50,
                "size_shares": 25,
                "active_order_notional_pair": 24,
                "reward_density_per_day": 0.1,
            },
            {
                "timestamp": pd.Timestamp("2026-01-01T00:05:00Z"),
                "condition_id": "m1",
                "side": "NO",
                "bid_price": 0.46,
                "size_shares": 25,
                "active_order_notional_pair": 24,
                "reward_density_per_day": 0.1,
            },
        ]
    )
    _, summary = analyze_paper_quotes(snapshots, quotes)
    assert summary["raw_pending_quote_rate"] == 0.5
    assert summary["pending_quote_rate"] == 0.0
    assert summary["evaluable_quote_rows"] == 2
    assert summary["right_censored_pending_quote_rows"] == 2


def test_run_paper_analysis_to_files_writes_summary(tmp_path) -> None:
    snapshots = make_synthetic_snapshots(seed=3, days=1, n_markets=3)
    quotes = build_paper_quotes(snapshots, LPConfig(quote_size_shares=25, quote_offset=0.025, active_capital_limit=200))
    summary = run_paper_analysis_to_files(snapshots=snapshots, quotes=quotes, out_dir=tmp_path)
    assert summary["snapshot_rows"] == len(snapshots)
    assert "paper analytics only" in summary["safety"]
    assert (tmp_path / "paper_summary.json").exists()
    assert (tmp_path / "paper_quote_outcomes.csv").exists()


def test_target_monitor_separates_density_from_deployment_proof() -> None:
    summary = {
        "duration_hours": 12,
        "quote_rows": 24,
        "estimated_reward_accrual_usdc": 40,
        "avg_active_pair_notional": 1500,
        "fill_proxy_rate": 0,
        "stale_fill_rate": 0,
    }
    result = target_monitor_from_summary(
        summary,
        TargetMonitorConfig(
            target_monthly_usdc=1000,
            reward_to_loss_haircut=8,
            min_observation_hours=24,
            paid_reward_verified=False,
        ),
    )
    assert result["gates"]["density_gate_passed"]
    assert result["gates"]["capture_gate_passed"]
    assert not result["gates"]["sample_gate_passed"]
    assert not result["gates"]["deployment_proof_passed"]
    assert result["status"] == "target_density_passed_sample_too_short"


def test_target_monitor_flags_concentration_when_required() -> None:
    summary = {
        "duration_hours": 30,
        "quote_rows": 24,
        "unique_markets_quoted": 1,
        "estimated_reward_accrual_usdc": 100,
        "avg_active_pair_notional": 1500,
        "fill_proxy_rate": 0,
        "stale_fill_rate": 0,
    }
    result = target_monitor_from_summary(
        summary,
        TargetMonitorConfig(target_monthly_usdc=1000, reward_to_loss_haircut=8, min_unique_markets=2),
    )
    assert result["gates"]["density_gate_passed"]
    assert not result["gates"]["diversification_gate_passed"]
    assert result["status"] == "diversification_failed"


def test_target_monitor_flags_active_notional_limit() -> None:
    summary = {
        "duration_hours": 30,
        "quote_rows": 24,
        "unique_markets_quoted": 2,
        "estimated_reward_accrual_usdc": 100,
        "avg_active_pair_notional": 1500,
        "max_active_pair_notional": 1700,
        "fill_proxy_rate": 0,
        "stale_fill_rate": 0,
    }
    result = target_monitor_from_summary(
        summary,
        TargetMonitorConfig(
            target_monthly_usdc=1000,
            reward_to_loss_haircut=8,
            min_unique_markets=2,
            max_active_pair_notional=1600,
        ),
    )
    assert result["gates"]["diversification_gate_passed"]
    assert not result["gates"]["active_notional_gate_passed"]
    assert result["status"] == "active_notional_failed"


def test_target_monitor_flags_pending_quote_quality() -> None:
    summary = {
        "duration_hours": 30,
        "quote_rows": 24,
        "unique_markets_quoted": 2,
        "estimated_reward_accrual_usdc": 100,
        "avg_active_pair_notional": 1500,
        "max_active_pair_notional": 1500,
        "fill_proxy_rate": 0,
        "stale_fill_rate": 0,
        "pending_quote_rate": 0.5,
    }
    result = target_monitor_from_summary(
        summary,
        TargetMonitorConfig(
            target_monthly_usdc=1000,
            reward_to_loss_haircut=8,
            min_unique_markets=2,
            max_pending_quote_rate=0.05,
        ),
    )
    assert not result["gates"]["risk_proxy_gate_passed"]
    assert result["status"] == "risk_proxy_failed"


def test_target_bootstrap_reports_p05_target_gate() -> None:
    quotes = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-01-01T00:00:00Z"),
                "condition_id": "m1",
                "side": "YES",
                "reward_density_per_day": 1.0,
                "active_order_notional_pair": 1000,
            },
            {
                "timestamp": pd.Timestamp("2026-01-01T00:00:00Z"),
                "condition_id": "m1",
                "side": "NO",
                "reward_density_per_day": 1.0,
                "active_order_notional_pair": 1000,
            },
            {
                "timestamp": pd.Timestamp("2026-01-01T00:05:00Z"),
                "condition_id": "m1",
                "side": "YES",
                "reward_density_per_day": 1.0,
                "active_order_notional_pair": 1000,
            },
            {
                "timestamp": pd.Timestamp("2026-01-01T00:05:00Z"),
                "condition_id": "m1",
                "side": "NO",
                "reward_density_per_day": 1.0,
                "active_order_notional_pair": 1000,
            },
        ]
    )
    result = _bootstrap_target_from_quotes(
        quotes,
        cfg=TargetMonitorConfig(target_monthly_usdc=1000, reward_to_loss_haircut=8),
        max_reward_gap_seconds=300,
        resamples=50,
        seed=1,
        block_size=1,
        capture_rate=0.5,
        min_target_margin=1.0,
    )
    assert result["enabled"]
    assert result["intervals"] == 1
    assert result["net_monthly_p05"] > 1000
    assert result["p05_target_gate_passed"]
    assert result["captured_net_monthly_p05"] > 1000
    assert result["captured_p05_target_gate_passed"]


def test_capture_stress_grid_reports_target_breakpoint() -> None:
    result = _capture_stress_grid(
        {"enabled": True, "net_monthly_p05": 2400.0},
        capture_rates=[0.35, 0.5],
        target_monthly_usdc=1000,
        min_target_margin=1.0,
    )
    assert result[0]["captured_net_monthly_p05"] == 840.0
    assert not result[0]["target_gate_passed"]
    assert result[1]["captured_net_monthly_p05"] == 1200.0
    assert result[1]["target_gate_passed"]


def test_json_safe_replaces_nonfinite_numbers() -> None:
    result = _json_safe({"x": float("nan"), "y": [float("inf"), 1.0]})
    assert result == {"x": None, "y": [None, 1.0]}


def test_execution_telemetry_audit_proves_reward_and_cancel_capture() -> None:
    orders = pd.DataFrame(
        [
            {"client_order_id": "o1", "created_ts": "2026-01-01T00:00:00Z"},
            {"client_order_id": "o2", "created_ts": "2026-01-01T00:00:01Z"},
        ]
    )
    fills = pd.DataFrame(
        [
            {"client_order_id": "o1", "fill_price": 0.4, "fill_size": 10, "fee_usdc": 0.01},
        ]
    )
    cancels = pd.DataFrame(
        [
            {
                "client_order_id": "o2",
                "cancel_requested_ts": "2026-01-01T00:05:00Z",
                "cancel_confirmed_ts": "2026-01-01T00:05:05Z",
            }
        ]
    )
    rewards = pd.DataFrame([{"estimated_reward_usdc": 10.0, "paid_reward_usdc": 6.0}])
    result = audit_execution_telemetry(
        orders=orders,
        fills=fills,
        cancels=cancels,
        rewards=rewards,
        cfg=ExecutionTelemetryConfig(
            min_orders=2,
            min_cancels=1,
            min_reward_capture_rate=0.5,
            max_cancel_latency_seconds=10,
        ),
    )
    assert result["metrics"]["reward_capture_rate"] == 0.6
    assert result["metrics"]["max_cancel_latency_seconds"] == 5
    assert result["gates"]["deployment_telemetry_passed"]


def test_execution_telemetry_audit_rejects_missing_paid_rewards() -> None:
    result = audit_execution_telemetry(
        orders=pd.DataFrame([{"client_order_id": "o1"}]),
        cancels=pd.DataFrame(
            [
                {
                    "client_order_id": "o1",
                    "cancel_requested_ts": "2026-01-01T00:05:00Z",
                    "cancel_confirmed_ts": "2026-01-01T00:05:01Z",
                }
            ]
        ),
        rewards=pd.DataFrame([{"estimated_reward_usdc": 10.0, "paid_reward_usdc": 0.0}]),
    )
    assert not result["gates"]["reward_payment_present"]
    assert not result["gates"]["deployment_telemetry_passed"]


def test_deployment_readiness_requires_sample_and_telemetry() -> None:
    target_status = {
        "paper_summary": {"duration_hours": 1, "unique_markets_quoted": 2, "max_active_pair_notional": 1500},
        "target_monitor": {
            "input": {
                "duration_hours": 1,
                "unique_markets_quoted": 2,
                "max_active_pair_notional": 1500,
                "fill_proxy_rate": 0,
                "stale_fill_rate": 0,
                "pending_quote_rate": 0,
            },
            "target_math": {"net_monthly_after_loss_haircut": 2500, "capture_needed_for_target": 0.4},
            "gates": {
                "density_gate_passed": True,
                "capture_gate_passed": True,
                "risk_proxy_gate_passed": True,
                "diversification_gate_passed": True,
                "active_notional_gate_passed": True,
                "sample_gate_passed": False,
            },
        },
        "bootstrap_target": {"net_monthly_p05": 2400, "intervals": 10},
        "capture_stress_grid": [{"capture_rate": 0.5, "captured_net_monthly_p05": 1200}],
    }
    result = evaluate_deployment_readiness(target_status=target_status, cfg=DeploymentReadinessConfig())
    assert result["gates"]["capture_stress_gate_passed"]
    assert not result["gates"]["sample_gate_passed"]
    assert not result["gates"]["telemetry_gate_passed"]
    assert result["status"] == "deployment_not_ready"


def test_deployment_readiness_passes_when_all_gates_pass() -> None:
    target_status = {
        "target_monitor": {
            "input": {
                "duration_hours": 25,
                "unique_markets_quoted": 3,
                "max_active_pair_notional": 1500,
                "fill_proxy_rate": 0,
                "stale_fill_rate": 0,
                "pending_quote_rate": 0,
            },
            "target_math": {"net_monthly_after_loss_haircut": 2500, "capture_needed_for_target": 0.4},
            "gates": {
                "density_gate_passed": True,
                "capture_gate_passed": True,
                "risk_proxy_gate_passed": True,
                "diversification_gate_passed": True,
                "active_notional_gate_passed": True,
                "sample_gate_passed": True,
            },
        },
        "capture_stress_grid": [{"capture_rate": 0.5, "captured_net_monthly_p05": 1200}],
    }
    telemetry = {"gates": {"deployment_telemetry_passed": True}}
    result = evaluate_deployment_readiness(target_status=target_status, telemetry_audit=telemetry)
    assert result["gates"]["deployment_ready"]
    assert result["blockers"] == []


def test_deployment_readiness_blocks_pending_quotes() -> None:
    target_status = {
        "target_monitor": {
            "input": {
                "duration_hours": 25,
                "unique_markets_quoted": 3,
                "max_active_pair_notional": 1500,
                "fill_proxy_rate": 0,
                "stale_fill_rate": 0,
                "pending_quote_rate": 0.5,
            },
            "target_math": {"net_monthly_after_loss_haircut": 2500, "capture_needed_for_target": 0.4},
            "gates": {
                "density_gate_passed": True,
                "capture_gate_passed": True,
                "risk_proxy_gate_passed": True,
                "diversification_gate_passed": True,
                "active_notional_gate_passed": True,
                "sample_gate_passed": True,
            },
        },
        "capture_stress_grid": [{"capture_rate": 0.5, "captured_net_monthly_p05": 1200}],
    }
    telemetry = {"gates": {"deployment_telemetry_passed": True}}
    result = evaluate_deployment_readiness(target_status=target_status, telemetry_audit=telemetry)
    assert not result["gates"]["pending_quote_gate_passed"]
    assert not result["gates"]["deployment_ready"]


def test_deployment_readiness_blocks_mid_move_risk() -> None:
    target_status = {
        "paper_summary": {"max_abs_mid_change_to_next": 0.02},
        "target_monitor": {
            "input": {
                "duration_hours": 25,
                "unique_markets_quoted": 3,
                "max_active_pair_notional": 1500,
                "fill_proxy_rate": 0,
                "stale_fill_rate": 0,
                "pending_quote_rate": 0,
            },
            "target_math": {"net_monthly_after_loss_haircut": 2500, "capture_needed_for_target": 0.4},
            "gates": {
                "density_gate_passed": True,
                "capture_gate_passed": True,
                "risk_proxy_gate_passed": True,
                "diversification_gate_passed": True,
                "active_notional_gate_passed": True,
                "sample_gate_passed": True,
            },
        },
        "capture_stress_grid": [{"capture_rate": 0.5, "captured_net_monthly_p05": 1200}],
    }
    telemetry = {"gates": {"deployment_telemetry_passed": True}}
    result = evaluate_deployment_readiness(
        target_status=target_status,
        telemetry_audit=telemetry,
        cfg=DeploymentReadinessConfig(max_abs_mid_change_to_next=0.003),
    )
    assert not result["gates"]["mid_move_gate_passed"]
    assert not result["gates"]["deployment_ready"]


def test_deployment_readiness_blocks_low_cash_reserve() -> None:
    target_status = {
        "target_monitor": {
            "input": {
                "duration_hours": 25,
                "unique_markets_quoted": 3,
                "max_active_pair_notional": 1900,
                "fill_proxy_rate": 0,
                "stale_fill_rate": 0,
                "pending_quote_rate": 0,
            },
            "target_math": {"net_monthly_after_loss_haircut": 2500, "capture_needed_for_target": 0.4},
            "gates": {
                "density_gate_passed": True,
                "capture_gate_passed": True,
                "risk_proxy_gate_passed": True,
                "diversification_gate_passed": True,
                "active_notional_gate_passed": True,
                "sample_gate_passed": True,
            },
        },
        "capture_stress_grid": [{"capture_rate": 0.5, "captured_net_monthly_p05": 1200}],
    }
    telemetry = {"gates": {"deployment_telemetry_passed": True}}
    result = evaluate_deployment_readiness(
        target_status=target_status,
        telemetry_audit=telemetry,
        cfg=DeploymentReadinessConfig(
            initial_capital=2000,
            max_active_pair_notional=2000,
            min_cash_reserve_fraction=0.2,
        ),
    )
    assert not result["gates"]["cash_reserve_gate_passed"]
    assert not result["gates"]["deployment_ready"]


def test_target_config_grid_rejects_pending_and_midmove_risk() -> None:
    summary = {
        "quote_rows": 24,
        "quote_pair_intervals": 12,
        "unique_markets_quoted": 2,
        "duration_hours": 1.0,
        "avg_active_pair_notional": 1500,
        "avg_reward_density_per_day": 0.1,
        "estimated_reward_accrual_usdc": 10,
        "fill_proxy_rate": 0,
        "stale_fill_rate": 0,
        "pending_quote_rate": 0.2,
        "max_abs_mid_change_to_next": 0.01,
    }
    monitor = {"target_math": {"net_monthly_after_loss_haircut": 3000, "capture_needed_for_target": 1 / 3}}
    bootstrap = {
        "captured_net_monthly_p05": 1500,
        "captured_p05_target_gate_passed": True,
        "intervals": 12,
        "net_monthly_p05": 3000,
    }
    row = _candidate_row(
        0.02,
        0.0256,
        summary,
        monitor,
        bootstrap,
        SelectionConfig(max_pending_quote_rate=0.05, max_abs_mid_change_to_next=0.003),
    )
    assert not row["selected_gate_passed"]
    assert row["pending_quote_rate"] == 0.2


def test_rolling_target_windows_are_fixed_length_and_step() -> None:
    start = pd.Timestamp("2026-01-01T00:00:00Z")
    end = pd.Timestamp("2026-01-01T03:00:00Z")
    windows = _time_windows(start, end, window_hours=1.0, step_hours=0.5)
    assert windows == [
        (pd.Timestamp("2026-01-01T00:00:00Z"), pd.Timestamp("2026-01-01T01:00:00Z")),
        (pd.Timestamp("2026-01-01T00:30:00Z"), pd.Timestamp("2026-01-01T01:30:00Z")),
        (pd.Timestamp("2026-01-01T01:00:00Z"), pd.Timestamp("2026-01-01T02:00:00Z")),
        (pd.Timestamp("2026-01-01T01:30:00Z"), pd.Timestamp("2026-01-01T02:30:00Z")),
        (pd.Timestamp("2026-01-01T02:00:00Z"), pd.Timestamp("2026-01-01T03:00:00Z")),
    ]


def test_capital_risk_stress_reports_recovery_days() -> None:
    quotes = pd.DataFrame(
        [
            {"timestamp": "2026-01-01T00:00:00Z", "condition_id": "m1", "cluster": "macro", "side": "YES", "bid_price": 0.8, "size_shares": 100, "active_order_notional_pair": 95},
            {"timestamp": "2026-01-01T00:00:00Z", "condition_id": "m1", "cluster": "macro", "side": "NO", "bid_price": 0.15, "size_shares": 100, "active_order_notional_pair": 95},
            {"timestamp": "2026-01-01T00:00:00Z", "condition_id": "m2", "cluster": "macro", "side": "YES", "bid_price": 0.4, "size_shares": 100, "active_order_notional_pair": 95},
            {"timestamp": "2026-01-01T00:00:00Z", "condition_id": "m2", "cluster": "macro", "side": "NO", "bid_price": 0.55, "size_shares": 100, "active_order_notional_pair": 95},
        ]
    )
    target_status = {"capture_stress_grid": [{"capture_rate": 0.5, "captured_net_monthly_p05": 1500.0}]}
    result = evaluate_capital_risk_stress(quotes, target_status=target_status, cfg=CapitalRiskStressConfig())
    assert result["metrics"]["all_active_unhedged_one_side_loss_to_zero"] == 135.0
    assert result["metrics"]["configured_inventory_cap_loss_to_zero"] == 115.0
    assert result["metrics"]["capped_recovery_days_at_p05_income"] == 115.0 / 50.0
    assert result["metrics"]["capture_needed_after_cap_loss"] == pytest.approx(0.37166666666666665)
    assert result["gates"]["capital_risk_stress_passed"]


def test_capital_risk_stress_blocks_total_ruin() -> None:
    quotes = pd.DataFrame(
        [
            {"timestamp": "2026-01-01T00:00:00Z", "condition_id": f"m{i}", "cluster": "macro", "side": "YES", "bid_price": 0.9, "size_shares": 1000, "active_order_notional_pair": 950}
            for i in range(3)
        ]
    )
    result = evaluate_capital_risk_stress(
        quotes,
        target_status={"capture_stress_grid": [{"capture_rate": 0.5, "captured_net_monthly_p05": 1000.0}]},
        cfg=CapitalRiskStressConfig(initial_capital=2000),
    )
    assert not result["gates"]["no_total_ruin_unhedged_gate_passed"]
    assert not result["gates"]["capital_risk_stress_passed"]


def test_capital_risk_stress_blocks_single_market_concentration() -> None:
    quotes = pd.DataFrame(
        [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "condition_id": "m1",
                "cluster": "macro",
                "side": "YES",
                "bid_price": 0.50,
                "size_shares": 1000,
                "active_order_notional_pair": 900,
            },
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "condition_id": "m1",
                "cluster": "macro",
                "side": "NO",
                "bid_price": 0.45,
                "size_shares": 1000,
                "active_order_notional_pair": 900,
            },
        ]
    )
    result = evaluate_capital_risk_stress(
        quotes,
        target_status={"capture_stress_grid": [{"capture_rate": 0.5, "captured_net_monthly_p05": 2000.0}]},
        cfg=CapitalRiskStressConfig(
            initial_capital=2000,
            min_latest_markets=2,
            max_single_market_active_fraction=0.35,
            max_single_market_unhedged_loss_fraction=0.20,
        ),
    )
    assert not result["gates"]["latest_market_count_gate_passed"]
    assert not result["gates"]["single_market_active_gate_passed"]
    assert not result["gates"]["single_market_loss_gate_passed"]
    assert not result["gates"]["capital_risk_stress_passed"]


def test_capital_risk_stress_blocks_high_capture_needed_after_cap_loss() -> None:
    quotes = pd.DataFrame(
        [
            {"timestamp": "2026-01-01T00:00:00Z", "condition_id": "m1", "cluster": "macro", "side": "YES", "bid_price": 0.5, "size_shares": 100, "active_order_notional_pair": 95},
            {"timestamp": "2026-01-01T00:00:00Z", "condition_id": "m1", "cluster": "macro", "side": "NO", "bid_price": 0.45, "size_shares": 100, "active_order_notional_pair": 95},
            {"timestamp": "2026-01-01T00:00:00Z", "condition_id": "m2", "cluster": "other", "side": "YES", "bid_price": 0.5, "size_shares": 100, "active_order_notional_pair": 95},
            {"timestamp": "2026-01-01T00:00:00Z", "condition_id": "m2", "cluster": "other", "side": "NO", "bid_price": 0.45, "size_shares": 100, "active_order_notional_pair": 95},
        ]
    )
    target_status = {"capture_stress_grid": [{"capture_rate": 0.5, "captured_net_monthly_p05": 1200.0}]}
    result = evaluate_capital_risk_stress(
        quotes,
        target_status=target_status,
        cfg=CapitalRiskStressConfig(max_capture_needed_after_cap_loss=0.40),
    )
    assert result["metrics"]["capture_needed_after_cap_loss"] > 0.40
    assert not result["gates"]["capture_needed_after_cap_loss_gate_passed"]
    assert not result["gates"]["capital_risk_stress_passed"]


def test_allocation_selector_balances_income_and_survival() -> None:
    rows = [
        {
            "qsize": 300,
            "selected_gate_passed": True,
            "capital_risk_stress_passed": True,
            "unique_markets_quoted": 3,
            "quote_offset": 0.02,
            "min_reward_density_per_day": 0.10,
            "net_monthly_after_loss_haircut": 4557,
            "captured_net_monthly_p05": 1860,
            "capture_needed_for_target": 0.22,
            "cash_reserve_fraction": 0.568,
            "unhedged_loss_to_zero": 689,
            "unhedged_loss_fraction": 0.344,
            "unhedged_recovery_days": 8.8,
            "configured_cap_loss": 180,
            "configured_cap_recovery_days": 2.3,
            "max_abs_mid_change_to_next": 0.006,
        },
        {
            "qsize": 800,
            "selected_gate_passed": True,
            "capital_risk_stress_passed": True,
            "unique_markets_quoted": 2,
            "quote_offset": 0.02,
            "min_reward_density_per_day": 0.0256,
            "net_monthly_after_loss_haircut": 8566,
            "captured_net_monthly_p05": 3463,
            "capture_needed_for_target": 0.117,
            "cash_reserve_fraction": 0.233,
            "unhedged_loss_to_zero": 1296,
            "unhedged_loss_fraction": 0.648,
            "unhedged_recovery_days": 16.6,
            "configured_cap_loss": 120,
            "configured_cap_recovery_days": 1.53,
            "max_abs_mid_change_to_next": 0.006,
        },
    ]
    result = select_allocation(rows, AllocationSelectionConfig())
    assert result["status"] == "allocation_selected"
    assert result["selected"]["metrics"]["qsize"] == 300
    assert result["income_max"]["metrics"]["qsize"] == 800


def test_allocation_selector_reports_no_pass_when_target_or_risk_fails() -> None:
    rows = [
        {
            "qsize": 300,
            "selected_gate_passed": True,
            "capital_risk_stress_passed": True,
            "unique_markets_quoted": 1,
            "captured_net_monthly_p05": 900,
            "cash_reserve_fraction": 0.2,
            "unhedged_loss_fraction": 0.7,
            "configured_cap_loss": 800,
            "configured_cap_recovery_days": 12,
            "max_abs_mid_change_to_next": 0.02,
        }
    ]
    result = select_allocation(rows, AllocationSelectionConfig())
    assert result["status"] == "no_allocation_passed"
    assert result["blockers"]


def test_objective_proof_requires_sample_rolling_telemetry_and_deployment() -> None:
    evidence = {
        "status": "proof_not_ready",
        "gates": {
            "income_50pct_p05_above_target": True,
            "cash_reserve_passed": True,
            "capital_stress_passed": True,
            "sample_24h_passed": False,
            "rolling_all_windows_passed": False,
            "telemetry_passed": False,
            "deployment_ready": False,
        },
        "income": {
            "observation_hours": 1,
            "p05_monthly_50pct_capture": 1800,
            "net_monthly_after_loss_haircut": 3600,
        },
        "risk": {"cash_reserve_fraction": 0.56, "unhedged_loss_fraction_of_capital": 0.35},
    }
    allocation = {
        "status": "allocation_selected",
        "selected": {
            "metrics": {
                "qsize": 300,
                "captured_net_monthly_p05": 1800,
                "net_monthly_after_loss_haircut": 4500,
                "cash_reserve_fraction": 0.56,
                "unhedged_loss_fraction": 0.35,
                "configured_cap_recovery_days": 3,
            },
            "gates": {
                "unhedged_loss_gate_passed": True,
                "configured_cap_recovery_gate_passed": True,
            },
        },
    }
    result = evaluate_objective_proof(evidence_packet=evidence, allocation_selection=allocation)
    assert result["status"] == "objective_not_proven"
    assert "needs >=24h public-paper observation" in result["blockers"]
    assert "order/fill/cancel/paid-reward telemetry missing" in result["blockers"]


def test_objective_proof_passes_when_all_required_gates_pass() -> None:
    evidence = {
        "status": "proof_ready",
        "gates": {
            "income_50pct_p05_above_target": True,
            "cash_reserve_passed": True,
            "capital_stress_passed": True,
            "sample_24h_passed": True,
            "rolling_all_windows_passed": True,
            "telemetry_passed": True,
            "deployment_ready": True,
        },
        "income": {
            "observation_hours": 24,
            "p05_monthly_50pct_capture": 1800,
            "net_monthly_after_loss_haircut": 3600,
        },
        "risk": {"cash_reserve_fraction": 0.56, "unhedged_loss_fraction_of_capital": 0.35},
    }
    allocation = {
        "status": "allocation_selected",
        "selected": {
            "metrics": {
                "qsize": 300,
                "captured_net_monthly_p05": 1800,
                "net_monthly_after_loss_haircut": 4500,
                "cash_reserve_fraction": 0.56,
                "unhedged_loss_fraction": 0.35,
                "configured_cap_recovery_days": 3,
            },
            "gates": {
                "unhedged_loss_gate_passed": True,
                "configured_cap_recovery_gate_passed": True,
            },
        },
    }
    result = evaluate_objective_proof(evidence_packet=evidence, allocation_selection=allocation)
    assert result["status"] == "objective_proven"
    assert result["blockers"] == []


def test_sustainability_stress_passes_configured_cap_but_warns_unhedged_recovery() -> None:
    evidence = {
        "income": {"p05_monthly_raw": 3600.0},
        "risk": {
            "configured_inventory_cap_loss_to_zero": 180.0,
            "all_active_unhedged_one_side_loss_to_zero": 720.0,
        },
    }
    allocation = {
        "status": "allocation_selected",
        "selected": {
            "metrics": {
                "captured_net_monthly_p05": 1900.0,
                "cash_reserve_fraction": 0.56,
                "unhedged_loss_fraction": 0.36,
                "configured_cap_loss": 180.0,
                "unhedged_loss_to_zero": 720.0,
            },
        },
    }
    result = evaluate_sustainability_stress(
        evidence_packet=evidence,
        allocation_selection=allocation,
        cfg=SustainabilityStressConfig(max_unhedged_recovery_days=10.0),
    )
    assert result["status"] == "sustainability_stress_passed"
    assert result["gates"]["configured_cap_shock_income_passed"]
    assert not result["gates"]["unhedged_recovery_warning_passed"]
    assert result["metrics"]["breakeven_reward_multiplier_configured_cap_at_reference_capture"] < 0.75


def test_sustainability_stress_fails_when_cap_loss_breaks_target() -> None:
    evidence = {"income": {"p05_monthly_raw": 2200.0}, "risk": {"configured_inventory_cap_loss_to_zero": 250.0}}
    allocation = {
        "status": "allocation_selected",
        "selected": {
            "metrics": {
                "captured_net_monthly_p05": 1100.0,
                "cash_reserve_fraction": 0.50,
                "unhedged_loss_fraction": 0.30,
                "configured_cap_loss": 250.0,
            },
        },
    }
    result = evaluate_sustainability_stress(evidence_packet=evidence, allocation_selection=allocation)
    assert result["status"] == "sustainability_stress_failed"
    assert "configured-cap monthly loss shock drops p05 income below target" in result["blockers"]


def test_risk_governor_continues_paper_when_risk_passes_but_proof_missing() -> None:
    evidence = {
        "income": {"p05_monthly_50pct_capture": 1900},
        "risk": {
            "max_active_pair_notional": 864,
            "cash_reserve_fraction": 0.568,
            "all_active_unhedged_one_side_loss_to_zero": 690,
            "configured_inventory_cap_loss_to_zero": 180,
            "configured_cap_recovery_days": 2.9,
        },
        "gates": {
            "sample_24h_passed": False,
            "rolling_all_windows_passed": False,
            "telemetry_passed": False,
            "deployment_ready": False,
        },
    }
    allocation = {
        "status": "allocation_selected",
        "selected": {
            "metrics": {
                "qsize": 300,
                "captured_net_monthly_p05": 1860,
                "avg_active_pair_notional": 864,
            }
        },
    }
    result = evaluate_risk_governor(
        evidence_packet=evidence,
        allocation_selection=allocation,
        objective_audit={"objective_proven": False},
        sustainability_stress={"status": "sustainability_stress_passed"},
    )
    assert result["status"] == "continue_public_paper_collect_signed_telemetry_next"
    assert result["metrics"]["recommended_qsize"] == 300
    assert not result["deployment_allowed"]


def test_risk_governor_scales_down_when_cash_or_loss_limits_bind() -> None:
    evidence = {
        "income": {"p05_monthly_50pct_capture": 2000},
        "risk": {
            "max_active_pair_notional": 1600,
            "cash_reserve_fraction": 0.20,
            "all_active_unhedged_one_side_loss_to_zero": 1200,
            "configured_inventory_cap_loss_to_zero": 100,
            "configured_cap_recovery_days": 2,
        },
        "gates": {},
    }
    allocation = {"status": "allocation_selected", "selected": {"metrics": {"qsize": 800, "captured_net_monthly_p05": 2000}}}
    result = evaluate_risk_governor(
        evidence_packet=evidence,
        allocation_selection=allocation,
        cfg=RiskGovernorConfig(min_cash_reserve_fraction=0.40, max_unhedged_loss_fraction=0.50),
    )
    assert result["metrics"]["recommended_scale"] == 0.75
    assert result["metrics"]["recommended_qsize"] == 600
    assert result["status"] == "reduce_size_and_continue_paper"


def test_completion_audit_requires_all_terminal_gates() -> None:
    result = evaluate_completion_audit(
        objective_audit={
            "objective_proven": False,
            "metrics": {"packet_50pct_capture_p05": 1900, "selected_qsize": 300},
        },
        sustainability_stress={
            "status": "sustainability_stress_passed",
            "metrics": {"reference_monthly_income": 1860, "configured_cap_reference_monthly_after_loss": 1680},
        },
        risk_governor={
            "risk_core_passed": True,
            "deployment_allowed": False,
            "metrics": {"recommended_qsize": 300, "governing_50pct_p05_monthly_income": 1860},
        },
        rescue_stress={"status": "rescue_stress_passed", "metrics": {"price_feasible_rate": 1.0}},
    )
    assert result["status"] == "completion_not_proven"
    assert "objective proof audit is not proven" in result["blockers"]
    assert "deployment is not allowed by risk governor" in result["blockers"]


def test_completion_audit_passes_when_terminal_gates_pass() -> None:
    result = evaluate_completion_audit(
        objective_audit={"objective_proven": True, "metrics": {"packet_50pct_capture_p05": 1900}},
        sustainability_stress={"status": "sustainability_stress_passed", "metrics": {"reference_monthly_income": 1860}},
        risk_governor={
            "risk_core_passed": True,
            "deployment_allowed": True,
            "metrics": {"recommended_qsize": 300, "governing_50pct_p05_monthly_income": 1860},
        },
        rescue_stress={"status": "rescue_stress_passed", "metrics": {"price_feasible_rate": 1.0}},
    )
    assert result["status"] == "completion_proven"
    assert result["blockers"] == []


def test_completion_audit_requires_rescue_stress_when_enabled() -> None:
    result = evaluate_completion_audit(
        objective_audit={"objective_proven": True, "metrics": {"packet_50pct_capture_p05": 1900}},
        sustainability_stress={"status": "sustainability_stress_passed", "metrics": {"reference_monthly_income": 1860}},
        risk_governor={"risk_core_passed": True, "deployment_allowed": True, "metrics": {}},
    )
    assert result["status"] == "completion_not_proven"
    assert "rescue stress is not passed" in result["blockers"]


def test_rescue_stress_reports_taker_depth_feasibility() -> None:
    quotes = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-01-01T00:00:00Z"),
                "condition_id": "m1",
                "side": "YES",
                "bid_price": 0.40,
                "size_shares": 100,
                "quote_offset": 0.02,
                "yes_best_ask": 0.43,
                "yes_best_ask_size": 100,
                "no_best_ask": 0.58,
                "no_best_ask_size": 120,
            },
            {
                "timestamp": pd.Timestamp("2026-01-01T00:00:00Z"),
                "condition_id": "m1",
                "side": "NO",
                "bid_price": 0.55,
                "size_shares": 100,
                "quote_offset": 0.02,
                "yes_best_ask": 0.43,
                "yes_best_ask_size": 100,
                "no_best_ask": 0.58,
                "no_best_ask_size": 120,
            },
        ]
    )
    result = evaluate_rescue_stress(
        quotes,
        RescueStressConfig(require_taker_rescue_depth=True, min_taker_rescue_feasible_rate=1.0),
    )
    assert result["status"] == "rescue_stress_passed"
    assert result["metrics"]["taker_rescue_book_scenarios"] == 2
    assert result["metrics"]["taker_rescue_feasible_rate"] == 1.0
    assert result["gates"]["taker_rescue_depth_gate_passed"]


def test_rescue_stress_reports_partial_taker_residual_loss() -> None:
    quotes = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-01-01T00:00:00Z"),
                "condition_id": "m1",
                "side": "YES",
                "bid_price": 0.40,
                "size_shares": 100,
                "quote_offset": 0.02,
                "yes_best_ask": 0.43,
                "yes_best_ask_size": 100,
                "no_best_ask": 0.58,
                "no_best_ask_size": 25,
            },
            {
                "timestamp": pd.Timestamp("2026-01-01T00:00:00Z"),
                "condition_id": "m1",
                "side": "NO",
                "bid_price": 0.55,
                "size_shares": 100,
                "quote_offset": 0.02,
                "yes_best_ask": 0.43,
                "yes_best_ask_size": 100,
                "no_best_ask": 0.58,
                "no_best_ask_size": 25,
            },
        ]
    )
    result = evaluate_rescue_stress(
        quotes,
        RescueStressConfig(
            require_taker_residual_loss=True,
            max_latest_taker_residual_loss_fraction=0.02,
        ),
    )
    assert result["status"] == "rescue_stress_passed"
    assert round(result["metrics"]["taker_size_weighted_rescue_fraction"], 4) == 0.625
    assert round(result["metrics"]["latest_taker_residual_loss_to_zero"], 6) == 30.0
    assert result["gates"]["taker_residual_loss_gate_passed"]


def test_rescue_stress_blocks_excess_partial_taker_residual_loss() -> None:
    quotes = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-01-01T00:00:00Z"),
                "condition_id": "m1",
                "side": "YES",
                "bid_price": 0.40,
                "size_shares": 100,
                "quote_offset": 0.02,
                "yes_best_ask": 0.43,
                "yes_best_ask_size": 100,
                "no_best_ask": 0.58,
                "no_best_ask_size": 5,
            },
            {
                "timestamp": pd.Timestamp("2026-01-01T00:00:00Z"),
                "condition_id": "m1",
                "side": "NO",
                "bid_price": 0.55,
                "size_shares": 100,
                "quote_offset": 0.02,
                "yes_best_ask": 0.43,
                "yes_best_ask_size": 5,
                "no_best_ask": 0.58,
                "no_best_ask_size": 100,
            },
        ]
    )
    result = evaluate_rescue_stress(
        quotes,
        RescueStressConfig(
            require_taker_residual_loss=True,
            max_latest_taker_residual_loss_fraction=0.005,
        ),
    )
    assert result["status"] == "rescue_stress_failed"
    assert result["metrics"]["latest_taker_residual_loss_fraction"] > 0.005
    assert not result["gates"]["taker_residual_loss_gate_passed"]


def test_depth_readiness_requires_income_sample_and_taker_depth() -> None:
    target_status = {
        "paper_summary": {"duration_hours": 6.5, "quote_rows": 30, "quote_data_quality_counts": {"clob_book_both_sides": 30}},
        "target_monitor": {
            "input": {"duration_hours": 6.5, "quote_rows": 30, "unique_markets_quoted": 3},
            "target_math": {"net_monthly_after_loss_haircut": 3000},
        },
        "capture_stress_grid": [{"capture_rate": 0.5, "captured_net_monthly_p05": 1200}],
    }
    rescue = {
        "metrics": {
            "taker_rescue_book_scenarios": 30,
            "taker_rescue_feasible_rate": 0.9,
            "taker_rescue_min_pair_edge_per_share": 0.01,
            "taker_rescue_min_depth_fraction": 1.2,
        }
    }
    result = evaluate_depth_readiness(
        target_status=target_status,
        rescue_stress=rescue,
        cfg=DepthReadinessConfig(min_observation_hours=6, min_quote_rows=24, min_book_scenarios=24),
    )
    assert result["status"] == "depth_ready"
    assert result["blockers"] == []


def test_depth_readiness_can_accept_partial_rescue_residual_cap() -> None:
    target_status = {
        "paper_summary": {"duration_hours": 6.5, "quote_rows": 30, "quote_data_quality_counts": {"clob_book_both_sides": 30}},
        "target_monitor": {
            "input": {"duration_hours": 6.5, "quote_rows": 30, "unique_markets_quoted": 3},
            "target_math": {"net_monthly_after_loss_haircut": 3000},
        },
        "capture_stress_grid": [{"capture_rate": 0.5, "captured_net_monthly_p05": 1200}],
    }
    rescue = {
        "metrics": {
            "taker_rescue_book_scenarios": 30,
            "taker_rescue_feasible_rate": 0.85,
            "taker_rescue_min_pair_edge_per_share": 0.01,
            "taker_rescue_min_depth_fraction": 0.2,
            "taker_size_weighted_rescue_fraction": 0.9,
            "latest_taker_residual_loss_to_zero": 50,
            "latest_taker_residual_loss_fraction": 0.025,
        }
    }
    result = evaluate_depth_readiness(
        target_status=target_status,
        rescue_stress=rescue,
        cfg=DepthReadinessConfig(
            min_observation_hours=6,
            min_quote_rows=24,
            min_book_scenarios=24,
            allow_partial_taker_rescue=True,
            max_latest_taker_residual_loss_fraction=0.05,
        ),
    )
    assert result["status"] == "depth_ready"
    assert result["gates"]["taker_depth_gate_passed"]
    assert result["gates"]["taker_residual_loss_gate_passed"]


def test_depth_readiness_residual_cap_has_float_tolerance() -> None:
    target_status = {
        "paper_summary": {"duration_hours": 6.0, "quote_rows": 24, "quote_data_quality_counts": {"clob_book_both_sides": 24}},
        "target_monitor": {
            "input": {"duration_hours": 6.0, "quote_rows": 24, "unique_markets_quoted": 2},
            "target_math": {"net_monthly_after_loss_haircut": 3000},
        },
        "capture_stress_grid": [{"capture_rate": 0.5, "captured_net_monthly_p05": 1000.0}],
    }
    rescue = {
        "metrics": {
            "taker_rescue_book_scenarios": 24,
            "taker_rescue_feasible_rate": 0.80,
            "taker_rescue_min_pair_edge_per_share": 0.0,
            "taker_rescue_min_depth_fraction": 0.75,
            "latest_taker_residual_loss_to_zero": 100.0,
            "latest_taker_residual_loss_fraction": 0.05000000000000001,
        }
    }
    result = evaluate_depth_readiness(
        target_status=target_status,
        rescue_stress=rescue,
        cfg=DepthReadinessConfig(
            allow_partial_taker_rescue=True,
            max_latest_taker_residual_loss_fraction=0.05,
        ),
    )
    assert result["status"] == "depth_ready"
    assert result["gates"]["taker_residual_loss_gate_passed"]


def test_depth_readiness_blocks_short_non_depth_sample() -> None:
    result = evaluate_depth_readiness(
        target_status={
            "paper_summary": {"duration_hours": 1, "quote_rows": 6, "quote_data_quality_counts": {"gamma_best_bid_ask": 6}},
            "target_monitor": {
                "input": {"duration_hours": 1, "quote_rows": 6, "unique_markets_quoted": 1},
                "target_math": {"net_monthly_after_loss_haircut": 4000},
            },
        },
        rescue_stress={"metrics": {"taker_rescue_book_scenarios": 0}},
    )
    assert result["status"] == "depth_not_ready"
    assert "quote evidence is not CLOB-depth quality" in result["blockers"]


def test_governed_config_applies_risk_governor_size_and_cash_cap() -> None:
    cfg = LPConfig(quote_size_shares=800, active_capital_limit=1900)
    governed, meta = apply_risk_governor_to_lp_config(
        cfg,
        {
            "status": "continue_public_paper_collect_signed_telemetry_next",
            "risk_core_passed": True,
            "deployment_allowed": False,
            "metrics": {
                "recommended_qsize": 300,
                "recommended_scale": 0.375,
                "max_active_pair_notional_by_cash": 1200,
            },
        },
    )
    assert governed.quote_size_shares == 300
    assert governed.active_capital_limit == 1200
    assert meta["quote_size_before"] == 800
    assert not meta["deployment_allowed"]


def test_governed_config_rejects_failed_core_gates() -> None:
    cfg = LPConfig()
    try:
        apply_risk_governor_to_lp_config(cfg, {"risk_core_passed": False, "metrics": {"recommended_qsize": 300}})
    except ValueError as exc:
        assert "core gates" in str(exc)
    else:
        raise AssertionError("expected failed risk governor core gates to block config")


def test_paper_replay_make_config_can_use_risk_governor_json(tmp_path) -> None:
    risk_path = tmp_path / "risk_governor.json"
    risk_path.write_text(
        json.dumps(
            {
                "risk_core_passed": True,
                "metrics": {
                    "recommended_qsize": 250,
                    "recommended_scale": 0.5,
                    "max_active_pair_notional_by_cash": 1000,
                },
            }
        ),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        initial_capital=2000,
        quote_size=800,
        quote_offset=0.02,
        safety_margin=0.015,
        active_capital_limit=1900,
        min_reward_daily=0,
        max_market_competitiveness=1,
        allowed_categories="",
        excluded_categories="sports,crypto",
        min_reward_density_per_day=0.1,
        recent_vol_window=6,
        max_recent_vol=0.006,
        max_recent_jump=0.025,
        vol_quote_multiplier=0.5,
        depth_cap_quote_size=False,
        depth_quote_size_fraction=1.0,
        min_depth_capped_quote_size=1.0,
        partial_rescue_max_residual_loss_usdc=0.0,
        risk_governor_json=str(risk_path),
        allow_risk_governor_not_core=False,
    )
    cfg = make_lp_config(args)
    assert cfg.quote_size_shares == 250
    assert cfg.active_capital_limit == 1000


def test_hedge_feasibility_identifies_partial_internal_hedge() -> None:
    capital_risk = {
        "config": {"initial_capital": 2000, "exit_slippage": 0.005},
        "metrics": {
            "all_active_unhedged_one_side_loss_to_zero": 700,
            "configured_inventory_cap_loss_to_zero": 180,
            "capped_recovery_days_at_p05_income": 3,
            "max_pair_cost_per_share": 0.96,
            "min_locked_pair_edge_usdc": 12,
        },
        "market_stress": [{"locked_pair_edge_usdc": 12}, {"locked_pair_edge_usdc": 14}],
        "gates": {"no_total_ruin_unhedged_gate_passed": True},
    }
    result = evaluate_hedge_feasibility(capital_risk=capital_risk)
    assert result["status"] == "partial_internal_hedge_feasible"
    assert result["metrics"]["configured_cap_loss_reduction_fraction"] > 0.7
    assert result["metrics"]["pair_lock_edge_total_usdc"] == 26
    assert result["gates"]["emergency_exit_slippage_cushion_passed"]
    assert "no perfect external hedge assumed" in result["blockers"][-1]


def test_hedge_feasibility_blocks_when_cap_or_slippage_fails() -> None:
    capital_risk = {
        "config": {"initial_capital": 2000, "exit_slippage": 0.02},
        "metrics": {
            "all_active_unhedged_one_side_loss_to_zero": 700,
            "configured_inventory_cap_loss_to_zero": 600,
            "capped_recovery_days_at_p05_income": 12,
            "max_pair_cost_per_share": 0.995,
            "min_locked_pair_edge_usdc": -1,
        },
        "gates": {"no_total_ruin_unhedged_gate_passed": True},
    }
    result = evaluate_hedge_feasibility(
        capital_risk=capital_risk,
        cfg=HedgeFeasibilityConfig(min_loss_reduction_fraction=0.5, max_configured_cap_loss_fraction=0.25),
    )
    assert result["status"] == "hedge_not_feasible"
    assert not result["gates"]["pair_lock_when_both_sides_fill_passed"]
    assert not result["gates"]["emergency_exit_slippage_cushion_passed"]
    assert not result["gates"]["configured_cap_tail_reduction_passed"]


def test_candidate_leaderboard_prefers_depth_ready_then_residual_risk() -> None:
    weak = {
        "status": "depth_not_ready",
        "metrics": {
            "duration_hours": 1,
            "quote_rows": 20,
            "unique_markets_quoted": 4,
            "income_p05_at_required_capture": 2000,
            "taker_rescue_feasible_rate": 1,
            "taker_size_weighted_rescue_fraction": 1,
            "latest_taker_residual_loss_to_zero": 0,
            "latest_taker_residual_loss_fraction": 0,
        },
        "gates": {
            "depth_ready": False,
            "income_p05_gate_passed": True,
            "clob_quality_gate_passed": True,
            "taker_rescue_rate_gate_passed": True,
            "taker_pair_edge_gate_passed": True,
            "taker_depth_gate_passed": True,
            "taker_residual_loss_gate_passed": True,
            "sample_hours_gate_passed": False,
            "quote_rows_gate_passed": False,
            "book_scenario_gate_passed": False,
        },
        "blockers": ["needs sample"],
    }
    ready = {
        "status": "depth_ready",
        "metrics": {
            "duration_hours": 6,
            "quote_rows": 30,
            "unique_markets_quoted": 3,
            "income_p05_at_required_capture": 1100,
            "taker_rescue_feasible_rate": 0.9,
            "taker_size_weighted_rescue_fraction": 0.99,
            "latest_taker_residual_loss_to_zero": 5,
            "latest_taker_residual_loss_fraction": 0.0025,
        },
        "gates": {
            "depth_ready": True,
            "income_p05_gate_passed": True,
            "clob_quality_gate_passed": True,
            "taker_rescue_rate_gate_passed": True,
            "taker_pair_edge_gate_passed": True,
            "taker_depth_gate_passed": True,
            "taker_residual_loss_gate_passed": True,
            "sample_hours_gate_passed": True,
            "quote_rows_gate_passed": True,
            "book_scenario_gate_passed": True,
        },
        "blockers": [],
    }
    result = build_candidate_leaderboard(
        [CandidateEvidence("weak_short_sample", weak), CandidateEvidence("ready", ready)]
    )
    assert result["status"] == "public_paper_leader_depth_ready"
    assert result["leader"]["name"] == "ready"
    assert result["candidates"][1]["name"] == "weak_short_sample"


def test_candidate_leaderboard_ignores_tiny_residual_float_noise_before_income() -> None:
    base_gate = {
        "status": "depth_not_ready",
        "metrics": {
            "duration_hours": 1,
            "quote_rows": 40,
            "unique_markets_quoted": 4,
            "taker_rescue_feasible_rate": 0.97,
            "taker_size_weighted_rescue_fraction": 0.99,
            "latest_taker_residual_loss_fraction": 0.005,
            "latest_taker_residual_loss_to_zero": 10.0,
        },
        "gates": {
            "depth_ready": False,
            "income_p05_gate_passed": True,
            "clob_quality_gate_passed": True,
            "taker_rescue_rate_gate_passed": True,
            "taker_pair_edge_gate_passed": True,
            "taker_depth_gate_passed": True,
            "taker_residual_loss_gate_passed": True,
            "sample_hours_gate_passed": False,
            "quote_rows_gate_passed": True,
            "book_scenario_gate_passed": True,
        },
    }
    lower_income = json.loads(json.dumps(base_gate))
    lower_income["metrics"]["income_p05_at_required_capture"] = 1400
    lower_income["metrics"]["latest_taker_residual_loss_to_zero"] = 9.999999999999995
    higher_income = json.loads(json.dumps(base_gate))
    higher_income["metrics"]["income_p05_at_required_capture"] = 1800
    higher_income["metrics"]["latest_taker_residual_loss_to_zero"] = 10.000000000000005
    result = build_candidate_leaderboard(
        [CandidateEvidence("lower_income_float_edge", lower_income), CandidateEvidence("higher_income", higher_income)]
    )
    assert result["leader"]["name"] == "higher_income"


def test_candidate_leaderboard_exposes_risk_income_and_sample_policy_leaders() -> None:
    def gate(income: float, residual: float, hours: float, rows: int) -> dict[str, object]:
        return {
            "status": "depth_not_ready",
            "config": {"target_monthly_usdc": 1000},
            "metrics": {
                "duration_hours": hours,
                "quote_rows": rows,
                "unique_markets_quoted": 4,
                "income_p05_at_required_capture": income,
                "taker_rescue_feasible_rate": 0.95,
                "taker_size_weighted_rescue_fraction": 0.99,
                "latest_taker_residual_loss_to_zero": residual,
                "latest_taker_residual_loss_fraction": residual / 2000,
            },
            "gates": {
                "depth_ready": False,
                "income_p05_gate_passed": True,
                "clob_quality_gate_passed": True,
                "taker_rescue_rate_gate_passed": True,
                "taker_pair_edge_gate_passed": True,
                "taker_depth_gate_passed": True,
                "taker_residual_loss_gate_passed": True,
                "sample_hours_gate_passed": False,
                "quote_rows_gate_passed": True,
                "book_scenario_gate_passed": True,
            },
        }

    result = build_candidate_leaderboard(
        [
            CandidateEvidence("low_risk", gate(1050, 2, 0.2, 24)),
            CandidateEvidence("high_income", gate(1800, 10, 0.7, 40)),
            CandidateEvidence("mature", gate(1200, 8, 2.0, 80)),
        ]
    )
    assert result["leader"]["name"] == "low_risk"
    leaders = result["policy_leaders"]
    assert leaders["risk_first_leader"]["name"] == "low_risk"
    assert leaders["income_first_leader"]["name"] == "high_income"
    assert leaders["sample_first_leader"]["name"] == "mature"


def test_candidate_leaderboard_demotes_drawdown_core_failures() -> None:
    def gate(income: float, residual: float) -> dict[str, object]:
        return {
            "status": "depth_not_ready",
            "config": {"target_monthly_usdc": 1000},
            "metrics": {
                "duration_hours": 1,
                "quote_rows": 40,
                "unique_markets_quoted": 4,
                "income_p05_at_required_capture": income,
                "taker_rescue_feasible_rate": 0.95,
                "taker_size_weighted_rescue_fraction": 0.99,
                "latest_taker_residual_loss_to_zero": residual,
                "latest_taker_residual_loss_fraction": residual / 2000,
            },
            "gates": {
                "depth_ready": False,
                "income_p05_gate_passed": True,
                "clob_quality_gate_passed": True,
                "taker_rescue_rate_gate_passed": True,
                "taker_pair_edge_gate_passed": True,
                "taker_depth_gate_passed": True,
                "taker_residual_loss_gate_passed": True,
                "sample_hours_gate_passed": False,
                "quote_rows_gate_passed": True,
                "book_scenario_gate_passed": True,
            },
        }

    good_drawdown = {
        "status": "drawdown_guard_sample_pending",
        "risk_core_passed": True,
        "gates": {"sample_hours_gate_passed": False, "drawdown_guard_passed": False},
        "metrics": {"reward_to_trading_loss_ratio": 3.2, "max_drawdown_mtm_fraction": 0.001},
        "blockers": ["needs sample"],
    }
    bad_drawdown = {
        "status": "drawdown_guard_failed",
        "risk_core_passed": False,
        "gates": {"sample_hours_gate_passed": False, "drawdown_guard_passed": False},
        "metrics": {"reward_to_trading_loss_ratio": 2.0, "max_drawdown_mtm_fraction": 0.001},
        "blockers": ["reward/trading-loss ratio below 3.00"],
    }
    result = build_candidate_leaderboard(
        [
            CandidateEvidence("higher_income_bad_dd", gate(1800, 0), drawdown_guard=bad_drawdown),
            CandidateEvidence("lower_income_good_dd", gate(1200, 5), drawdown_guard=good_drawdown),
        ]
    )
    assert result["leader"]["name"] == "lower_income_good_dd"
    assert not result["candidates"][1]["drawdown_core_passed"]
    assert result["policy_leaders"]["risk_first_leader"]["name"] == "lower_income_good_dd"


def test_candidate_leaderboard_demotes_capital_risk_failures() -> None:
    def gate(income: float) -> dict[str, object]:
        return {
            "status": "depth_not_ready",
            "config": {"target_monthly_usdc": 1000},
            "metrics": {
                "duration_hours": 1,
                "quote_rows": 40,
                "unique_markets_quoted": 4,
                "income_p05_at_required_capture": income,
                "taker_rescue_feasible_rate": 1,
                "taker_size_weighted_rescue_fraction": 1,
                "latest_taker_residual_loss_to_zero": 0,
                "latest_taker_residual_loss_fraction": 0,
            },
            "gates": {
                "depth_ready": False,
                "income_p05_gate_passed": True,
                "clob_quality_gate_passed": True,
                "taker_rescue_rate_gate_passed": True,
                "taker_pair_edge_gate_passed": True,
                "taker_depth_gate_passed": True,
                "taker_residual_loss_gate_passed": True,
                "sample_hours_gate_passed": False,
                "quote_rows_gate_passed": True,
                "book_scenario_gate_passed": True,
            },
        }

    drawdown = {
        "status": "drawdown_guard_sample_pending",
        "risk_core_passed": True,
        "gates": {"sample_hours_gate_passed": False, "drawdown_guard_passed": False},
        "metrics": {"reward_to_trading_loss_ratio": 5.0, "max_drawdown_mtm_fraction": 0.001},
    }
    good_capital = {
        "status": "capital_risk_stress_passed",
        "metrics": {
            "cash_reserve_fraction": 0.55,
            "unhedged_loss_fraction_of_capital": 0.30,
            "configured_inventory_cap_loss_to_zero": 120,
            "configured_inventory_cap_loss_fraction": 0.06,
            "capped_recovery_days_at_p05_income": 3.0,
        },
        "blockers": [],
    }
    bad_capital = {
        "status": "capital_risk_stress_failed",
        "metrics": {
            "cash_reserve_fraction": 0.10,
            "unhedged_loss_fraction_of_capital": 0.90,
            "configured_inventory_cap_loss_to_zero": 800,
            "configured_inventory_cap_loss_fraction": 0.40,
            "capped_recovery_days_at_p05_income": 18.0,
        },
        "blockers": ["configured inventory-cap loss exceeds 25% of capital"],
    }
    result = build_candidate_leaderboard(
        [
            CandidateEvidence("higher_income_bad_capital", gate(1900), drawdown_guard=drawdown, capital_risk=bad_capital),
            CandidateEvidence("lower_income_good_capital", gate(1300), drawdown_guard=drawdown, capital_risk=good_capital),
        ]
    )
    assert result["leader"]["name"] == "lower_income_good_capital"
    assert not result["candidates"][1]["capital_core_passed"]
    assert result["leader"]["capital_configured_cap_loss_usdc"] == 120
    assert result["policy_leaders"]["risk_first_leader"]["name"] == "lower_income_good_capital"


def test_candidate_leaderboard_exposes_and_prefers_lower_inventory_pressure() -> None:
    def gate(income: float) -> dict[str, object]:
        return {
            "status": "depth_not_ready",
            "metrics": {
                "duration_hours": 1,
                "quote_rows": 40,
                "unique_markets_quoted": 4,
                "income_p05_at_required_capture": income,
                "taker_rescue_feasible_rate": 1,
                "taker_size_weighted_rescue_fraction": 1,
                "latest_taker_residual_loss_to_zero": 0,
                "latest_taker_residual_loss_fraction": 0,
            },
            "gates": {
                "depth_ready": False,
                "income_p05_gate_passed": True,
                "clob_quality_gate_passed": True,
                "taker_rescue_rate_gate_passed": True,
                "taker_pair_edge_gate_passed": True,
                "taker_depth_gate_passed": True,
                "taker_residual_loss_gate_passed": True,
                "sample_hours_gate_passed": False,
                "quote_rows_gate_passed": True,
                "book_scenario_gate_passed": True,
            },
        }

    def drawdown(active_fraction: float) -> dict[str, object]:
        return {
            "status": "drawdown_guard_sample_pending",
            "risk_core_passed": True,
            "gates": {"sample_hours_gate_passed": False, "drawdown_guard_passed": False},
            "lp_config": {
                "quote_size_shares": 200,
                "active_capital_limit": 1200,
                "partial_rescue_max_residual_loss_usdc": 0.5,
                "max_total_unpaired": 450,
            },
            "metrics": {
                "reward_to_trading_loss_ratio": 10,
                "max_drawdown_mtm_fraction": 0.0,
                "max_open_inventory_notional": 0.0,
                "max_open_inventory_fraction": 0.0,
                "max_active_order_notional": 2000 * active_fraction,
                "max_active_order_fraction": active_fraction,
            },
            "blockers": ["needs sample"],
        }

    result = build_candidate_leaderboard(
        [
            CandidateEvidence("higher_income_higher_active", gate(1800), drawdown_guard=drawdown(0.60)),
            CandidateEvidence("lower_active", gate(1500), drawdown_guard=drawdown(0.40)),
        ]
    )
    assert result["leader"]["name"] == "lower_active"
    assert result["leader"]["configured_quote_size_shares"] == 200
    assert result["leader"]["configured_active_capital_limit"] == 1200
    assert result["leader"]["configured_residual_loss_cap_usdc"] == 0.5
    assert result["leader"]["configured_max_total_unpaired"] == 450
    assert result["leader"]["drawdown_max_active_order_fraction"] == 0.40


def test_candidate_leaderboard_reports_capture_needed_after_cap_loss() -> None:
    gate = {
        "status": "depth_not_ready",
        "config": {"target_monthly_usdc": 1000},
        "metrics": {
            "duration_hours": 1,
            "quote_rows": 40,
            "unique_markets_quoted": 4,
            "income_p05_at_required_capture": 1600,
            "required_capture_rate": 0.5,
            "taker_rescue_feasible_rate": 1,
            "taker_size_weighted_rescue_fraction": 1,
            "latest_taker_residual_loss_to_zero": 0,
            "latest_taker_residual_loss_fraction": 0,
        },
        "gates": {
            "depth_ready": False,
            "income_p05_gate_passed": True,
            "clob_quality_gate_passed": True,
            "taker_rescue_rate_gate_passed": True,
            "taker_pair_edge_gate_passed": True,
            "taker_depth_gate_passed": True,
            "taker_residual_loss_gate_passed": True,
            "sample_hours_gate_passed": False,
            "quote_rows_gate_passed": True,
            "book_scenario_gate_passed": True,
        },
    }
    capital = {
        "status": "capital_risk_stress_passed",
        "metrics": {
            "cash_reserve_fraction": 0.55,
            "unhedged_loss_fraction_of_capital": 0.30,
            "configured_inventory_cap_loss_to_zero": 300,
            "configured_inventory_cap_loss_fraction": 0.15,
            "capped_recovery_days_at_p05_income": 5.0,
        },
        "blockers": [],
    }
    result = build_candidate_leaderboard([CandidateEvidence("candidate", gate, capital_risk=capital)])
    leader = result["leader"]
    assert leader["capture_needed_for_target"] == pytest.approx(0.3125)
    assert leader["capture_needed_after_cap_loss"] == pytest.approx(0.40625)
    assert leader["after_cap_loss_income_buffer_at_required_capture"] == pytest.approx(0.3)
    assert leader["capital_after_cap_loss_target_passed"]


def test_candidate_leaderboard_does_not_promote_under_minimum_rows_over_mature_sample() -> None:
    def gate(name: str, income: float, rows: int, hours: float) -> CandidateEvidence:
        row_gate_passed = rows >= 24
        gate_doc = {
            "status": "depth_not_ready",
            "config": {"target_monthly_usdc": 1000},
            "metrics": {
                "duration_hours": hours,
                "quote_rows": rows,
                "unique_markets_quoted": 5,
                "income_p05_at_required_capture": income,
                "required_capture_rate": 0.5,
                "taker_rescue_feasible_rate": 1,
                "taker_size_weighted_rescue_fraction": 1,
                "latest_taker_residual_loss_to_zero": 0,
                "latest_taker_residual_loss_fraction": 0,
            },
            "gates": {
                "depth_ready": False,
                "income_p05_gate_passed": True,
                "clob_quality_gate_passed": True,
                "taker_rescue_rate_gate_passed": True,
                "taker_pair_edge_gate_passed": True,
                "taker_depth_gate_passed": True,
                "taker_residual_loss_gate_passed": True,
                "sample_hours_gate_passed": False,
                "quote_rows_gate_passed": row_gate_passed,
                "book_scenario_gate_passed": row_gate_passed,
                "diversification_gate_passed": True,
            },
        }
        return CandidateEvidence(name, gate_doc)

    result = build_candidate_leaderboard(
        [
            gate("immature_high_income", 1800, 20, 0.2),
            gate("mature_enough_rows", 1650, 80, 1.4),
        ]
    )
    assert result["leader"]["name"] == "mature_enough_rows"
    assert not result["candidates"][1]["quote_rows_gate_passed"]
    assert result["candidates"][0]["quote_rows_gate_passed"]


def test_candidate_leaderboard_uses_provisional_sample_hours_before_short_scout() -> None:
    def gate_doc(name: str, income: float, hours: float) -> CandidateEvidence:
        return CandidateEvidence(
            name,
            {
                "status": "depth_not_ready",
                "config": {"target_monthly_usdc": 1000, "min_observation_hours": 6.0},
                "metrics": {
                    "duration_hours": hours,
                    "quote_rows": 40,
                    "unique_markets_quoted": 5,
                    "income_p05_at_required_capture": income,
                    "required_capture_rate": 0.5,
                    "taker_rescue_feasible_rate": 1,
                    "taker_size_weighted_rescue_fraction": 1,
                    "latest_taker_residual_loss_to_zero": 0,
                    "latest_taker_residual_loss_fraction": 0,
                },
                "gates": {
                    "depth_ready": False,
                    "income_p05_gate_passed": True,
                    "clob_quality_gate_passed": True,
                    "taker_rescue_rate_gate_passed": True,
                    "taker_pair_edge_gate_passed": True,
                    "taker_depth_gate_passed": True,
                    "taker_residual_loss_gate_passed": True,
                    "sample_hours_gate_passed": False,
                    "quote_rows_gate_passed": True,
                    "book_scenario_gate_passed": True,
                    "diversification_gate_passed": True,
                },
            },
        )

    result = build_candidate_leaderboard(
        [
            gate_doc("short_high_income_scout", 1800, 0.5),
            gate_doc("provisional_mature_leader", 1650, 1.6),
        ]
    )
    assert result["leader"]["name"] == "provisional_mature_leader"
    assert result["leader"]["provisional_sample_hours_gate_passed"]
    assert not result["candidates"][1]["provisional_sample_hours_gate_passed"]


def test_refresh_candidate_leaderboard_parses_named_paths_safely() -> None:
    assert _split_named_path("q300=C:/tmp/bg.json", "--candidate") == ("q300", "C:/tmp/bg.json")
    assert _safe_name("q300/cap10 d0.06") == "q300_cap10_d0.06"
    with pytest.raises(SystemExit):
        _split_named_path("missing_equals", "--candidate")


def test_refresh_candidate_leaderboard_pending_candidate_is_non_promotable() -> None:
    gate = _pending_gate("snapshot missing")
    drawdown = _pending_drawdown("snapshot missing")
    capital = _pending_capital("snapshot missing")
    result = build_candidate_leaderboard([CandidateEvidence("pending", gate, drawdown_guard=drawdown, capital_risk=capital)])
    assert result["status"] == "no_public_paper_candidate_ready"
    assert result["leader"]["name"] == "pending"
    assert not result["leader"]["income_gate_passed"]
    assert not result["leader"]["drawdown_core_passed"]
    assert not result["leader"]["capital_core_passed"]


def test_refresh_candidate_leaderboard_freshness_gate_is_opt_in(tmp_path) -> None:
    snapshot = tmp_path / "snapshots.csv"
    quotes = tmp_path / "quotes.csv"
    snapshot.write_text("timestamp\n", encoding="utf-8")
    quotes.write_text("timestamp\n", encoding="utf-8")
    manifest = {"snapshot": str(snapshot), "quotes": str(quotes)}
    now = max(snapshot.stat().st_mtime, quotes.stat().st_mtime) + 120

    metrics = _input_freshness_metrics(manifest, now=now)
    assert metrics["snapshot"]["exists"]
    assert metrics["quotes"]["exists"]
    assert metrics["max_age_seconds"] == pytest.approx(120, abs=0.01)
    assert _input_staleness_error(manifest, 0.0, now=now) == ""
    assert _input_staleness_error(manifest, 300.0, now=now) == ""
    message = _input_staleness_error(manifest, 60.0, now=now)
    assert message.startswith("input freshness gate failed")
    assert "snapshot age" in message
    assert "quotes age" in message

    result = build_candidate_leaderboard([CandidateEvidence("stale", _pending_gate(message))])
    assert result["status"] == "no_public_paper_candidate_ready"
    assert not result["leader"]["income_gate_passed"]


def test_candidate_leaderboard_exposes_input_freshness_metadata() -> None:
    result = build_candidate_leaderboard(
        [
            CandidateEvidence(
                "fresh",
                {
                    "status": "depth_not_ready",
                    "metrics": {"income_p05_at_required_capture": 1200, "required_capture_rate": 0.5},
                    "gates": {},
                },
                metadata={
                    "_input_freshness": {
                        "snapshot": {"age_seconds": 11},
                        "quotes": {"age_seconds": 22},
                        "max_age_seconds": 22,
                    },
                    "_max_input_staleness_seconds": 1800,
                },
            )
        ]
    )
    leader = result["leader"]
    assert leader["input_snapshot_age_seconds"] == 11
    assert leader["input_quotes_age_seconds"] == 22
    assert leader["input_max_age_seconds"] == 22
    assert leader["input_freshness_gate_seconds"] == 1800


def test_partial_rescue_grid_selection_requires_drawdown_core() -> None:
    assert (
        partial_rescue_grid._selection_status(
            {"depth_ready": False, "risk_income_gate_passed": True, "drawdown_core_passed": True}
        )
        == "selected_risk_income_drawdown_passed_sample_not_ready"
    )
    assert (
        partial_rescue_grid._selection_status(
            {"depth_ready": False, "risk_income_gate_passed": True, "drawdown_core_passed": False}
        )
        == "selected_risk_income_passed_drawdown_failed"
    )


def test_launch_live_paper_candidate_generates_parameterized_public_scripts(tmp_path) -> None:
    cfg = LaunchCandidateConfig(
        name="q200 cap5 d006",
        state_dir=str(tmp_path),
        repo="C:/repo/Polymarket-LP",
        python="C:/repo/Polymarket-LP/.venv/Scripts/python.exe",
        quote_size=200,
        partial_rescue_max_residual_loss_usdc=5,
        min_reward_density_per_day=0.06,
        active_capital_limit=1200,
        max_unpaired_per_market=40,
        max_total_unpaired=300,
        max_cluster_unpaired=160,
        max_unpaired_minutes=20,
        iterations=2,
        extension_iterations=3,
        interval_seconds=60,
    )
    manifest = write_launch_artifacts(cfg, run_id="unit_run", start=False)
    assert manifest["pid"] == 0
    assert manifest["quote_size"] == 200
    assert manifest["partial_rescue_max_residual_loss_usdc"] == 5
    assert manifest["max_unpaired_per_market"] == 40
    assert manifest["lp_config"]["max_total_unpaired"] == 300
    assert manifest["safety"].startswith("public CLOB/Gamma reads only")
    collector = tmp_path.joinpath("unit_run", "run_q200_cap5_d006_collector.ps1").read_text(encoding="utf-8")
    watcher = tmp_path.joinpath("unit_run", "run_q200_cap5_d006_watcher.ps1").read_text(encoding="utf-8")
    extend = tmp_path.joinpath("unit_run", "run_q200_cap5_d006_extend_to24h_and_audit.ps1").read_text(encoding="utf-8")
    assert "--quote-size 200" in collector
    assert "--partial-rescue-max-residual-loss-usdc 5" in collector
    assert "--max-unpaired-per-market 40" in collector
    assert "--max-total-unpaired 300" in collector
    assert "--max-cluster-unpaired 160" in collector
    assert "--max-unpaired-minutes 20" in collector
    assert "--min-reward-density-per-day 0.06" in collector
    assert "--min-observation-hours 6" in watcher
    assert "--min-unique-markets 4" in watcher
    assert "--min-observation-hours 24" in extend
    assert "private" not in collector.lower()


def test_lp_config_from_manifest_maps_candidate_risk_parameters() -> None:
    cfg = lp_config_from_manifest(
        {
            "quote_size": 200,
            "partial_rescue_max_residual_loss_usdc": 1,
            "min_reward_density_per_day": 0.06,
            "active_capital_limit": 1200,
            "max_unpaired_per_market": 40,
            "max_total_unpaired": 300,
            "max_cluster_unpaired": 160,
            "max_unpaired_minutes": 20,
            "quote_offset": 0.02,
            "max_recent_vol": 0.006,
            "max_recent_jump": 0.025,
            "vol_quote_multiplier": 0.5,
        }
    )
    assert cfg.quote_size_shares == 200
    assert cfg.partial_rescue_max_residual_loss_usdc == 1
    assert cfg.min_reward_density_per_day == 0.06
    assert cfg.active_capital_limit == 1200
    assert cfg.max_unpaired_per_market == 40
    assert cfg.max_total_unpaired == 300
    assert cfg.max_cluster_unpaired == 160
    assert cfg.max_unpaired_minutes == 20


def test_paper_replay_config_exposes_inventory_caps() -> None:
    cfg = make_lp_config(
        argparse.Namespace(
            initial_capital=2000,
            quote_size=200,
            quote_offset=0.02,
            safety_margin=0.015,
            max_unpaired_per_market=35,
            max_total_unpaired=250,
            max_cluster_unpaired=140,
            max_unpaired_minutes=18,
            active_capital_limit=1200,
            min_reward_daily=0.0,
            max_market_competitiveness=1.0,
            allowed_categories="",
            excluded_categories="sports,crypto",
            min_reward_density_per_day=0.06,
            recent_vol_window=6,
            max_recent_vol=0.006,
            max_recent_jump=0.025,
            vol_quote_multiplier=0.5,
            depth_cap_quote_size=False,
            depth_quote_size_fraction=1.0,
            min_depth_capped_quote_size=1.0,
            partial_rescue_max_residual_loss_usdc=0.5,
            risk_governor_json="",
        )
    )
    assert cfg.max_unpaired_per_market == 35
    assert cfg.max_total_unpaired == 250
    assert cfg.max_cluster_unpaired == 140
    assert cfg.max_unpaired_minutes == 18


def test_capital_risk_config_from_manifest_accepts_top_level_inventory_caps() -> None:
    cfg = config_from_lp_manifest(
        {
            "initial_capital": 2000,
            "max_unpaired_per_market": 35,
            "max_total_unpaired": 250,
            "max_cluster_unpaired": 140,
        },
        CapitalRiskStressConfig(),
    )
    assert cfg.max_unpaired_per_market == 35
    assert cfg.max_total_unpaired == 250
    assert cfg.max_cluster_unpaired == 140


def test_lp_config_from_manifest_infers_legacy_candidate_text() -> None:
    cfg = lp_config_from_manifest({"_candidate_name": "q300_cap50_d010_baseline", "strategy": "q300safe_partial_rescue_cap50"})
    assert cfg.quote_size_shares == 300
    assert cfg.partial_rescue_max_residual_loss_usdc == 50
    assert cfg.min_reward_density_per_day == 0.10


def test_drawdown_guard_separates_short_sample_from_risk_failure() -> None:
    snapshots = make_synthetic_snapshots(seed=5, days=1, n_markets=6)
    lp_cfg = LPConfig(
        quote_size_shares=25,
        active_capital_limit=500,
        excluded_categories="sports,crypto",
        min_reward_density_per_day=0.0,
    )
    result = evaluate_drawdown_guard(
        snapshots,
        lp_cfg,
        DrawdownGuardConfig(
            min_observation_hours=10_000,
            max_mtm_drawdown_fraction=1.0,
            max_realized_drawdown_fraction=1.0,
            max_open_inventory_fraction=1.0,
            max_active_order_fraction=1.0,
            min_reward_to_trading_loss_ratio=0.0,
        ),
    )
    assert result["status"] == "drawdown_guard_sample_pending"
    assert result["risk_core_passed"]
    assert not result["gates"]["sample_hours_gate_passed"]
    assert "max_drawdown_mtm_fraction" in result["metrics"]


def test_drawdown_guard_blocks_oversized_active_orders() -> None:
    snapshots = make_synthetic_snapshots(seed=6, days=1, n_markets=6)
    result = evaluate_drawdown_guard(
        snapshots,
        LPConfig(quote_size_shares=100, active_capital_limit=1200, min_reward_density_per_day=0.0),
        DrawdownGuardConfig(
            min_observation_hours=0,
            max_mtm_drawdown_fraction=1.0,
            max_realized_drawdown_fraction=1.0,
            max_open_inventory_fraction=1.0,
            max_active_order_fraction=0.001,
            min_reward_to_trading_loss_ratio=0.0,
        ),
    )
    assert result["status"] == "drawdown_guard_failed"
    assert not result["gates"]["active_order_gate_passed"]
