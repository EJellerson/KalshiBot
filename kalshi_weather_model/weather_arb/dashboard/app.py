from __future__ import annotations

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
    by_day: dict[str, Any] = payload.get("by_day", {})

    equity_series: list[dict[str, Any]] = []
    pnl_series: list[dict[str, Any]] = []

    if by_day:
        running = float(config.PAPER_ACCOUNT_SIZE)
        for day_key in sorted(by_day.keys()):
            day_pnl = float(by_day[day_key].get("pnl_dollars", 0) or 0)
            running += day_pnl
            equity_series.append({"date": day_key, "equity": round(running, 2)})
            pnl_series.append({"date": day_key, "pnl": round(day_pnl, 2)})

    signal_series: list[dict[str, Any]] = []
    for path in sorted(config.SIGNALS_DIR.glob("signals_*.parquet")):
        date_part = path.stem.replace("signals_", "", 1)
        signal_series.append({"date": date_part, "count": _parquet_row_count(path)})

    return {
        "equity_curve": equity_series,
        "daily_pnl": pnl_series,
        "signal_count": signal_series,
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
    files = sorted(config.SIGNALS_DIR.glob("signals_*.parquet"))
    if not files:
        return []
    return _tail_parquet_rows(files[-1], limit)


def _latest_quotes_rows(limit: int) -> list[dict[str, Any]]:
    files = sorted(config.MARKET_QUOTES_DIR.glob("quotes_*.parquet"))
    if not files:
        return []
    return _tail_parquet_rows(files[-1], limit)


def _contracts_rows(limit: int) -> list[dict[str, Any]]:
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
                "allow_live_trading": bool(config.ALLOW_LIVE_TRADING),
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
        alerts = operational_alerts_snapshot(train, inventory, events)
        return {
            "ts_utc": _now_iso(),
            "train_gate": train,
            "inventory": inventory,
            "events": events,
            "alerts": alerts,
            "runtime": {
                "scheduler_interval_minutes": config.SCHEDULER_INTERVAL_MINUTES,
                "obs_sync_interval_minutes": config.OBS_SYNC_INTERVAL_MINUTES,
                "allow_live_trading": bool(config.ALLOW_LIVE_TRADING),
            },
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

        # Bootstrap (re-load the service definition)
        subprocess.run(
            ["launchctl", "bootstrap", prefix, plist],
            capture_output=True, timeout=10,
        )
        # Enable
        subprocess.run(
            ["launchctl", "enable", f"{prefix}/{_SCHEDULER_LABEL}"],
            capture_output=True, timeout=10,
        )
        # Kickstart
        r = subprocess.run(
            ["launchctl", "kickstart", "-k", f"{prefix}/{_SCHEDULER_LABEL}"],
            capture_output=True, timeout=10,
        )

        ok = r.returncode == 0
        return {
            "ok": ok,
            "action": "start-scheduler",
            "message": "Scheduler started" if ok else f"Failed (rc={r.returncode})",
        }

    return app
