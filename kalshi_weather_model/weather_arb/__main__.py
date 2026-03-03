from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import load_dotenv

# Load .env before importing config-driven modules.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_PROJECT_ROOT / ".env")

from weather_arb import config
from weather_arb.analytics.monitoring import train_gate_snapshot
from weather_arb.connectors.kalshi import (
    KalshiAuthClient,
    KalshiPublicClient,
    parse_dollar_orderbook,
)
from weather_arb.connectors.noaa import NOAAClient
from weather_arb.eval.backtest_gate import evaluate_backtest_gate
from weather_arb.eval.wf_gate import evaluate_wf_gate
from weather_arb.execution.live_engine import run_live_cycle
from weather_arb.execution.metrics import compute_day_metrics, max_drawdown_from_daily_pnl
from weather_arb.execution.paper_engine import run_paper_cycle
from weather_arb.execution.settlement import apply_settlements_to_positions, parse_settlements_payload
from weather_arb.governance import lifecycle, model_registry
from weather_arb.governance.live_routing import live_routing_status
from weather_arb.model.contract_discovery import contracts_to_frame, discover_temperature_contracts
from weather_arb.model.fair_value import (
    SeasonalResidualModel,
    build_residual_training_rows,
    compute_ev_cents,
)
from weather_arb.model.thresholds import calibrate_min_ev_threshold
from weather_arb.pipeline.ingest import (
    append_forecast_rows,
    append_observation_rows,
    append_quote_rows,
    append_signal_rows,
    write_contract_snapshots,
)
from weather_arb.pipeline.scheduler import SchedulerHooks, WeatherScheduler
from weather_arb.reporting.daily_report import generate_daily_report
from weather_arb.strategies import (
    compute_portfolio_leaderboard,
    contract_health_snapshot,
    evaluate_all_strategy_gates,
    evaluate_strategy_gates,
    migrate_legacy_artifacts,
    portfolio_promote,
    run_all_strategies_cycle,
    run_strategy_cycle,
    strategies_summary_snapshot,
)
from weather_arb.utils.io_utils import read_or_create_json, safe_read_json, safe_write_json_atomic
from weather_arb.utils.time_utils import day_key_in_zone


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _date_key_now() -> str:
    return _utc_now().astimezone(ZoneInfo(config.SCHEDULER_TZ)).date().isoformat()


def _run_id_now() -> str:
    return _utc_now().strftime("%Y%m%d_%H%M%S")


def _gate_result_path(name: str) -> Path:
    return config.EVAL_DIR / f"{name}_latest.json"


def _load_or_init_daily_metrics(path: Path) -> dict[str, Any]:
    return read_or_create_json(path, {"by_day": {}, "updated_at": None})


def _load_thresholds() -> dict[str, Any]:
    return read_or_create_json(
        config.THRESHOLD_CONFIG_PATH,
        {
            "updated_at": None,
            "global_min_ev_cents": config.BOOTSTRAP_MIN_EV_CENTS,
            "by_city": {city: config.BOOTSTRAP_MIN_EV_CENTS for city in config.CITIES},
        },
    )


def _append_governance_log(event: dict[str, Any]) -> None:
    payload = read_or_create_json(config.GOVERNANCE_LOG_PATH, {"events": []})
    payload.setdefault("events", []).append(event)
    payload["updated_at"] = _utc_now().isoformat(timespec="seconds")
    safe_write_json_atomic(config.GOVERNANCE_LOG_PATH, payload)


def _latest_model_for_status(status: str) -> dict[str, Any] | None:
    items = model_registry.get_models_by_status(status)
    if not items:
        return None
    items.sort(key=lambda x: str(x.get("updated_at", x.get("created_at", ""))), reverse=True)
    return items[0]


def _ensure_training_model() -> dict[str, Any]:
    existing = _latest_model_for_status("training")
    if existing:
        return existing

    run_id = _run_id_now()
    model_id = model_registry.make_model_id(
        run_id=run_id,
        label_key="weather_temp",
        task_mode="hybrid",
        scope_key="global",
    )
    return model_registry.register_model(
        model_id=model_id,
        run_id=run_id,
        label_key="weather_temp",
        task_mode="hybrid",
        scope_key="global",
        status="training",
        model_dir=str(config.ROOT_DIR),
    )


def _read_all_parquet_rows(dir_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for file_path in sorted(dir_path.glob("*.parquet")):
        try:
            frame = pd.read_parquet(file_path)
        except Exception:
            continue
        rows.extend(frame.to_dict(orient="records"))
    return rows


_CITY_WEATHER_ALIASES: dict[str, list[str]] = {
    "NYC": ["NEW YORK CITY", "NEW YORK", "NYC"],
    "Chicago": ["CHICAGO"],
    "Dallas": ["DALLAS"],
    "Miami": ["MIAMI"],
    "Atlanta": ["ATLANTA"],
    "Seattle": ["SEATTLE"],
}


def _extract_city_from_text(text: str) -> str | None:
    up = text.upper()
    for city, aliases in _CITY_WEATHER_ALIASES.items():
        if any(alias in up for alias in aliases):
            return city
    return None


def _is_target_temperature_event(event_row: dict[str, Any]) -> bool:
    if str(event_row.get("category", "")).strip().lower() != "climate and weather":
        return False
    title = str(event_row.get("title", ""))
    subtitle = str(event_row.get("sub_title", ""))
    text = f"{title} {subtitle}"
    if _extract_city_from_text(text) is None:
        return False
    up = text.upper()
    if "HIGHEST TEMPERATURE" in up or "HIGH TEMPERATURE" in up:
        return True
    return False


def _discover_weather_markets(public_client: KalshiPublicClient) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cursor: str | None = None
    pages = 0
    weather_events: list[dict[str, Any]] = []
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
            if not _is_target_temperature_event(row):
                continue
            event_ticker = str(row.get("event_ticker", "")).strip()
            if not event_ticker:
                continue
            event_tickers.add(event_ticker)
            weather_events.append(row)

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
            merged["category"] = str((event_obj or {}).get("category", "Climate and Weather"))

            market_title = str(merged.get("title", "")).strip()
            if event_title and market_title:
                merged["title"] = f"{event_title} {market_title}"
            elif event_title and not market_title:
                merged["title"] = event_title
            if event_sub and not str(merged.get("subtitle", "")).strip():
                merged["subtitle"] = event_sub
            markets.append(merged)

    metadata = {
        "pages_scanned": pages,
        "weather_events": len(event_tickers),
        "markets": len(markets),
    }
    return markets, metadata


def _load_discovery_payload(public_client: KalshiPublicClient, now_utc: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    cached = safe_read_json(config.CONTRACT_DISCOVERY_CACHE_PATH) or {}
    updated_at = str(cached.get("updated_at", "")).strip()
    cached_markets = cached.get("markets")
    if updated_at and isinstance(cached_markets, list):
        try:
            cache_ts = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            age_minutes = (now_utc - cache_ts).total_seconds() / 60.0
            if age_minutes < float(config.MARKET_DISCOVERY_MINUTES):
                return {"markets": cached_markets}, {
                    "source": "cache",
                    "age_minutes": round(age_minutes, 2),
                    "markets": len(cached_markets),
                }
        except Exception:
            pass

    markets, meta = _discover_weather_markets(public_client)
    cache_payload = {
        "updated_at": now_utc.isoformat(),
        "markets": markets,
        "meta": meta,
    }
    safe_write_json_atomic(config.CONTRACT_DISCOVERY_CACHE_PATH, cache_payload)
    return {"markets": markets}, {"source": "fresh", **meta}


def _city_forecast_daily_max(noaa: NOAAClient) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for city, meta in config.CITY_CONFIG.items():
        raw = noaa.get_hourly_forecast(float(meta["lat"]), float(meta["lon"]))
        periods = raw.get("properties", {}).get("periods", [])
        tz = ZoneInfo(str(meta["tz"]))
        by_day: dict[str, float] = {}
        for p in periods:
            try:
                start_ts = datetime.fromisoformat(str(p.get("startTime", "")).replace("Z", "+00:00"))
                temp_f = float(p.get("temperature"))
            except Exception:
                continue
            day_key = start_ts.astimezone(tz).date().isoformat()
            by_day[day_key] = max(by_day.get(day_key, temp_f), temp_f)
        out[city] = by_day
    return out


def _build_signals_and_quotes(now_utc: datetime) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    public_client = KalshiPublicClient()
    noaa = NOAAClient()

    market_payload, _discovery_meta = _load_discovery_payload(public_client, now_utc)
    contracts, skipped = discover_temperature_contracts(market_payload)

    active_rows = contracts_to_frame(contracts).to_dict(orient="records") if contracts else []
    write_contract_snapshots(active_rows, active_rows)

    forecast_by_city_day = _city_forecast_daily_max(noaa)
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
    thresholds = _load_thresholds()

    quote_rows: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []

    for contract in contracts:
        day_map = forecast_by_city_day.get(contract.city, {})
        if contract.contract_date_local not in day_map:
            continue

        try:
            raw_book = public_client.get_market_orderbook(contract.ticker)
            parsed = parse_dollar_orderbook(raw_book, contract.ticker)
        except Exception as exc:
            skipped.append({"ticker": contract.ticker, "reason": f"orderbook_unavailable: {exc}"})
            continue

        parsed["ts_utc"] = now_utc.isoformat()
        quote_rows.append(parsed)

        forecast_temp = float(day_map[contract.contract_date_local])
        p_fair = residual_model.p_exceeds(
            city=contract.city,
            forecast_temp_f=forecast_temp,
            threshold_f=contract.threshold_f,
            target_date=contract.contract_date_local,
        )

        yes_price = float(parsed["yes_ask_dollars"])
        no_price = float(parsed["no_ask_dollars"])
        est_cost_cents = (config.KALSHI_FEE_PER_CONTRACT_DOLLARS * 100.0) + config.DEFAULT_SLIPPAGE_CENTS

        ev_yes = compute_ev_cents(p_fair=p_fair, p_market=yes_price, est_cost_cents=est_cost_cents)
        ev_no = compute_ev_cents(p_fair=(1.0 - p_fair), p_market=no_price, est_cost_cents=est_cost_cents)

        side = "buy_yes" if ev_yes >= ev_no else "buy_no"
        best_ev = max(ev_yes, ev_no)
        if best_ev <= 0:
            continue

        min_ev_cents = float((thresholds.get("by_city") or {}).get(contract.city, thresholds.get("global_min_ev_cents", config.BOOTSTRAP_MIN_EV_CENTS)))

        signals.append(
            {
                "ticker": contract.ticker,
                "city": contract.city,
                "side": side,
                "p_fair": p_fair,
                "p_mkt": yes_price if side == "buy_yes" else no_price,
                "ev_cents": best_ev,
                "min_ev_cents": min_ev_cents,
                "threshold_f": contract.threshold_f,
                "settlement_ts_utc": contract.settlement_ts_utc.isoformat(),
                "generated_at_utc": now_utc.isoformat(),
            }
        )

    quote_map = {str(r["ticker"]): r for r in quote_rows}
    return signals, quote_map, skipped


def _collect_forecast_rows(noaa: NOAAClient, now: datetime) -> list[dict[str, Any]]:
    forecast_rows: list[dict[str, Any]] = []
    for city, meta in config.CITY_CONFIG.items():
        forecast = noaa.get_hourly_forecast(float(meta["lat"]), float(meta["lon"]))
        periods = forecast.get("properties", {}).get("periods", [])
        for p in periods:
            try:
                start_ts = datetime.fromisoformat(str(p.get("startTime", "")).replace("Z", "+00:00"))
                temp_f = float(p.get("temperature"))
            except Exception:
                continue
            forecast_rows.append(
                {
                    "city": city,
                    "fetched_at_utc": now.isoformat(),
                    "forecast_time_utc": start_ts.isoformat(),
                    "temperature_f": temp_f,
                    "source": "nws",
                }
            )
    return forecast_rows


def _collect_observation_rows(
    noaa: NOAAClient,
    now: datetime,
    *,
    use_history: bool,
) -> list[dict[str, Any]]:
    observation_rows: list[dict[str, Any]] = []
    for city, meta in config.CITY_CONFIG.items():
        if use_history:
            obs = noaa.get_station_observations_history(
                str(meta["station"]),
                lookback_days=config.OBS_LOOKBACK_DAYS,
                page_limit=config.OBS_MAX_PAGES,
                page_size=config.OBS_PAGE_SIZE,
            )
        else:
            # Light sync path: recent observations only; dedupe keeps one row/city/day.
            obs = noaa.get_station_observations(
                str(meta["station"]),
                limit=config.OBS_PAGE_SIZE,
            )

        features = obs.get("features", [])
        tz = ZoneInfo(str(meta["tz"]))
        max_by_day: dict[str, float] = {}
        latest_ts_by_day: dict[str, str] = {}
        for feat in features:
            props = feat.get("properties", {})
            ts = props.get("timestamp")
            temp_c = ((props.get("temperature") or {}).get("value"))
            if ts is None or temp_c is None:
                continue
            try:
                obs_ts = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                temp_f = (float(temp_c) * 9.0 / 5.0) + 32.0
            except Exception:
                continue
            day_key = obs_ts.astimezone(tz).date().isoformat()
            if temp_f > max_by_day.get(day_key, -10_000):
                max_by_day[day_key] = temp_f
                latest_ts_by_day[day_key] = obs_ts.astimezone(timezone.utc).isoformat()

        for day_key, max_temp in max_by_day.items():
            observation_rows.append(
                {
                    "city": city,
                    "obs_date_local": day_key,
                    "max_temp_f": max_temp,
                    "observed_at_utc": latest_ts_by_day[day_key],
                }
            )
    return observation_rows


def _write_daily_metrics(path: Path, date_key: str, day_metrics: dict[str, Any]) -> None:
    payload = _load_or_init_daily_metrics(path)
    payload.setdefault("by_day", {})[date_key] = {
        **dict(payload.get("by_day", {}).get(date_key, {})),
        **day_metrics,
    }
    payload["updated_at"] = _utc_now().isoformat(timespec="seconds")
    safe_write_json_atomic(path, payload)


def _aggregate_gate_metrics(metrics_path: Path, *, starting_equity: float) -> dict[str, Any]:
    payload = safe_read_json(metrics_path) or {}
    by_day = dict(payload.get("by_day", {}))

    trading_days = len(by_day)
    trades = sum(int(v.get("trades", 0) or 0) for v in by_day.values())
    wins = sum(int(v.get("wins", 0) or 0) for v in by_day.values())
    pnl_total = sum(float(v.get("pnl_dollars", 0.0) or 0.0) for v in by_day.values())
    avg_daily_pnl = (pnl_total / trading_days) if trading_days > 0 else 0.0

    roi_values = [float(v.get("roi_per_trade", 0.0) or 0.0) for v in by_day.values() if int(v.get("trades", 0) or 0) > 0]
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


def _week_key_for_ts(ts_utc: str) -> str:
    dt = datetime.fromisoformat(str(ts_utc))
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def _apply_live_closures_to_state(state: dict[str, Any], closed_positions: list[dict[str, Any]]) -> dict[str, Any]:
    if not closed_positions:
        return state

    next_state = dict(state)
    next_state.setdefault("daily_pnl", {})
    next_state.setdefault("weekly_pnl", {})

    equity = float(next_state.get("equity", config.LIVE_STARTING_EQUITY) or config.LIVE_STARTING_EQUITY)
    consecutive_losses = int(next_state.get("consecutive_losses", 0) or 0)

    for pos in closed_positions:
        pnl = float(pos.get("realized_pnl_dollars", 0.0) or 0.0)
        closed_ts = str(pos.get("closed_at_utc", ""))
        if not closed_ts:
            continue
        date_key = day_key_in_zone(datetime.fromisoformat(closed_ts), config.SCHEDULER_TZ)
        week_key = _week_key_for_ts(closed_ts)

        next_state["daily_pnl"][date_key] = float(next_state["daily_pnl"].get(date_key, 0.0) or 0.0) + pnl
        next_state["weekly_pnl"][week_key] = float(next_state["weekly_pnl"].get(week_key, 0.0) or 0.0) + pnl
        equity += pnl

        if pnl < 0:
            consecutive_losses += 1
        else:
            consecutive_losses = 0

    next_state["equity"] = round(equity, 6)
    next_state["consecutive_losses"] = consecutive_losses
    return next_state


def cmd_bootstrap(_args: argparse.Namespace) -> None:
    config.ensure_dirs()
    model_registry.save_registry(model_registry.load_registry())
    lifecycle.save_lifecycle_state(lifecycle.load_lifecycle_state())
    _load_or_init_daily_metrics(config.PAPER_METRICS_DAILY_PATH)
    _load_or_init_daily_metrics(config.LIVE_METRICS_DAILY_PATH)
    _load_thresholds()
    station_map_path = config.CONFIG_DIR / "station_city_map.json"
    if not station_map_path.exists():
        station_map_path.write_text(json.dumps(config.CITY_CONFIG, indent=2), encoding="utf-8")

    migration_report = migrate_legacy_artifacts()
    print(json.dumps({"bootstrap": str(config.ROOT_DIR), "migration": migration_report}, indent=2))


def cmd_discover_contracts(_args: argparse.Namespace) -> None:
    now = _utc_now()
    client = KalshiPublicClient()
    payload, discovery_meta = _load_discovery_payload(client, now)
    contracts, skipped = discover_temperature_contracts(payload)
    active_rows = contracts_to_frame(contracts).to_dict(orient="records") if contracts else []
    write_contract_snapshots(active_rows, active_rows)
    print(
        json.dumps(
            {
                "contracts": len(contracts),
                "skipped": len(skipped),
                "ts": now.isoformat(),
                "discovery": discovery_meta,
            },
            indent=2,
        )
    )


def cmd_ingest(args: argparse.Namespace) -> None:
    now = _utc_now()
    noaa = NOAAClient()
    forecast_rows = _collect_forecast_rows(noaa, now)
    observation_rows = _collect_observation_rows(noaa, now, use_history=True)

    append_forecast_rows(forecast_rows, now)
    append_observation_rows(observation_rows, now)
    out = {"forecast_rows": len(forecast_rows), "observation_rows": len(observation_rows), "start": args.start, "end": args.end}
    _append_governance_log({"ts": now.isoformat(), "event": "ingest", **out})
    print(json.dumps(out, indent=2))


def cmd_ingest_forecasts(_args: argparse.Namespace) -> None:
    now = _utc_now()
    noaa = NOAAClient()
    forecast_rows = _collect_forecast_rows(noaa, now)
    append_forecast_rows(forecast_rows, now)
    out = {"forecast_rows": len(forecast_rows), "observation_rows": 0}
    _append_governance_log({"ts": now.isoformat(), "event": "ingest_forecasts", **out})
    print(json.dumps(out, indent=2))


def cmd_sync_observations(_args: argparse.Namespace) -> None:
    now = _utc_now()
    noaa = NOAAClient()
    observation_rows = _collect_observation_rows(noaa, now, use_history=False)
    append_observation_rows(observation_rows, now)
    out = {"forecast_rows": 0, "observation_rows": len(observation_rows)}
    _append_governance_log({"ts": now.isoformat(), "event": "sync_observations", **out})
    print(json.dumps(out, indent=2))


def cmd_calibrate(_args: argparse.Namespace) -> None:
    state = safe_read_json(config.PAPER_POSITIONS_PATH) or {}
    closed = list(state.get("closed_positions", []))

    rows: list[dict[str, Any]] = []
    for pos in closed:
        rows.append(
            {
                "ev_cents": float(pos.get("entry_ev_cents", config.BOOTSTRAP_MIN_EV_CENTS) or config.BOOTSTRAP_MIN_EV_CENTS),
                "realized_pnl_dollars": float(pos.get("realized_pnl_dollars", 0.0) or 0.0),
                "notional_dollars": abs(float(pos.get("entry_price_dollars", 0.0) or 0.0) * int(pos.get("contracts", 0) or 0)),
                "date_key": str(pos.get("closed_at_utc", "")[:10]),
            }
        )

    outcome = calibrate_min_ev_threshold(rows)
    payload = {
        "updated_at": _utc_now().isoformat(timespec="seconds"),
        "global_min_ev_cents": outcome.min_ev_cents,
        "by_city": {city: outcome.min_ev_cents for city in config.CITIES},
        "evidence": {
            "trades": outcome.trades,
            "avg_daily_pnl": outcome.avg_daily_pnl,
            "roi_per_trade": outcome.roi_per_trade,
            "score": outcome.score,
        },
    }
    safe_write_json_atomic(config.THRESHOLD_CONFIG_PATH, payload)
    print(json.dumps(payload, indent=2))


def cmd_run_train_gate(_args: argparse.Namespace) -> dict[str, Any]:
    snap = train_gate_snapshot()
    result = dict(snap.get("gate", {}))
    result["details"] = snap.get("city_stats", {})
    status: dict[str, Any] | None = None
    if bool(result.get("pass", False)):
        model = _ensure_training_model()
        status = lifecycle.apply_train_gate(
            model_id=str(model["model_id"]),
            passed=True,
            reason="; ".join(result.get("reasons", [])),
        )
    out = {
        **result,
        "model_status": status.get("status") if status else "training_pending_data",
        "days_remaining_by_city": snap.get("days_remaining_by_city", {}),
        "max_days_remaining": snap.get("max_days_remaining", 0),
    }
    safe_write_json_atomic(_gate_result_path("train"), {**result, "model": status, "snapshot": snap})
    _append_governance_log({"ts": _utc_now().isoformat(), "event": "train_gate", **out})
    print(json.dumps(out, indent=2))
    return out


def cmd_run_wf_gate(_args: argparse.Namespace) -> dict[str, Any]:
    model = _latest_model_for_status("validating")
    if not model:
        out = {
            "skipped": True,
            "reason": "no model in validating status; run train gate first",
        }
        safe_write_json_atomic(_gate_result_path("wf"), out)
        _append_governance_log({"ts": _utc_now().isoformat(), "event": "wf_gate", **out})
        print(json.dumps(out, indent=2))
        return out

    signal_rows = _read_all_parquet_rows(config.SIGNALS_DIR)
    if not signal_rows:
        windows: list[dict[str, Any]] = []
    else:
        frame = pd.DataFrame(signal_rows)
        frame["day"] = frame["generated_at_utc"].astype(str).str[:10]
        windows = []
        for day, grp in frame.groupby("day"):
            windows.append(
                {
                    "window": day,
                    "feasible": len(grp) > 0,
                    "ev_day": float(grp["ev_cents"].mean() / 100.0),
                }
            )

    result = evaluate_wf_gate(windows)
    status = lifecycle.apply_wf_gate(model_id=str(model["model_id"]), passed=bool(result["pass"]), reason="; ".join(result["reasons"]))
    out = {**result, "model_status": status.get("status")}
    safe_write_json_atomic(_gate_result_path("wf"), {**result, "model": status})
    _append_governance_log({"ts": _utc_now().isoformat(), "event": "wf_gate", **out})
    print(json.dumps(out, indent=2))
    return out


def cmd_run_backtest_gate(_args: argparse.Namespace) -> dict[str, Any]:
    model = _latest_model_for_status("wf_passed")
    if not model:
        out = {
            "skipped": True,
            "reason": "no model in wf_passed status; run wf gate first",
        }
        safe_write_json_atomic(_gate_result_path("backtest"), out)
        _append_governance_log({"ts": _utc_now().isoformat(), "event": "backtest_gate", **out})
        print(json.dumps(out, indent=2))
        return out

    # Backtest input preference: explicit backtest trades file, then closed paper positions fallback.
    trades_rows = _read_all_parquet_rows(config.EVAL_DIR / "backtest")
    if not trades_rows:
        state = safe_read_json(config.PAPER_POSITIONS_PATH) or {}
        for pos in state.get("closed_positions", []):
            trades_rows.append(
                {
                    "pnl_dollars": float(pos.get("realized_pnl_dollars", 0.0) or 0.0),
                    "notional_dollars": abs(float(pos.get("entry_price_dollars", 0.0) or 0.0) * int(pos.get("contracts", 0) or 0)),
                    "date_key": str(pos.get("closed_at_utc", "")[:10]),
                }
            )

    result = evaluate_backtest_gate(trades_rows)
    status = lifecycle.apply_backtest_gate(model_id=str(model["model_id"]), passed=bool(result["pass"]), reason="; ".join(result["reasons"]))
    out = {**result, "model_status": status.get("status")}
    safe_write_json_atomic(_gate_result_path("backtest"), {**result, "model": status})
    _append_governance_log({"ts": _utc_now().isoformat(), "event": "backtest_gate", **out})
    print(json.dumps(out, indent=2))
    return out


def cmd_run_daily_gates(_args: argparse.Namespace) -> dict[str, Any]:
    run_ts = _utc_now().isoformat()
    steps = [
        ("train_gate", cmd_run_train_gate),
        ("wf_gate", cmd_run_wf_gate),
        ("backtest_gate", cmd_run_backtest_gate),
    ]
    step_results: dict[str, Any] = {}
    for name, fn in steps:
        try:
            out = fn(argparse.Namespace())
            step_results[name] = out if isinstance(out, dict) else {"ok": True}
        except Exception as exc:
            step_results[name] = {"ok": False, "error": str(exc)}

    summary = {
        "ts": run_ts,
        "event": "daily_gate_eval",
        "steps": {
            name: {
                "pass": bool((payload or {}).get("pass")) if isinstance(payload, dict) else None,
                "skipped": bool((payload or {}).get("skipped")) if isinstance(payload, dict) else False,
                "reason": (payload or {}).get("reason") if isinstance(payload, dict) else None,
                "model_status": (payload or {}).get("model_status") if isinstance(payload, dict) else None,
                "error": (payload or {}).get("error") if isinstance(payload, dict) else None,
            }
            for name, payload in step_results.items()
        },
    }
    _append_governance_log(summary)
    print(json.dumps(summary, indent=2, default=str))
    return summary


def cmd_paper_cycle(_args: argparse.Namespace) -> None:
    now = _utc_now()
    signals, quote_map, skipped = _build_signals_and_quotes(now)
    append_quote_rows(list(quote_map.values()), now)
    append_signal_rows(signals, now)

    model = (
        _latest_model_for_status("wf_passed")
        or _latest_model_for_status("qualified")
        or _latest_model_for_status("paper")
        or _latest_model_for_status("champion_live")
    )
    if not model:
        out = {
            "skipped": True,
            "reason": "no eligible model for paper trading",
            "signals": len(signals),
            "skipped_contracts": len(skipped),
            "quotes": len(quote_map),
        }
        _append_governance_log({"ts": now.isoformat(), "event": "paper_cycle_data_only", **out})
        print(json.dumps(out, indent=2))
        return

    summary = run_paper_cycle(signals, quote_map, now)
    paper_state = safe_read_json(config.PAPER_POSITIONS_PATH) or {}
    metrics = compute_day_metrics(list(paper_state.get("closed_positions", [])), _date_key_now())
    metrics["date_key"] = _date_key_now()
    _write_daily_metrics(config.PAPER_METRICS_DAILY_PATH, _date_key_now(), metrics)

    out = {
        "summary": summary,
        "signals": len(signals),
        "skipped_contracts": len(skipped),
        "model_status": model.get("status"),
    }
    _append_governance_log({"ts": now.isoformat(), "event": "paper_cycle", **out})
    print(json.dumps(out, indent=2))


def cmd_strategy_run(args: argparse.Namespace) -> dict[str, Any]:
    now = _utc_now()
    if bool(getattr(args, "all", False)):
        out = run_all_strategies_cycle(now)
        _append_governance_log({"ts": now.isoformat(), "event": "strategy_cycle_all", **out})
        print(json.dumps(out, indent=2, default=str))
        return out

    strategy_id = str(getattr(args, "strategy", "weather_temp_high") or "weather_temp_high")
    result = run_strategy_cycle(strategy_id, now_utc=now)
    out = {"ts_utc": now.isoformat(timespec="seconds"), "count": 1, "results": {strategy_id: result}}
    _append_governance_log({"ts": now.isoformat(), "event": "strategy_cycle", "strategy_id": strategy_id, "result": result})
    print(json.dumps(out, indent=2, default=str))
    return out


def cmd_strategy_gates(args: argparse.Namespace) -> dict[str, Any]:
    now = _utc_now()
    if bool(getattr(args, "all", False)):
        out = evaluate_all_strategy_gates(now)
        _append_governance_log({"ts": now.isoformat(), "event": "strategy_gates_all", **out})
        print(json.dumps(out, indent=2, default=str))
        return out

    strategy_id = str(getattr(args, "strategy", "weather_temp_high") or "weather_temp_high")
    result = evaluate_strategy_gates(strategy_id, now_utc=now)
    out = {"ts_utc": now.isoformat(timespec="seconds"), "results": {strategy_id: result}}
    _append_governance_log({"ts": now.isoformat(), "event": "strategy_gates", "strategy_id": strategy_id, "result": result})
    print(json.dumps(out, indent=2, default=str))
    return out


def cmd_contract_health(args: argparse.Namespace) -> dict[str, Any]:
    strategy_id = str(getattr(args, "strategy", "weather_temp_high") or "weather_temp_high")
    out = contract_health_snapshot(strategy_id)
    print(json.dumps(out, indent=2, default=str))
    return out


def cmd_portfolio_rank(_args: argparse.Namespace) -> dict[str, Any]:
    now = _utc_now()
    out = compute_portfolio_leaderboard(now)
    _append_governance_log({"ts": now.isoformat(), "event": "portfolio_rank", **out})
    print(json.dumps(out, indent=2, default=str))
    return out


def cmd_portfolio_promote(args: argparse.Namespace) -> dict[str, Any]:
    now = _utc_now()
    force = bool(getattr(args, "force", False))
    out = portfolio_promote(now, force=force)
    _append_governance_log({"ts": now.isoformat(), "event": "portfolio_promote", **out})
    print(json.dumps(out, indent=2, default=str))
    return out


def cmd_governance_eval(_args: argparse.Namespace) -> None:
    model = (
        _latest_model_for_status("qualified")
        or _latest_model_for_status("paper")
        or _latest_model_for_status("champion_live")
    )
    if not model:
        print(json.dumps({"skipped": True, "reason": "no model available for governance evaluation"}, indent=2))
        return

    metrics = _aggregate_gate_metrics(
        config.PAPER_METRICS_DAILY_PATH,
        starting_equity=config.PAPER_ACCOUNT_SIZE,
    )
    result = lifecycle.apply_paper_metrics(str(model["model_id"]), metrics)

    promoted = None
    latest = model_registry.get_model(str(model["model_id"]))
    if latest and str(latest.get("status")) == "paper":
        promoted = lifecycle.auto_promote_if_ready(str(model["model_id"]), scope_key="global")

    event = {
        "ts": _utc_now().isoformat(timespec="seconds"),
        "model_id": model.get("model_id"),
        "metrics": metrics,
        "result": result,
        "promoted": promoted,
    }
    _append_governance_log(event)
    print(json.dumps(event, indent=2, default=str))


def cmd_live_cycle(_args: argparse.Namespace) -> None:
    live_status = live_routing_status()
    champion = _latest_model_for_status("champion_live")
    strategy_champion = str(live_status.get("champion_id") or "").strip()

    if champion is None and not strategy_champion:
        print(
            json.dumps(
                {
                    "skipped": True,
                    "reason": "no_live_champion_selected",
                    "live_routing": live_status,
                },
                indent=2,
            )
        )
        return

    now = _utc_now()
    signals, quote_map, skipped = _build_signals_and_quotes(now)
    append_quote_rows(list(quote_map.values()), now)
    append_signal_rows(signals, now)

    live_enabled = bool(live_status.get("enabled", False))
    auth_client = KalshiAuthClient() if live_enabled else None
    summary = run_live_cycle(
        signals,
        quote_map,
        now,
        auth_client=auth_client,
        live_routing_enabled=live_enabled,
    )

    # Settlement reconciliation when auth is available.
    if auth_client is not None:
        settlements_payload = auth_client.get_settlements(limit=500)
        settlements = parse_settlements_payload(settlements_payload)
        if settlements:
            state = read_or_create_json(config.LIVE_POSITIONS_PATH, {})
            still_open, closed = apply_settlements_to_positions(
                list(state.get("open_positions", [])),
                settlements,
                now,
            )
            state["open_positions"] = still_open
            state.setdefault("closed_positions", []).extend(closed)
            state = _apply_live_closures_to_state(state, closed)
            safe_write_json_atomic(config.LIVE_POSITIONS_PATH, state)

    live_state = safe_read_json(config.LIVE_POSITIONS_PATH) or {}
    metrics = compute_day_metrics(list(live_state.get("closed_positions", [])), _date_key_now())
    metrics["date_key"] = _date_key_now()
    _write_daily_metrics(config.LIVE_METRICS_DAILY_PATH, _date_key_now(), metrics)

    current = _aggregate_gate_metrics(
        config.LIVE_METRICS_DAILY_PATH,
        starting_equity=config.LIVE_STARTING_EQUITY,
    )
    if champion is not None:
        baseline = dict(champion.get("paper_metrics") or {})
        degradation = lifecycle.apply_live_degradation(str(champion["model_id"]), baseline, current)
    else:
        degradation = {"skipped": True, "reason": "no_champion_live_model"}

    out = {
        "summary": summary,
        "signals": len(signals),
        "skipped_contracts": len(skipped),
        "degradation": degradation,
        "live_mode_enabled": live_enabled,
        "live_routing": live_status,
        "strategy_champion": strategy_champion or None,
        "model_champion_id": champion.get("model_id") if champion else None,
    }
    _append_governance_log({"ts": now.isoformat(), "event": "live_cycle", **out})
    print(json.dumps(out, indent=2, default=str))

def cmd_report(args: argparse.Namespace) -> None:
    date_key = args.date or _date_key_now()
    out_path = generate_daily_report(date_key)
    print(str(out_path))


def cmd_api_check(_args: argparse.Namespace) -> None:
    out: dict[str, Any] = {
        "ts_utc": _utc_now().isoformat(timespec="seconds"),
        "config": {
            "base_url": config.KALSHI_API_BASE_URL,
            "api_key_present": bool(config.KALSHI_API_KEY),
            "rsa_key_path_present": bool(config.KALSHI_RSA_KEY_PATH),
            "rsa_key_inline_present": bool(config.KALSHI_RSA_PRIVATE_KEY),
        },
        "public": {},
        "auth": {},
    }

    try:
        pub = KalshiPublicClient()
        markets_payload = pub.get_markets(status="open", limit=1)
        rows = markets_payload.get("markets")
        if not isinstance(rows, list):
            rows = markets_payload.get("data")
        out["public"] = {
            "ok": True,
            "markets_payload_keys": sorted(list(markets_payload.keys()))[:20],
            "sample_count": len(rows) if isinstance(rows, list) else 0,
        }
    except Exception as exc:
        out["public"] = {"ok": False, "error": str(exc)}

    has_auth_cfg = bool(config.KALSHI_API_KEY and (config.KALSHI_RSA_KEY_PATH or config.KALSHI_RSA_PRIVATE_KEY))
    if not has_auth_cfg:
        out["auth"] = {"ok": False, "skipped": True, "reason": "missing auth env config"}
        print(json.dumps(out, indent=2, default=str))
        return

    try:
        auth = KalshiAuthClient()
        positions_payload = auth.get_positions()
        rows = positions_payload.get("positions")
        if not isinstance(rows, list):
            rows = positions_payload.get("data")
        out["auth"] = {
            "ok": True,
            "positions_payload_keys": sorted(list(positions_payload.keys()))[:20],
            "sample_count": len(rows) if isinstance(rows, list) else 0,
        }
    except Exception as exc:
        out["auth"] = {"ok": False, "error": str(exc)}

    print(json.dumps(out, indent=2, default=str))


def cmd_dashboard(args: argparse.Namespace) -> None:
    import uvicorn
    from weather_arb.dashboard.app import create_app as create_dashboard_app

    app = create_dashboard_app()
    uvicorn.run(
        app,
        host=str(args.host),
        port=int(args.port),
        reload=bool(args.reload),
    )


def cmd_scheduler(_args: argparse.Namespace) -> None:
    hooks = SchedulerHooks(
        ingest_hook=lambda: cmd_ingest_forecasts(argparse.Namespace()),
        cycle_hook=lambda: cmd_strategy_run(argparse.Namespace(all=True, strategy=None)),
        obs_sync_hook=lambda: cmd_sync_observations(argparse.Namespace()),
        gate_eval_hook=lambda: cmd_strategy_gates(argparse.Namespace(all=True, strategy=None)),
        settlement_hook=lambda: cmd_live_cycle(argparse.Namespace()),
        governance_hook=lambda: (
            cmd_portfolio_rank(argparse.Namespace()),
            cmd_portfolio_promote(argparse.Namespace(force=False)),
        ),
        calibration_hook=lambda: cmd_calibrate(argparse.Namespace()),
    )
    scheduler = WeatherScheduler(hooks)
    scheduler.configure()
    scheduler.run_forever()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kalshi weather model CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("bootstrap")
    sub.add_parser("discover-contracts")

    p_ingest = sub.add_parser("ingest")
    p_ingest.add_argument("--start", required=False)
    p_ingest.add_argument("--end", required=False)
    sub.add_parser("ingest-forecasts")
    sub.add_parser("sync-observations")

    sub.add_parser("calibrate")
    sub.add_parser("run-train-gate")
    sub.add_parser("run-wf-gate")
    sub.add_parser("run-backtest-gate")
    sub.add_parser("run-daily-gates")
    sub.add_parser("paper-cycle")

    p_strategy_run = sub.add_parser("strategy-run")
    g_run = p_strategy_run.add_mutually_exclusive_group(required=True)
    g_run.add_argument("--strategy", choices=config.WEATHER_STRATEGY_IDS)
    g_run.add_argument("--all", action="store_true")

    p_strategy_gates = sub.add_parser("strategy-gates")
    g_gates = p_strategy_gates.add_mutually_exclusive_group(required=True)
    g_gates.add_argument("--strategy", choices=config.WEATHER_STRATEGY_IDS)
    g_gates.add_argument("--all", action="store_true")

    p_contract_health = sub.add_parser("contract-health")
    p_contract_health.add_argument("--strategy", required=True, choices=config.WEATHER_STRATEGY_IDS)

    sub.add_parser("portfolio-rank")
    p_portfolio_promote = sub.add_parser("portfolio-promote")
    p_portfolio_promote.add_argument("--force", action="store_true")

    sub.add_parser("governance-eval")
    sub.add_parser("live-cycle")
    sub.add_parser("api-check")

    p_report = sub.add_parser("report")
    p_report.add_argument("--date", required=False)

    p_dash = sub.add_parser("dashboard")
    p_dash.add_argument("--host", default="127.0.0.1")
    p_dash.add_argument("--port", type=int, default=8077)
    p_dash.add_argument("--reload", action="store_true")

    sub.add_parser("scheduler")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    commands = {
        "bootstrap": cmd_bootstrap,
        "discover-contracts": cmd_discover_contracts,
        "ingest": cmd_ingest,
        "ingest-forecasts": cmd_ingest_forecasts,
        "sync-observations": cmd_sync_observations,
        "calibrate": cmd_calibrate,
        "run-train-gate": cmd_run_train_gate,
        "run-wf-gate": cmd_run_wf_gate,
        "run-backtest-gate": cmd_run_backtest_gate,
        "run-daily-gates": cmd_run_daily_gates,
        "paper-cycle": cmd_paper_cycle,
        "strategy-run": cmd_strategy_run,
        "strategy-gates": cmd_strategy_gates,
        "contract-health": cmd_contract_health,
        "portfolio-rank": cmd_portfolio_rank,
        "portfolio-promote": cmd_portfolio_promote,
        "governance-eval": cmd_governance_eval,
        "live-cycle": cmd_live_cycle,
        "api-check": cmd_api_check,
        "report": cmd_report,
        "dashboard": cmd_dashboard,
        "scheduler": cmd_scheduler,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
