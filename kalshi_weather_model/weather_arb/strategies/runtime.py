from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from weather_arb import config
from weather_arb.connectors.kalshi import KalshiPublicClient, parse_dollar_orderbook
from weather_arb.connectors.noaa import NOAAClient
from weather_arb.eval.backtest_gate import evaluate_backtest_gate
from weather_arb.eval.train_gate import evaluate_train_gate
from weather_arb.eval.wf_gate import evaluate_wf_gate
from weather_arb.execution.metrics import compute_day_metrics, max_drawdown_from_daily_pnl
from weather_arb.execution.paper_engine import run_paper_cycle
from weather_arb.governance.gates import evaluate_paper_gates
from weather_arb.model.contract_discovery import (
    classify_weather_strategy,
    contracts_to_frame,
    detect_supported_city,
    discover_weather_contracts,
)
from weather_arb.model.fair_value import (
    SeasonalResidualModel,
    build_residual_training_rows,
    compute_ev_cents,
)
from weather_arb.pipeline.ingest import append_rows_to_parquet
from weather_arb.utils.io_utils import read_or_create_json, safe_read_json, safe_write_json_atomic
from weather_arb.utils.time_utils import day_key_in_zone


@dataclass(slots=True)
class StrategyContext:
    forecast_extremes: dict[str, dict[str, dict[str, float]]]
    residual_model: SeasonalResidualModel
    thresholds: dict[str, Any]


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: Any) -> datetime | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _minutes_since(value: Any, now_utc: datetime) -> float | None:
    ts = _parse_iso(value)
    if ts is None:
        return None
    return max(0.0, (now_utc - ts).total_seconds() / 60.0)


def _latest_parquet_mtime_iso(dir_path: Path, prefix: str) -> str | None:
    files = sorted(dir_path.glob(f"{prefix}_*.parquet"))
    if not files:
        return None
    latest = max(files, key=lambda p: p.stat().st_mtime)
    return datetime.fromtimestamp(latest.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")


def _read_all_parquet_rows(dir_path: Path, prefix: str | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    glob = f"{prefix}_*.parquet" if prefix else "*.parquet"
    for file_path in sorted(dir_path.glob(glob)):
        try:
            frame = pd.read_parquet(file_path)
        except Exception:
            continue
        if frame.empty:
            continue
        rows.extend(frame.to_dict(orient="records"))
    return rows


def _strategy_mode(strategy_id: str) -> str:
    return str((config.WEATHER_STRATEGY_METADATA.get(strategy_id) or {}).get("mode", "discovery_only"))


def is_tradable_strategy(strategy_id: str) -> bool:
    return strategy_id in set(config.TRADABLE_WEATHER_STRATEGIES)


def _initial_sleeve_equity() -> float:
    count = max(len(config.WEATHER_STRATEGY_IDS), 1)
    return float(config.PAPER_ACCOUNT_SIZE) / float(count)


def _strategy_day_path(dir_path: Path, prefix: str, now_utc: datetime) -> Path:
    day_key = day_key_in_zone(now_utc, config.SCHEDULER_TZ)
    return dir_path / f"{prefix}_{day_key}.parquet"


def _load_thresholds() -> dict[str, Any]:
    return read_or_create_json(
        config.THRESHOLD_CONFIG_PATH,
        {
            "updated_at": None,
            "global_min_ev_cents": config.BOOTSTRAP_MIN_EV_CENTS,
            "by_city": {city: config.BOOTSTRAP_MIN_EV_CENTS for city in config.CITIES},
        },
    )


def _is_weather_event(event_row: dict[str, Any]) -> bool:
    category = str(event_row.get("category", "")).upper()
    title = str(event_row.get("title", "")).upper()
    sub_title = str(event_row.get("sub_title", "")).upper()
    blob = f"{category} {title} {sub_title}"
    if "WEATHER" in blob or "CLIMATE" in blob:
        return True
    keywords = ("TEMPERATURE", "HIGHEST", "LOWEST", "RAIN", "SNOW", "WIND", "PRECIP")
    return any(k in blob for k in keywords)


def discover_weather_markets(public_client: KalshiPublicClient) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cursor: str | None = None
    pages = 0
    event_tickers: set[str] = set()

    while pages < config.EVENT_SCAN_MAX_PAGES:
        payload = public_client.get_events(status="open", limit=200, cursor=cursor)
        rows = payload.get("events")
        if not isinstance(rows, list) or not rows:
            break

        pages += 1
        for row in rows:
            if not isinstance(row, dict):
                continue
            if not _is_weather_event(row):
                continue
            event_ticker = str(row.get("event_ticker", "")).strip()
            if event_ticker:
                event_tickers.add(event_ticker)

        next_cursor = payload.get("cursor")
        if not next_cursor:
            break
        cursor = str(next_cursor)

    markets: list[dict[str, Any]] = []
    for event_ticker in sorted(event_tickers):
        try:
            event_payload = public_client.get_event(event_ticker)
        except Exception:
            continue

        event_obj = event_payload.get("event") if isinstance(event_payload.get("event"), dict) else {}
        event_title = str((event_obj or {}).get("title", "")).strip()
        event_sub = str((event_obj or {}).get("sub_title", "")).strip()

        event_markets = event_payload.get("markets")
        if not isinstance(event_markets, list):
            continue

        for m in event_markets:
            if not isinstance(m, dict):
                continue
            merged = dict(m)
            merged["event_ticker"] = event_ticker
            merged["category"] = str((event_obj or {}).get("category", ""))
            merged["event_title"] = event_title
            merged["event_subtitle"] = event_sub

            market_title = str(merged.get("title", "")).strip()
            if event_title and market_title:
                merged["title"] = f"{event_title} {market_title}".strip()
            elif event_title and not market_title:
                merged["title"] = event_title
            if event_sub and not str(merged.get("subtitle", "") or merged.get("sub_title", "")).strip():
                merged["subtitle"] = event_sub
            markets.append(merged)

    return markets, {
        "pages_scanned": pages,
        "weather_events": len(event_tickers),
        "markets": len(markets),
    }


def load_market_payload(
    public_client: KalshiPublicClient,
    now_utc: datetime,
    *,
    force_refresh: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    cached = safe_read_json(config.CONTRACT_DISCOVERY_CACHE_PATH) or {}
    cached_rows = cached.get("markets")
    updated_at = cached.get("updated_at")

    if not force_refresh and isinstance(cached_rows, list) and updated_at:
        age_minutes = _minutes_since(updated_at, now_utc)
        if age_minutes is not None and age_minutes < float(config.MARKET_DISCOVERY_MINUTES):
            return {"markets": cached_rows}, {
                "source": "cache",
                "age_minutes": round(age_minutes, 2),
                "markets": len(cached_rows),
            }

    markets, meta = discover_weather_markets(public_client)
    payload = {
        "updated_at": now_utc.isoformat(timespec="seconds"),
        "markets": markets,
        "meta": meta,
    }
    safe_write_json_atomic(config.CONTRACT_DISCOVERY_CACHE_PATH, payload)
    return {"markets": markets}, {"source": "fresh", **meta}


def _city_forecast_daily_extremes(noaa: NOAAClient) -> dict[str, dict[str, dict[str, float]]]:
    out: dict[str, dict[str, dict[str, float]]] = {}
    for city, meta in config.CITY_CONFIG.items():
        raw = noaa.get_hourly_forecast(float(meta["lat"]), float(meta["lon"]))
        periods = raw.get("properties", {}).get("periods", [])
        tz = ZoneInfo(str(meta["tz"]))
        by_day: dict[str, dict[str, float]] = {}
        for p in periods:
            try:
                start_ts = datetime.fromisoformat(str(p.get("startTime", "")).replace("Z", "+00:00"))
                temp_f = float(p.get("temperature"))
            except Exception:
                continue
            day_key = start_ts.astimezone(tz).date().isoformat()
            item = by_day.setdefault(day_key, {"max_f": temp_f, "min_f": temp_f})
            item["max_f"] = max(float(item.get("max_f", temp_f)), temp_f)
            item["min_f"] = min(float(item.get("min_f", temp_f)), temp_f)
        out[city] = by_day
    return out


def build_strategy_context(now_utc: datetime, noaa: NOAAClient | None = None) -> StrategyContext:
    noaa_client = noaa or NOAAClient()
    forecast_extremes = _city_forecast_daily_extremes(noaa_client)

    residual_model = SeasonalResidualModel()
    forecast_rows = _read_all_parquet_rows(config.FORECAST_SNAPSHOTS_DIR)
    observation_rows = _read_all_parquet_rows(config.OBSERVATIONS_DIR)
    city_timezones = {city: str(meta["tz"]) for city, meta in config.CITY_CONFIG.items()}
    residual_rows = build_residual_training_rows(
        forecast_rows=forecast_rows,
        observation_rows=observation_rows,
        city_timezones=city_timezones,
        lead_min_hours=config.RESIDUAL_LEAD_MIN_HOURS,
        lead_max_hours=config.RESIDUAL_LEAD_MAX_HOURS,
        target_lead_hours=config.RESIDUAL_TARGET_LEAD_HOURS,
        lookback_days=config.RESIDUAL_LOOKBACK_DAYS,
        as_of_utc=now_utc,
    )
    residual_model.fit(residual_rows)

    return StrategyContext(
        forecast_extremes=forecast_extremes,
        residual_model=residual_model,
        thresholds=_load_thresholds(),
    )


def _compute_contract_quality(
    strategy_id: str,
    market_payload: dict[str, Any],
    contracts_count: int,
    skipped: list[dict[str, Any]],
    eligible_count: int,
) -> dict[str, Any]:
    strategy_rows: list[dict[str, Any]] = []
    scoped_rows: list[dict[str, Any]] = []

    for row in market_payload.get("markets", []):
        if not isinstance(row, dict):
            continue
        if classify_weather_strategy(row) != strategy_id:
            continue
        strategy_rows.append(row)
        if detect_supported_city(row) is not None:
            scoped_rows.append(row)

    reject_counts = Counter()
    excluded_counts = Counter()
    for item in skipped:
        s_id = str(item.get("strategy_id") or strategy_id)
        if s_id != strategy_id:
            continue
        reason = str(item.get("reason", "unknown"))
        if reason == "strategy_filtered":
            continue
        if reason in {"family_unmapped", "city_unmapped"}:
            excluded_counts[reason] += 1
            continue
        reject_counts[reason] += 1

    raw_count_total = len(strategy_rows)
    raw_count = len(scoped_rows)
    parsed_count = int(contracts_count)
    parse_rate = (parsed_count / raw_count) if raw_count > 0 else 1.0

    return {
        "strategy_id": strategy_id,
        "raw_count": raw_count,
        "raw_count_total": raw_count_total,
        "scope_excluded_count": max(raw_count_total - raw_count, 0),
        "parsed_count": parsed_count,
        "eligible_count": int(eligible_count),
        "parse_rate": float(parse_rate),
        "parse_alert_sample_count": raw_count,
        "reject_reason_counts": dict(reject_counts),
        "excluded_reason_counts": dict(excluded_counts),
        "parse_rate_min": float(config.STRATEGY_PARSE_RATE_MIN),
        "parse_alert_min_raw": int(config.STRATEGY_PARSE_ALERT_MIN_RAW),
        "eligible_min": int(config.STRATEGY_MIN_ELIGIBLE_CONTRACTS),
    }


def _compute_freshness(strategy_id: str, now_utc: datetime) -> dict[str, Any]:
    contracts_ts = (
        datetime.fromtimestamp(config.strategy_contracts_active_path(strategy_id).stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
        if config.strategy_contracts_active_path(strategy_id).exists()
        else None
    )
    quotes_ts = _latest_parquet_mtime_iso(config.strategy_quotes_dir(strategy_id), "quotes")
    signals_ts = _latest_parquet_mtime_iso(config.strategy_signals_dir(strategy_id), "signals")
    benchmark = safe_read_json(config.strategy_benchmark_latest_path(strategy_id)) or {}
    benchmark_ts = benchmark.get("updated_at")

    thresholds = {
        "contracts": float(max(config.MARKET_DISCOVERY_MINUTES * config.STRATEGY_STALE_MULTIPLIER, 45.0)),
        "quotes": float(max(config.SCHEDULER_INTERVAL_MINUTES * config.STRATEGY_STALE_MULTIPLIER, 45.0)),
        "signals": float(max(config.SCHEDULER_INTERVAL_MINUTES * config.STRATEGY_STALE_MULTIPLIER, 45.0)),
        "benchmark": float(config.STRATEGY_BENCHMARK_MAX_AGE_MINUTES.get(strategy_id, 180.0)),
    }

    ages = {
        "contracts": _minutes_since(contracts_ts, now_utc),
        "quotes": _minutes_since(quotes_ts, now_utc),
        "signals": _minutes_since(signals_ts, now_utc),
        "benchmark": _minutes_since(benchmark_ts, now_utc),
    }

    stale = {
        name: (ages[name] is None or float(ages[name]) > float(thresholds[name]))
        for name in ["contracts", "quotes", "signals", "benchmark"]
    }

    return {
        "strategy_id": strategy_id,
        "ages_minutes": ages,
        "threshold_minutes": thresholds,
        "stale": stale,
        "updated_at": now_utc.isoformat(timespec="seconds"),
    }


def _compute_liquidity_state(strategy_id: str, now_utc: datetime) -> dict[str, Any]:
    lookback_start = now_utc - timedelta(days=max(int(config.STRATEGY_LIQ_LOOKBACK_DAYS), 1))
    rows = _read_all_parquet_rows(config.strategy_quotes_dir(strategy_id), prefix="quotes")

    spreads: list[float] = []
    depths: list[int] = []
    snapshot_count = 0

    for row in rows:
        ts = _parse_iso(row.get("ts_utc"))
        if ts is None or ts < lookback_start:
            continue
        snapshot_count += 1

        yes_bid = float(row.get("yes_bid_dollars", 0.0) or 0.0)
        yes_ask = float(row.get("yes_ask_dollars", 0.0) or 0.0)
        no_bid = float(row.get("no_bid_dollars", 0.0) or 0.0)
        no_ask = float(row.get("no_ask_dollars", 0.0) or 0.0)

        yes_mid = max((yes_ask + yes_bid) / 2.0, 1e-9)
        no_mid = max((no_ask + no_bid) / 2.0, 1e-9)
        yes_spread = max(yes_ask - yes_bid, 0.0) / yes_mid
        no_spread = max(no_ask - no_bid, 0.0) / no_mid
        spreads.append(max(yes_spread, no_spread))

        depth = min(
            int(row.get("yes_bid_size", 0) or 0),
            int(row.get("yes_ask_size", 0) or 0),
            int(row.get("no_bid_size", 0) or 0),
            int(row.get("no_ask_size", 0) or 0),
        )
        depths.append(depth)

    median_spread = float(median(spreads)) if spreads else None
    median_depth = float(median(depths)) if depths else None

    pass_window = (
        snapshot_count >= int(config.STRATEGY_LIQ_MIN_SNAPSHOTS)
        and median_spread is not None
        and median_depth is not None
        and median_spread <= float(config.STRATEGY_LIQ_MAX_SPREAD_PCT)
        and median_depth >= float(config.STRATEGY_LIQ_MIN_BOOK_SIZE)
    )

    state_path = config.strategy_runtime_liquidity_path(strategy_id)
    state = read_or_create_json(
        state_path,
        {
            "strategy_id": strategy_id,
            "qualified": False,
            "consecutive_failures": 0,
            "last_window": None,
            "history": [],
            "updated_at": None,
        },
    )

    consecutive_failures = int(state.get("consecutive_failures", 0) or 0)
    was_qualified = bool(state.get("qualified", False))

    if pass_window:
        consecutive_failures = 0
        qualified = True
    else:
        consecutive_failures += 1
        if was_qualified and consecutive_failures < int(config.STRATEGY_DEQUAL_CONSEC_FAILS):
            qualified = True
        elif consecutive_failures >= int(config.STRATEGY_DEQUAL_CONSEC_FAILS):
            qualified = False
        else:
            qualified = was_qualified

    last_window = {
        "ts_utc": now_utc.isoformat(timespec="seconds"),
        "lookback_days": int(config.STRATEGY_LIQ_LOOKBACK_DAYS),
        "snapshot_count": snapshot_count,
        "median_spread": median_spread,
        "median_depth": median_depth,
        "pass": bool(pass_window),
        "qualified": bool(qualified),
        "thresholds": {
            "max_spread": float(config.STRATEGY_LIQ_MAX_SPREAD_PCT),
            "min_depth": int(config.STRATEGY_LIQ_MIN_BOOK_SIZE),
            "min_snapshots": int(config.STRATEGY_LIQ_MIN_SNAPSHOTS),
        },
    }

    state["qualified"] = bool(qualified)
    state["consecutive_failures"] = int(consecutive_failures)
    state["last_window"] = last_window
    history = list(state.get("history") or [])
    history.append(last_window)
    state["history"] = history[-120:]
    state["updated_at"] = now_utc.isoformat(timespec="seconds")
    safe_write_json_atomic(state_path, state)

    return {
        "strategy_id": strategy_id,
        "qualified": bool(qualified),
        "consecutive_failures": int(consecutive_failures),
        "last_window": last_window,
    }


def _forecast_value_for_contract(contract: Any, context: StrategyContext) -> float | None:
    city_map = context.forecast_extremes.get(str(contract.city), {})
    day = city_map.get(str(contract.contract_date_local))
    if not isinstance(day, dict):
        return None
    if str(contract.strategy_id) == "weather_temp_low":
        value = day.get("min_f")
    else:
        value = day.get("max_f")
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _p_fair_for_contract(contract: Any, forecast_temp: float, context: StrategyContext) -> float:
    c = str(contract.comparator or "above").lower()
    if c == "below":
        p_above = context.residual_model.p_exceeds(
            city=contract.city,
            forecast_temp_f=forecast_temp,
            threshold_f=float(contract.threshold_f),
            target_date=contract.contract_date_local,
        )
        return max(0.0, min(1.0, 1.0 - p_above))

    if c == "between" and contract.lower_f is not None and contract.upper_f is not None:
        lo = float(min(contract.lower_f, contract.upper_f))
        hi = float(max(contract.lower_f, contract.upper_f))
        p_lo = context.residual_model.p_exceeds(
            city=contract.city,
            forecast_temp_f=forecast_temp,
            threshold_f=lo,
            target_date=contract.contract_date_local,
        )
        p_hi = context.residual_model.p_exceeds(
            city=contract.city,
            forecast_temp_f=forecast_temp,
            threshold_f=hi,
            target_date=contract.contract_date_local,
        )
        return max(0.0, min(1.0, p_lo - p_hi))

    return context.residual_model.p_exceeds(
        city=contract.city,
        forecast_temp_f=forecast_temp,
        threshold_f=float(contract.threshold_f),
        target_date=contract.contract_date_local,
    )


def _write_daily_metrics(path: Path, date_key: str, day_metrics: dict[str, Any]) -> None:
    payload = read_or_create_json(path, {"by_day": {}, "updated_at": None})
    payload.setdefault("by_day", {})[date_key] = {
        **dict(payload.get("by_day", {}).get(date_key, {})),
        **day_metrics,
    }
    payload["updated_at"] = _iso_now()
    safe_write_json_atomic(path, payload)


def _ensure_strategy_paper_state(strategy_id: str) -> dict[str, Any]:
    path = config.strategy_paper_positions_path(strategy_id)
    base_equity = _initial_sleeve_equity()
    state = read_or_create_json(
        path,
        {
            "strategy_id": strategy_id,
            "equity": base_equity,
            "cash": base_equity,
            "open_positions": [],
            "closed_positions": [],
            "daily_pnl": {},
            "weekly_pnl": {},
            "consecutive_losses": 0,
            "next_position_id": 1,
        },
    )
    if str(state.get("strategy_id", "")) != strategy_id:
        state["strategy_id"] = strategy_id
        safe_write_json_atomic(path, state)
    return state


def _sync_shared_sleeves(strategy_id: str, entry_allowed: bool, blocked_reasons: list[str]) -> None:
    sleeves = read_or_create_json(
        config.PAPER_SLEEVES_PATH,
        {
            "updated_at": None,
            "initial_sleeve_equity": _initial_sleeve_equity(),
            "sleeves": {},
        },
    )

    data = dict(sleeves.get("sleeves") or {})
    for sid in config.WEATHER_STRATEGY_IDS:
        if sid not in data:
            data[sid] = {
                "strategy_id": sid,
                "mode": _strategy_mode(sid),
                "equity": _initial_sleeve_equity(),
                "cash": _initial_sleeve_equity(),
                "open_positions": 0,
                "closed_positions": 0,
                "entry_allowed": False,
                "blocked_reasons": ["uninitialized"],
                "updated_at": None,
            }

    state = safe_read_json(config.strategy_paper_positions_path(strategy_id)) or {}
    data[strategy_id] = {
        "strategy_id": strategy_id,
        "mode": _strategy_mode(strategy_id),
        "equity": float(state.get("equity", _initial_sleeve_equity()) or _initial_sleeve_equity()),
        "cash": float(state.get("cash", _initial_sleeve_equity()) or _initial_sleeve_equity()),
        "open_positions": len(list(state.get("open_positions") or [])),
        "closed_positions": len(list(state.get("closed_positions") or [])),
        "entry_allowed": bool(entry_allowed),
        "blocked_reasons": list(blocked_reasons),
        "updated_at": _iso_now(),
    }

    sleeves["updated_at"] = _iso_now()
    sleeves["sleeves"] = data
    safe_write_json_atomic(config.PAPER_SLEEVES_PATH, sleeves)


def _variant_alerts(
    strategy_id: str,
    contract_quality: dict[str, Any],
    freshness: dict[str, Any],
    liquidity: dict[str, Any],
    benchmark_available: bool,
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []

    parse_rate = float(contract_quality.get("parse_rate", 0.0) or 0.0)
    parse_sample_count = int(
        contract_quality.get("parse_alert_sample_count", contract_quality.get("raw_count", 0)) or 0
    )
    if parse_rate < float(config.STRATEGY_PARSE_RATE_MIN):
        if parse_sample_count < int(config.STRATEGY_PARSE_ALERT_MIN_RAW):
            alerts.append(
                {
                    "severity": "info",
                    "code": f"contract_parse_degraded_{strategy_id}",
                    "message": (
                        "Contract parse sample still warming up "
                        f"({parse_sample_count} < {config.STRATEGY_PARSE_ALERT_MIN_RAW}); "
                        f"current rate {parse_rate:.2f}."
                    ),
                }
            )
        else:
            alerts.append(
                {
                    "severity": "warn",
                    "code": f"contract_parse_degraded_{strategy_id}",
                    "message": f"Contract parse rate degraded ({parse_rate:.2f} < {config.STRATEGY_PARSE_RATE_MIN:.2f}).",
                }
            )

    eligible_count = int(contract_quality.get("eligible_count", 0) or 0)
    if eligible_count < int(config.STRATEGY_MIN_ELIGIBLE_CONTRACTS):
        alerts.append(
            {
                "severity": "warn",
                "code": f"contract_eligible_low_{strategy_id}",
                "message": (
                    f"Eligible contracts below minimum ({eligible_count}/{config.STRATEGY_MIN_ELIGIBLE_CONTRACTS})."
                ),
            }
        )

    stale = dict(freshness.get("stale") or {})
    if stale.get("quotes"):
        alerts.append({"severity": "warn", "code": f"stale_quotes_{strategy_id}", "message": "Quotes stream is stale."})
    if stale.get("signals"):
        alerts.append({"severity": "warn", "code": f"stale_signals_{strategy_id}", "message": "Signals stream is stale."})
    if stale.get("benchmark"):
        alerts.append({"severity": "warn", "code": f"stale_benchmark_{strategy_id}", "message": "Benchmark stream is stale."})

    if is_tradable_strategy(strategy_id) and not bool(liquidity.get("qualified", False)):
        last_window = dict(liquidity.get("last_window") or {})
        thresholds = dict(last_window.get("thresholds") or {})
        median_spread = last_window.get("median_spread")
        median_depth = last_window.get("median_depth")
        spread_txt = "n/a" if median_spread is None else f"{float(median_spread):.3f}"
        depth_txt = "n/a" if median_depth is None else f"{float(median_depth):.1f}"
        alerts.append(
            {
                "severity": "warn",
                "code": f"liquidity_blocked_{strategy_id}",
                "message": (
                    "Liquidity gate blocked entries "
                    f"(median_spread={spread_txt} > {thresholds.get('max_spread')}, "
                    f"median_depth={depth_txt} < {thresholds.get('min_depth')})."
                ),
            }
        )

    if not benchmark_available and is_tradable_strategy(strategy_id):
        alerts.append(
            {
                "severity": "critical",
                "code": f"stale_benchmark_{strategy_id}",
                "message": "Benchmark unavailable; variant is fail-closed.",
            }
        )

    return alerts


def _entry_gate(
    strategy_id: str,
    contract_quality: dict[str, Any],
    freshness: dict[str, Any],
    liquidity: dict[str, Any],
    benchmark_available: bool,
) -> tuple[bool, list[str], dict[str, bool]]:
    parse_ok = float(contract_quality.get("parse_rate", 0.0) or 0.0) >= float(config.STRATEGY_PARSE_RATE_MIN)
    eligible_ok = int(contract_quality.get("eligible_count", 0) or 0) >= int(config.STRATEGY_MIN_ELIGIBLE_CONTRACTS)

    stale = dict(freshness.get("stale") or {})
    freshness_ok = not bool(stale.get("contracts") or stale.get("quotes") or stale.get("benchmark"))
    liquidity_ok = bool(liquidity.get("qualified", False))

    checks = {
        "parse_ok": bool(parse_ok),
        "eligible_ok": bool(eligible_ok),
        "freshness_ok": bool(freshness_ok),
        "liquidity_ok": bool(liquidity_ok),
        "benchmark_ok": bool(benchmark_available),
        "tradable": bool(is_tradable_strategy(strategy_id)),
    }

    reasons: list[str] = []
    if not checks["tradable"]:
        reasons.append("discovery_only")
    if not checks["parse_ok"]:
        reasons.append("parse_rate")
    if not checks["eligible_ok"]:
        reasons.append("eligible_contracts")
    if not checks["freshness_ok"]:
        reasons.append("freshness")
    if not checks["liquidity_ok"]:
        reasons.append("liquidity")
    if not checks["benchmark_ok"]:
        reasons.append("benchmark")

    return len(reasons) == 0, reasons, checks


def run_strategy_cycle(
    strategy_id: str,
    *,
    now_utc: datetime | None = None,
    market_payload: dict[str, Any] | None = None,
    public_client: KalshiPublicClient | None = None,
    noaa_client: NOAAClient | None = None,
    context: StrategyContext | None = None,
) -> dict[str, Any]:
    if strategy_id not in set(config.WEATHER_STRATEGY_IDS):
        raise ValueError(f"unknown strategy_id: {strategy_id}")

    config.ensure_dirs()
    now = now_utc or _utc_now()
    client = public_client or KalshiPublicClient()

    payload = market_payload
    discovery_meta: dict[str, Any] = {}
    if payload is None:
        payload, discovery_meta = load_market_payload(client, now)

    contracts, skipped = discover_weather_contracts(
        payload,
        allowed_strategy_ids={strategy_id},
        now_utc=now,
    )
    active_rows = contracts_to_frame(contracts).to_dict(orient="records") if contracts else []

    if active_rows:
        pd.DataFrame(active_rows).to_parquet(config.strategy_contracts_active_path(strategy_id), index=False)
    elif not config.strategy_contracts_active_path(strategy_id).exists():
        pd.DataFrame([]).to_parquet(config.strategy_contracts_active_path(strategy_id), index=False)

    history_rows = [{**r, "discovered_at_utc": now.isoformat(timespec="seconds")} for r in active_rows]
    append_rows_to_parquet(config.strategy_contracts_history_path(strategy_id), history_rows)

    quote_rows: list[dict[str, Any]] = []
    quote_map: dict[str, dict[str, Any]] = {}
    quote_skipped: list[dict[str, Any]] = []
    for contract in contracts:
        try:
            raw_book = client.get_market_orderbook(contract.ticker)
            parsed = parse_dollar_orderbook(raw_book, contract.ticker)
        except Exception as exc:
            quote_skipped.append({"ticker": contract.ticker, "reason": f"orderbook_unavailable: {exc}", "strategy_id": strategy_id})
            continue

        parsed["ts_utc"] = now.isoformat(timespec="seconds")
        parsed["strategy_id"] = strategy_id
        parsed["city"] = contract.city
        parsed["contract_date_local"] = contract.contract_date_local
        quote_rows.append(parsed)
        quote_map[str(parsed["ticker"])] = parsed

    append_rows_to_parquet(_strategy_day_path(config.strategy_quotes_dir(strategy_id), "quotes", now), quote_rows)

    benchmark: dict[str, Any]
    benchmark_available = False
    ctx = context
    if strategy_id.startswith("weather_temp"):
        try:
            if ctx is None:
                ctx = build_strategy_context(now, noaa=noaa_client)
            benchmark = {
                "strategy_id": strategy_id,
                "updated_at": now.isoformat(timespec="seconds"),
                "available": True,
                "source": "noaa_hourly",
                "cities": sorted(list(ctx.forecast_extremes.keys())),
            }
            benchmark_available = True
        except Exception as exc:
            benchmark = {
                "strategy_id": strategy_id,
                "updated_at": now.isoformat(timespec="seconds"),
                "available": False,
                "reason": f"benchmark_fetch_failed: {exc}",
            }
    else:
        benchmark = {
            "strategy_id": strategy_id,
            "updated_at": now.isoformat(timespec="seconds"),
            "available": False,
            "reason": "benchmark_adapter_missing",
        }

    safe_write_json_atomic(config.strategy_benchmark_latest_path(strategy_id), benchmark)

    signals: list[dict[str, Any]] = []
    signal_skipped: list[dict[str, Any]] = []
    eligible_count = 0

    if strategy_id.startswith("weather_temp") and ctx is not None:
        thresholds = dict(ctx.thresholds or {})
        by_city_thresholds = dict(thresholds.get("by_city") or {})
        for contract in contracts:
            quote = quote_map.get(contract.ticker)
            if quote is None:
                signal_skipped.append({"ticker": contract.ticker, "reason": "quote_missing", "strategy_id": strategy_id})
                continue

            forecast_temp = _forecast_value_for_contract(contract, ctx)
            if forecast_temp is None:
                signal_skipped.append({"ticker": contract.ticker, "reason": "forecast_missing", "strategy_id": strategy_id})
                continue

            eligible_count += 1
            p_fair = _p_fair_for_contract(contract, forecast_temp, ctx)

            yes_price = float(quote["yes_ask_dollars"])
            no_price = float(quote["no_ask_dollars"])
            est_cost_cents = (config.KALSHI_FEE_PER_CONTRACT_DOLLARS * 100.0) + config.DEFAULT_SLIPPAGE_CENTS

            ev_yes = compute_ev_cents(p_fair=p_fair, p_market=yes_price, est_cost_cents=est_cost_cents)
            ev_no = compute_ev_cents(p_fair=(1.0 - p_fair), p_market=no_price, est_cost_cents=est_cost_cents)

            side = "buy_yes" if ev_yes >= ev_no else "buy_no"
            best_ev = max(ev_yes, ev_no)
            if best_ev <= 0:
                continue

            min_ev_cents = float(by_city_thresholds.get(contract.city, thresholds.get("global_min_ev_cents", config.BOOTSTRAP_MIN_EV_CENTS)))

            signals.append(
                {
                    "strategy_id": strategy_id,
                    "ticker": contract.ticker,
                    "city": contract.city,
                    "side": side,
                    "p_fair": p_fair,
                    "p_mkt": yes_price if side == "buy_yes" else no_price,
                    "ev_cents": best_ev,
                    "min_ev_cents": min_ev_cents,
                    "threshold_f": contract.threshold_f,
                    "comparator": contract.comparator,
                    "lower_f": contract.lower_f,
                    "upper_f": contract.upper_f,
                    "settlement_ts_utc": contract.settlement_ts_utc.isoformat(),
                    "generated_at_utc": now.isoformat(timespec="seconds"),
                }
            )

    append_rows_to_parquet(_strategy_day_path(config.strategy_signals_dir(strategy_id), "signals", now), signals)

    contract_quality = _compute_contract_quality(
        strategy_id=strategy_id,
        market_payload=payload,
        contracts_count=len(contracts),
        skipped=[*skipped, *quote_skipped, *signal_skipped],
        eligible_count=eligible_count,
    )
    freshness = _compute_freshness(strategy_id, now)
    liquidity = _compute_liquidity_state(strategy_id, now)

    entry_allowed, blocked_reasons, checks = _entry_gate(
        strategy_id=strategy_id,
        contract_quality=contract_quality,
        freshness=freshness,
        liquidity=liquidity,
        benchmark_available=benchmark_available,
    )

    paper_summary: dict[str, Any] = {
        "opened": 0,
        "closed": 0,
        "open_positions": 0,
        "equity": _initial_sleeve_equity(),
    }

    if is_tradable_strategy(strategy_id):
        _ensure_strategy_paper_state(strategy_id)
        trade_signals = signals if entry_allowed else []
        paper_summary = run_paper_cycle(
            trade_signals,
            quote_map,
            now,
            state_path=config.strategy_paper_positions_path(strategy_id),
            blotter_dir=config.strategy_paper_dir(strategy_id) / "paper_blotter",
        )

        paper_state = safe_read_json(config.strategy_paper_positions_path(strategy_id)) or {}
        day_key = day_key_in_zone(now, config.SCHEDULER_TZ)
        day_metrics = compute_day_metrics(list(paper_state.get("closed_positions", [])), day_key)
        day_metrics["date_key"] = day_key
        _write_daily_metrics(config.strategy_paper_metrics_daily_path(strategy_id), day_key, day_metrics)

        # Live routing consumes this artifact directly; keep executable signal + quote payload fail-closed.
        safe_write_json_atomic(
            config.strategy_live_input_path(strategy_id),
            {
                "strategy_id": strategy_id,
                "ts_utc": now.isoformat(timespec="seconds"),
                "signals": signals,
                "quote_map": quote_map,
                "entry_allowed": bool(entry_allowed),
                "blocked_reasons": blocked_reasons,
            },
        )

    alerts = _variant_alerts(
        strategy_id=strategy_id,
        contract_quality=contract_quality,
        freshness=freshness,
        liquidity=liquidity,
        benchmark_available=benchmark_available,
    )

    _sync_shared_sleeves(strategy_id, entry_allowed=entry_allowed, blocked_reasons=blocked_reasons)

    payload_out = {
        "strategy_id": strategy_id,
        "ts_utc": now.isoformat(timespec="seconds"),
        "mode": _strategy_mode(strategy_id),
        "discovery": discovery_meta,
        "contracts": {
            "count": len(contracts),
            "skipped": len(skipped),
        },
        "quotes": {
            "count": len(quote_rows),
            "skipped": len(quote_skipped),
        },
        "signals": {
            "count": len(signals),
            "skipped": len(signal_skipped),
        },
        "benchmark": benchmark,
        "contract_quality": contract_quality,
        "freshness": freshness,
        "liquidity": liquidity,
        "entry_gate": {
            "allowed": bool(entry_allowed),
            "blocked_reasons": blocked_reasons,
            "checks": checks,
        },
        "paper": paper_summary,
        "alerts": alerts,
    }

    safe_write_json_atomic(config.strategy_runtime_cycle_path(strategy_id), payload_out)
    return payload_out


def run_all_strategies_cycle(now_utc: datetime | None = None) -> dict[str, Any]:
    now = now_utc or _utc_now()
    public_client = KalshiPublicClient()
    noaa = NOAAClient()

    market_payload, discovery_meta = load_market_payload(public_client, now)
    try:
        context = build_strategy_context(now, noaa=noaa)
    except Exception:
        context = None

    results: dict[str, Any] = {}
    for strategy_id in config.WEATHER_STRATEGY_IDS:
        try:
            results[strategy_id] = run_strategy_cycle(
                strategy_id,
                now_utc=now,
                market_payload=market_payload,
                public_client=public_client,
                noaa_client=noaa,
                context=context,
            )
        except Exception as exc:
            results[strategy_id] = {
                "strategy_id": strategy_id,
                "ts_utc": now.isoformat(timespec="seconds"),
                "error": str(exc),
            }

    return {
        "ts_utc": now.isoformat(timespec="seconds"),
        "discovery": discovery_meta,
        "count": len(results),
        "results": results,
        "failures": [sid for sid, row in results.items() if isinstance(row, dict) and row.get("error")],
    }


def _aggregate_gate_metrics(metrics_path: Path, *, starting_equity: float) -> dict[str, Any]:
    payload = safe_read_json(metrics_path) or {}
    by_day = dict(payload.get("by_day", {}))

    trading_days = len(by_day)
    trades = sum(int(v.get("trades", 0) or 0) for v in by_day.values())
    wins = sum(int(v.get("wins", 0) or 0) for v in by_day.values())
    pnl_total = sum(float(v.get("pnl_dollars", 0.0) or 0.0) for v in by_day.values())
    avg_daily_pnl = (pnl_total / trading_days) if trading_days > 0 else 0.0

    roi_values = [
        float(v.get("roi_per_trade", 0.0) or 0.0)
        for v in by_day.values()
        if int(v.get("trades", 0) or 0) > 0
    ]
    roi_per_trade = (sum(roi_values) / len(roi_values)) if roi_values else 0.0

    dd_map = {k: float(v.get("pnl_dollars", 0.0) or 0.0) for k, v in by_day.items()}
    max_dd = max_drawdown_from_daily_pnl(dd_map, starting_equity=starting_equity)

    return {
        "trading_days": trading_days,
        "trades": trades,
        "win_rate": wins / max(trades, 1),
        "avg_daily_pnl": avg_daily_pnl,
        "max_drawdown": max_dd,
        "roi_per_trade": roi_per_trade,
        "closed_trades": trades,
    }


def evaluate_strategy_gates(strategy_id: str, now_utc: datetime | None = None) -> dict[str, Any]:
    if strategy_id not in set(config.WEATHER_STRATEGY_IDS):
        raise ValueError(f"unknown strategy_id: {strategy_id}")

    now = now_utc or _utc_now()
    cycle = safe_read_json(config.strategy_runtime_cycle_path(strategy_id)) or {}

    if strategy_id.startswith("weather_temp"):
        observation_rows = _read_all_parquet_rows(config.OBSERVATIONS_DIR)
        city_days: dict[str, set[str]] = {city: set() for city in config.CITIES}
        for row in observation_rows:
            city = str(row.get("city", ""))
            day_key = str(row.get("obs_date_local", ""))
            if city in city_days and day_key:
                city_days[city].add(day_key)
        train_input = {
            city: {
                "observations": len(days),
                "missing_pct": max(config.TRAIN_MIN_OBSERVATIONS_PER_CITY - len(days), 0) / max(config.TRAIN_MIN_OBSERVATIONS_PER_CITY, 1),
            }
            for city, days in city_days.items()
        }
        train = evaluate_train_gate(train_input)
    else:
        train = {
            "pass": False,
            "reasons": ["benchmark adapter not implemented"],
        }

    signal_rows = _read_all_parquet_rows(config.strategy_signals_dir(strategy_id), prefix="signals")
    windows: list[dict[str, Any]] = []
    if signal_rows:
        frame = pd.DataFrame(signal_rows)
        if "generated_at_utc" in frame.columns and "ev_cents" in frame.columns:
            frame["day"] = frame["generated_at_utc"].astype(str).str[:10]
            for day, grp in frame.groupby("day"):
                windows.append(
                    {
                        "window": day,
                        "feasible": len(grp) >= int(config.WF_MIN_SIGNALS_PER_WINDOW),
                        "ev_day": float(grp["ev_cents"].mean() / 100.0),
                    }
                )
    wf = evaluate_wf_gate(windows)

    state = safe_read_json(config.strategy_paper_positions_path(strategy_id)) or {}
    trades_rows: list[dict[str, Any]] = []
    for pos in state.get("closed_positions", []):
        trades_rows.append(
            {
                "pnl_dollars": float(pos.get("realized_pnl_dollars", 0.0) or 0.0),
                "notional_dollars": abs(float(pos.get("entry_price_dollars", 0.0) or 0.0) * int(pos.get("contracts", 0) or 0)),
                "date_key": str(pos.get("closed_at_utc", "")[:10]),
            }
        )
    backtest = evaluate_backtest_gate(trades_rows)

    agg = _aggregate_gate_metrics(
        config.strategy_paper_metrics_daily_path(strategy_id),
        starting_equity=_initial_sleeve_equity(),
    )
    paper_pass, paper_enough_data, paper_reasons = evaluate_paper_gates(agg)

    checks = dict((cycle.get("entry_gate") or {}).get("checks") or {})
    data_health_green = bool(
        checks.get("parse_ok")
        and checks.get("freshness_ok")
        and checks.get("benchmark_ok")
        and (
            (not is_tradable_strategy(strategy_id))
            or checks.get("liquidity_ok")
        )
    )

    out = {
        "strategy_id": strategy_id,
        "ts_utc": now.isoformat(timespec="seconds"),
        "train": train,
        "wf": wf,
        "backtest": backtest,
        "paper": {
            "pass": bool(paper_pass),
            "enough_data": bool(paper_enough_data),
            "reasons": paper_reasons,
            "metrics": agg,
        },
        "data_health": {
            "green": bool(data_health_green),
            "entry_checks": checks,
        },
        "eligible_for_challenger": bool(
            bool(train.get("pass", False))
            and bool(wf.get("pass", False))
            and bool(backtest.get("pass", False))
            and bool(paper_pass)
            and bool(paper_enough_data)
            and bool(data_health_green)
            and int(agg.get("trading_days", 0) or 0) >= int(config.PROMOTION_MIN_TRADING_DAYS)
            and int(agg.get("trades", 0) or 0) >= int(config.PROMOTION_MIN_TRADES)
        ),
    }

    safe_write_json_atomic(config.strategy_runtime_gates_path(strategy_id), out)
    return out


def evaluate_all_strategy_gates(now_utc: datetime | None = None) -> dict[str, Any]:
    now = now_utc or _utc_now()
    out: dict[str, Any] = {
        "ts_utc": now.isoformat(timespec="seconds"),
        "results": {},
    }
    for strategy_id in config.WEATHER_STRATEGY_IDS:
        try:
            out["results"][strategy_id] = evaluate_strategy_gates(strategy_id, now_utc=now)
        except Exception as exc:
            out["results"][strategy_id] = {"strategy_id": strategy_id, "error": str(exc)}
    return out


def _last_n_day_items(by_day: dict[str, Any], n: int) -> dict[str, Any]:
    keys = sorted(by_day.keys())
    if n <= 0:
        return {}
    keep = keys[-n:]
    return {k: by_day[k] for k in keep}


def _normalize(values: list[float]) -> list[float]:
    if not values:
        return []
    lo = min(values)
    hi = max(values)
    if hi - lo <= 1e-12:
        return [0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def _leaderboard_row(strategy_id: str) -> dict[str, Any]:
    metrics_payload = safe_read_json(config.strategy_paper_metrics_daily_path(strategy_id)) or {}
    by_day = _last_n_day_items(dict(metrics_payload.get("by_day", {})), int(config.PORTFOLIO_TRAILING_DAYS))

    trading_days = len(by_day)
    trades = int(sum(int(v.get("trades", 0) or 0) for v in by_day.values()))
    wins = int(sum(int(v.get("wins", 0) or 0) for v in by_day.values()))
    pnl_total = float(sum(float(v.get("pnl_dollars", 0.0) or 0.0) for v in by_day.values()))
    ev_day = pnl_total / max(trading_days, 1)
    win_rate = wins / max(trades, 1)

    daily_map = {k: float(v.get("pnl_dollars", 0.0) or 0.0) for k, v in by_day.items()}
    drawdown = max_drawdown_from_daily_pnl(daily_map, starting_equity=_initial_sleeve_equity()) if by_day else 0.0

    positive_days = sum(1 for v in by_day.values() if float(v.get("pnl_dollars", 0.0) or 0.0) > 0)
    consistency = positive_days / max(trading_days, 1)

    gate = safe_read_json(config.strategy_runtime_gates_path(strategy_id)) or {}
    cycle = safe_read_json(config.strategy_runtime_cycle_path(strategy_id)) or {}
    checks = dict((cycle.get("entry_gate") or {}).get("checks") or {})
    if checks:
        data_health_green = bool(
            checks.get("parse_ok", False)
            and checks.get("eligible_ok", False)
            and checks.get("freshness_ok", False)
            and checks.get("liquidity_ok", False)
            and checks.get("benchmark_ok", False)
        )
        data_health_source = "cycle_entry_checks"
        data_health_source_ts_utc = cycle.get("ts_utc")
    else:
        data_health_green = bool((gate.get("data_health") or {}).get("green", False))
        data_health_source = "daily_gate_fallback"
        data_health_source_ts_utc = gate.get("ts_utc")

    return {
        "strategy_id": strategy_id,
        "mode": _strategy_mode(strategy_id),
        "trading_days": trading_days,
        "trades": trades,
        "win_rate": win_rate,
        "ev_day": ev_day,
        "drawdown": drawdown,
        "consistency": consistency,
        "data_health": 1.0 if data_health_green else 0.0,
        "data_health_green": bool(data_health_green),
        "data_health_source": data_health_source,
        "data_health_source_ts_utc": data_health_source_ts_utc,
        "eligible": bool(gate.get("eligible_for_challenger", False)),
    }


def compute_portfolio_leaderboard(now_utc: datetime | None = None) -> dict[str, Any]:
    now = now_utc or _utc_now()
    rows = [_leaderboard_row(sid) for sid in config.WEATHER_STRATEGY_IDS]
    weights = dict(config.PORTFOLIO_SCORE_WEIGHTS)
    tradable_indices = [i for i, row in enumerate(rows) if str(row.get("mode")) == "tradable"]

    if tradable_indices:
        tradable_rows = [rows[i] for i in tradable_indices]
        ev_norm = _normalize([float(r["ev_day"]) for r in tradable_rows])
        dd_norm = _normalize([float(r["drawdown"]) for r in tradable_rows])
        consistency_norm = _normalize([float(r["consistency"]) for r in tradable_rows])
        health_norm = _normalize([float(r["data_health"]) for r in tradable_rows])

        for norm_idx, row_idx in enumerate(tradable_indices):
            score = (
                float(weights.get("ev_day", 0.4)) * ev_norm[norm_idx]
                + float(weights.get("drawdown", 0.3)) * dd_norm[norm_idx]
                + float(weights.get("consistency", 0.2)) * consistency_norm[norm_idx]
                + float(weights.get("data_health", 0.1)) * health_norm[norm_idx]
            ) * 100.0
            rows[row_idx]["score"] = round(score, 4)

    for row in rows:
        if "score" not in row:
            row["score"] = 0.0

    rows.sort(
        key=lambda x: (
            float(x.get("score", 0.0)),
            1 if str(x.get("mode")) == "tradable" else 0,
        ),
        reverse=True,
    )
    for idx, row in enumerate(rows, start=1):
        row["rank"] = idx

    champion_state = safe_read_json(config.CHAMPION_STATE_PATH) or {}
    current_champion = champion_state.get("current_champion")

    challenger = None
    for row in rows:
        if not bool(row.get("eligible", False)):
            continue
        if str(row.get("strategy_id")) != str(current_champion):
            challenger = row.get("strategy_id")
            break

    out = {
        "ts_utc": now.isoformat(timespec="seconds"),
        "trailing_days": int(config.PORTFOLIO_TRAILING_DAYS),
        "rows": rows,
        "current_champion": current_champion,
        "challenger": challenger,
        "auto_promotion_enabled": bool(config.AUTO_PROMOTION_ENABLED),
    }
    safe_write_json_atomic(config.PORTFOLIO_RANKINGS_PATH, out)
    return out


def portfolio_promote(now_utc: datetime | None = None, *, force: bool = False) -> dict[str, Any]:
    now = now_utc or _utc_now()
    board = compute_portfolio_leaderboard(now)

    rows = list(board.get("rows") or [])
    by_id = {str(r.get("strategy_id")): r for r in rows}

    state = read_or_create_json(
        config.CHAMPION_STATE_PATH,
        {
            "updated_at": None,
            "auto_promotion_enabled": bool(config.AUTO_PROMOTION_ENABLED),
            "current_champion": None,
            "pending_challenger": None,
            "last_decision": None,
            "mode": "shadow",
        },
    )

    current = str(state.get("current_champion") or "") or None
    eligible_rows = [r for r in rows if bool(r.get("eligible", False))]
    top = eligible_rows[0] if eligible_rows else None

    decision = {
        "ts_utc": now.isoformat(timespec="seconds"),
        "current_champion": current,
        "selected": current,
        "reason": "no_change",
        "pending_challenger": None,
    }

    if top is None:
        decision["reason"] = "no_eligible_strategies"
    else:
        top_id = str(top.get("strategy_id"))
        if current is None:
            if bool(config.AUTO_PROMOTION_ENABLED) or force:
                decision["selected"] = top_id
                decision["reason"] = "initial_champion_selected"
            else:
                decision["pending_challenger"] = top_id
                decision["reason"] = "shadow_mode_pending_challenger"
        elif top_id != current:
            current_row = by_id.get(current)
            current_score = float((current_row or {}).get("score", 0.0) or 0.0)
            top_score = float(top.get("score", 0.0) or 0.0)
            current_dd = float((current_row or {}).get("drawdown", 0.0) or 0.0)
            top_dd = float(top.get("drawdown", 0.0) or 0.0)

            score_delta_ok = (top_score - current_score) >= float(config.PORTFOLIO_MIN_SCORE_DELTA)
            drawdown_ok = (top_dd - current_dd) >= -float(config.PORTFOLIO_MAX_DRAWDOWN_DELTA)

            if score_delta_ok and drawdown_ok:
                if bool(config.AUTO_PROMOTION_ENABLED) or force:
                    decision["selected"] = top_id
                    decision["reason"] = "challenger_promoted"
                else:
                    decision["pending_challenger"] = top_id
                    decision["reason"] = "shadow_mode_challenger_ready"
            else:
                decision["reason"] = "challenger_below_threshold"

    state["updated_at"] = now.isoformat(timespec="seconds")
    state["auto_promotion_enabled"] = bool(config.AUTO_PROMOTION_ENABLED)
    state["mode"] = "auto" if bool(config.AUTO_PROMOTION_ENABLED) else "shadow"
    state["pending_challenger"] = decision.get("pending_challenger")
    state["last_decision"] = decision

    selected = decision.get("selected")
    if selected and ((bool(config.AUTO_PROMOTION_ENABLED) or force) and selected != current):
        state["previous_champion"] = current
        state["current_champion"] = selected
        state["handoff"] = {
            "mode": "drain_then_switch",
            "status": "switched",
            "timeout_hours": int(config.LIVE_HANDOFF_MAX_DRAIN_HOURS),
            "switched_at": now.isoformat(timespec="seconds"),
        }

    safe_write_json_atomic(config.CHAMPION_STATE_PATH, state)
    return {
        "ts_utc": now.isoformat(timespec="seconds"),
        "decision": decision,
        "champion_state": state,
    }


def strategy_monitoring_snapshot(strategy_id: str) -> dict[str, Any]:
    if strategy_id not in set(config.WEATHER_STRATEGY_IDS):
        raise ValueError(f"unknown strategy_id: {strategy_id}")

    cycle = safe_read_json(config.strategy_runtime_cycle_path(strategy_id)) or {}
    gates = safe_read_json(config.strategy_runtime_gates_path(strategy_id)) or {}
    liquidity = safe_read_json(config.strategy_runtime_liquidity_path(strategy_id)) or {}
    benchmark = safe_read_json(config.strategy_benchmark_latest_path(strategy_id)) or {}

    return {
        "strategy_id": strategy_id,
        "mode": _strategy_mode(strategy_id),
        "cycle": cycle,
        "gates": gates,
        "liquidity": liquidity,
        "benchmark": benchmark,
        "paper_state": safe_read_json(config.strategy_paper_positions_path(strategy_id)) or {},
    }


def strategies_summary_snapshot() -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for strategy_id in config.WEATHER_STRATEGY_IDS:
        cycle = safe_read_json(config.strategy_runtime_cycle_path(strategy_id)) or {}
        gates = safe_read_json(config.strategy_runtime_gates_path(strategy_id)) or {}

        stage = "calibrating"
        if _strategy_mode(strategy_id) == "discovery_only":
            stage = "discovery_only"
        elif bool((gates.get("paper") or {}).get("pass", False)):
            stage = "paper"
        elif bool((gates.get("train") or {}).get("pass", False)):
            stage = "paper_candidate"

        rows.append(
            {
                "strategy_id": strategy_id,
                "mode": _strategy_mode(strategy_id),
                "stage": stage,
                "entry_allowed": bool((cycle.get("entry_gate") or {}).get("allowed", False)),
                "blocked_reasons": list((cycle.get("entry_gate") or {}).get("blocked_reasons", [])),
                "benchmark_fresh": not bool(((cycle.get("freshness") or {}).get("stale") or {}).get("benchmark", True)),
                "latest_cycle_ts_utc": cycle.get("ts_utc"),
                "alerts": list(cycle.get("alerts") or []),
            }
        )

    return {
        "ts_utc": _iso_now(),
        "count": len(rows),
        "rows": rows,
    }


def contract_health_snapshot(strategy_id: str) -> dict[str, Any]:
    if strategy_id not in set(config.WEATHER_STRATEGY_IDS):
        raise ValueError(f"unknown strategy_id: {strategy_id}")

    cycle = safe_read_json(config.strategy_runtime_cycle_path(strategy_id)) or {}
    return {
        "strategy_id": strategy_id,
        "ts_utc": _iso_now(),
        "contract_quality": cycle.get("contract_quality", {}),
        "freshness": cycle.get("freshness", {}),
        "entry_gate": cycle.get("entry_gate", {}),
        "alerts": cycle.get("alerts", []),
    }


def migrate_legacy_artifacts() -> dict[str, Any]:
    strategy_id = "weather_temp_high"
    created: list[str] = []

    config.ensure_dirs()

    if config.CONTRACTS_ACTIVE_PATH.exists() and not config.strategy_contracts_active_path(strategy_id).exists():
        frame = pd.read_parquet(config.CONTRACTS_ACTIVE_PATH)
        if "strategy_id" not in frame.columns:
            frame["strategy_id"] = strategy_id
        frame.to_parquet(config.strategy_contracts_active_path(strategy_id), index=False)
        created.append(str(config.strategy_contracts_active_path(strategy_id)))

    if config.CONTRACTS_HISTORY_PATH.exists() and not config.strategy_contracts_history_path(strategy_id).exists():
        frame = pd.read_parquet(config.CONTRACTS_HISTORY_PATH)
        if "strategy_id" not in frame.columns:
            frame["strategy_id"] = strategy_id
        frame.to_parquet(config.strategy_contracts_history_path(strategy_id), index=False)
        created.append(str(config.strategy_contracts_history_path(strategy_id)))

    if config.PAPER_POSITIONS_PATH.exists() and not config.strategy_paper_positions_path(strategy_id).exists():
        safe_write_json_atomic(config.strategy_paper_positions_path(strategy_id), safe_read_json(config.PAPER_POSITIONS_PATH) or {})
        created.append(str(config.strategy_paper_positions_path(strategy_id)))

    if config.PAPER_METRICS_DAILY_PATH.exists() and not config.strategy_paper_metrics_daily_path(strategy_id).exists():
        safe_write_json_atomic(
            config.strategy_paper_metrics_daily_path(strategy_id),
            safe_read_json(config.PAPER_METRICS_DAILY_PATH) or {"by_day": {}},
        )
        created.append(str(config.strategy_paper_metrics_daily_path(strategy_id)))

    report = {
        "ts_utc": _iso_now(),
        "strategy_seeded_from_legacy": strategy_id,
        "created": created,
    }
    safe_write_json_atomic(config.GOVERNANCE_DIR / "strategy_migration_report.json", report)
    return report
