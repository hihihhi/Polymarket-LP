from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


@dataclass(slots=True)
class LPConfig:
    initial_capital: float = 2000.0
    quote_size_shares: float = 25.0
    quote_offset: float = 0.02
    safety_margin: float = 0.015
    max_unpaired_per_market: float = 60.0
    max_total_unpaired: float = 450.0
    max_cluster_unpaired: float = 250.0
    enable_rescue_quotes: bool = True
    rescue_min_pair_edge_per_share: float = 0.0
    rescue_quote_offset: float = 0.005
    exit_loss_cents: float = 0.025
    max_unpaired_minutes: float = 30.0
    exit_slippage: float = 0.005
    assumed_competitor_score: float = 2000.0
    active_capital_limit: float = 1700.0
    max_dt_seconds: float = 300.0
    min_reward_daily: float = 0.0
    max_market_competitiveness: float = 1.0
    allowed_categories: str = ""
    excluded_categories: str = "sports,crypto"
    # Candidate selection / adaptive risk gates
    rank_by_reward_density: bool = True
    min_reward_density_per_day: float = 0.0
    recent_vol_window: int = 6
    max_recent_vol: float = 1.0
    max_recent_jump: float = 1.0
    vol_quote_multiplier: float = 0.0
    depth_cap_quote_size: bool = False
    depth_quote_size_fraction: float = 1.0
    min_depth_capped_quote_size_shares: float = 1.0


@dataclass(slots=True)
class InventoryLot:
    condition_id: str
    side: str
    price: float
    size: float
    entry_ts: pd.Timestamp
    cluster: str = "unknown"

    @property
    def notional(self) -> float:
        return self.price * self.size


def load_snapshots(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    required = {"timestamp", "condition_id", "reward_daily", "max_incentive_spread", "min_incentive_size"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"snapshots missing required columns: {sorted(missing)}")
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    for c in [
        "reward_daily", "max_incentive_spread", "min_incentive_size", "yes_mid", "no_mid",
        "yes_best_bid", "yes_best_ask", "no_best_bid", "no_best_ask",
        "yes_best_bid_size", "yes_best_ask_size", "no_best_bid_size", "no_best_ask_size",
        "market_competitiveness", "competitor_score",
    ]:
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    if "yes_mid" not in df:
        if {"yes_best_bid", "yes_best_ask"}.issubset(df.columns):
            df["yes_mid"] = (df["yes_best_bid"] + df["yes_best_ask"]) / 2
        else:
            raise ValueError("Provide yes_mid or yes_best_bid+yes_best_ask")
    if "no_mid" not in df:
        df["no_mid"] = ((df["no_best_bid"] + df["no_best_ask"]) / 2) if {"no_best_bid", "no_best_ask"}.issubset(df.columns) else 1 - df["yes_mid"]
    if "cluster" not in df:
        df["cluster"] = df["category"] if "category" in df else "unknown"
    df["condition_id"] = df["condition_id"].astype(str)
    df["cluster"] = df["cluster"].fillna("unknown").astype(str)
    df = df.dropna(subset=["timestamp", "condition_id", "yes_mid", "no_mid", "reward_daily", "max_incentive_spread"])
    return df.sort_values(["timestamp", "condition_id"]).reset_index(drop=True)


def _csv_set(value: str | None) -> set[str]:
    if not value:
        return set()
    return {part.strip().lower() for part in str(value).split(",") if part.strip()}


DEFAULT_EXCLUSION_ALIASES = {
    "sports": {
        "sports",
        "sport",
        "nba",
        "nfl",
        "mlb",
        "nhl",
        "wnba",
        "soccer",
        "football",
        "basketball",
        "baseball",
        "hockey",
        "tennis",
        "golf",
        "mma",
        "ufc",
        "boxing",
        "f1",
        "formula-1",
        "motorsports",
        "olympics",
    },
    "crypto": {
        "crypto",
        "cryptocurrency",
        "bitcoin",
        "btc",
        "ethereum",
        "eth",
        "solana",
        "sol",
        "xrp",
        "doge",
        "memecoin",
    },
}


def _expanded_category_set(values: set[str]) -> set[str]:
    expanded = set(values)
    for value in values:
        expanded.update(DEFAULT_EXCLUSION_ALIASES.get(value, set()))
    return expanded


def _row_category_tokens(df: pd.DataFrame) -> pd.Series:
    cols = [col for col in ["category", "cluster", "tags"] if col in df.columns]
    if not cols:
        return pd.Series(["unknown"] * len(df), index=df.index)
    tokens = pd.Series("", index=df.index, dtype="object")
    for col in cols:
        tokens = tokens.str.cat(df[col].fillna("").astype(str).str.lower(), sep=",")
    return tokens.str.replace(r"[^a-z0-9]+", ",", regex=True).str.strip(",")


def filter_snapshots_for_strategy(snapshots: pd.DataFrame, cfg: LPConfig) -> pd.DataFrame:
    """Apply portfolio-level market filters before quoting.

    The default mandate intentionally avoids sports and crypto-style high-volatility
    clusters, including subcategories such as NBA/BTC. This is not a data-mined
    truth; it is a risk-control default for the LP strategy because one-sided
    fills dominate rewards in jumpy markets.
    """
    df = snapshots.copy()
    if "reward_daily" in df:
        df = df[pd.to_numeric(df["reward_daily"], errors="coerce").fillna(0) >= cfg.min_reward_daily]
    if "market_competitiveness" in df:
        df = df[pd.to_numeric(df["market_competitiveness"], errors="coerce").fillna(1) <= cfg.max_market_competitiveness]
    if any(col in df.columns for col in ["category", "cluster", "tags"]):
        token_text = _row_category_tokens(df)
        allowed = _expanded_category_set(_csv_set(cfg.allowed_categories))
        excluded = _expanded_category_set(_csv_set(cfg.excluded_categories))
        if allowed:
            df = df[token_text.apply(lambda text: bool(set(text.split(",")) & allowed))]
            token_text = _row_category_tokens(df)
        if excluded:
            df = df[~token_text.apply(lambda text: bool(set(text.split(",")) & excluded))]
    return df.reset_index(drop=True)


def order_score(max_spread: float, distance_from_mid: float, size: float) -> float:
    v = float(max_spread)
    s = abs(float(distance_from_mid))
    if v <= 0 or s > v or size <= 0 or not math.isfinite(v):
        return 0.0
    return ((v - s) / v) ** 2 * size


def competitor_score_for_row(row: pd.Series, cfg: LPConfig) -> float:
    if "competitor_score" in row and pd.notna(row["competitor_score"]):
        return max(float(row["competitor_score"]), 0.0)
    comp = row.get("market_competitiveness", np.nan)
    if pd.notna(comp):
        return 500 + 7500 * max(0.0, min(1.0, float(comp)))
    return cfg.assumed_competitor_score


def depth_capped_quote_size(row: pd.Series, cfg: LPConfig, *, base_size: float | None = None) -> float:
    size = cfg.quote_size_shares if base_size is None else float(base_size)
    if not cfg.depth_cap_quote_size:
        return size
    ask_sizes: list[float] = []
    for col in ["yes_best_ask_size", "no_best_ask_size"]:
        value = row.get(col, np.nan)
        if pd.notna(value) and math.isfinite(float(value)) and float(value) > 0:
            ask_sizes.append(float(value))
    if len(ask_sizes) < 2:
        return 0.0
    capped = min(size, min(ask_sizes) * max(0.0, float(cfg.depth_quote_size_fraction)))
    return capped if capped >= cfg.min_depth_capped_quote_size_shares else 0.0


def quote_for_row(row: pd.Series, cfg: LPConfig, *, quote_offset: float | None = None, quote_size: float | None = None) -> dict[str, float | bool]:
    offset = cfg.quote_offset if quote_offset is None else float(quote_offset)
    size = depth_capped_quote_size(row, cfg, base_size=quote_size)
    y_mid = float(row["yes_mid"])
    n_mid = float(row["no_mid"])
    y_bid = max(0.001, y_mid - offset)
    n_bid = max(0.001, n_mid - offset)
    pair_cost = y_bid + n_bid
    if pair_cost > 1 - cfg.safety_margin:
        cut = (pair_cost - (1 - cfg.safety_margin)) / 2
        y_bid = max(0.001, y_bid - cut)
        n_bid = max(0.001, n_bid - cut)
        pair_cost = y_bid + n_bid
    max_spread = float(row["max_incentive_spread"])
    min_size = float(row.get("min_incentive_size", 0))
    eligible = size > 0 and size >= min_size and offset <= max_spread and pair_cost <= 1 - cfg.safety_margin + 1e-9
    y_score = order_score(max_spread, y_mid - y_bid, size)
    n_score = order_score(max_spread, n_mid - n_bid, size)
    our_score = min(y_score, n_score) if eligible else 0.0
    comp = competitor_score_for_row(row, cfg)
    share = our_score / (our_score + comp) if our_score > 0 else 0.0
    active_order_notional = size * (y_bid + n_bid)
    expected_reward_per_day = float(row.get("reward_daily", 0.0)) * share
    reward_density_per_day = expected_reward_per_day / active_order_notional if active_order_notional > 0 else 0.0
    return {"eligible": bool(eligible), "yes_bid": y_bid, "no_bid": n_bid, "pair_cost": pair_cost, "our_score": our_score, "competitor_score": comp, "reward_share": share, "quote_offset": offset, "quote_size": size, "active_order_notional": active_order_notional, "expected_reward_per_day": expected_reward_per_day, "reward_density_per_day": reward_density_per_day}


def inv_notional(inv: list[InventoryLot], *, condition_id: str | None = None, cluster: str | None = None) -> float:
    return sum(x.notional for x in inv if (condition_id is None or x.condition_id == condition_id) and (cluster is None or x.cluster == cluster))


def mark_lot(lot: InventoryLot, row: pd.Series, cfg: LPConfig) -> float:
    mid = float(row["yes_mid"] if lot.side == "YES" else row["no_mid"])
    return max(0.0, min(1.0, mid - cfg.exit_slippage))


def rescue_quote_for_inventory(
    *,
    row: pd.Series,
    inventory_side: str,
    worst_entry_price: float,
    size: float,
    cfg: LPConfig,
) -> dict[str, float | str | bool]:
    """Plan an opposite-side rescue quote for one-sided inventory.

    A rescue quote is only allowed when the completed YES+NO pair still locks at
    least ``rescue_min_pair_edge_per_share`` before rewards. This turns a naked
    LP fill into a complete set when the opposite side becomes fillable, instead
    of relying on directional beliefs.
    """

    side = str(inventory_side).upper()
    rescue_side = "NO" if side == "YES" else "YES"
    opposite_mid = float(row["no_mid"] if rescue_side == "NO" else row["yes_mid"])
    max_bid = 1.0 - float(worst_entry_price) - cfg.rescue_min_pair_edge_per_share
    post_only_bid = opposite_mid - cfg.rescue_quote_offset
    bid = min(max_bid, post_only_bid)
    eligible = (
        bool(cfg.enable_rescue_quotes)
        and float(size) > 0
        and math.isfinite(max_bid)
        and math.isfinite(post_only_bid)
        and max_bid >= 0.001
        and post_only_bid >= 0.001
        and bid >= 0.001
    )
    if eligible:
        bid = max(0.001, min(0.999, bid))
    pair_cost = float(worst_entry_price) + bid if eligible else math.nan
    return {
        "eligible": bool(eligible),
        "side": rescue_side,
        "bid_price": bid if eligible else math.nan,
        "size": float(size),
        "worst_entry_price": float(worst_entry_price),
        "max_rescue_bid": max_bid,
        "opposite_mid": opposite_mid,
        "pair_cost": pair_cost,
        "pair_edge_per_share": 1.0 - pair_cost if eligible else math.nan,
    }


def _inventory_rescue_groups(inventory: list[InventoryLot]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for lot in inventory:
        key = (lot.condition_id, lot.side)
        row = grouped.setdefault(
            key,
            {
                "condition_id": lot.condition_id,
                "side": lot.side,
                "cluster": lot.cluster,
                "size": 0.0,
                "worst_entry_price": 0.0,
            },
        )
        row["size"] += lot.size
        row["worst_entry_price"] = max(float(row["worst_entry_price"]), lot.price)
    return list(grouped.values())


def handle_fill(*, inventory: list[InventoryLot], events: list[dict[str, Any]], timestamp: pd.Timestamp, condition_id: str, side: str, price: float, size: float, cluster: str, cfg: LPConfig) -> tuple[list[InventoryLot], float, float, float, float, float]:
    rem = float(size)
    opp = "NO" if side == "YES" else "YES"
    pair_pnl = exit_pnl = paired = opened = cap_exit = 0.0
    keep: list[InventoryLot] = []
    for lot in inventory:
        if lot.condition_id == condition_id and lot.side == opp and rem > 1e-9:
            qty = min(lot.size, rem)
            pnl = qty * (1 - lot.price - price)
            pair_pnl += pnl
            paired += qty
            rem -= qty
            lot.size -= qty
            events.append({"timestamp": timestamp, "condition_id": condition_id, "event": "PAIR_MERGED", "new_side": side, "new_price": price, "old_side": lot.side, "old_price": lot.price, "size": qty, "pnl_usdc": pnl, "pair_cost": lot.price + price, "cluster": cluster})
            if lot.size > 1e-9:
                keep.append(lot)
        else:
            keep.append(lot)
    inventory = keep
    if rem > 1e-9:
        notional = price * rem
        can_open = (
            inv_notional(inventory, condition_id=condition_id) + notional <= cfg.max_unpaired_per_market
            and inv_notional(inventory) + notional <= cfg.max_total_unpaired
            and inv_notional(inventory, cluster=cluster) + notional <= cfg.max_cluster_unpaired
        )
        if can_open:
            inventory.append(InventoryLot(condition_id, side, price, rem, timestamp, cluster))
            opened += rem
            events.append({"timestamp": timestamp, "condition_id": condition_id, "event": "OPEN_UNPAIRED", "side": side, "price": price, "size": rem, "notional": notional, "cluster": cluster})
        else:
            exit_price = max(0.0, price - cfg.exit_slippage)
            pnl = (exit_price - price) * rem
            exit_pnl += pnl
            cap_exit += rem
            events.append({"timestamp": timestamp, "condition_id": condition_id, "event": "RISK_CAP_IMMEDIATE_EXIT", "side": side, "price": price, "exit_price": exit_price, "size": rem, "notional": notional, "pnl_usdc": pnl, "cluster": cluster})
    return inventory, pair_pnl, exit_pnl, paired, opened, cap_exit


def simulate_lp(snapshots: pd.DataFrame, cfg: LPConfig) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    df = filter_snapshots_for_strategy(snapshots, cfg).sort_values(["condition_id", "timestamp"]).reset_index(drop=True)
    if df.empty:
        empty_events = pd.DataFrame()
        empty_equity = pd.DataFrame()
        return empty_events, empty_equity, pd.DataFrame()
    df["mid_change"] = df.groupby("condition_id")["yes_mid"].diff().abs().fillna(0.0)
    df["recent_vol"] = df.groupby("condition_id")["mid_change"].transform(lambda s: s.rolling(cfg.recent_vol_window, min_periods=1).mean())
    df["recent_jump"] = df.groupby("condition_id")["mid_change"].transform(lambda s: s.rolling(cfg.recent_vol_window, min_periods=1).max())
    df["next_ts"] = df.groupby("condition_id")["timestamp"].shift(-1)
    df["next_yes_mid"] = df.groupby("condition_id")["yes_mid"].shift(-1)
    df["next_no_mid"] = df.groupby("condition_id")["no_mid"].shift(-1)
    df["dt_seconds"] = (df["next_ts"] - df["timestamp"]).dt.total_seconds().clip(lower=0, upper=cfg.max_dt_seconds).fillna(0)
    inv: list[InventoryLot] = []
    events: list[dict[str, Any]] = []
    eq: list[dict[str, Any]] = []
    reward = pair_pnl = exit_pnl = 0.0
    for ts, rows in df.groupby("timestamp", sort=True):
        still: list[InventoryLot] = []
        for lot in inv:
            r = rows[rows["condition_id"].astype(str).eq(lot.condition_id)]
            if r.empty:
                still.append(lot)
                continue
            mark = mark_lot(lot, r.iloc[0], cfg)
            age = (ts - lot.entry_ts).total_seconds() / 60
            if lot.price - mark >= cfg.exit_loss_cents or age >= cfg.max_unpaired_minutes:
                pnl = (mark - lot.price) * lot.size
                exit_pnl += pnl
                events.append({"timestamp": ts, "condition_id": lot.condition_id, "event": "EXIT_UNPAIRED", "side": lot.side, "price": lot.price, "exit_price": mark, "size": lot.size, "pnl_usdc": pnl, "age_minutes": age, "cluster": lot.cluster})
            else:
                still.append(lot)
        inv = still
        active = one_open = paired_sh = cap_sh = rew_ts = pair_ts = exit_ts = 0.0
        eligible = skipped = 0
        if cfg.enable_rescue_quotes and inv:
            rows_by_condition = {str(row["condition_id"]): row for _, row in rows.iterrows()}
            for group in _inventory_rescue_groups(inv):
                condition_id = str(group["condition_id"])
                row = rows_by_condition.get(condition_id)
                if row is None:
                    continue
                current_same_side = [lot for lot in inv if lot.condition_id == condition_id and lot.side == group["side"]]
                if not current_same_side:
                    continue
                size = sum(lot.size for lot in current_same_side)
                worst_entry = max(lot.price for lot in current_same_side)
                rescue = rescue_quote_for_inventory(
                    row=row,
                    inventory_side=str(group["side"]),
                    worst_entry_price=worst_entry,
                    size=size,
                    cfg=cfg,
                )
                if not rescue["eligible"]:
                    continue
                rescue_notional = float(rescue["bid_price"]) * float(rescue["size"])
                if active + rescue_notional > cfg.active_capital_limit:
                    skipped += 1
                    events.append({"timestamp": ts, "condition_id": condition_id, "event": "RESCUE_SKIPPED_CAPITAL", "side": rescue["side"], "bid_price": rescue["bid_price"], "size": rescue["size"], "rescue_notional": rescue_notional, "cluster": group["cluster"]})
                    continue
                active += rescue_notional
                events.append({"timestamp": ts, "condition_id": condition_id, "event": "RESCUE_QUOTE", "side": rescue["side"], "bid_price": rescue["bid_price"], "size": rescue["size"], "worst_entry_price": worst_entry, "max_rescue_bid": rescue["max_rescue_bid"], "pair_cost": rescue["pair_cost"], "pair_edge_per_share": rescue["pair_edge_per_share"], "rescue_notional": rescue_notional, "cluster": group["cluster"]})
                next_mid = row["next_no_mid"] if rescue["side"] == "NO" else row["next_yes_mid"]
                if pd.notna(row["next_ts"]) and pd.notna(next_mid) and float(next_mid) <= float(rescue["bid_price"]):
                    inv, pp, ep, ps, os, cs = handle_fill(inventory=inv, events=events, timestamp=pd.Timestamp(row["next_ts"]), condition_id=condition_id, side=str(rescue["side"]), price=float(rescue["bid_price"]), size=float(rescue["size"]), cluster=str(group["cluster"]), cfg=cfg)
                    pair_pnl += pp; exit_pnl += ep; pair_ts += pp; exit_ts += ep; paired_sh += ps; one_open += os; cap_sh += cs
        candidates: list[tuple[float, pd.Series, dict[str, float | bool]]] = []
        for _, row in rows.iterrows():
            if float(row.get("recent_vol", 0.0)) > cfg.max_recent_vol or float(row.get("recent_jump", 0.0)) > cfg.max_recent_jump:
                events.append({"timestamp": ts, "condition_id": row["condition_id"], "event": "SKIP_VOLATILITY", "recent_vol": float(row.get("recent_vol", 0.0)), "recent_jump": float(row.get("recent_jump", 0.0)), "cluster": row.get("cluster", "unknown")})
                continue
            adaptive_offset = cfg.quote_offset + cfg.vol_quote_multiplier * float(row.get("recent_vol", 0.0))
            q = quote_for_row(row, cfg, quote_offset=adaptive_offset)
            if not q["eligible"]:
                continue
            if float(q["reward_density_per_day"]) < cfg.min_reward_density_per_day:
                events.append({"timestamp": ts, "condition_id": row["condition_id"], "event": "SKIP_REWARD_DENSITY", "reward_density_per_day": float(q["reward_density_per_day"]), "cluster": row.get("cluster", "unknown")})
                continue
            sort_key = float(q["reward_density_per_day"] if cfg.rank_by_reward_density else q["expected_reward_per_day"])
            candidates.append((sort_key, row, q))
        candidates.sort(key=lambda item: item[0], reverse=True)
        for _, row, q in candidates:
            order_notional = float(q["active_order_notional"])
            if active + order_notional > cfg.active_capital_limit:
                skipped += 1
                continue
            active += order_notional
            eligible += 1
            rw = float(row["reward_daily"]) * (float(row["dt_seconds"]) / 86400) * float(q["reward_share"])
            reward += rw
            rew_ts += rw
            events.append({"timestamp": ts, "condition_id": row["condition_id"], "event": "REWARD_ACCRUAL", "reward_usdc": rw, "reward_share": q["reward_share"], "our_score": q["our_score"], "competitor_score": q["competitor_score"], "active_order_notional": order_notional, "pair_cost": q["pair_cost"], "quote_offset": q["quote_offset"], "reward_density_per_day": q["reward_density_per_day"], "cluster": row.get("cluster", "unknown")})
            if pd.isna(row["next_yes_mid"]) or pd.isna(row["next_no_mid"]):
                continue
            fills = []
            if float(row["next_yes_mid"]) <= float(q["yes_bid"]):
                fills.append(("YES", float(q["yes_bid"])))
            if float(row["next_no_mid"]) <= float(q["no_bid"]):
                fills.append(("NO", float(q["no_bid"])))
            for side, price in fills:
                inv, pp, ep, ps, os, cs = handle_fill(inventory=inv, events=events, timestamp=pd.Timestamp(row["next_ts"]), condition_id=str(row["condition_id"]), side=side, price=price, size=float(q["quote_size"]), cluster=str(row.get("cluster", "unknown")), cfg=cfg)
                pair_pnl += pp; exit_pnl += ep; pair_ts += pp; exit_ts += ep; paired_sh += ps; one_open += os; cap_sh += cs
        mtm = 0.0
        for lot in inv:
            r = rows[rows["condition_id"].astype(str).eq(lot.condition_id)]
            if not r.empty:
                mtm += (mark_lot(lot, r.iloc[0], cfg) - lot.price) * lot.size
        realized = reward + pair_pnl + exit_pnl
        eq.append({"timestamp": ts, "reward_pnl_usdc": reward, "pair_pnl_usdc": pair_pnl, "exit_pnl_usdc": exit_pnl, "realized_pnl_usdc": realized, "mtm_inventory_pnl_usdc": mtm, "equity_realized": cfg.initial_capital + realized, "equity_mtm": cfg.initial_capital + realized + mtm, "open_inventory_lots": len(inv), "open_inventory_notional": inv_notional(inv), "active_order_notional": active, "reward_this_ts": rew_ts, "pair_pnl_this_ts": pair_ts, "exit_pnl_this_ts": exit_ts, "one_sided_open_shares": one_open, "paired_shares": paired_sh, "risk_cap_exit_shares": cap_sh, "eligible_quotes": eligible, "skipped_capital": skipped})
    events_df = pd.DataFrame(events)
    equity = pd.DataFrame(eq)
    if not equity.empty:
        equity["running_peak_realized"] = equity["equity_realized"].cummax()
        equity["drawdown_realized_usdc"] = equity["equity_realized"] - equity["running_peak_realized"]
        equity["drawdown_realized_pct"] = equity["drawdown_realized_usdc"] / equity["running_peak_realized"]
        equity["running_peak_mtm"] = equity["equity_mtm"].cummax()
        equity["drawdown_mtm_usdc"] = equity["equity_mtm"] - equity["running_peak_mtm"]
        equity["drawdown_mtm_pct"] = equity["drawdown_mtm_usdc"] / equity["running_peak_mtm"]
    return events_df, equity, summarize(events_df, equity, cfg)


def summarize(events: pd.DataFrame, equity: pd.DataFrame, cfg: LPConfig) -> pd.DataFrame:
    if equity.empty:
        return pd.DataFrame()
    final = equity.iloc[-1]
    pnl_col = pd.to_numeric(events.get("pnl_usdc", pd.Series(dtype=float)), errors="coerce").fillna(0)
    losses = float(-pnl_col[pnl_col < 0].sum())
    gains = float(pnl_col[pnl_col > 0].sum())
    reward = float(events.get("reward_usdc", pd.Series(dtype=float)).sum()) if not events.empty else 0.0
    pair = float(events.loc[events.get("event", pd.Series(dtype=str)).eq("PAIR_MERGED"), "pnl_usdc"].sum()) if not events.empty and "pnl_usdc" in events else 0.0
    exitp = float(events.loc[events.get("event", pd.Series(dtype=str)).isin(["EXIT_UNPAIRED", "RISK_CAP_IMMEDIATE_EXIT"]), "pnl_usdc"].sum()) if not events.empty and "pnl_usdc" in events else 0.0
    one = float(events.loc[events.get("event", pd.Series(dtype=str)).eq("OPEN_UNPAIRED"), "size"].sum()) if not events.empty and "size" in events else 0.0
    paired = float(events.loc[events.get("event", pd.Series(dtype=str)).eq("PAIR_MERGED"), "size"].sum()) if not events.empty and "size" in events else 0.0
    rescue_quotes = int(events.get("event", pd.Series(dtype=str)).eq("RESCUE_QUOTE").sum()) if not events.empty else 0
    rescue_skipped = int(events.get("event", pd.Series(dtype=str)).eq("RESCUE_SKIPPED_CAPITAL").sum()) if not events.empty else 0
    daily = equity.set_index("timestamp")["equity_mtm"].resample("1D").last().dropna().pct_change().dropna()
    sharpe = sortino = bad_day = np.nan
    if len(daily) >= 2:
        bad_day = float(daily.quantile(0.05))
        if daily.std(ddof=1) > 0:
            sharpe = float(daily.mean() / daily.std(ddof=1) * math.sqrt(365))
        down = daily[daily < 0]
        if len(down) > 0:
            ds = down.std(ddof=1) if len(down) > 1 else abs(float(down.iloc[0]))
            sortino = float(daily.mean() / ds * math.sqrt(365)) if ds > 0 else np.nan
    total = float(final["equity_mtm"] - cfg.initial_capital)
    avg_inv = float(equity["open_inventory_notional"].mean())
    max_dd = abs(float(equity["drawdown_mtm_usdc"].min()))
    return pd.DataFrame([{**asdict(cfg), "total_reward_usdc": reward, "total_pair_spread_pnl_usdc": pair, "total_inventory_exit_pnl_usdc": exitp, "total_pnl_usdc": total, "return_on_initial_capital": total / cfg.initial_capital, "max_drawdown_realized_usdc": float(equity["drawdown_realized_usdc"].min()), "max_drawdown_realized_pct": float(equity["drawdown_realized_pct"].min()), "max_drawdown_mtm_usdc": float(equity["drawdown_mtm_usdc"].min()), "max_drawdown_mtm_pct": float(equity["drawdown_mtm_pct"].min()), "daily_sharpe_mtm": sharpe, "daily_sortino_mtm": sortino, "bad_day_p95_return": bad_day, "profit_factor_trading_only": gains / losses if losses else math.inf, "reward_to_trading_loss_ratio": reward / losses if losses else math.inf, "one_sided_open_shares": one, "paired_shares": paired, "pair_completion_ratio_shares": paired / max(paired + one, 1.0), "pair_completion_ratio_of_opened_shares": paired / one if one else math.inf, "rescue_quote_count": rescue_quotes, "rescue_skipped_capital_count": rescue_skipped, "max_open_inventory_notional": float(equity["open_inventory_notional"].max()), "avg_open_inventory_notional": avg_inv, "max_active_order_notional": float(equity["active_order_notional"].max()), "avg_active_order_notional": float(equity["active_order_notional"].mean()), "reward_per_dollar_avg_inventory": reward / avg_inv if avg_inv else math.inf, "pnl_per_dollar_avg_inventory": total / avg_inv if avg_inv else math.nan, "recovery_factor": total / max_dd if max_dd else math.inf, "quote_eligibility_intervals": int(equity["eligible_quotes"].sum()), "capital_skipped_intervals": int(equity["skipped_capital"].sum())}])


def make_synthetic_snapshots(seed: int = 7, days: int = 14, n_markets: int = 30) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    times = pd.date_range("2026-01-01", periods=days * 24 * 12, freq="5min", tz="UTC")
    cats = ["politics", "culture", "sports", "crypto", "economy", "weather"]
    for i in range(n_markets):
        cat = cats[i % len(cats)]
        base = rng.uniform(0.2, 0.8)
        p = base
        vol = rng.uniform(0.0015, 0.006) * (2 if cat in {"sports", "crypto"} else 1)
        for t in times:
            shock = rng.normal(0, vol)
            if rng.random() < (0.0009 if cat in {"politics", "culture", "economy"} else 0.003):
                shock += rng.normal(0, vol * 8)
            p = float(np.clip(p + 0.03 * (base - p) + shock, 0.03, 0.97))
            rows.append({"timestamp": t, "condition_id": f"synthetic_{i:03d}", "category": cat, "cluster": cat, "yes_mid": p, "no_mid": 1 - p, "reward_daily": rng.choice([25, 50, 100, 200, 500], p=[0.25, 0.3, 0.25, 0.15, 0.05]), "max_incentive_spread": rng.choice([0.03, 0.04, 0.05]), "min_incentive_size": rng.choice([10, 20, 25]), "market_competitiveness": rng.beta(2, 4)})
    return pd.DataFrame(rows)


def run_backtest_to_files(snapshots: pd.DataFrame, cfg: LPConfig, out_dir: str | Path) -> pd.DataFrame:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    events, equity, summary = simulate_lp(snapshots, cfg)
    events.to_csv(out / "lp_events.csv", index=False)
    equity.to_csv(out / "lp_equity_curve.csv", index=False)
    summary.to_csv(out / "lp_summary.csv", index=False)
    return summary
