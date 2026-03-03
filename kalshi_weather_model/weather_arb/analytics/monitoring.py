from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pyarrow.parquet as pq

from weather_arb import config
from weather_arb.eval.train_gate import evaluate_train_gate
from weather_arb.utils.io_utils import safe_read_json


def read_all_parquet_rows(dir_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for file_path in sorted(dir_path.glob("*.parquet")):
        try:
            frame = pd.read_parquet(file_path)
        except Exception:
            continue
        rows.extend(frame.to_dict(orient="records"))
    return rows


def _local_today(tz_name: str) -> date:
    return datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name)).date()


def _parse_iso(ts_value: Any) -> datetime | None:
    if not ts_value:
        return None
    raw = str(ts_value).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _minutes_since(ts_value: Any, now_utc: datetime | None = None) -> float | None:
    parsed = _parse_iso(ts_value)
    if parsed is None:
        return None
    now = now_utc or datetime.now(timezone.utc)
    delta = now - parsed
    return max(0.0, delta.total_seconds() / 60.0)


def build_train_city_stats(observation_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    window_days = int(config.TRAIN_MIN_OBSERVATIONS_PER_CITY)
    today = _local_today(config.SCHEDULER_TZ)
    expected = {
        (today - timedelta(days=i)).isoformat()
        for i in range(window_days)
    }

    city_to_days_total: dict[str, set[str]] = {city: set() for city in config.CITIES}
    for row in observation_rows:
        city = str(row.get("city", "")).strip()
        day_key = str(row.get("obs_date_local", "")).strip()
        if city not in city_to_days_total or not day_key:
            continue
        city_to_days_total[city].add(day_key)

    out: dict[str, dict[str, Any]] = {}
    for city in config.CITIES:
        total_days = city_to_days_total[city]
        in_window = total_days.intersection(expected)
        obs_count = len(in_window)
        missing_days = max(window_days - obs_count, 0)
        missing_pct = (missing_days / window_days) if window_days > 0 else 1.0
        out[city] = {
            "observations": obs_count,
            "observations_total": len(total_days),
            "missing_days_window": missing_days,
            "missing_pct": missing_pct,
            "window_required_days": window_days,
            "coverage_pct": 1.0 - missing_pct,
        }
    return out


def train_gate_snapshot() -> dict[str, Any]:
    observation_rows = read_all_parquet_rows(config.OBSERVATIONS_DIR)
    city_stats = build_train_city_stats(observation_rows)
    eval_input = {
        city: {
            "observations": int(stats["observations"]),
            "missing_pct": float(stats["missing_pct"]),
        }
        for city, stats in city_stats.items()
    }
    gate = evaluate_train_gate(eval_input)
    remaining = {
        city: max(config.TRAIN_MIN_OBSERVATIONS_PER_CITY - int(stats["observations"]), 0)
        for city, stats in city_stats.items()
    }
    max_days_remaining = max(remaining.values()) if remaining else 0
    ready_date_local = (_local_today(config.SCHEDULER_TZ) + timedelta(days=max_days_remaining)).isoformat()
    return {
        "gate": gate,
        "city_stats": city_stats,
        "days_remaining_by_city": remaining,
        "min_days_collected": min(int(s["observations"]) for s in city_stats.values()) if city_stats else 0,
        "max_days_remaining": max_days_remaining,
        "estimated_ready_date_local": ready_date_local,
    }


def _parquet_rows_quick(path: Path) -> int:
    try:
        return int(pq.ParquetFile(path).metadata.num_rows)
    except Exception:
        return 0


def parquet_inventory(dir_path: Path, prefix: str = "") -> dict[str, Any]:
    files = sorted(dir_path.glob(f"{prefix}*.parquet")) if prefix else sorted(dir_path.glob("*.parquet"))
    total_rows = 0
    for p in files:
        total_rows += _parquet_rows_quick(p)
    latest = max(files, key=lambda p: p.stat().st_mtime) if files else None
    return {
        "files": len(files),
        "rows": total_rows,
        "latest_file": latest.name if latest else None,
        "latest_mtime_utc": (
            datetime.fromtimestamp(latest.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
            if latest
            else None
        ),
    }


def today_parquet_rows(dir_path: Path, prefix: str, tz_name: str) -> int:
    local_day_key = _local_today(tz_name).isoformat()
    utc_day_key = datetime.now(timezone.utc).date().isoformat()
    for day_key in [local_day_key, utc_day_key]:
        path = dir_path / f"{prefix}_{day_key}.parquet"
        if path.exists():
            return _parquet_rows_quick(path)
    return 0


def _stream_freshness_thresholds() -> dict[str, float]:
    return {
        "forecasts": float(max(config.SCHEDULER_INTERVAL_MINUTES * 3, 45)),
        "observations": float(max(config.OBS_SYNC_INTERVAL_MINUTES * 2 + 10, 130)),
        "quotes": float(max(config.SCHEDULER_INTERVAL_MINUTES * 3, 45)),
        "signals": float(max(config.SCHEDULER_INTERVAL_MINUTES * 3, 45)),
    }


def extract_observation_unique_days() -> dict[str, int]:
    rows = read_all_parquet_rows(config.OBSERVATIONS_DIR)
    city_to_days: dict[str, set[str]] = {city: set() for city in config.CITIES}
    for row in rows:
        city = str(row.get("city", "")).strip()
        day_key = str(row.get("obs_date_local", "")).strip()
        if city in city_to_days and day_key:
            city_to_days[city].add(day_key)
    return {city: len(days) for city, days in city_to_days.items()}


def data_inventory_snapshot() -> dict[str, Any]:
    now_utc = datetime.now(timezone.utc)
    inventory = {
        "forecasts": parquet_inventory(config.FORECAST_SNAPSHOTS_DIR, prefix="forecast_"),
        "observations": parquet_inventory(config.OBSERVATIONS_DIR, prefix="obs_"),
        "quotes": parquet_inventory(config.MARKET_QUOTES_DIR, prefix="quotes_"),
        "signals": parquet_inventory(config.SIGNALS_DIR, prefix="signals_"),
    }
    inventory["today_rows"] = {
        "forecasts": today_parquet_rows(config.FORECAST_SNAPSHOTS_DIR, "forecast", config.SCHEDULER_TZ),
        "observations": today_parquet_rows(config.OBSERVATIONS_DIR, "obs", config.SCHEDULER_TZ),
        "quotes": today_parquet_rows(config.MARKET_QUOTES_DIR, "quotes", config.SCHEDULER_TZ),
        "signals": today_parquet_rows(config.SIGNALS_DIR, "signals", config.SCHEDULER_TZ),
    }
    inventory["observation_unique_days_by_city"] = extract_observation_unique_days()

    try:
        active_contracts = int(_parquet_rows_quick(config.CONTRACTS_ACTIVE_PATH))
    except Exception:
        active_contracts = 0
    inventory["contracts_active"] = active_contracts
    inventory["contracts_latest_mtime_utc"] = (
        datetime.fromtimestamp(config.CONTRACTS_ACTIVE_PATH.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
        if config.CONTRACTS_ACTIVE_PATH.exists()
        else None
    )
    inventory["contracts_age_minutes"] = _minutes_since(inventory["contracts_latest_mtime_utc"], now_utc)

    thresholds = _stream_freshness_thresholds()
    stream_age_minutes: dict[str, float | None] = {}
    stale_streams: list[str] = []
    for stream in ["forecasts", "observations", "quotes", "signals"]:
        age = _minutes_since((inventory.get(stream) or {}).get("latest_mtime_utc"), now_utc)
        stream_age_minutes[stream] = age
        threshold = thresholds.get(stream)
        if age is not None and threshold is not None and age > threshold:
            stale_streams.append(stream)

    inventory["stream_age_minutes"] = stream_age_minutes
    inventory["freshness_threshold_minutes"] = thresholds
    inventory["stale_streams"] = stale_streams
    return inventory


def recent_events_snapshot(limit: int = 200) -> dict[str, Any]:
    now_utc = datetime.now(timezone.utc)
    payload = safe_read_json(config.GOVERNANCE_LOG_PATH) or {}
    events = payload.get("events", [])
    if not isinstance(events, list):
        events = []
    tail = [dict(e) for e in events[-max(1, int(limit)) :]]
    counts = Counter(str(e.get("event", "unknown")) for e in tail)
    latest_by_event: dict[str, dict[str, Any]] = {}
    for row in reversed(tail):
        key = str(row.get("event", "unknown"))
        if key not in latest_by_event:
            latest_by_event[key] = row
    minutes_since_by_event = {
        event: _minutes_since(row.get("ts"), now_utc)
        for event, row in latest_by_event.items()
    }
    latest_ts = max(
        (_parse_iso(row.get("ts")) for row in tail),
        default=None,
    )
    return {
        "count": len(tail),
        "counts": dict(counts),
        "latest_by_event": latest_by_event,
        "minutes_since_by_event": minutes_since_by_event,
        "latest_event_ts_utc": latest_ts.isoformat(timespec="seconds") if latest_ts else None,
    }


def operational_alerts_snapshot(
    train_snapshot: dict[str, Any],
    inventory_snapshot: dict[str, Any],
    events_snapshot: dict[str, Any],
) -> dict[str, Any]:
    alerts: list[dict[str, Any]] = []

    def add_alert(severity: str, code: str, message: str, **extra: Any) -> None:
        alerts.append({"severity": severity, "code": code, "message": message, **extra})

    gate = dict(train_snapshot.get("gate") or {})
    if not bool(gate.get("pass", False)):
        max_days_remaining = int(train_snapshot.get("max_days_remaining", 0) or 0)
        eta_local = train_snapshot.get("estimated_ready_date_local")
        add_alert(
            "warn",
            "train_gate_blocked",
            f"Train gate is blocked; {max_days_remaining} day(s) remaining at current data coverage.",
            max_days_remaining=max_days_remaining,
            estimated_ready_date_local=eta_local,
        )

    for stream in list(inventory_snapshot.get("stale_streams") or []):
        age = (inventory_snapshot.get("stream_age_minutes") or {}).get(stream)
        threshold = (inventory_snapshot.get("freshness_threshold_minutes") or {}).get(stream)
        add_alert(
            "warn",
            f"stale_{stream}",
            f"{stream} stream is stale ({age:.1f}m > {threshold:.1f}m).",
            stream=stream,
            age_minutes=age,
            threshold_minutes=threshold,
        )

    contracts_active = int(inventory_snapshot.get("contracts_active", 0) or 0)
    if contracts_active <= 0:
        add_alert("critical", "no_contracts", "No active contracts discovered. Signal generation is effectively halted.")

    contracts_age = inventory_snapshot.get("contracts_age_minutes")
    contract_stale_limit = float(max(config.MARKET_DISCOVERY_MINUTES * 3, 180))
    if contracts_age is not None and float(contracts_age) > contract_stale_limit:
        add_alert(
            "warn",
            "stale_contract_map",
            f"Contract map is stale ({float(contracts_age):.1f}m > {contract_stale_limit:.1f}m).",
            age_minutes=contracts_age,
            threshold_minutes=contract_stale_limit,
        )

    minutes_since_by_event = dict(events_snapshot.get("minutes_since_by_event") or {})
    ingest_age = minutes_since_by_event.get("ingest_forecasts")
    ingest_threshold = float(max(config.SCHEDULER_INTERVAL_MINUTES * 3, 45))
    if ingest_age is None or float(ingest_age) > ingest_threshold:
        add_alert(
            "warn",
            "scheduler_ingest_gap",
            "No recent forecast ingest event within expected interval.",
            age_minutes=ingest_age,
            threshold_minutes=ingest_threshold,
        )

    obs_age = minutes_since_by_event.get("sync_observations")
    obs_threshold = float(max(config.OBS_SYNC_INTERVAL_MINUTES * 2 + 10, 130))
    if obs_age is None or float(obs_age) > obs_threshold:
        add_alert(
            "warn",
            "scheduler_observation_gap",
            "No recent observation sync event within expected interval.",
            age_minutes=obs_age,
            threshold_minutes=obs_threshold,
        )

    cycle_age_candidates = [
        x for x in [
            minutes_since_by_event.get("paper_cycle"),
            minutes_since_by_event.get("paper_cycle_data_only"),
        ] if x is not None
    ]
    cycle_age = min(cycle_age_candidates) if cycle_age_candidates else None
    cycle_threshold = float(max(config.SCHEDULER_INTERVAL_MINUTES * 3, 45))
    if cycle_age is None or float(cycle_age) > cycle_threshold:
        add_alert(
            "warn",
            "scheduler_cycle_gap",
            "No recent paper cycle event within expected interval.",
            age_minutes=cycle_age,
            threshold_minutes=cycle_threshold,
        )

    signals_today = int((inventory_snapshot.get("today_rows") or {}).get("signals", 0) or 0)
    signals_recent_age = (inventory_snapshot.get("stream_age_minutes") or {}).get("signals")
    signals_recent_threshold = float(max(config.SCHEDULER_INTERVAL_MINUTES * 3, 45))
    has_recent_signals = signals_recent_age is not None and float(signals_recent_age) <= signals_recent_threshold
    if signals_today == 0 and not has_recent_signals and contracts_active > 0:
        add_alert(
            "warn",
            "no_signals_today",
            "No signals generated today despite active contracts.",
        )

    severity_order = {"info": 0, "warn": 1, "critical": 2}
    status = "ok"
    if alerts:
        worst = max(alerts, key=lambda a: severity_order.get(str(a.get("severity")), 0))
        status = str(worst.get("severity") or "warn")

    return {
        "status": status,
        "alerts": alerts,
        "counts": dict(Counter(str(a.get("severity", "warn")) for a in alerts)),
    }
