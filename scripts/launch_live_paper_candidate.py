#!/usr/bin/env python3
"""Generate and optionally start a parameterized LP public-paper candidate run.

The launcher writes the same collector/watcher/rolling/24h-extension scripts used
by manual candidate tests, but makes quote size, residual cap, reward density,
capital limits, and proof gates explicit CLI parameters. It is public-data only:
no private keys, signing, order submission, or cancellation.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class LaunchCandidateConfig:
    name: str
    state_dir: str
    repo: str
    python: str
    quote_size: float
    partial_rescue_max_residual_loss_usdc: float
    min_reward_density_per_day: float
    active_capital_limit: float = 1200.0
    initial_capital: float = 2000.0
    target_monthly: float = 1000.0
    reward_to_loss_haircut: float = 8.03937017359762
    quote_offset: float = 0.02
    safety_margin: float = 0.015
    excluded_categories: str = "sports,crypto"
    max_recent_vol: float = 0.006
    max_recent_jump: float = 0.025
    vol_quote_multiplier: float = 0.5
    iterations: int = 37
    extension_iterations: int = 108
    interval_seconds: int = 600
    max_events: int = 20
    event_limit: int = 500
    request_timeout_seconds: float = 20.0
    sleep_between_book_requests_seconds: float = 0.02
    required_capture_rate: float = 0.5
    min_target_margin: float = 1.0
    max_active_pair_notional: float = 1200.0
    max_pending_quote_rate: float = 0.05
    min_unique_markets: int = 4
    min_quote_rows_6h: int = 24
    min_book_scenarios_6h: int = 24
    min_quote_rows_24h: int = 96
    min_book_scenarios_24h: int = 96
    min_taker_rescue_feasible_rate: float = 0.80
    min_taker_rescue_depth_fraction: float = 1.0
    taker_rescue_min_pair_edge_per_share: float = 0.0
    max_latest_taker_residual_loss_fraction: float = 0.05
    bootstrap_resamples_watch: int = 1000
    bootstrap_resamples_rolling: int = 5000
    bootstrap_resamples_after24: int = 2000
    bootstrap_seed: int = 132
    bootstrap_block_size: int = 2
    gamma_base_url: str = "https://gamma-api.polymarket.com"
    clob_base_url: str = "https://clob.polymarket.com"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--name", required=True)
    p.add_argument("--state-dir", required=True)
    p.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    p.add_argument("--python", default=str(Path(__file__).resolve().parents[1] / ".venv" / "Scripts" / "python.exe"))
    p.add_argument("--quote-size", type=float, required=True)
    p.add_argument("--partial-rescue-max-residual-loss-usdc", type=float, required=True)
    p.add_argument("--min-reward-density-per-day", type=float, required=True)
    p.add_argument("--active-capital-limit", type=float, default=1200.0)
    p.add_argument("--initial-capital", type=float, default=2000.0)
    p.add_argument("--target-monthly", type=float, default=1000.0)
    p.add_argument("--reward-to-loss-haircut", type=float, default=8.03937017359762)
    p.add_argument("--quote-offset", type=float, default=0.02)
    p.add_argument("--safety-margin", type=float, default=0.015)
    p.add_argument("--excluded-categories", default="sports,crypto")
    p.add_argument("--max-recent-vol", type=float, default=0.006)
    p.add_argument("--max-recent-jump", type=float, default=0.025)
    p.add_argument("--vol-quote-multiplier", type=float, default=0.5)
    p.add_argument("--iterations", type=int, default=37)
    p.add_argument("--extension-iterations", type=int, default=108)
    p.add_argument("--interval-seconds", type=int, default=600)
    p.add_argument("--max-events", type=int, default=20)
    p.add_argument("--event-limit", type=int, default=500)
    p.add_argument("--required-capture-rate", type=float, default=0.5)
    p.add_argument("--max-active-pair-notional", type=float, default=1200.0)
    p.add_argument("--run-id", default="")
    p.add_argument("--start", action="store_true")
    return p.parse_args()


def main() -> None:
    ns = parse_args()
    cfg = LaunchCandidateConfig(
        name=ns.name,
        state_dir=ns.state_dir,
        repo=ns.repo,
        python=ns.python,
        quote_size=ns.quote_size,
        partial_rescue_max_residual_loss_usdc=ns.partial_rescue_max_residual_loss_usdc,
        min_reward_density_per_day=ns.min_reward_density_per_day,
        active_capital_limit=ns.active_capital_limit,
        initial_capital=ns.initial_capital,
        target_monthly=ns.target_monthly,
        reward_to_loss_haircut=ns.reward_to_loss_haircut,
        quote_offset=ns.quote_offset,
        safety_margin=ns.safety_margin,
        excluded_categories=ns.excluded_categories,
        max_recent_vol=ns.max_recent_vol,
        max_recent_jump=ns.max_recent_jump,
        vol_quote_multiplier=ns.vol_quote_multiplier,
        iterations=ns.iterations,
        extension_iterations=ns.extension_iterations,
        interval_seconds=ns.interval_seconds,
        max_events=ns.max_events,
        event_limit=ns.event_limit,
        required_capture_rate=ns.required_capture_rate,
        max_active_pair_notional=ns.max_active_pair_notional,
    )
    manifest = write_launch_artifacts(cfg, run_id=ns.run_id, start=ns.start)
    print(json.dumps(_json_safe(manifest), indent=2, allow_nan=False))


def write_launch_artifacts(
    cfg: LaunchCandidateConfig,
    *,
    run_id: str = "",
    start: bool = False,
) -> dict[str, Any]:
    safe = _safe_name(cfg.name)
    stamp = run_id or f"{safe}_{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    out_dir = Path(cfg.state_dir) / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    paths = _paths(out_dir, safe)
    scripts: dict[str, str] = {}
    scripts["collector"] = _collector_script(cfg, paths)
    _write(paths["collector_script"], scripts["collector"])

    pids: dict[str, int] = {"pid": 0, "watcher_pid": 0, "rolling_pid": 0, "extend24_pid": 0}
    if start:
        pids["pid"] = _start_powershell(paths["collector_script"])

    scripts["watcher"] = _watcher_script(cfg, paths, pids["pid"])
    scripts["rolling"] = _rolling_script(cfg, paths, pids["pid"])
    scripts["extend24"] = _extend24_script(cfg, paths, pids["pid"], 0, 0)
    _write(paths["watcher_script"], scripts["watcher"])
    _write(paths["rolling_script"], scripts["rolling"])

    if start:
        pids["watcher_pid"] = _start_powershell(paths["watcher_script"])
        pids["rolling_pid"] = _start_powershell(paths["rolling_script"])
        scripts["extend24"] = _extend24_script(cfg, paths, pids["pid"], pids["watcher_pid"], pids["rolling_pid"])
    _write(paths["extend24_script"], scripts["extend24"])

    if start:
        pids["extend24_pid"] = _start_powershell(paths["extend24_script"])

    manifest = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "strategy": cfg.name,
        **pids,
        "duration_hours": "~6 then 18h extension",
        "iterations": cfg.iterations,
        "extension_iterations": cfg.extension_iterations,
        "interval_seconds": cfg.interval_seconds,
        "max_events": cfg.max_events,
        "include_clob_books": True,
        "quote_size": cfg.quote_size,
        "partial_rescue_max_residual_loss_usdc": cfg.partial_rescue_max_residual_loss_usdc,
        "min_reward_density_per_day": cfg.min_reward_density_per_day,
        "active_capital_limit": cfg.active_capital_limit,
        "snapshot": str(paths["snapshot"]),
        "quotes": str(paths["quotes"]),
        "manifest": str(paths["collector_manifest"]),
        "log": str(paths["collector_log"]),
        "watcher_log": str(paths["watcher_log"]),
        "rolling_log": str(paths["rolling_log"]),
        "extend24_log": str(paths["extend24_log"]),
        "gate_json": str(paths["gate_json"]),
        "after24_gate": str(paths["after24_gate"]),
        "scripts": {
            "collector": str(paths["collector_script"]),
            "watcher": str(paths["watcher_script"]),
            "rolling": str(paths["rolling_script"]),
            "extend24": str(paths["extend24_script"]),
        },
        "safety": "public CLOB/Gamma reads only; no private keys, signing, order submission, or cancellation",
    }
    manifest_path = Path(cfg.state_dir) / f"{safe}_background_latest.json"
    _write(manifest_path, json.dumps(_json_safe(manifest), indent=2, allow_nan=False) + "\n")
    return manifest


def _paths(out_dir: Path, safe: str) -> dict[str, Path]:
    return {
        "snapshot": out_dir / f"{safe}_snapshots.csv",
        "quotes": out_dir / f"{safe}_quotes.csv",
        "collector_manifest": out_dir / f"{safe}_manifest.json",
        "collector_log": out_dir / f"{safe}_collector.log",
        "watcher_log": out_dir / f"{safe}_watcher.log",
        "rolling_log": out_dir / f"{safe}_rolling_after6h.log",
        "extend24_log": out_dir / f"{safe}_extend_to24h_and_audit.log",
        "gate_json": out_dir / f"{safe}_gate_latest.json",
        "gate_md": out_dir / f"{safe}_gate_latest.md",
        "rescue_json": out_dir / f"{safe}_rescue_stress_latest.json",
        "rescue_md": out_dir / f"{safe}_rescue_stress_latest.md",
        "status_dir": out_dir / f"{safe}_status_latest",
        "history": out_dir / f"{safe}_status_history.csv",
        "rolling_dir": out_dir / "rolling_depth_after6h",
        "after24_status_dir": out_dir / f"{safe}_status_after24h",
        "after24_history": out_dir / f"{safe}_status_after24h_history.csv",
        "after24_rescue_json": out_dir / f"{safe}_rescue_stress_after24h.json",
        "after24_rescue_md": out_dir / f"{safe}_rescue_stress_after24h.md",
        "after24_gate": out_dir / f"{safe}_gate_after24h.json",
        "after24_gate_md": out_dir / f"{safe}_gate_after24h.md",
        "after24_rolling_dir": out_dir / "rolling_depth_after24h",
        "after24_shadow_dir": out_dir / f"{safe}_shadow_telemetry_after24h",
        "collector_script": out_dir / f"run_{safe}_collector.ps1",
        "watcher_script": out_dir / f"run_{safe}_watcher.ps1",
        "rolling_script": out_dir / f"run_{safe}_rolling_after6h.ps1",
        "extend24_script": out_dir / f"run_{safe}_extend_to24h_and_audit.ps1",
    }


def _collector_script(cfg: LaunchCandidateConfig, p: dict[str, Path]) -> str:
    return "\n".join(
        [
            "$ErrorActionPreference='Continue'",
            _vars(cfg, p),
            "Push-Location $Repo",
            (
                "& $Python scripts\\paper_replay.py --live --snapshot-out $Snapshot --out $Quotes "
                "--manifest-out $Manifest --iterations {iterations} --interval-seconds {interval_seconds} "
                "--gamma-base-url {gamma} --clob-base-url {clob} --event-limit {event_limit} "
                "--max-events {max_events} --include-clob-books --request-timeout-seconds {timeout} "
                "--sleep-between-book-requests-seconds {sleep} --initial-capital {capital} "
                "--quote-size {qsize} --quote-offset {offset} --safety-margin {margin} "
                "--active-capital-limit {active_cap} --excluded-categories {excluded} "
                "--min-reward-density-per-day {density} --recent-vol-window 6 "
                "--max-recent-vol {max_vol} --max-recent-jump {max_jump} "
                "--vol-quote-multiplier {vol_mult} --partial-rescue-max-residual-loss-usdc {residual} "
                ">> $Log 2>&1"
            ).format(
                iterations=cfg.iterations,
                interval_seconds=cfg.interval_seconds,
                gamma=cfg.gamma_base_url,
                clob=cfg.clob_base_url,
                event_limit=cfg.event_limit,
                max_events=cfg.max_events,
                timeout=cfg.request_timeout_seconds,
                sleep=cfg.sleep_between_book_requests_seconds,
                capital=cfg.initial_capital,
                qsize=cfg.quote_size,
                offset=cfg.quote_offset,
                margin=cfg.safety_margin,
                active_cap=cfg.active_capital_limit,
                excluded=cfg.excluded_categories,
                density=cfg.min_reward_density_per_day,
                max_vol=cfg.max_recent_vol,
                max_jump=cfg.max_recent_jump,
                vol_mult=cfg.vol_quote_multiplier,
                residual=cfg.partial_rescue_max_residual_loss_usdc,
            ),
            "Pop-Location",
            "",
        ]
    )


def _watcher_script(cfg: LaunchCandidateConfig, p: dict[str, Path], collector_pid: int) -> str:
    return "\n".join(
        [
            "$ErrorActionPreference='Continue'",
            _vars(cfg, p),
            f"$CollectorPid={int(collector_pid)}",
            "function Add-Log($Message) { \"$(Get-Date -Format o) $Message\" | Add-Content -LiteralPath $WatcherLog }",
            "function Refresh-Depth($Label) {",
            "  try {",
            "    if (-not (Test-Path $Snapshot) -or -not (Test-Path $Quotes)) { Add-Log \"refresh_skip label=$Label no_csv_yet\"; return }",
            "    Add-Log \"refresh_start label=$Label\"; Push-Location $Repo",
            _update_target_command(cfg, "$StatusDir", "$History", cfg.bootstrap_resamples_watch, 6, "$WatcherLog"),
            _rescue_command(cfg, "$RescueJson", "$RescueMd", "$WatcherLog"),
            _depth_command(cfg, "$(Join-Path $StatusDir 'target_status.json')", "$RescueJson", "$GateJson", "$GateMd", 6, cfg.min_quote_rows_6h, cfg.min_book_scenarios_6h, "$WatcherLog"),
            "    Pop-Location; Add-Log \"refresh_done label=$Label\"",
            "  } catch { Add-Log \"refresh_error label=$Label error=$($_.Exception.Message)\"; try { Pop-Location } catch {} }",
            "}",
            "Add-Log \"watcher_start collector_pid=$CollectorPid\"",
            "for ($i=0; $i -lt 80; $i++) { Refresh-Depth \"loop_$i\"; if ($CollectorPid -gt 0 -and -not (Get-Process -Id $CollectorPid -ErrorAction SilentlyContinue)) { break }; Start-Sleep -Seconds $IntervalSeconds }",
            "Refresh-Depth 'final'; Add-Log 'watcher_exit'",
            "",
        ]
    )


def _rolling_script(cfg: LaunchCandidateConfig, p: dict[str, Path], collector_pid: int) -> str:
    return "\n".join(
        [
            "$ErrorActionPreference='Continue'",
            _vars(cfg, p),
            f"$CollectorPid={int(collector_pid)}",
            "function Add-Log($Message) { \"$(Get-Date -Format o) $Message\" | Add-Content -LiteralPath $RollingLog }",
            "try {",
            "  Add-Log \"rolling_after6h_wait collector_pid=$CollectorPid\"",
            "  if ($CollectorPid -gt 0 -and (Get-Process -Id $CollectorPid -ErrorAction SilentlyContinue)) { Wait-Process -Id $CollectorPid }",
            "  Add-Log 'rolling_after6h_start'; Push-Location $Repo",
            _rolling_command(cfg, "$RollingDir", cfg.bootstrap_resamples_rolling, 6, cfg.min_quote_rows_6h, cfg.min_book_scenarios_6h, "$RollingLog"),
            "  Pop-Location; Add-Log 'rolling_after6h_done'",
            "} catch { Add-Log \"rolling_after6h_error error=$($_.Exception.Message)\"; try { Pop-Location } catch {} }",
            "",
        ]
    )


def _extend24_script(
    cfg: LaunchCandidateConfig,
    p: dict[str, Path],
    collector_pid: int,
    watcher_pid: int,
    rolling_pid: int,
) -> str:
    return "\n".join(
        [
            "$ErrorActionPreference='Continue'",
            _vars(cfg, p),
            f"$InitialCollectorProcessId={int(collector_pid)}; $InitialWatcherProcessId={int(watcher_pid)}; $InitialRollingProcessId={int(rolling_pid)}",
            "function Add-Log($Message) { \"$(Get-Date -Format o) $Message\" | Add-Content -LiteralPath $Extend24Log }",
            "function Wait-ForExit([int]$WaitProcessId,[string]$Label) { if ($WaitProcessId -le 0) { return }; while (Get-Process -Id $WaitProcessId -ErrorAction SilentlyContinue) { Add-Log \"wait label=$Label process_id=$WaitProcessId\"; Start-Sleep -Seconds 300 }; Add-Log \"done_wait label=$Label process_id=$WaitProcessId\" }",
            "Add-Log 'extend24_start safety=public_reads_only_no_keys_no_orders'",
            "Wait-ForExit $InitialCollectorProcessId 'initial_collector'; Wait-ForExit $InitialWatcherProcessId 'initial_watcher'; Wait-ForExit $InitialRollingProcessId 'initial_rolling6h'",
            "Push-Location $Repo; Add-Log 'extension_collector_start'",
            (
                "& $Python scripts\\paper_replay.py --live --snapshot-out $Snapshot --out $Quotes "
                "--manifest-out $Manifest --iterations {iterations} --interval-seconds {interval_seconds} "
                "--gamma-base-url {gamma} --clob-base-url {clob} --event-limit {event_limit} "
                "--max-events {max_events} --include-clob-books --request-timeout-seconds {timeout} "
                "--sleep-between-book-requests-seconds {sleep} --initial-capital {capital} "
                "--quote-size {qsize} --quote-offset {offset} --safety-margin {margin} "
                "--active-capital-limit {active_cap} --excluded-categories {excluded} "
                "--min-reward-density-per-day {density} --recent-vol-window 6 "
                "--max-recent-vol {max_vol} --max-recent-jump {max_jump} "
                "--vol-quote-multiplier {vol_mult} --partial-rescue-max-residual-loss-usdc {residual} "
                ">> $Extend24Log 2>&1"
            ).format(
                iterations=cfg.extension_iterations,
                interval_seconds=cfg.interval_seconds,
                gamma=cfg.gamma_base_url,
                clob=cfg.clob_base_url,
                event_limit=cfg.event_limit,
                max_events=cfg.max_events,
                timeout=cfg.request_timeout_seconds,
                sleep=cfg.sleep_between_book_requests_seconds,
                capital=cfg.initial_capital,
                qsize=cfg.quote_size,
                offset=cfg.quote_offset,
                margin=cfg.safety_margin,
                active_cap=cfg.active_capital_limit,
                excluded=cfg.excluded_categories,
                density=cfg.min_reward_density_per_day,
                max_vol=cfg.max_recent_vol,
                max_jump=cfg.max_recent_jump,
                vol_mult=cfg.vol_quote_multiplier,
                residual=cfg.partial_rescue_max_residual_loss_usdc,
            ),
            'Add-Log "extension_collector_exit code=$LASTEXITCODE"; Add-Log "after24_audit_start"',
            _update_target_command(cfg, "$After24StatusDir", "$After24History", cfg.bootstrap_resamples_after24, 24, "$Extend24Log"),
            _rescue_command(cfg, "$After24RescueJson", "$After24RescueMd", "$Extend24Log"),
            _depth_command(cfg, "$(Join-Path $After24StatusDir 'target_status.json')", "$After24RescueJson", "$After24GateJson", "$After24GateMd", 24, cfg.min_quote_rows_24h, cfg.min_book_scenarios_24h, "$Extend24Log"),
            _rolling_command(cfg, "$After24RollingDir", cfg.bootstrap_resamples_after24, 6, cfg.min_quote_rows_6h, cfg.min_book_scenarios_6h, "$Extend24Log"),
            "& $Python scripts\\shadow_telemetry.py --snapshots $Snapshot --quotes $Quotes --out-dir $After24ShadowDir --paid-reward-capture-rate 0.0 >> $Extend24Log 2>&1",
            "Pop-Location; Add-Log 'after24_audit_done'",
            "",
        ]
    )


def _vars(cfg: LaunchCandidateConfig, p: dict[str, Path]) -> str:
    pairs = {
        "Repo": cfg.repo,
        "Python": cfg.python,
        "Snapshot": str(p["snapshot"]),
        "Quotes": str(p["quotes"]),
        "Manifest": str(p["collector_manifest"]),
        "Log": str(p["collector_log"]),
        "WatcherLog": str(p["watcher_log"]),
        "RollingLog": str(p["rolling_log"]),
        "Extend24Log": str(p["extend24_log"]),
        "StatusDir": str(p["status_dir"]),
        "History": str(p["history"]),
        "RescueJson": str(p["rescue_json"]),
        "RescueMd": str(p["rescue_md"]),
        "GateJson": str(p["gate_json"]),
        "GateMd": str(p["gate_md"]),
        "RollingDir": str(p["rolling_dir"]),
        "After24StatusDir": str(p["after24_status_dir"]),
        "After24History": str(p["after24_history"]),
        "After24RescueJson": str(p["after24_rescue_json"]),
        "After24RescueMd": str(p["after24_rescue_md"]),
        "After24GateJson": str(p["after24_gate"]),
        "After24GateMd": str(p["after24_gate_md"]),
        "After24RollingDir": str(p["after24_rolling_dir"]),
        "After24ShadowDir": str(p["after24_shadow_dir"]),
    }
    lines = [f"${key}={_ps_quote(value)}" for key, value in pairs.items()]
    lines.append(f"$IntervalSeconds={int(cfg.interval_seconds)}")
    return "; ".join(lines)


def _update_target_command(
    cfg: LaunchCandidateConfig,
    out_dir: str,
    history: str,
    resamples: int,
    min_hours: int,
    log_var: str,
) -> str:
    return (
        f"    & $Python scripts\\update_target_status.py --snapshots $Snapshot --quotes $Quotes --out-dir {out_dir} "
        f"--history-csv {history} --initial-capital {cfg.initial_capital} --target-monthly {cfg.target_monthly} "
        f"--reward-to-loss-haircut {cfg.reward_to_loss_haircut} --min-observation-hours {min_hours} "
        f"--min-unique-markets {cfg.min_unique_markets} --max-active-pair-notional {cfg.max_active_pair_notional} "
        f"--max-pending-quote-rate {cfg.max_pending_quote_rate} --bootstrap-resamples {resamples} "
        f"--bootstrap-seed {cfg.bootstrap_seed} --bootstrap-block-size {cfg.bootstrap_block_size} "
        f"--bootstrap-capture-rate {cfg.required_capture_rate} --bootstrap-capture-rates '0.25,0.35,0.4,0.5,0.75,1.0' "
        f"--bootstrap-min-target-margin {cfg.min_target_margin} >> {log_var} 2>&1"
    )


def _rescue_command(cfg: LaunchCandidateConfig, out_json: str, out_md: str, log_var: str) -> str:
    return (
        f"    & $Python scripts\\rescue_stress.py --quotes $Quotes --out {out_json} --markdown-out {out_md} "
        f"--initial-capital {cfg.initial_capital} --require-taker-residual-loss "
        f"--max-latest-taker-residual-loss-fraction {cfg.max_latest_taker_residual_loss_fraction} "
        f"--min-taker-rescue-depth-fraction {cfg.min_taker_rescue_depth_fraction} "
        f"--taker-rescue-min-pair-edge-per-share {cfg.taker_rescue_min_pair_edge_per_share} >> {log_var} 2>&1"
    )


def _depth_command(
    cfg: LaunchCandidateConfig,
    target_status: str,
    rescue_json: str,
    out_json: str,
    out_md: str,
    min_hours: int,
    min_rows: int,
    min_scenarios: int,
    log_var: str,
) -> str:
    return (
        f"    & $Python scripts\\depth_gate.py --target-status {target_status} --rescue-stress {rescue_json} "
        f"--out {out_json} --markdown-out {out_md} --target-monthly {cfg.target_monthly} "
        f"--required-capture-rate {cfg.required_capture_rate} --min-observation-hours {min_hours} "
        f"--min-quote-rows {min_rows} --min-unique-markets {cfg.min_unique_markets} "
        f"--min-book-scenarios {min_scenarios} --min-taker-rescue-feasible-rate {cfg.min_taker_rescue_feasible_rate} "
        f"--min-taker-rescue-depth-fraction {cfg.min_taker_rescue_depth_fraction} "
        f"--min-taker-rescue-pair-edge-per-share {cfg.taker_rescue_min_pair_edge_per_share} "
        f"--allow-partial-taker-rescue --max-latest-taker-residual-loss-fraction {cfg.max_latest_taker_residual_loss_fraction} "
        f">> {log_var} 2>&1"
    )


def _rolling_command(
    cfg: LaunchCandidateConfig,
    out_dir: str,
    resamples: int,
    min_hours: int,
    min_rows: int,
    min_scenarios: int,
    log_var: str,
) -> str:
    return (
        f"    & $Python scripts\\rolling_depth_windows.py --snapshots $Snapshot --quotes $Quotes --out-dir {out_dir} "
        f"--initial-capital {cfg.initial_capital} --target-monthly {cfg.target_monthly} "
        f"--reward-to-loss-haircut {cfg.reward_to_loss_haircut} --window-hours 6 --step-hours 1 "
        f"--min-window-hours {min_hours} --min-quote-rows {min_rows} --min-unique-markets {cfg.min_unique_markets} "
        f"--max-active-pair-notional {cfg.max_active_pair_notional} --max-pending-quote-rate {cfg.max_pending_quote_rate} "
        f"--required-capture-rate {cfg.required_capture_rate} --bootstrap-resamples {resamples} "
        f"--bootstrap-seed {cfg.bootstrap_seed} --bootstrap-block-size {cfg.bootstrap_block_size} "
        f"--min-book-scenarios {min_scenarios} --min-taker-rescue-feasible-rate {cfg.min_taker_rescue_feasible_rate} "
        f"--min-taker-rescue-depth-fraction {cfg.min_taker_rescue_depth_fraction} "
        f"--taker-rescue-min-pair-edge-per-share {cfg.taker_rescue_min_pair_edge_per_share} "
        f"--allow-partial-taker-rescue --max-latest-taker-residual-loss-fraction {cfg.max_latest_taker_residual_loss_fraction} "
        f">> {log_var} 2>&1"
    )


def _start_powershell(script: Path) -> int:
    command = (
        "$p=Start-Process -FilePath 'powershell.exe' "
        f"-ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',{_ps_quote(str(script))}) "
        "-WindowStyle Hidden -PassThru; $p.Id"
    )
    out = subprocess.check_output(["powershell", "-NoProfile", "-Command", command], text=True)
    return int(out.strip().splitlines()[-1])


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip()).strip("._-") or "candidate"


def _ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if hasattr(value, "item"):
        return _json_safe(value.item())
    return value


if __name__ == "__main__":
    main()
