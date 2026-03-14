from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import json

from weather_arb import config
from weather_arb.connectors.kalshi import KalshiAuthClient
from weather_arb.fees import kalshi_trading_fee_dollars, split_entry_fees_dollars
from weather_arb.risk.limits import (
    cap_contracts_to_top_of_book,
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


def _parse_iso_utc(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_quote(raw: dict[str, Any]) -> MarketQuote:
    ts = _parse_iso_utc(raw.get("ts_utc")) or datetime.now(timezone.utc)
    return MarketQuote(
        ticker=str(raw["ticker"]),
        ts_utc=ts,
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
    if ticker in signal_map:
        if signal_map[ticker] <= config.EXIT_EV_CENTS:
            return True
    else:
        opened_at = _parse_iso_utc(pos.get("opened_at_utc"))
        if opened_at is not None:
            age_hours = max(0.0, (now_utc - opened_at).total_seconds() / 3600.0)
            if age_hours >= float(config.ORPHAN_EXIT_HOURS):
                return True

    max_hold = _parse_iso_utc(pos.get("max_hold_until_utc"))
    if max_hold is None:
        return True
    if now_utc >= max_hold:
        return True

    settlement_ts = _parse_iso_utc(pos.get("settlement_ts_utc"))
    if settlement_ts is None:
        return True
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
    return bool(broker_resp.get("ok", False))


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


def _entry_status(position: dict[str, Any]) -> str:
    status = str(position.get("entry_status", "")).strip().lower()
    if status in {"pending_confirmation", "confirmed", "unconfirmed"}:
        return status
    return "confirmed"


def _extract_broker_position_tickers(payload: dict[str, Any] | None) -> set[str]:
    tickers: set[str] = set()
    if not isinstance(payload, dict):
        return tickers

    queue: list[Any] = [payload.get("positions"), payload.get("data"), payload]
    seen: set[int] = set()
    ticker_keys = {"ticker", "market_ticker", "marketticker", "event_ticker", "eventticker"}

    while queue:
        item = queue.pop(0)
        marker = id(item)
        if marker in seen:
            continue
        seen.add(marker)

        if isinstance(item, dict):
            for key, value in item.items():
                key_l = str(key).strip().lower()
                if key_l in ticker_keys:
                    ticker = str(value or "").strip()
                    if ticker:
                        tickers.add(ticker)
                elif isinstance(value, (dict, list, tuple)):
                    queue.append(value)
        elif isinstance(item, (list, tuple)):
            queue.extend(item)

    return tickers


def _reconcile_optimistic_entries(
    open_positions: list[dict[str, Any]],
    now_utc: datetime,
    auth_client: KalshiAuthClient | None,
) -> tuple[list[dict[str, Any]], dict[str, int], list[dict[str, Any]]]:
    grace_minutes = max(int(config.LIVE_ENTRY_CONFIRM_GRACE_MINUTES), 0)
    grace = timedelta(minutes=grace_minutes)

    needs_broker_check = False
    for pos in open_positions:
        status = _entry_status(pos)
        if status not in {"pending_confirmation", "unconfirmed"}:
            continue
        submitted_at = _parse_iso_utc(pos.get("entry_submitted_at_utc")) or _parse_iso_utc(pos.get("opened_at_utc"))
        if status == "unconfirmed":
            needs_broker_check = True
            break
        if submitted_at is None or (now_utc - submitted_at) >= grace:
            needs_broker_check = True
            break

    broker_tickers: set[str] | None = None
    reconcile_error = ""
    if needs_broker_check and auth_client is not None:
        try:
            broker_payload = auth_client.get_positions()
            broker_tickers = _extract_broker_position_tickers(broker_payload if isinstance(broker_payload, dict) else {})
        except Exception as exc:
            reconcile_error = str(exc)

    next_positions: list[dict[str, Any]] = []
    for pos in open_positions:
        row = dict(pos)
        status = _entry_status(row)
        if status not in {"pending_confirmation", "unconfirmed"}:
            next_positions.append(row)
            continue

        submitted_at = _parse_iso_utc(row.get("entry_submitted_at_utc")) or _parse_iso_utc(row.get("opened_at_utc"))
        due = status == "unconfirmed" or submitted_at is None or (now_utc - submitted_at) >= grace
        if not due:
            row.setdefault("entry_status", status)
            row.setdefault("entry_reconcile_notes", "awaiting_broker_confirmation")
            next_positions.append(row)
            continue

        row["entry_last_checked_at_utc"] = now_utc.isoformat()
        ticker = str(row.get("ticker", "")).strip()
        if broker_tickers is not None:
            if ticker and ticker in broker_tickers:
                row["entry_status"] = "confirmed"
                row["entry_reconcile_notes"] = "confirmed_via_broker_positions"
            else:
                row["entry_status"] = "unconfirmed"
                row["entry_reconcile_notes"] = "grace_elapsed_not_in_broker_positions"
        elif reconcile_error:
            row.setdefault("entry_status", status)
            row["entry_reconcile_notes"] = f"reconcile_error:{reconcile_error}"
        else:
            row.setdefault("entry_status", status)
            row["entry_reconcile_notes"] = "reconcile_skipped_no_auth_client"
        next_positions.append(row)

    counts = {"pending": 0, "confirmed": 0, "unconfirmed": 0}
    for pos in next_positions:
        status = _entry_status(pos)
        if status == "pending_confirmation":
            counts["pending"] += 1
        elif status == "unconfirmed":
            counts["unconfirmed"] += 1
        else:
            counts["confirmed"] += 1

    alerts: list[dict[str, Any]] = []
    if reconcile_error:
        alerts.append(
            {
                "severity": "warn",
                "code": "live_entry_reconcile_error",
                "message": "Entry reconciliation failed while fetching broker positions.",
                "error": reconcile_error,
            }
        )
    if counts["unconfirmed"] > 0:
        alerts.append(
            {
                "severity": "warn",
                "code": "live_entry_unconfirmed",
                "message": f"{counts['unconfirmed']} live entries are still unconfirmed after grace period.",
                "count": counts["unconfirmed"],
            }
        )

    return next_positions, counts, alerts


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
    allow_new_entries: bool = True,
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

    open_positions = list(state.get("open_positions", []))
    closed_positions = list(state.get("closed_positions", []))
    open_positions, entry_reconcile_counts, reconcile_alerts = _reconcile_optimistic_entries(open_positions, now_utc, auth_client)

    cycle_alerts = list(reconcile_alerts)
    entry_block_reasons: list[str] = []
    if not effective_live_routing:
        entry_block_reasons.append("live_routing_disabled")
    if not allow_new_entries:
        entry_block_reasons.append("entries_blocked_by_policy")
    if effective_live_routing and auth_client is None:
        entry_block_reasons.append("live_auth_client_missing")
        cycle_alerts.append(
            {
                "severity": "critical",
                "code": "live_auth_client_missing",
                "message": "Live routing is enabled but no auth client is available for order routing.",
            }
        )

    entries_allowed = allow_new_entries and effective_live_routing and auth_client is not None
    exits_routable = effective_live_routing and auth_client is not None

    signal_ev_map: dict[str, float] = {}
    for raw in signals:
        ticker = str(raw.get("ticker", ""))
        signal_ev_map[ticker] = float(raw.get("ev_cents", 0.0) or 0.0)

    closed = 0
    capped_entry_count = 0
    capped_exit_count = 0
    depth_skipped_count = 0
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
        requested_contracts = int(pos.get("contracts", 0) or 0)
        if requested_contracts <= 0:
            kept_open.append(pos)
            continue
        close_contracts, cap_reason = cap_contracts_to_top_of_book(
            requested_contracts,
            quote,
            side,
            action="exit",
        )
        if close_contracts < requested_contracts:
            capped_exit_count += 1
        if close_contracts <= 0:
            pending = dict(pos)
            pending["pending_exit_submitted_at_utc"] = now_utc.isoformat()
            pending["pending_exit_reason"] = cap_reason or "depth_cap_exit"
            pending["requested_contracts"] = requested_contracts
            pending["capped_contracts"] = close_contracts
            pending["cap_reason"] = cap_reason
            kept_open.append(pending)
            depth_skipped_count += 1
            exit_order_events.append(
                {
                    "ticker": ticker,
                    "position_id": pos.get("position_id"),
                    "side": _close_side_from_position_side(side),
                    "contracts": close_contracts,
                    "requested_contracts": requested_contracts,
                    "capped_contracts": close_contracts,
                    "cap_reason": cap_reason,
                    "exit_price": _exit_price(quote, side),
                    "filled": False,
                    "error": cap_reason or "depth_cap_exit",
                }
            )
            continue

        exit_px = _exit_price(quote, side)
        close_side = _close_side_from_position_side(side)
        payload = {
            "ticker": ticker,
            "side": close_side,
            "action": "sell",
            "count": close_contracts,
            "yes_price_dollars": exit_px if close_side == "yes" else None,
            "no_price_dollars": exit_px if close_side == "no" else None,
            "reduce_only": True,
            "time_in_force": "fill_or_kill",
        }
        if exits_routable and auth_client is not None:
            try:
                broker_resp = auth_client.place_order(**payload)
                filled = _exit_filled(broker_resp)
                exit_error = ""
            except Exception as exc:
                broker_resp = {}
                filled = False
                exit_error = str(exc)
        else:
            broker_resp = {}
            filled = False
            exit_error = "live_routing_disabled" if not effective_live_routing else "live_auth_client_missing"

        exit_order_events.append(
            {
                "ticker": ticker,
                "position_id": pos.get("position_id"),
                "side": close_side,
                "contracts": close_contracts,
                "requested_contracts": requested_contracts,
                "capped_contracts": close_contracts,
                "cap_reason": cap_reason,
                "exit_price": exit_px,
                "filled": bool(filled),
                "error": exit_error or None,
            }
        )
        if not filled:
            pending = dict(pos)
            pending["pending_exit_submitted_at_utc"] = now_utc.isoformat()
            pending["pending_exit_response"] = broker_resp or {}
            pending["requested_contracts"] = requested_contracts
            pending["capped_contracts"] = close_contracts
            pending["cap_reason"] = cap_reason
            if exit_error:
                pending["pending_exit_error"] = exit_error
            kept_open.append(pending)
            continue

        entry_px = float(pos.get("entry_price_dollars", 0.0) or 0.0)
        entry_fees_closed, entry_fees_remaining = split_entry_fees_dollars(
            float(pos.get("entry_fees_dollars", 0.0) or 0.0),
            requested_contracts,
            close_contracts,
        )
        exit_fees = kalshi_trading_fee_dollars(close_contracts, exit_px)
        pnl = (exit_px - entry_px) * close_contracts - entry_fees_closed - exit_fees
        out = dict(pos)
        out["status"] = "closed"
        out["closed_at_utc"] = now_utc.isoformat()
        out["contracts"] = close_contracts
        out["close_price_dollars"] = exit_px
        out["realized_pnl_dollars"] = float(pnl)
        out["close_reason"] = "signal_or_time_partial" if close_contracts < requested_contracts else "signal_or_time"
        out["requested_contracts"] = requested_contracts
        out["capped_contracts"] = close_contracts
        out["cap_reason"] = cap_reason
        out["live_exit_order_response"] = broker_resp or {}
        closed_positions.append(out)
        closed += 1

        if close_contracts < requested_contracts:
            remaining = dict(pos)
            remaining_contracts = requested_contracts - close_contracts
            remaining["contracts"] = remaining_contracts
            remaining["entry_fees_dollars"] = entry_fees_remaining
            kept_open.append(remaining)

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
    halt_new_entries = False
    if entries_allowed:
        for raw in signals:
            ticker = str(raw.get("ticker", ""))
            if any(str(p.get("ticker", "")) == ticker for p in open_positions):
                continue
            if not can_open_more(len(open_positions), limits):
                break
            if halt_new_entries:
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
            requested_contracts = contracts_for_notional(entry_price, limits.max_position_dollars)
            contracts, cap_reason = cap_contracts_to_top_of_book(
                requested_contracts,
                quote,
                side,
                action="entry",
            )
            if contracts < requested_contracts:
                capped_entry_count += 1
            if contracts <= 0:
                depth_skipped_count += 1
                continue

            new_notional = open_notional + (entry_price * contracts)
            if equity > 0 and (new_notional / equity) > config.LIVE_MAX_NOTIONAL_UTILIZATION:
                continue

            payload = {
                "ticker": ticker,
                "side": side,
                "count": contracts,
                "yes_price_dollars": entry_price if side == "buy_yes" else None,
                "no_price_dollars": entry_price if side == "buy_no" else None,
                "time_in_force": "gtc",
            }
            placed = False
            submission_uncertain = False
            entry_error = ""
            try:
                broker_resp = auth_client.place_order(**payload)
                placed = True
            except Exception as exc:
                # Treat submit errors as potentially accepted to avoid duplicate buy-to-open retries.
                broker_resp = {}
                placed = True
                submission_uncertain = True
                entry_error = str(exc)
                halt_new_entries = True
                cycle_alerts.append(
                    {
                        "severity": "warn",
                        "code": "live_entry_submit_error",
                        "message": "Live entry submit raised an exception; order assumed sent and further entries halted.",
                        "ticker": ticker,
                        "error": entry_error,
                    }
                )

            pos_id = int(state.get("next_position_id", 1) or 1)
            state["next_position_id"] = pos_id + 1
            settlement_ts = raw.get("settlement_ts_utc") or (now_utc + timedelta(days=1)).isoformat()
            settlement_dt = _parse_iso_utc(settlement_ts) or (now_utc + timedelta(days=1))
            max_hold = min(
                now_utc + timedelta(hours=config.MAX_HOLD_HOURS),
                settlement_dt - timedelta(hours=config.SETTLEMENT_CUTOFF_HOURS),
            )

            pos = {
                "position_id": f"live_{pos_id}",
                "ticker": ticker,
                "city": str(raw.get("city", "")),
                "side": side,
                "contracts": contracts,
                "entry_price_dollars": entry_price,
                "entry_fees_dollars": kalshi_trading_fee_dollars(contracts, entry_price),
                "opened_at_utc": now_utc.isoformat(),
                "max_hold_until_utc": max_hold.isoformat(),
                "settlement_ts_utc": settlement_ts,
                "status": "open",
                "live_order_submitted": placed,
                "live_order_response": broker_resp or {},
                "entry_status": "pending_confirmation",
                "entry_submitted_at_utc": now_utc.isoformat(),
                "entry_last_checked_at_utc": now_utc.isoformat(),
                "entry_reconcile_notes": (
                    "entry_submit_error_assumed_sent" if submission_uncertain else "awaiting_broker_confirmation"
                ),
                "entry_submission_uncertain": submission_uncertain,
                "requested_contracts": requested_contracts,
                "capped_contracts": contracts,
                "cap_reason": cap_reason,
            }
            if entry_error:
                pos["live_order_submit_error"] = entry_error
            open_positions.append(pos)
            open_notional = new_notional
            opened += 1
            order_events.append(
                {
                    "ticker": ticker,
                    "side": side,
                    "contracts": contracts,
                    "requested_contracts": requested_contracts,
                    "capped_contracts": contracts,
                    "cap_reason": cap_reason,
                    "entry_price": entry_price,
                    "live_order_submitted": placed,
                    "submission_uncertain": submission_uncertain,
                    "error": entry_error or None,
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
                    "capped_entries": capped_entry_count,
                    "capped_exits": capped_exit_count,
                    "depth_skipped": depth_skipped_count,
                    "entry_reconciliation": entry_reconcile_counts,
                    "alerts": cycle_alerts,
                    "limits": state.get("live_limits", {}),
                },
                default=str,
            )
            + "\n"
        )

    blocked = bool(entry_block_reasons)
    reason = entry_block_reasons[0] if entry_block_reasons else None
    return {
        "opened": opened,
        "closed": closed,
        "blocked": blocked,
        "reason": reason,
        "orders": order_events,
        "exit_orders": exit_order_events,
        "capped_entries": capped_entry_count,
        "capped_exits": capped_exit_count,
        "depth_skipped": depth_skipped_count,
        "pending": entry_reconcile_counts["pending"],
        "confirmed": entry_reconcile_counts["confirmed"],
        "unconfirmed": entry_reconcile_counts["unconfirmed"],
        "alerts": cycle_alerts,
        "entry_block_reasons": entry_block_reasons,
        "limits": state.get("live_limits", {}),
    }
