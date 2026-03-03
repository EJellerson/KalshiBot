from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import json

from weather_arb import config
from weather_arb.connectors.kalshi import KalshiAuthClient
from weather_arb.risk.limits import (
    can_open_more,
    contracts_for_notional,
    daily_stop_hit,
    depth_ok,
    resolve_live_limits_for_day,
    spread_ok,
    weekly_stop_hit,
)
from weather_arb.types import MarketQuote
from weather_arb.utils.io_utils import read_or_create_json, safe_write_json_atomic
from weather_arb.utils.time_utils import day_key_in_zone


def _default_live_state() -> dict[str, Any]:
    return {
        "equity": config.LIVE_STARTING_EQUITY,
        "equity_source": "config",
        "last_equity_sync_day": "",
        "open_positions": [],
        "closed_positions": [],
        "daily_pnl": {},
        "weekly_pnl": {},
        "consecutive_losses": 0,
        "next_position_id": 1,
        "last_limits_day": "",
        "live_limits": {},
    }


def _week_key(now_utc: datetime) -> str:
    y, w, _ = now_utc.isocalendar()
    return f"{y}-W{w:02d}"


def _to_quote(raw: dict[str, Any]) -> MarketQuote:
    return MarketQuote(
        ticker=str(raw["ticker"]),
        ts_utc=datetime.fromisoformat(str(raw.get("ts_utc") or datetime.now(timezone.utc).isoformat())),
        yes_bid_dollars=float(raw["yes_bid_dollars"]),
        yes_ask_dollars=float(raw["yes_ask_dollars"]),
        no_bid_dollars=float(raw["no_bid_dollars"]),
        no_ask_dollars=float(raw["no_ask_dollars"]),
        yes_bid_size=int(raw.get("yes_bid_size", 0) or 0),
        yes_ask_size=int(raw.get("yes_ask_size", 0) or 0),
        no_bid_size=int(raw.get("no_bid_size", 0) or 0),
        no_ask_size=int(raw.get("no_ask_size", 0) or 0),
    )


def _entry_price(quote: MarketQuote, side: str) -> float:
    return quote.yes_ask_dollars if side == "buy_yes" else quote.no_ask_dollars


def _exit_price(quote: MarketQuote, side: str) -> float:
    return quote.yes_bid_dollars if side == "buy_yes" else quote.no_bid_dollars


def _should_exit(pos: dict[str, Any], signal_map: dict[str, float], now_utc: datetime) -> bool:
    ticker = str(pos.get("ticker", ""))
    if ticker in signal_map and signal_map[ticker] <= config.EXIT_EV_CENTS:
        return True

    max_hold = datetime.fromisoformat(str(pos.get("max_hold_until_utc")))
    if now_utc >= max_hold:
        return True

    settlement_ts = datetime.fromisoformat(str(pos.get("settlement_ts_utc")))
    if now_utc >= settlement_ts - timedelta(hours=config.SETTLEMENT_CUTOFF_HOURS):
        return True

    return False


def _close_side_from_position_side(position_side: str) -> str:
    side = str(position_side or "").strip().lower()
    if side == "buy_yes":
        return "yes"
    if side == "buy_no":
        return "no"
    raise ValueError(f"unsupported position side: {position_side}")


def _exit_filled(broker_resp: dict[str, Any] | None) -> bool:
    if not isinstance(broker_resp, dict):
        return False
    status_sources = [broker_resp, broker_resp.get("order"), broker_resp.get("data")]
    status_value = ""
    for source in status_sources:
        if not isinstance(source, dict):
            continue
        status_value = str(source.get("status") or source.get("state") or "").strip().lower()
        if status_value:
            break
    if status_value:
        if status_value in {"executed", "filled", "complete", "completed", "closed", "matched"}:
            return True
        if status_value in {"open", "resting", "pending", "partially_filled", "cancelled", "canceled", "rejected"}:
            return False
    # Backward-compatible fallback for thin test doubles that don't include status.
    return bool(broker_resp.get("ok", True))


def _extract_first_float(payload: dict[str, Any], keys: list[str]) -> float | None:
    containers: list[dict[str, Any]] = [payload]
    for nested_key in ("portfolio", "account", "summary", "data"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            containers.append(nested)

    for container in containers:
        for key in keys:
            value = container.get(key)
            try:
                parsed = float(value)
            except Exception:
                continue
            if parsed > 0:
                return parsed
    return None


def _sync_live_equity_from_api(
    state: dict[str, Any],
    now_utc: datetime,
    auth_client: KalshiAuthClient | None,
    *,
    live_routing_enabled: bool | None = None,
) -> dict[str, Any]:
    effective_live_routing = (
        bool(config.ALLOW_LIVE_TRADING)
        if live_routing_enabled is None
        else bool(live_routing_enabled)
    )
    if not config.LIVE_EQUITY_SYNC_ENABLED:
        return state
    if not effective_live_routing or auth_client is None:
        return state

    today_key = day_key_in_zone(now_utc, config.SCHEDULER_TZ)
    if str(state.get("last_equity_sync_day", "")) == today_key:
        return state

    try:
        payload = auth_client.get_positions()
    except Exception:
        return state

    equity = _extract_first_float(
        payload if isinstance(payload, dict) else {},
        keys=[
            "equity_dollars",
            "portfolio_value_dollars",
            "balance_dollars",
            "cash_balance_dollars",
            "equity",
            "portfolio_value",
            "balance",
            "cash",
        ],
    )
    if equity is None:
        return state

    next_state = dict(state)
    next_state["equity"] = round(float(equity), 6)
    next_state["equity_source"] = "api"
    next_state["last_equity_sync_day"] = today_key
    return next_state


def run_live_cycle(
    signals: list[dict[str, Any]],
    quote_map: dict[str, dict[str, Any]],
    now_utc: datetime,
    *,
    auth_client: KalshiAuthClient | None = None,
    state_path: Path = config.LIVE_POSITIONS_PATH,
    blotter_dir: Path = config.LIVE_BLOTTER_DIR,
    live_routing_enabled: bool | None = None,
) -> dict[str, Any]:
    effective_live_routing = (
        bool(config.ALLOW_LIVE_TRADING)
        if live_routing_enabled is None
        else bool(live_routing_enabled)
    )
    state = read_or_create_json(state_path, _default_live_state())
    state = _sync_live_equity_from_api(
        state,
        now_utc,
        auth_client,
        live_routing_enabled=effective_live_routing,
    )
    equity = float(state.get("equity", config.LIVE_STARTING_EQUITY) or config.LIVE_STARTING_EQUITY)
    limits, state = resolve_live_limits_for_day(equity, now_utc, state, tz_name=config.SCHEDULER_TZ)

    date_key = day_key_in_zone(now_utc, config.SCHEDULER_TZ)
    week_key = _week_key(now_utc)
    day_pnl = float(state.get("daily_pnl", {}).get(date_key, 0.0) or 0.0)
    week_pnl = float(state.get("weekly_pnl", {}).get(week_key, 0.0) or 0.0)

    if daily_stop_hit(day_pnl, limits) or weekly_stop_hit(week_pnl, limits):
        safe_write_json_atomic(state_path, state)
        return {
            "opened": 0,
            "closed": 0,
            "blocked": True,
            "reason": "loss_stop",
            "limits": state.get("live_limits", {}),
        }
    if int(state.get("consecutive_losses", 0) or 0) >= limits.consecutive_loss_halt:
        safe_write_json_atomic(state_path, state)
        return {
            "opened": 0,
            "closed": 0,
            "blocked": True,
            "reason": "consecutive_loss_halt",
            "limits": state.get("live_limits", {}),
        }
    if not effective_live_routing:
        safe_write_json_atomic(state_path, state)
        return {
            "opened": 0,
            "closed": 0,
            "blocked": True,
            "reason": "live_routing_disabled",
            "limits": state.get("live_limits", {}),
        }

    open_positions = list(state.get("open_positions", []))
    closed_positions = list(state.get("closed_positions", []))

    signal_ev_map: dict[str, float] = {}
    for raw in signals:
        ticker = str(raw.get("ticker", ""))
        signal_ev_map[ticker] = abs(float(raw.get("ev_cents", 0.0) or 0.0))

    closed = 0
    exit_order_events: list[dict[str, Any]] = []
    kept_open: list[dict[str, Any]] = []
    for pos in open_positions:
        ticker = str(pos.get("ticker", ""))
        quote_raw = quote_map.get(ticker)
        if not quote_raw:
            kept_open.append(pos)
            continue
        quote = _to_quote(quote_raw)
        if not _should_exit(pos, signal_ev_map, now_utc):
            kept_open.append(pos)
            continue

        side = str(pos.get("side", ""))
        contracts = int(pos.get("contracts", 0) or 0)
        if contracts <= 0:
            kept_open.append(pos)
            continue

        exit_px = _exit_price(quote, side)
        close_side = _close_side_from_position_side(side)
        payload = {
            "ticker": ticker,
            "side": close_side,
            "action": "sell",
            "count": contracts,
            "yes_price_dollars": exit_px if close_side == "yes" else None,
            "no_price_dollars": exit_px if close_side == "no" else None,
            "reduce_only": True,
            "time_in_force": "fill_or_kill",
        }
        if auth_client is None:
            raise RuntimeError("live routing enabled but no auth client was provided")
        try:
            broker_resp = auth_client.place_order(**payload)
            filled = _exit_filled(broker_resp)
            exit_error = ""
        except Exception as exc:
            broker_resp = {}
            filled = False
            exit_error = str(exc)

        exit_order_events.append(
            {
                "ticker": ticker,
                "position_id": pos.get("position_id"),
                "side": close_side,
                "contracts": contracts,
                "exit_price": exit_px,
                "filled": bool(filled),
                "error": exit_error or None,
            }
        )
        if not filled:
            pending = dict(pos)
            pending["pending_exit_submitted_at_utc"] = now_utc.isoformat()
            pending["pending_exit_response"] = broker_resp or {}
            if exit_error:
                pending["pending_exit_error"] = exit_error
            kept_open.append(pending)
            continue

        entry_px = float(pos.get("entry_price_dollars", 0.0) or 0.0)
        fees = contracts * config.KALSHI_FEE_PER_CONTRACT_DOLLARS
        pnl = (exit_px - entry_px) * contracts - fees
        out = dict(pos)
        out["status"] = "closed"
        out["closed_at_utc"] = now_utc.isoformat()
        out["close_price_dollars"] = exit_px
        out["realized_pnl_dollars"] = float(pnl)
        out["close_reason"] = "signal_or_time"
        out["live_exit_order_response"] = broker_resp or {}
        closed_positions.append(out)
        closed += 1

        state.setdefault("daily_pnl", {})[date_key] = float(state.get("daily_pnl", {}).get(date_key, 0.0) or 0.0) + pnl
        state.setdefault("weekly_pnl", {})[week_key] = float(state.get("weekly_pnl", {}).get(week_key, 0.0) or 0.0) + pnl
        state["equity"] = float(state.get("equity", config.LIVE_STARTING_EQUITY) or config.LIVE_STARTING_EQUITY) + pnl
        if pnl < 0:
            state["consecutive_losses"] = int(state.get("consecutive_losses", 0) or 0) + 1
        else:
            state["consecutive_losses"] = 0

    open_positions = kept_open
    open_notional = sum(
        abs(float(p.get("entry_price_dollars", 0.0) or 0.0) * int(p.get("contracts", 0) or 0))
        for p in open_positions
    )

    opened = 0
    order_events: list[dict[str, Any]] = []

    for raw in signals:
        ticker = str(raw.get("ticker", ""))
        if any(str(p.get("ticker", "")) == ticker for p in open_positions):
            continue
        if not can_open_more(len(open_positions), limits):
            break

        ev_cents = float(raw.get("ev_cents", 0.0) or 0.0)
        if ev_cents < float(raw.get("min_ev_cents", config.BOOTSTRAP_MIN_EV_CENTS)):
            continue

        side = str(raw.get("side", "buy_yes"))
        quote_raw = quote_map.get(ticker)
        if not quote_raw:
            continue
        quote = _to_quote(quote_raw)
        if not spread_ok(quote, side=side) or not depth_ok(quote):
            continue

        entry_price = _entry_price(quote, side)
        contracts = contracts_for_notional(entry_price, limits.max_position_dollars)
        if contracts <= 0:
            continue

        new_notional = open_notional + (entry_price * contracts)
        if equity > 0 and (new_notional / equity) > config.LIVE_MAX_NOTIONAL_UTILIZATION:
            continue

        placed = False
        broker_resp: dict[str, Any] | None = None
        if effective_live_routing:
            if auth_client is None:
                raise RuntimeError("live routing enabled but no auth client was provided")
            payload = {
                "ticker": ticker,
                "side": side,
                "count": contracts,
                "yes_price_dollars": entry_price if side == "buy_yes" else None,
                "no_price_dollars": entry_price if side == "buy_no" else None,
            }
            broker_resp = auth_client.place_order(**payload)
            placed = True

        pos_id = int(state.get("next_position_id", 1) or 1)
        state["next_position_id"] = pos_id + 1
        settlement_ts = raw.get("settlement_ts_utc") or (now_utc + timedelta(days=1)).isoformat()
        max_hold = min(
            now_utc + timedelta(hours=config.MAX_HOLD_HOURS),
            datetime.fromisoformat(str(settlement_ts)) - timedelta(hours=config.SETTLEMENT_CUTOFF_HOURS),
        )

        pos = {
            "position_id": f"live_{pos_id}",
            "ticker": ticker,
            "city": str(raw.get("city", "")),
            "side": side,
            "contracts": contracts,
            "entry_price_dollars": entry_price,
            "entry_fees_dollars": contracts * config.KALSHI_FEE_PER_CONTRACT_DOLLARS,
            "opened_at_utc": now_utc.isoformat(),
            "max_hold_until_utc": max_hold.isoformat(),
            "settlement_ts_utc": settlement_ts,
            "status": "open",
            "live_order_submitted": placed,
            "live_order_response": broker_resp or {},
        }
        open_positions.append(pos)
        open_notional = new_notional
        opened += 1
        order_events.append(
            {
                "ticker": ticker,
                "side": side,
                "contracts": contracts,
                "entry_price": entry_price,
                "live_order_submitted": placed,
            }
        )

    state["open_positions"] = open_positions
    state["closed_positions"] = closed_positions
    safe_write_json_atomic(state_path, state)

    blotter_dir.mkdir(parents=True, exist_ok=True)
    line_path = blotter_dir / f"live_blotter_{date_key}.jsonl"
    with line_path.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "ts": now_utc.isoformat(),
                    "opened": opened,
                    "closed": closed,
                    "orders": order_events,
                    "exit_orders": exit_order_events,
                    "limits": state.get("live_limits", {}),
                },
                default=str,
            )
            + "\n"
        )

    return {
        "opened": opened,
        "closed": closed,
        "orders": order_events,
        "exit_orders": exit_order_events,
        "limits": state.get("live_limits", {}),
    }
