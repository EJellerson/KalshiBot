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


def _strategy_stream_inventory(prefix: str) -> dict[str, Any]:
    files: list[Path] = []
    for strategy_id in config.WEATHER_STRATEGY_IDS:
        if prefix == "quotes":
            stream_dir = config.strategy_quotes_dir(strategy_id)
        elif prefix == "signals":
            stream_dir = config.strategy_signals_dir(strategy_id)
        else:
            continue
        files.extend(sorted(stream_dir.glob(f"{prefix}_*.parquet")))

    total_rows = sum(_parquet_rows_quick(p) for p in files)
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


def _strategy_today_rows(prefix: str, tz_name: str) -> int:
    total = 0
    for strategy_id in config.WEATHER_STRATEGY_IDS:
        if prefix == "quotes":
            stream_dir = config.strategy_quotes_dir(strategy_id)
        elif prefix == "signals":
            stream_dir = config.strategy_signals_dir(strategy_id)
        else:
            continue
        total += today_parquet_rows(stream_dir, prefix, tz_name)
    return total


def _merge_stream_inventory(legacy: dict[str, Any], strategy: dict[str, Any]) -> dict[str, Any]:
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for inv in (legacy, strategy):
        ts = _parse_iso(inv.get("latest_mtime_utc"))
        if ts is not None:
            candidates.append((ts, inv))

    latest_name = None
    latest_ts = None
    if candidates:
        latest_inv = max(candidates, key=lambda item: item[0])[1]
        latest_name = latest_inv.get("latest_file")
        latest_ts = latest_inv.get("latest_mtime_utc")

    return {
        "files": int(legacy.get("files", 0) or 0) + int(strategy.get("files", 0) or 0),
        "rows": int(legacy.get("rows", 0) or 0) + int(strategy.get("rows", 0) or 0),
        "latest_file": latest_name,
        "latest_mtime_utc": latest_ts,
    }


def contracts_active_inventory_snapshot(now_utc: datetime | None = None) -> dict[str, Any]:
    now = now_utc or datetime.now(timezone.utc)
    strategy_rows = 0
    latest_strategy_mtime: float | None = None

    for strategy_id in config.WEATHER_STRATEGY_IDS:
        path = config.strategy_contracts_active_path(strategy_id)
        if not path.exists():
            continue
        strategy_rows += int(_parquet_rows_quick(path))
        mtime = path.stat().st_mtime
        if latest_strategy_mtime is None or mtime > latest_strategy_mtime:
            latest_strategy_mtime = mtime

    if strategy_rows > 0 and latest_strategy_mtime is not None:
        latest_ts = datetime.fromtimestamp(latest_strategy_mtime, tz=timezone.utc).isoformat(timespec="seconds")
        return {
            "contracts_active": strategy_rows,
            "contracts_latest_mtime_utc": latest_ts,
            "contracts_age_minutes": _minutes_since(latest_ts, now),
            "contracts_source": "strategy_aggregate",
        }

    global_rows = int(_parquet_rows_quick(config.CONTRACTS_ACTIVE_PATH))
    latest_ts = (
        datetime.fromtimestamp(config.CONTRACTS_ACTIVE_PATH.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
        if config.CONTRACTS_ACTIVE_PATH.exists()
        else None
    )
    return {
        "contracts_active": global_rows,
        "contracts_latest_mtime_utc": latest_ts,
        "contracts_age_minutes": _minutes_since(latest_ts, now),
        "contracts_source": "global_fallback",
    }


def data_inventory_snapshot() -> dict[str, Any]:
    now_utc = datetime.now(timezone.utc)
    legacy_quotes = parquet_inventory(config.MARKET_QUOTES_DIR, prefix="quotes_")
    legacy_signals = parquet_inventory(config.SIGNALS_DIR, prefix="signals_")
    strategy_quotes = _strategy_stream_inventory("quotes")
    strategy_signals = _strategy_stream_inventory("signals")

    inventory = {
        "forecasts": parquet_inventory(config.FORECAST_SNAPSHOTS_DIR, prefix="forecast_"),
        "observations": parquet_inventory(config.OBSERVATIONS_DIR, prefix="obs_"),
        "quotes": _merge_stream_inventory(legacy_quotes, strategy_quotes),
        "signals": _merge_stream_inventory(legacy_signals, strategy_signals),
    }
    inventory["today_rows"] = {
        "forecasts": today_parquet_rows(config.FORECAST_SNAPSHOTS_DIR, "forecast", config.SCHEDULER_TZ),
        "observations": today_parquet_rows(config.OBSERVATIONS_DIR, "obs", config.SCHEDULER_TZ),
        "quotes": (
            today_parquet_rows(config.MARKET_QUOTES_DIR, "quotes", config.SCHEDULER_TZ)
            + _strategy_today_rows("quotes", config.SCHEDULER_TZ)
        ),
        "signals": (
            today_parquet_rows(config.SIGNALS_DIR, "signals", config.SCHEDULER_TZ)
            + _strategy_today_rows("signals", config.SCHEDULER_TZ)
        ),
    }
    inventory["observation_unique_days_by_city"] = extract_observation_unique_days()
    inventory.update(contracts_active_inventory_snapshot(now_utc=now_utc))

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


def _alert_status(alerts: list[dict[str, Any]]) -> str:
    severity_order = {"info": 0, "warn": 1, "critical": 2}
    if not alerts:
        return "ok"
    worst = max(alerts, key=lambda a: severity_order.get(str(a.get("severity", "warn")), 0))
    return str(worst.get("severity") or "warn")


def _dedupe_alerts(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    dedup: dict[str, dict[str, Any]] = {}
    for alert in alerts:
        code = str(alert.get("code", "")).strip()
        if code and code not in dedup:
            dedup[code] = alert
    return list(dedup.values())


def operational_alerts_snapshot(
    train_snapshot: dict[str, Any],
    inventory_snapshot: dict[str, Any],
    events_snapshot: dict[str, Any],
) -> dict[str, Any]:
    alerts: list[dict[str, Any]] = []
    info_alerts: list[dict[str, Any]] = []
    suppressed = Counter()

    def add_alert(severity: str, code: str, message: str, **extra: Any) -> None:
        alerts.append({"severity": severity, "code": code, "message": message, **extra})

    def add_info(code: str, message: str, *, suppression_reason: str, **extra: Any) -> None:
        info_alerts.append({"severity": "info", "code": code, "message": message, **extra})
        suppressed[suppression_reason] += 1

    gate = dict(train_snapshot.get("gate") or {})
    if not bool(gate.get("pass", False)):
        max_days_remaining = int(train_snapshot.get("max_days_remaining", 0) or 0)
        eta_local = train_snapshot.get("estimated_ready_date_local")
        add_info(
            "train_gate_blocked",
            f"Train gate warmup in progress; {max_days_remaining} day(s) remaining at current data coverage.",
            suppression_reason="warmup_train_gate",
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
            minutes_since_by_event.get("strategy_cycle_all"),
            minutes_since_by_event.get("strategy_cycle"),
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
            "No recent strategy/paper cycle event within expected interval.",
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

    unique_alerts = _dedupe_alerts(alerts)
    unique_info = _dedupe_alerts(info_alerts)

    return {
        "status": _alert_status(unique_alerts),
        "alerts": unique_alerts,
        "counts": dict(Counter(str(a.get("severity", "warn")) for a in unique_alerts)),
        "info": unique_info,
        "info_count": len(unique_info),
        "suppressed": {
            "count": int(sum(suppressed.values())),
            "by_reason": dict(suppressed),
        },
    }


def strategies_health_snapshot() -> dict[str, Any]:
    from weather_arb.strategies.runtime import strategy_monitoring_snapshot

    rows: list[dict[str, Any]] = []
    for strategy_id in config.WEATHER_STRATEGY_IDS:
        try:
            snap = strategy_monitoring_snapshot(strategy_id)
            rows.append(
                {
                    "strategy_id": strategy_id,
                    "mode": str((config.WEATHER_STRATEGY_METADATA.get(strategy_id) or {}).get("mode", "discovery_only")),
                    "entry_allowed": bool(((snap.get("cycle") or {}).get("entry_gate") or {}).get("allowed", False)),
                    "alerts": list((snap.get("cycle") or {}).get("alerts") or []),
                    "contract_quality": dict((snap.get("cycle") or {}).get("contract_quality") or {}),
                    "freshness": dict((snap.get("cycle") or {}).get("freshness") or {}),
                }
            )
        except Exception as exc:
            rows.append({"strategy_id": strategy_id, "error": str(exc)})

    return {"ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "rows": rows}


def variant_operational_alerts_snapshot() -> dict[str, Any]:
    snap = strategies_health_snapshot()
    tradable = set(config.TRADABLE_WEATHER_STRATEGIES)

    actionable_alerts: list[dict[str, Any]] = []
    info_alerts: list[dict[str, Any]] = []
    suppressed = Counter()

    for row in list(snap.get("rows") or []):
        strategy_id = str(row.get("strategy_id", ""))
        if not strategy_id:
            continue

        mode = str(row.get("mode", "discovery_only"))
        row_alerts: list[dict[str, Any]] = []

        for alert in list(row.get("alerts") or []):
            if not isinstance(alert, dict):
                continue
            normalized = dict(alert)
            normalized.setdefault("strategy_id", strategy_id)
            normalized.setdefault("mode", mode)
            row_alerts.append(normalized)

        contract_quality = dict(row.get("contract_quality") or {})
        parse_rate = float(contract_quality.get("parse_rate", 0.0) or 0.0)
        parse_sample_count = int(
            contract_quality.get("parse_alert_sample_count", contract_quality.get("raw_count", 0)) or 0
        )
        if parse_rate < float(config.STRATEGY_PARSE_RATE_MIN):
            severity = (
                "warn"
                if parse_sample_count >= int(config.STRATEGY_PARSE_ALERT_MIN_RAW)
                else "info"
            )
            row_alerts.append(
                {
                    "severity": severity,
                    "code": f"contract_parse_degraded_{strategy_id}",
                    "message": (
                        f"Contract parse rate degraded ({parse_rate:.2f} < {config.STRATEGY_PARSE_RATE_MIN:.2f})."
                        if severity == "warn"
                        else (
                            "Contract parse sample warming up "
                            f"({parse_sample_count} < {config.STRATEGY_PARSE_ALERT_MIN_RAW}); "
                            f"current rate {parse_rate:.2f}."
                        )
                    ),
                    "strategy_id": strategy_id,
                    "mode": mode,
                    "parse_sample_count": parse_sample_count,
                }
            )

        eligible_count = int(contract_quality.get("eligible_count", 0) or 0)
        if eligible_count < int(config.STRATEGY_MIN_ELIGIBLE_CONTRACTS):
            row_alerts.append(
                {
                    "severity": "warn",
                    "code": f"contract_eligible_low_{strategy_id}",
                    "message": f"Eligible contracts low for {strategy_id}.",
                    "strategy_id": strategy_id,
                    "mode": mode,
                }
            )

        is_actionable_strategy = strategy_id in tradable and mode == "tradable"
        if is_actionable_strategy:
            for alert in row_alerts:
                sev = str(alert.get("severity", "warn"))
                if sev == "info":
                    info_alerts.append(dict(alert))
                    suppressed["warmup_parse_sample"] += 1
                else:
                    actionable_alerts.append(dict(alert))
        else:
            for alert in row_alerts:
                info = dict(alert)
                info["severity"] = "info"
                info["routed_severity"] = str(alert.get("severity", "warn"))
                info_alerts.append(info)
                suppressed["discovery_only"] += 1

    unique_actionable = _dedupe_alerts(actionable_alerts)
    unique_info = _dedupe_alerts(info_alerts)

    return {
        "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": _alert_status(unique_actionable),
        "alerts": unique_actionable,
        "count": len(unique_actionable),
        "counts": dict(Counter(str(a.get("severity", "warn")) for a in unique_actionable)),
        "info": unique_info,
        "info_count": len(unique_info),
        "suppressed": {
            "count": int(sum(suppressed.values())),
            "by_reason": dict(suppressed),
        },
    }
