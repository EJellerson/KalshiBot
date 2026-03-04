from __future__ import annotations

from collections import Counter
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response

from weather_arb import config
import pyarrow.parquet as pq

from weather_arb.analytics.monitoring import (
    data_inventory_snapshot,
    operational_alerts_snapshot,
    recent_events_snapshot,
    train_gate_snapshot,
    variant_operational_alerts_snapshot,
)
from weather_arb.governance.live_routing import live_routing_status
from weather_arb.strategies.runtime import (
    compute_portfolio_leaderboard,
    strategies_summary_snapshot,
    strategy_monitoring_snapshot,
)
from weather_arb.utils.io_utils import safe_read_json


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parquet_row_count(path: Path) -> int:
    try:
        return int(pq.ParquetFile(path).metadata.num_rows)
    except Exception:
        return 0


def _chart_data() -> dict[str, Any]:
    """Build all three chart series for the Overview tab."""
    payload = safe_read_json(config.PAPER_METRICS_DAILY_PATH) or {}
    by_day_payload: dict[str, Any] = dict(payload.get("by_day", {}))
    by_day_totals = {
        day_key: float((metrics or {}).get("pnl_dollars", 0.0) or 0.0)
        for day_key, metrics in by_day_payload.items()
    }
    starting_equity = float(config.PAPER_ACCOUNT_SIZE)

    equity_series: list[dict[str, Any]] = []
    pnl_series: list[dict[str, Any]] = []

    if by_day_totals:
        running = starting_equity
        for day_key in sorted(by_day_totals.keys()):
            day_pnl = float(by_day_totals.get(day_key, 0.0) or 0.0)
            running += day_pnl
            equity_series.append({"date": day_key, "equity": round(running, 2)})
            pnl_series.append({"date": day_key, "pnl": round(day_pnl, 2)})

    signal_counts: dict[str, int] = {}
    for strategy_id in config.WEATHER_STRATEGY_IDS:
        for path in sorted(config.strategy_signals_dir(strategy_id).glob("signals_*.parquet")):
            date_part = path.stem.replace("signals_", "", 1)
            signal_counts[date_part] = int(signal_counts.get(date_part, 0) + _parquet_row_count(path))
    signal_series = [{"date": day, "count": signal_counts[day]} for day in sorted(signal_counts.keys())]
    data_as_of = sorted(by_day_totals.keys())[-1] if by_day_totals else None

    return {
        "equity_curve": equity_series,
        "daily_pnl": pnl_series,
        "signal_count": signal_series,
        "source": "governance_paper_account",
        "data_as_of": data_as_of,
    }


def _latest_day_metrics(path: Path) -> dict[str, Any]:
    payload = safe_read_json(path) or {}
    by_day = dict(payload.get("by_day", {}))
    if not by_day:
        return {"date_key": None, "metrics": {}}
    latest_key = sorted(by_day.keys())[-1]
    return {"date_key": latest_key, "metrics": dict(by_day[latest_key])}


def _tail_parquet_rows(path: Path, limit: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        frame = pd.read_parquet(path)
    except Exception:
        return []
    if frame.empty:
        return []
    tail = frame.tail(max(1, int(limit)))
    return tail.to_dict(orient="records")


def _tail_json_events(path: Path, limit: int) -> list[dict[str, Any]]:
    payload = safe_read_json(path) or {}
    events = payload.get("events", [])
    if not isinstance(events, list):
        return []
    return [dict(x) for x in events[-max(1, int(limit)) :]]


def _latest_signal_rows(limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for strategy_id in config.WEATHER_STRATEGY_IDS:
        files = sorted(config.strategy_signals_dir(strategy_id).glob("signals_*.parquet"))
        for path in files[-3:]:
            rows.extend(_tail_parquet_rows(path, limit))
    rows.sort(key=lambda r: str(r.get("generated_at_utc") or r.get("ts_utc") or ""), reverse=True)
    return rows[:limit]


def _latest_quotes_rows(limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for strategy_id in config.WEATHER_STRATEGY_IDS:
        files = sorted(config.strategy_quotes_dir(strategy_id).glob("quotes_*.parquet"))
        for path in files[-3:]:
            rows.extend(_tail_parquet_rows(path, limit))
    rows.sort(key=lambda r: str(r.get("ts_utc") or ""), reverse=True)
    return rows[:limit]


def _contracts_rows(limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for strategy_id in config.WEATHER_STRATEGY_IDS:
        strategy_rows = _tail_parquet_rows(config.strategy_contracts_active_path(strategy_id), limit)
        for row in strategy_rows:
            if "strategy_id" not in row:
                row["strategy_id"] = strategy_id
        rows.extend(strategy_rows)
    if rows:
        rows.sort(
            key=lambda r: str(
                r.get("ts_utc")
                or r.get("discovered_at_utc")
                or r.get("settlement_ts_utc")
                or r.get("ticker")
                or ""
            ),
            reverse=True,
        )
        return rows[:limit]
    return _tail_parquet_rows(config.CONTRACTS_ACTIVE_PATH, limit)


def _state_for_mode(mode: Literal["paper", "live"]) -> dict[str, Any]:
    if mode == "paper":
        return safe_read_json(config.PAPER_POSITIONS_PATH) or {}
    return safe_read_json(config.LIVE_POSITIONS_PATH) or {}


def _status_counts(registry: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in registry.get("models", []):
        status = str(row.get("status", "unknown"))
        out[status] = int(out.get(status, 0)) + 1
    return out


def create_app() -> FastAPI:
    app = FastAPI(title="Kalshi Weather Dashboard", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "ts_utc": _now_iso()}

    @app.get("/api/summary")
    def summary() -> dict[str, Any]:
        registry = safe_read_json(config.MODEL_REGISTRY_PATH) or {}
        lifecycle = safe_read_json(config.LIFECYCLE_STATE_PATH) or {}
        paper_state = _state_for_mode("paper")
        live_state = _state_for_mode("live")
        paper_latest = _latest_day_metrics(config.PAPER_METRICS_DAILY_PATH)
        live_latest = _latest_day_metrics(config.LIVE_METRICS_DAILY_PATH)
        latest_signals = _latest_signal_rows(limit=1)
        train_snapshot = train_gate_snapshot()
        inventory = data_inventory_snapshot()
        events = recent_events_snapshot(limit=500)
        alerts = operational_alerts_snapshot(train_snapshot, inventory, events)
        live_routing = live_routing_status()

        return {
            "ts_utc": _now_iso(),
            "registry": {
                "models_total": len(registry.get("models", [])),
                "champion_global": ((registry.get("champion_by_scope") or {}).get("global")),
                "status_counts": _status_counts(registry),
            },
            "lifecycle": lifecycle,
            "paper": {
                "equity": float(paper_state.get("equity", config.PAPER_ACCOUNT_SIZE) or config.PAPER_ACCOUNT_SIZE),
                "open_positions": len(paper_state.get("open_positions", [])),
                "closed_positions": len(paper_state.get("closed_positions", [])),
                "latest_metrics": paper_latest,
            },
            "live": {
                "equity": float(live_state.get("equity", config.LIVE_STARTING_EQUITY) or config.LIVE_STARTING_EQUITY),
                "open_positions": len(live_state.get("open_positions", [])),
                "closed_positions": len(live_state.get("closed_positions", [])),
                "latest_metrics": live_latest,
                "allow_live_trading": bool(live_routing.get("enabled", False)),
                "routing_reason": live_routing.get("reason"),
                "routing_source": live_routing.get("source"),
                "routing_champion": live_routing.get("champion_id"),
                "manual_live_override": bool(live_routing.get("manual_enabled", False)),
                "auto_live_enabled": bool(live_routing.get("auto_enabled", False)),
                "auto_live_toggle_enabled": bool(live_routing.get("auto_toggle_enabled", False)),
                "limits": dict(live_state.get("live_limits", {})),
            },
            "latest_signal": latest_signals[0] if latest_signals else None,
            "train_progress": {
                "pass": bool((train_snapshot.get("gate") or {}).get("pass", False)),
                "max_days_remaining": int(train_snapshot.get("max_days_remaining", 0) or 0),
                "estimated_ready_date_local": train_snapshot.get("estimated_ready_date_local"),
            },
            "inventory": {
                "today_rows": inventory.get("today_rows", {}),
                "contracts_active": inventory.get("contracts_active", 0),
            },
            "ops": {
                "status": alerts.get("status"),
                "alert_counts": alerts.get("counts", {}),
                "alerts_total": len(alerts.get("alerts", [])),
            },
            "strategies": strategies_summary_snapshot(),
            "portfolio": {
                "leaderboard": compute_portfolio_leaderboard(),
                "champion": safe_read_json(config.CHAMPION_STATE_PATH) or {},
            },
        }

    @app.get("/api/positions")
    def positions(
        mode: Literal["paper", "live"] = Query("paper"),
        status: Literal["open", "closed"] = Query("open"),
        limit: int = Query(100, ge=1, le=2000),
    ) -> dict[str, Any]:
        state = _state_for_mode(mode)
        key = "open_positions" if status == "open" else "closed_positions"
        rows = state.get(key, [])
        if not isinstance(rows, list):
            rows = []
        tail = rows[-limit:]
        return {"mode": mode, "status": status, "count": len(rows), "rows": tail}

    @app.get("/api/signals")
    def signals(limit: int = Query(200, ge=1, le=2000)) -> dict[str, Any]:
        rows = _latest_signal_rows(limit)
        return {"count": len(rows), "rows": rows}

    @app.get("/api/quotes")
    def quotes(limit: int = Query(200, ge=1, le=2000)) -> dict[str, Any]:
        rows = _latest_quotes_rows(limit)
        return {"count": len(rows), "rows": rows}

    @app.get("/api/contracts")
    def contracts(limit: int = Query(200, ge=1, le=2000)) -> dict[str, Any]:
        rows = _contracts_rows(limit)
        return {"count": len(rows), "rows": rows}

    @app.get("/api/governance/events")
    def governance_events(limit: int = Query(200, ge=1, le=5000)) -> dict[str, Any]:
        rows = _tail_json_events(config.GOVERNANCE_LOG_PATH, limit)
        return {"count": len(rows), "rows": rows}

    @app.get("/api/monitoring")
    def monitoring() -> dict[str, Any]:
        train = train_gate_snapshot()
        inventory = data_inventory_snapshot()
        events = recent_events_snapshot(limit=500)
        global_alerts = operational_alerts_snapshot(train, inventory, events)
        variant_alerts = variant_operational_alerts_snapshot()
        live_routing = live_routing_status()

        actionable_all = [
            *(list(global_alerts.get("alerts") or [])),
            *(list(variant_alerts.get("alerts") or [])),
        ]
        dedup_actionable: dict[str, dict[str, Any]] = {}
        for row in actionable_all:
            code = str((row or {}).get("code", "")).strip()
            if code and code not in dedup_actionable:
                dedup_actionable[code] = dict(row)
        actionable_rows = list(dedup_actionable.values())

        severity_order = {"info": 0, "warn": 1, "critical": 2}
        actionable_status = "ok"
        if actionable_rows:
            worst = max(actionable_rows, key=lambda a: severity_order.get(str(a.get("severity", "warn")), 0))
            actionable_status = str(worst.get("severity") or "warn")
        actionable_counts = dict(Counter(str(a.get("severity", "warn")) for a in actionable_rows))

        info_all = [
            *(list(global_alerts.get("info") or [])),
            *(list(variant_alerts.get("info") or [])),
        ]
        dedup_info: dict[str, dict[str, Any]] = {}
        for row in info_all:
            code = str((row or {}).get("code", "")).strip()
            if code and code not in dedup_info:
                dedup_info[code] = dict(row)
        info_rows = list(dedup_info.values())

        suppression_counter = Counter()
        suppression_counter.update(dict((global_alerts.get("suppressed") or {}).get("by_reason") or {}))
        suppression_counter.update(dict((variant_alerts.get("suppressed") or {}).get("by_reason") or {}))

        return {
            "ts_utc": _now_iso(),
            "train_gate": train,
            "inventory": inventory,
            "events": events,
            "alerts": {
                "status": actionable_status,
                "alerts": actionable_rows,
                "counts": actionable_counts,
            },
            # Backward-compatible alias.
            "variant_alerts": variant_alerts,
            "variant_alerts_actionable": variant_alerts,
            "variant_alerts_info": {
                "status": "info" if info_rows else "ok",
                "alerts": info_rows,
                "count": len(info_rows),
            },
            "alert_suppression": {
                "count": int(sum(suppression_counter.values())),
                "by_reason": dict(suppression_counter),
                "global": dict(global_alerts.get("suppressed") or {}),
                "variant": dict(variant_alerts.get("suppressed") or {}),
            },
            "info_alerts": {
                "status": "info" if info_rows else "ok",
                "alerts": info_rows,
                "count": len(info_rows),
            },
            "strategies": strategies_summary_snapshot(),
            "runtime": {
                "scheduler_interval_minutes": config.SCHEDULER_INTERVAL_MINUTES,
                "obs_sync_interval_minutes": config.OBS_SYNC_INTERVAL_MINUTES,
                "allow_live_trading": bool(live_routing.get("enabled", False)),
                "live_routing": live_routing,
            },
        }

    @app.get("/api/strategies/summary")
    def strategies_summary() -> dict[str, Any]:
        return strategies_summary_snapshot()

    @app.get("/api/strategies/{strategy_id}/monitoring")
    def strategy_monitoring(strategy_id: str) -> dict[str, Any]:
        if strategy_id not in set(config.WEATHER_STRATEGY_IDS):
            return {"error": "unknown strategy", "strategy_id": strategy_id}
        return strategy_monitoring_snapshot(strategy_id)

    @app.get("/api/portfolio/leaderboard")
    def portfolio_leaderboard() -> dict[str, Any]:
        return compute_portfolio_leaderboard()

    @app.get("/api/portfolio/champion")
    def portfolio_champion() -> dict[str, Any]:
        leaderboard = compute_portfolio_leaderboard()
        state = safe_read_json(config.CHAMPION_STATE_PATH) or {}
        return {
            "ts_utc": _now_iso(),
            "champion": state,
            "leaderboard_top": (leaderboard.get("rows") or [None])[0],
            "challenger": leaderboard.get("challenger"),
        }

    @app.get("/api/charts")
    def charts() -> dict[str, Any]:
        data = _chart_data()
        data["ts_utc"] = _now_iso()
        return data

    @app.get("/api/raw/{name}")
    def raw_file(name: str) -> JSONResponse:
        mapping = {
            "registry": config.MODEL_REGISTRY_PATH,
            "lifecycle": config.LIFECYCLE_STATE_PATH,
            "paper_state": config.PAPER_POSITIONS_PATH,
            "live_state": config.LIVE_POSITIONS_PATH,
            "paper_metrics": config.PAPER_METRICS_DAILY_PATH,
            "live_metrics": config.LIVE_METRICS_DAILY_PATH,
            "governance_log": config.GOVERNANCE_LOG_PATH,
        }
        path = mapping.get(name)
        if not path:
            return JSONResponse({"error": "unknown resource"}, status_code=404)
        payload = safe_read_json(path)
        if payload is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(payload)

    static_dir = Path(__file__).resolve().parent / "static"

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/api")
    def api_index() -> dict[str, Any]:
        return {
            "endpoints": [
                "/api/summary",
                "/api/positions?mode=paper&status=open",
                "/api/positions?mode=live&status=closed",
                "/api/signals",
                "/api/quotes",
                "/api/contracts",
                "/api/governance/events",
                "/api/monitoring",
                "/api/strategies/summary",
                "/api/strategies/{id}/monitoring",
                "/api/portfolio/leaderboard",
                "/api/portfolio/champion",
                "/api/charts",
                "/api/ops/status",
                "/api/ops/restart",
                "/api/ops/shutdown",
                "/api/ops/start-scheduler",
            ]
        }

    # ── Service management ────────────────────────────────────

    _SCHEDULER_LABEL = "com.ericjellerson.kalshi-weather.scheduler"
    _DASHBOARD_LABEL = "com.ericjellerson.kalshi-weather.dashboard"
    _SCHEDULER_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{_SCHEDULER_LABEL}.plist"

    def _gui_prefix() -> str:
        return f"gui/{os.getuid()}"

    def _run_launchctl(args: list[str], *, timeout: int = 10) -> tuple[bool, str, int | None]:
        try:
            result = subprocess.run(args, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, "timeout", None
        except OSError as exc:
            return False, str(exc), None

        if result.returncode == 0:
            return True, "", 0

        stderr = (result.stderr or b"").decode("utf-8", errors="ignore").strip()
        stdout = (result.stdout or b"").decode("utf-8", errors="ignore").strip()
        detail = stderr or stdout or f"return_code={result.returncode}"
        return False, detail, int(result.returncode)

    def _service_loaded(label: str) -> bool:
        try:
            r = subprocess.run(
                ["launchctl", "print", f"{_gui_prefix()}/{label}"],
                capture_output=True, timeout=5,
            )
            return r.returncode == 0
        except Exception:
            return False

    @app.get("/api/ops/status")
    def ops_status() -> dict[str, Any]:
        return {
            "scheduler": "loaded" if _service_loaded(_SCHEDULER_LABEL) else "stopped",
            "dashboard": "loaded" if _service_loaded(_DASHBOARD_LABEL) else "stopped",
        }

    @app.post("/api/ops/restart")
    def ops_restart() -> dict[str, Any]:
        """Restart both scheduler and dashboard via kickstart -k.

        The dashboard kickstart is delayed 1s so this response can be sent
        before the process is killed.
        """
        prefix = _gui_prefix()
        results: dict[str, str] = {}

        # Restart scheduler immediately
        try:
            subprocess.run(
                ["launchctl", "kickstart", "-k", f"{prefix}/{_SCHEDULER_LABEL}"],
                capture_output=True, timeout=10,
            )
            results["scheduler"] = "restarted"
        except Exception as exc:
            results["scheduler"] = f"error: {exc}"

        # Restart dashboard with a short delay so the HTTP response lands first
        try:
            subprocess.Popen(
                ["bash", "-c", f"sleep 1 && launchctl kickstart -k '{prefix}/{_DASHBOARD_LABEL}'"],
            )
            results["dashboard"] = "restarting"
        except Exception as exc:
            results["dashboard"] = f"error: {exc}"

        return {"ok": True, "action": "restart", "results": results}

    @app.post("/api/ops/shutdown")
    def ops_shutdown() -> dict[str, Any]:
        """Stop the scheduler (bootout) and restart the dashboard.

        The dashboard restart is delayed so the response can be sent first.
        """
        prefix = _gui_prefix()
        results: dict[str, str] = {}

        # Stop scheduler (fully unload from launchd)
        try:
            subprocess.run(
                ["launchctl", "bootout", f"{prefix}/{_SCHEDULER_LABEL}"],
                capture_output=True, timeout=10,
            )
            results["scheduler"] = "stopped"
        except Exception as exc:
            results["scheduler"] = f"error: {exc}"

        # Restart dashboard so it picks up any code changes
        try:
            subprocess.Popen(
                ["bash", "-c", f"sleep 1 && launchctl kickstart -k '{prefix}/{_DASHBOARD_LABEL}'"],
            )
            results["dashboard"] = "restarting"
        except Exception as exc:
            results["dashboard"] = f"error: {exc}"

        return {"ok": True, "action": "shutdown", "results": results}

    @app.post("/api/ops/start-scheduler")
    def ops_start_scheduler() -> dict[str, Any]:
        """Re-bootstrap and start the scheduler after a shutdown."""
        prefix = _gui_prefix()
        plist = str(_SCHEDULER_PLIST)

        steps = [
            ("bootstrap", ["launchctl", "bootstrap", prefix, plist]),
            ("enable", ["launchctl", "enable", f"{prefix}/{_SCHEDULER_LABEL}"]),
            ("kickstart", ["launchctl", "kickstart", "-k", f"{prefix}/{_SCHEDULER_LABEL}"]),
        ]
        for step_name, cmd in steps:
            ok, detail, rc = _run_launchctl(cmd, timeout=10)
            if not ok:
                return {
                    "ok": False,
                    "action": "start-scheduler",
                    "step": step_name,
                    "error": detail,
                    "return_code": rc,
                }
        return {"ok": True, "action": "start-scheduler", "message": "Scheduler started"}

    return app
