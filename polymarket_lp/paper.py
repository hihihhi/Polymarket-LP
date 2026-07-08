from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from .lp_backtest import (
    LPConfig,
    filter_snapshots_for_strategy,
    load_snapshots,
    quote_for_row,
    quote_one_sided_loss_to_zero,
    quote_risk_caps_allow,
)


JsonGetter = Callable[[str, dict[str, Any] | None, float], Any]

PAPER_QUOTE_COLUMNS = [
    "timestamp",
    "condition_id",
    "market_id",
    "question",
    "category",
    "cluster",
    "side",
    "bid_price",
    "size_shares",
    "pair_cost",
    "quote_offset",
    "reward_share_est",
    "reward_density_per_day",
    "active_order_notional_pair",
    "recent_vol",
    "recent_jump",
    "quote_data_quality",
    "yes_best_bid",
    "yes_best_ask",
    "no_best_bid",
    "no_best_ask",
    "yes_best_bid_size",
    "yes_best_ask_size",
    "no_best_bid_size",
    "no_best_ask_size",
]


@dataclass(slots=True)
class LiveSnapshotConfig:
    gamma_base_url: str = "https://gamma-api.polymarket.com"
    clob_base_url: str = "https://clob.polymarket.com"
    event_limit: int = 500
    max_events: int | None = None
    order: str = "volume"
    ascending: bool = False
    include_clob_books: bool = False
    request_timeout_seconds: float = 20.0
    sleep_between_book_requests_seconds: float = 0.0


@dataclass(slots=True)
class PaperAnalysisConfig:
    max_stale_seconds: float = 900.0
    stale_mid_change: float = 0.03
    fill_mid_cross_buffer: float = 0.0
    max_reward_gap_seconds: float = 300.0


PAPER_ANALYSIS_COLUMNS = [
    "timestamp",
    "condition_id",
    "side",
    "bid_price",
    "size_shares",
    "next_ts",
    "next_gap_seconds",
    "next_mid",
    "mid_change_to_next",
    "would_fill",
    "pending_quote",
    "stale_quote",
    "stale_fill",
    "mark_to_next_pnl_if_filled",
    "same_interval_pair_complete",
]


def collect_live_reward_snapshots(
    cfg: LiveSnapshotConfig | None = None,
    *,
    now: pd.Timestamp | None = None,
    get_json: JsonGetter | None = None,
) -> pd.DataFrame:
    """Collect one point-in-time reward-market snapshot from public endpoints.

    This only reads public market/reward/orderbook data. It does not place,
    sign, cancel, or submit orders.
    """

    cfg = cfg or LiveSnapshotConfig()
    getter = get_json or _get_json
    timestamp = now or pd.Timestamp.now(tz="UTC")
    events = getter(
        f"{cfg.gamma_base_url.rstrip('/')}/events",
        {
            "active": True,
            "closed": False,
            "limit": cfg.event_limit,
            "order": cfg.order,
            "ascending": cfg.ascending,
        },
        cfg.request_timeout_seconds,
    )
    if isinstance(events, dict):
        events = events.get("events") or events.get("data") or []
    if cfg.max_events is not None:
        events = list(events)[: cfg.max_events]

    rows: list[dict[str, Any]] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        category, tag_text = _event_category(event)
        for market in event.get("markets") or []:
            row = _market_snapshot_row(
                event=event,
                market=market,
                category=category,
                tag_text=tag_text,
                timestamp=timestamp,
                cfg=cfg,
                get_json=getter,
            )
            if row is not None:
                rows.append(row)
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    return frame.sort_values(["reward_daily", "liquidity_clob"], ascending=[False, False]).reset_index(drop=True)


def build_paper_quotes(snapshots: pd.DataFrame, cfg: LPConfig) -> pd.DataFrame:
    """Generate quote-intent rows from point-in-time snapshots.

    The result is a paper log only: it records what would be quoted after risk,
    reward-density and capital-budget filters.
    """

    if snapshots.empty:
        return pd.DataFrame(columns=PAPER_QUOTE_COLUMNS)
    rows = add_risk_features(filter_snapshots_for_strategy(snapshots, cfg), cfg)
    if rows.empty:
        return pd.DataFrame(columns=PAPER_QUOTE_COLUMNS)
    out_rows: list[dict[str, Any]] = []
    for ts, group in rows.groupby("timestamp", sort=True):
        candidates: list[tuple[float, pd.Series, dict[str, float | bool]]] = []
        for _, row in group.iterrows():
            if float(row.get("recent_vol", 0.0)) > cfg.max_recent_vol:
                continue
            if float(row.get("recent_jump", 0.0)) > cfg.max_recent_jump:
                continue
            adaptive_offset = cfg.quote_offset + cfg.vol_quote_multiplier * float(row.get("recent_vol", 0.0))
            quote = quote_for_row(row, cfg, quote_offset=adaptive_offset)
            if not quote["eligible"]:
                continue
            if float(quote["reward_density_per_day"]) < cfg.min_reward_density_per_day:
                continue
            candidates.append((float(quote["reward_density_per_day"]), row, quote))
        candidates.sort(key=lambda item: item[0], reverse=True)

        active_notional = 0.0
        active_one_sided_risk = 0.0
        active_market_risk: dict[str, float] = {}
        active_cluster_risk: dict[str, float] = {}
        for _, row, quote in candidates:
            notional = float(quote["active_order_notional"])
            if active_notional + notional > cfg.active_capital_limit:
                continue
            condition_id = str(row["condition_id"])
            cluster = str(row.get("cluster", "unknown"))
            one_sided_risk = quote_one_sided_loss_to_zero(quote)
            if not quote_risk_caps_allow(
                cfg,
                quote,
                market_risk=active_market_risk.get(condition_id, 0.0),
                total_risk=active_one_sided_risk,
                cluster_risk=active_cluster_risk.get(cluster, 0.0),
            ):
                continue
            active_notional += notional
            active_one_sided_risk += one_sided_risk
            active_market_risk[condition_id] = active_market_risk.get(condition_id, 0.0) + one_sided_risk
            active_cluster_risk[cluster] = active_cluster_risk.get(cluster, 0.0) + one_sided_risk
            for side, bid_col in [("YES", "yes_bid"), ("NO", "no_bid")]:
                out_rows.append(
                    {
                        "timestamp": ts,
                        "condition_id": row["condition_id"],
                        "market_id": row.get("market_id", ""),
                        "question": row.get("question", ""),
                        "category": row.get("category", ""),
                        "cluster": row.get("cluster", "unknown"),
                        "side": side,
                        "bid_price": quote[bid_col],
                        "size_shares": quote["quote_size"],
                        "pair_cost": quote["pair_cost"],
                        "quote_offset": quote["quote_offset"],
                        "reward_share_est": quote["reward_share"],
                        "reward_density_per_day": quote["reward_density_per_day"],
                        "active_order_notional_pair": notional,
                        "recent_vol": row.get("recent_vol", 0.0),
                        "recent_jump": row.get("recent_jump", 0.0),
                        "quote_data_quality": row.get("quote_data_quality", ""),
                        "yes_best_bid": row.get("yes_best_bid", float("nan")),
                        "yes_best_ask": row.get("yes_best_ask", float("nan")),
                        "no_best_bid": row.get("no_best_bid", float("nan")),
                        "no_best_ask": row.get("no_best_ask", float("nan")),
                        "yes_best_bid_size": row.get("yes_best_bid_size", float("nan")),
                        "yes_best_ask_size": row.get("yes_best_ask_size", float("nan")),
                        "no_best_bid_size": row.get("no_best_bid_size", float("nan")),
                        "no_best_ask_size": row.get("no_best_ask_size", float("nan")),
                    }
                )
    return pd.DataFrame(out_rows, columns=PAPER_QUOTE_COLUMNS)


def analyze_paper_quotes(
    snapshots: pd.DataFrame,
    quotes: pd.DataFrame,
    cfg: PaperAnalysisConfig | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Replay paper quote intents against the next point-in-time snapshot.

    This is not a live fill ledger. It is a conservative feasibility diagnostic:
    a quote is marked as a would-fill only if the next observed midpoint crosses
    through the paper bid. All outcome fields are computed using data after the
    paper timestamp, never before it.
    """

    cfg = cfg or PaperAnalysisConfig()
    if snapshots.empty or quotes.empty:
        per_quote = pd.DataFrame(columns=PAPER_ANALYSIS_COLUMNS)
        return per_quote, _paper_summary(snapshots, quotes, per_quote, cfg)

    snap = _snapshot_next_features(snapshots)
    quote = _normalise_quotes(quotes)
    if snap.empty or quote.empty:
        per_quote = pd.DataFrame(columns=PAPER_ANALYSIS_COLUMNS)
        return per_quote, _paper_summary(snapshots, quotes, per_quote, cfg)

    merged = quote.merge(
        snap,
        on=["timestamp", "condition_id"],
        how="left",
        suffixes=("", "_snapshot"),
    )
    side = merged["side"].astype(str).str.upper()
    next_mid = merged["next_yes_mid"].where(side.eq("YES"), merged["next_no_mid"])
    current_mid = merged["yes_mid"].where(side.eq("YES"), merged["no_mid"])
    bid = pd.to_numeric(merged["bid_price"], errors="coerce")
    size = pd.to_numeric(merged["size_shares"], errors="coerce").fillna(0.0)
    next_gap = pd.to_numeric(merged["next_gap_seconds"], errors="coerce")
    mid_change = (next_mid - current_mid).abs()

    would_fill = next_mid.notna() & bid.notna() & (next_mid <= bid - cfg.fill_mid_cross_buffer)
    pending_quote = next_gap.isna()
    stale_quote = next_gap.notna() & (next_gap > cfg.max_stale_seconds)
    stale_fill = would_fill & (stale_quote | (mid_change >= cfg.stale_mid_change))
    mark_pnl = (next_mid - bid) * size
    mark_pnl = mark_pnl.where(would_fill, 0.0).fillna(0.0)

    per_quote = merged[
        [
            "timestamp",
            "condition_id",
            "side",
            "bid_price",
            "size_shares",
            "next_ts",
            "next_gap_seconds",
        ]
    ].copy()
    per_quote["next_mid"] = next_mid
    per_quote["mid_change_to_next"] = mid_change
    per_quote["would_fill"] = would_fill
    per_quote["pending_quote"] = pending_quote
    per_quote["stale_quote"] = stale_quote
    per_quote["stale_fill"] = stale_fill
    per_quote["mark_to_next_pnl_if_filled"] = mark_pnl
    pair_fill_count = per_quote.groupby(["timestamp", "condition_id"])["would_fill"].transform("sum")
    per_quote["same_interval_pair_complete"] = per_quote["would_fill"] & pair_fill_count.ge(2)
    per_quote = per_quote[PAPER_ANALYSIS_COLUMNS]
    return per_quote, _paper_summary(snapshots, quote, per_quote, cfg, snap)


def run_paper_analysis_to_files(
    *,
    snapshots: pd.DataFrame,
    quotes: pd.DataFrame,
    out_dir: str | Path,
    cfg: PaperAnalysisConfig | None = None,
) -> dict[str, Any]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    per_quote, summary = analyze_paper_quotes(snapshots, quotes, cfg)
    per_quote.to_csv(out / "paper_quote_outcomes.csv", index=False)
    (out / "paper_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return summary


def add_risk_features(df: pd.DataFrame, cfg: LPConfig) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    rows = df.copy().sort_values(["condition_id", "timestamp"]).reset_index(drop=True)
    if rows.empty:
        return rows
    rows["mid_change"] = rows.groupby("condition_id")["yes_mid"].diff().abs().fillna(0.0)
    rows["recent_vol"] = rows.groupby("condition_id")["mid_change"].transform(
        lambda s: s.rolling(cfg.recent_vol_window, min_periods=1).mean()
    )
    rows["recent_jump"] = rows.groupby("condition_id")["mid_change"].transform(
        lambda s: s.rolling(cfg.recent_vol_window, min_periods=1).max()
    )
    return rows


def _snapshot_next_features(snapshots: pd.DataFrame) -> pd.DataFrame:
    snap = snapshots.copy()
    if "timestamp" not in snap or "condition_id" not in snap:
        return pd.DataFrame()
    snap["timestamp"] = pd.to_datetime(snap["timestamp"], utc=True, errors="coerce")
    snap["condition_id"] = snap["condition_id"].astype(str)
    for col in ["yes_mid", "no_mid", "reward_daily", "max_incentive_spread", "market_competitiveness"]:
        if col in snap:
            snap[col] = pd.to_numeric(snap[col], errors="coerce")
    if "yes_mid" not in snap:
        if {"yes_best_bid", "yes_best_ask"}.issubset(snap.columns):
            snap["yes_mid"] = (pd.to_numeric(snap["yes_best_bid"], errors="coerce") + pd.to_numeric(snap["yes_best_ask"], errors="coerce")) / 2
        else:
            return pd.DataFrame()
    if "no_mid" not in snap:
        if {"no_best_bid", "no_best_ask"}.issubset(snap.columns):
            snap["no_mid"] = (pd.to_numeric(snap["no_best_bid"], errors="coerce") + pd.to_numeric(snap["no_best_ask"], errors="coerce")) / 2
        else:
            snap["no_mid"] = 1 - snap["yes_mid"]
    if "cluster" not in snap:
        snap["cluster"] = snap["category"] if "category" in snap else "unknown"
    keep = [
        col
        for col in [
            "timestamp",
            "condition_id",
            "yes_mid",
            "no_mid",
            "reward_daily",
            "max_incentive_spread",
            "market_competitiveness",
            "cluster",
            "quote_data_quality",
        ]
        if col in snap
    ]
    snap = snap[keep].dropna(subset=["timestamp", "condition_id", "yes_mid", "no_mid"])
    snap = snap.sort_values(["condition_id", "timestamp"]).drop_duplicates(["timestamp", "condition_id"], keep="last")
    grouped = snap.groupby("condition_id", sort=False)
    snap["next_ts"] = grouped["timestamp"].shift(-1)
    snap["next_yes_mid"] = grouped["yes_mid"].shift(-1)
    snap["next_no_mid"] = grouped["no_mid"].shift(-1)
    snap["next_gap_seconds"] = (snap["next_ts"] - snap["timestamp"]).dt.total_seconds()
    return snap.reset_index(drop=True)


def _normalise_quotes(quotes: pd.DataFrame) -> pd.DataFrame:
    quote = quotes.copy()
    if quote.empty or not {"timestamp", "condition_id", "side", "bid_price"}.issubset(quote.columns):
        return pd.DataFrame()
    quote["timestamp"] = pd.to_datetime(quote["timestamp"], utc=True, errors="coerce")
    quote["condition_id"] = quote["condition_id"].astype(str)
    quote["side"] = quote["side"].astype(str).str.upper()
    for col in [
        "bid_price",
        "size_shares",
        "pair_cost",
        "reward_share_est",
        "reward_density_per_day",
        "active_order_notional_pair",
    ]:
        if col in quote:
            quote[col] = pd.to_numeric(quote[col], errors="coerce")
    return quote.dropna(subset=["timestamp", "condition_id", "side", "bid_price"]).reset_index(drop=True)


def _paper_summary(
    snapshots: pd.DataFrame,
    quotes: pd.DataFrame,
    per_quote: pd.DataFrame,
    cfg: PaperAnalysisConfig,
    snap_features: pd.DataFrame | None = None,
) -> dict[str, Any]:
    snap_ts = pd.to_datetime(snapshots["timestamp"], utc=True, errors="coerce") if "timestamp" in snapshots else pd.Series(dtype="datetime64[ns, UTC]")
    quote = _normalise_quotes(quotes)
    quote_pairs = quote.drop_duplicates(["timestamp", "condition_id"]) if not quote.empty else quote
    pair_with_next = None
    if snap_features is not None and not quote_pairs.empty:
        pair_with_next = quote_pairs.merge(
            snap_features[["timestamp", "condition_id", "next_gap_seconds"]],
            on=["timestamp", "condition_id"],
            how="left",
        )
    active_by_ts = (
        quote_pairs.groupby("timestamp")["active_order_notional_pair"].sum()
        if not quote_pairs.empty and "active_order_notional_pair" in quote_pairs
        else pd.Series(dtype=float)
    )
    reward_accrual = 0.0
    if pair_with_next is not None and {"reward_density_per_day", "active_order_notional_pair", "next_gap_seconds"}.issubset(pair_with_next.columns):
        gap = pd.to_numeric(pair_with_next["next_gap_seconds"], errors="coerce").clip(lower=0, upper=cfg.max_reward_gap_seconds).fillna(0)
        reward_per_day = pd.to_numeric(pair_with_next["reward_density_per_day"], errors="coerce").fillna(0) * pd.to_numeric(pair_with_next["active_order_notional_pair"], errors="coerce").fillna(0)
        reward_accrual = float((reward_per_day * gap / 86400).sum())
    quality_counts = _value_counts(snapshots.get("quote_data_quality", pd.Series(dtype=str)))
    cluster_loss = {}
    if not per_quote.empty and "cluster" in quote:
        tmp = per_quote.merge(quote[["timestamp", "condition_id", "side", "cluster"]], on=["timestamp", "condition_id", "side"], how="left")
        pnl = pd.to_numeric(tmp["mark_to_next_pnl_if_filled"], errors="coerce").fillna(0)
        tmp = tmp.assign(mark_loss=(-pnl.clip(upper=0)))
        cluster_loss = {str(k): float(v) for k, v in tmp.groupby("cluster")["mark_loss"].sum().sort_values(ascending=False).items()}
    fill_rows = int(per_quote["would_fill"].sum()) if "would_fill" in per_quote else 0
    stale_fill_rows = int(per_quote["stale_fill"].sum()) if "stale_fill" in per_quote else 0
    pair_complete_rows = int(per_quote["same_interval_pair_complete"].sum()) if "same_interval_pair_complete" in per_quote else 0
    pending_quote_rate = 0.0
    raw_pending_quote_rate = 0.0
    right_censored_pending_rows = 0
    evaluable_quote_rows = int(len(per_quote))
    evaluable_pending_rows = 0
    if "pending_quote" in per_quote and len(per_quote):
        pending = per_quote["pending_quote"].fillna(False).astype(bool)
        raw_pending_quote_rate = float(pending.mean())
        right_censored = pd.Series(False, index=per_quote.index)
        if "timestamp" in per_quote:
            quote_ts = pd.to_datetime(per_quote["timestamp"], utc=True, errors="coerce")
            latest_ts = quote_ts.max()
            if pd.notna(latest_ts):
                right_censored = pending & quote_ts.eq(latest_ts)
        right_censored_pending_rows = int(right_censored.sum())
        evaluable = ~right_censored
        evaluable_quote_rows = int(evaluable.sum())
        evaluable_pending_rows = int((pending & evaluable).sum())
        pending_quote_rate = (
            float(evaluable_pending_rows / evaluable_quote_rows)
            if evaluable_quote_rows > 0
            else raw_pending_quote_rate
        )
    return {
        "snapshot_rows": int(len(snapshots)),
        "snapshot_intervals": int(snap_ts.nunique()) if len(snap_ts) else 0,
        "quote_rows": int(len(quote)),
        "quote_pair_intervals": int(len(quote_pairs)),
        "unique_markets_quoted": int(quote["condition_id"].nunique()) if not quote.empty else 0,
        "start_utc": str(snap_ts.min()) if len(snap_ts.dropna()) else None,
        "end_utc": str(snap_ts.max()) if len(snap_ts.dropna()) else None,
        "duration_hours": _duration_hours(snap_ts),
        "median_snapshot_gap_seconds": _median_gap_seconds(snap_ts),
        "latest_active_pair_notional": float(active_by_ts.iloc[-1]) if len(active_by_ts) else 0.0,
        "max_active_pair_notional": float(active_by_ts.max()) if len(active_by_ts) else 0.0,
        "avg_active_pair_notional": float(active_by_ts.mean()) if len(active_by_ts) else 0.0,
        "avg_reward_density_per_day": _mean_col(quote_pairs, "reward_density_per_day"),
        "estimated_reward_accrual_usdc": reward_accrual,
        "would_fill_rows": fill_rows,
        "fill_proxy_rate": fill_rows / max(len(per_quote), 1),
        "same_interval_pair_complete_rows": pair_complete_rows,
        "same_interval_pair_completion_rate": pair_complete_rows / max(fill_rows, 1),
        "pending_quote_rate": pending_quote_rate,
        "raw_pending_quote_rate": raw_pending_quote_rate,
        "evaluable_quote_rows": evaluable_quote_rows,
        "evaluable_pending_quote_rows": evaluable_pending_rows,
        "right_censored_pending_quote_rows": right_censored_pending_rows,
        "right_censored_pending_quote_rate": right_censored_pending_rows / max(len(per_quote), 1),
        "stale_quote_rate": float(per_quote["stale_quote"].mean()) if "stale_quote" in per_quote and len(per_quote) else 0.0,
        "stale_fill_rows": stale_fill_rows,
        "stale_fill_rate": stale_fill_rows / max(fill_rows, 1),
        "estimated_mark_to_next_pnl_if_all_fills_usdc": float(per_quote["mark_to_next_pnl_if_filled"].sum()) if "mark_to_next_pnl_if_filled" in per_quote else 0.0,
        "mean_abs_mid_change_to_next": _mean_col(per_quote, "mid_change_to_next"),
        "max_abs_mid_change_to_next": _max_col(per_quote, "mid_change_to_next"),
        "quote_data_quality_counts": quality_counts,
        "cluster_mark_loss_if_filled": cluster_loss,
        "analysis_config": asdict(cfg),
        "safety": "paper analytics only; fill outcomes are midpoint-cross proxies, not executed order fills",
    }


def _duration_hours(ts: pd.Series) -> float:
    clean = ts.dropna()
    if clean.empty:
        return 0.0
    return float((clean.max() - clean.min()).total_seconds() / 3600)


def _median_gap_seconds(ts: pd.Series) -> float:
    clean = pd.Series(ts.dropna().sort_values().unique())
    if len(clean) < 2:
        return 0.0
    return float(clean.diff().dt.total_seconds().dropna().median())


def _mean_col(frame: pd.DataFrame, col: str) -> float:
    if col not in frame or frame.empty:
        return 0.0
    values = pd.to_numeric(frame[col], errors="coerce").dropna()
    return float(values.mean()) if len(values) else 0.0


def _max_col(frame: pd.DataFrame, col: str) -> float:
    if col not in frame or frame.empty:
        return 0.0
    values = pd.to_numeric(frame[col], errors="coerce").dropna()
    return float(values.max()) if len(values) else 0.0


def _value_counts(values: pd.Series) -> dict[str, int]:
    if values.empty:
        return {}
    return {str(k): int(v) for k, v in values.fillna("unknown").astype(str).value_counts().items()}


def run_live_paper_loop(
    *,
    snapshot_path: str | Path,
    quotes_path: str | Path,
    manifest_path: str | Path,
    lp_config: LPConfig,
    snapshot_config: LiveSnapshotConfig | None = None,
    iterations: int = 1,
    interval_seconds: float = 300.0,
    get_json: JsonGetter | None = None,
) -> dict[str, Any]:
    if iterations < 1:
        raise ValueError("iterations must be >= 1")
    snapshot_path = Path(snapshot_path)
    quotes_path = Path(quotes_path)
    manifest_path = Path(manifest_path)
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    quotes_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {}
    with _exclusive_output_lock(manifest_path.with_name(f"{manifest_path.name}.lock")):
        for i in range(iterations):
            batch = collect_live_reward_snapshots(snapshot_config, get_json=get_json)
            _append_csv(snapshot_path, batch)
            all_snapshots = load_snapshots(snapshot_path) if snapshot_path.exists() and snapshot_path.stat().st_size else batch
            quotes = build_paper_quotes(all_snapshots, lp_config)
            _atomic_to_csv(quotes, quotes_path)
            manifest = {
                "iterations_completed": i + 1,
                "last_batch_snapshots": int(len(batch)),
                "total_snapshots": int(len(all_snapshots)),
                "paper_quote_rows": int(len(quotes)),
                "unique_markets_quoted": int(quotes["condition_id"].nunique()) if not quotes.empty else 0,
                "active_pair_notional_latest_snapshot": _latest_active_pair_notional(quotes),
                "lp_config": asdict(lp_config),
                "snapshot_config": asdict(snapshot_config or LiveSnapshotConfig()),
                "safety": "paper only; no private keys, order signing, order submission, or cancellation",
                "outputs": {
                    "snapshots": str(snapshot_path),
                    "quotes": str(quotes_path),
                    "manifest": str(manifest_path),
                },
            }
            _atomic_write_text(manifest_path, json.dumps(manifest, indent=2, default=str))
            if i + 1 < iterations and interval_seconds > 0:
                time.sleep(interval_seconds)
    return manifest


def _market_snapshot_row(
    *,
    event: dict[str, Any],
    market: dict[str, Any],
    category: str,
    tag_text: str,
    timestamp: pd.Timestamp,
    cfg: LiveSnapshotConfig,
    get_json: JsonGetter,
) -> dict[str, Any] | None:
    if not isinstance(market, dict):
        return None
    reward_daily = sum(float(item.get("rewardsDailyRate") or 0.0) for item in market.get("clobRewards") or [])
    max_spread = _to_float(market.get("rewardsMaxSpread"))
    if max_spread > 1.0:
        max_spread /= 100.0
    min_size = _to_float(market.get("rewardsMinSize") or market.get("orderMinSize"))
    if reward_daily <= 0 or max_spread <= 0 or min_size <= 0:
        return None
    if not bool(market.get("acceptingOrders")) or not bool(market.get("enableOrderBook")):
        return None

    token_ids = [str(token) for token in _parse_jsonish(market.get("clobTokenIds"), [])]
    yes_bid = _to_float(market.get("bestBid"), default=float("nan"))
    yes_ask = _to_float(market.get("bestAsk"), default=float("nan"))
    no_bid = no_ask = float("nan")
    yes_bid_size = yes_ask_size = no_bid_size = no_ask_size = float("nan")
    quality = "gamma_best_bid_ask"

    if cfg.include_clob_books and len(token_ids) >= 2:
        yes_bid, yes_ask, yes_bid_size, yes_ask_size = _book_bid_ask(
            cfg=cfg, token_id=token_ids[0], get_json=get_json, fallback_bid=yes_bid, fallback_ask=yes_ask
        )
        if cfg.sleep_between_book_requests_seconds:
            time.sleep(cfg.sleep_between_book_requests_seconds)
        no_bid, no_ask, no_bid_size, no_ask_size = _book_bid_ask(
            cfg=cfg, token_id=token_ids[1], get_json=get_json, fallback_bid=float("nan"), fallback_ask=float("nan")
        )
        quality = "clob_book_both_sides" if pd.notna(no_bid) and pd.notna(no_ask) else "clob_book_yes_only"

    yes_mid = _midpoint(yes_bid, yes_ask, _to_float(market.get("lastTradePrice"), default=float("nan")))
    if pd.isna(yes_mid):
        return None
    no_mid = _midpoint(no_bid, no_ask, float("nan"))
    if pd.isna(no_mid):
        no_mid = 1.0 - yes_mid

    spread = _to_float(market.get("spread"), default=max_spread)
    return {
        "timestamp": timestamp,
        "event_id": event.get("id"),
        "market_id": market.get("id"),
        "condition_id": market.get("conditionId"),
        "question": market.get("question"),
        "category": category,
        "cluster": category or tag_text or "unknown",
        "yes_mid": _clip01(yes_mid),
        "no_mid": _clip01(no_mid),
        "yes_best_bid": yes_bid,
        "yes_best_ask": yes_ask,
        "no_best_bid": no_bid,
        "no_best_ask": no_ask,
        "yes_best_bid_size": yes_bid_size,
        "yes_best_ask_size": yes_ask_size,
        "no_best_bid_size": no_bid_size,
        "no_best_ask_size": no_ask_size,
        "reward_daily": reward_daily,
        "max_incentive_spread": max_spread,
        "min_incentive_size": min_size,
        "market_competitiveness": max(0.0, min(1.0, spread / max(max_spread, 1e-9))),
        "liquidity_clob": _to_float(market.get("liquidityClob") or market.get("liquidityNum") or market.get("liquidity")),
        "volume24hr_clob": _to_float(market.get("volume24hrClob") or market.get("volume24hr")),
        "spread": spread,
        "tags": tag_text,
        "outcomes": json.dumps(_parse_jsonish(market.get("outcomes"), [])),
        "clob_token_ids": json.dumps(token_ids),
        "quote_data_quality": quality,
        "source": "gamma_active_events_snapshot",
    }


def _book_bid_ask(
    *,
    cfg: LiveSnapshotConfig,
    token_id: str,
    get_json: JsonGetter,
    fallback_bid: float,
    fallback_ask: float,
) -> tuple[float, float, float, float]:
    try:
        book = get_json(
            f"{cfg.clob_base_url.rstrip('/')}/book",
            {"token_id": token_id},
            cfg.request_timeout_seconds,
        )
    except Exception:
        return fallback_bid, fallback_ask, float("nan"), float("nan")
    bid, ask, bid_size, ask_size = _parse_book_bid_ask(book)
    return (
        fallback_bid if pd.isna(bid) else bid,
        fallback_ask if pd.isna(ask) else ask,
        bid_size,
        ask_size,
    )


def _parse_book_bid_ask(book: Any) -> tuple[float, float, float, float]:
    if not isinstance(book, dict):
        return float("nan"), float("nan"), float("nan"), float("nan")
    bids = _price_size_rows(book.get("bids") or [])
    asks = _price_size_rows(book.get("asks") or [])
    best_bid = max((price for price, _ in bids), default=float("nan"))
    best_ask = min((price for price, _ in asks), default=float("nan"))
    bid_size = sum(size for price, size in bids if price == best_bid) if pd.notna(best_bid) else float("nan")
    ask_size = sum(size for price, size in asks if price == best_ask) if pd.notna(best_ask) else float("nan")
    return best_bid, best_ask, bid_size, ask_size


def _price_size_rows(rows: Any) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        price = _to_float(item.get("price"), default=float("nan"))
        size = _to_float(item.get("size"), default=0.0)
        if pd.notna(price) and pd.notna(size) and size > 0:
            out.append((price, size))
    return out


def _event_category(event: dict[str, Any]) -> tuple[str, str]:
    tags = [tag for tag in event.get("tags") or [] if isinstance(tag, dict)]
    tag_text = ",".join(str(tag.get("slug") or tag.get("label") or "") for tag in tags)
    category = str(event.get("category") or (tags[0].get("slug") if tags else "") or "unknown").lower()
    return category, tag_text


def _append_csv(path: Path, frame: pd.DataFrame) -> None:
    if frame.empty:
        return
    frame.to_csv(path, mode="a", header=not path.exists() or path.stat().st_size == 0, index=False)


@contextmanager
def _exclusive_output_lock(lock_path: Path) -> Iterator[None]:
    """Prevent two live-paper loops from writing the same evidence files."""

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError(f"live-paper output lock exists: {lock_path}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "created_utc": pd.Timestamp.now(tz="UTC").isoformat(),
                        "safety": "single-writer guard for public-paper evidence files",
                    },
                    indent=2,
                )
            )
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _atomic_to_csv(frame: pd.DataFrame, path: Path) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        frame.to_csv(tmp, index=False)
        tmp.replace(path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text + "\n", encoding="utf-8")
        tmp.replace(path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _latest_active_pair_notional(quotes: pd.DataFrame) -> float:
    if quotes.empty:
        return 0.0
    latest = pd.to_datetime(quotes["timestamp"], utc=True, errors="coerce").max()
    latest_quotes = quotes[pd.to_datetime(quotes["timestamp"], utc=True, errors="coerce").eq(latest)]
    return float(latest_quotes.drop_duplicates("condition_id")["active_order_notional_pair"].sum())


def _get_json(url: str, params: dict[str, Any] | None, timeout: float) -> Any:
    query = urlencode(_clean_params(params), doseq=False)
    full_url = f"{url}?{query}" if query else url
    request = Request(full_url, headers={"User-Agent": "polymarket-lp-research/0.1"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _clean_params(params: dict[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in (params or {}).items():
        if value is None:
            continue
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        else:
            out[key] = str(value)
    return out


def _parse_jsonish(value: Any, default: list[Any]) -> list[Any]:
    if value is None:
        return default
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, list) else default
    except json.JSONDecodeError:
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _midpoint(bid: float, ask: float, fallback: float) -> float:
    if pd.notna(bid) and pd.notna(ask) and ask >= bid:
        return (float(bid) + float(ask)) / 2.0
    return fallback


def _clip01(value: float) -> float:
    return max(0.001, min(0.999, float(value)))
