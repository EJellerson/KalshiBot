from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
import json

from weather_arb import config
from weather_arb.risk.limits import (
    can_open_more,
    contracts_for_notional,
    daily_stop_hit,
    depth_ok,
    paper_limits,
    spread_ok,
    weekly_stop_hit,
)
from weather_arb.types import FairValueSignal, MarketQuote
from weather_arb.utils.io_utils import read_or_create_json, safe_write_json_atomic
from weather_arb.utils.time_utils import day_key_in_zone


def _default_state() -> dict[str, Any]:
    return {
        "equity": config.PAPER_ACCOUNT_SIZE,
        "cash": config.PAPER_ACCOUNT_SIZE,
        "open_positions": [],
        "closed_positions": [],
        "daily_pnl": {},
        "weekly_pnl": {},
        "consecutive_losses": 0,
        "next_position_id": 1,
    }


def _week_key(now_utc: datetime) -> str:
    y, w, _ = now_utc.isocalendar()
    return f"{y}-W{w:02d}"


def _entry_price(quote: MarketQuote, side: str) -> float:
    return quote.yes_ask_dollars if side == "buy_yes" else quote.no_ask_dollars


def _exit_price(quote: MarketQuote, side: str) -> float:
    return quote.yes_bid_dollars if side == "buy_yes" else quote.no_bid_dollars


def _to_quote(raw: dict[str, Any]) -> MarketQuote:
    return MarketQuote(
        ticker=str(raw["ticker"]),
        ts_utc=datetime.fromisoformat(str(raw.get("ts_utc") or datetime.utcnow().isoformat())),
        yes_bid_dollars=float(raw["yes_bid_dollars"]),
        yes_ask_dollars=float(raw["yes_ask_dollars"]),
        no_bid_dollars=float(raw["no_bid_dollars"]),
        no_ask_dollars=float(raw["no_ask_dollars"]),
        yes_bid_size=int(raw.get("yes_bid_size", 0) or 0),
        yes_ask_size=int(raw.get("yes_ask_size", 0) or 0),
        no_bid_size=int(raw.get("no_bid_size", 0) or 0),
        no_ask_size=int(raw.get("no_ask_size", 0) or 0),
    )


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


def _write_blotter_line(blotter_dir: Path, date_key: str, payload: dict[str, Any]) -> None:
    blotter_dir.mkdir(parents=True, exist_ok=True)
    path = blotter_dir / f"paper_blotter_{date_key}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, default=str) + "\n")


def run_paper_cycle(
    signals: list[FairValueSignal | dict[str, Any]],
    quote_map: dict[str, dict[str, Any]],
    now_utc: datetime,
    *,
    state_path: Path = config.PAPER_POSITIONS_PATH,
    blotter_dir: Path = config.PAPER_BLOTTER_DIR,
) -> dict[str, Any]:
    state = read_or_create_json(state_path, _default_state())
    limits = paper_limits()

    date_key = day_key_in_zone(now_utc, config.SCHEDULER_TZ)
    week_key = _week_key(now_utc)
    day_pnl = float(state.get("daily_pnl", {}).get(date_key, 0.0) or 0.0)
    week_pnl = float(state.get("weekly_pnl", {}).get(week_key, 0.0) or 0.0)

    if daily_stop_hit(day_pnl, limits) or weekly_stop_hit(week_pnl, limits):
        return {
            "opened": 0,
            "closed": 0,
            "blocked": True,
            "reason": "loss_stop",
            "day_pnl": day_pnl,
            "week_pnl": week_pnl,
        }
    if int(state.get("consecutive_losses", 0) or 0) >= limits.consecutive_loss_halt:
        return {
            "opened": 0,
            "closed": 0,
            "blocked": True,
            "reason": "consecutive_loss_halt",
            "day_pnl": day_pnl,
            "week_pnl": week_pnl,
        }

    open_positions = list(state.get("open_positions", []))
    closed_positions = list(state.get("closed_positions", []))

    signal_ev_map: dict[str, float] = {}
    normalized_signals: list[dict[str, Any]] = []
    for signal in signals:
        raw = signal.__dict__ if hasattr(signal, "__dict__") else dict(signal)
        ticker = str(raw.get("ticker", ""))
        ev = float(raw.get("ev_cents", 0.0) or 0.0)
        signal_ev_map[ticker] = abs(ev)
        normalized_signals.append(raw)

    closed_count = 0
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

        exit_px = _exit_price(quote, str(pos.get("side")))
        contracts = int(pos.get("contracts", 0) or 0)
        entry_px = float(pos.get("entry_price_dollars", 0.0) or 0.0)
        fees = contracts * config.KALSHI_FEE_PER_CONTRACT_DOLLARS
        pnl = (exit_px - entry_px) * contracts - fees

        out = dict(pos)
        out["status"] = "closed"
        out["closed_at_utc"] = now_utc.isoformat()
        out["close_price_dollars"] = exit_px
        out["realized_pnl_dollars"] = float(pnl)
        out["close_reason"] = "signal_or_time"
        closed_positions.append(out)
        closed_count += 1

        state.setdefault("daily_pnl", {})[date_key] = float(state.get("daily_pnl", {}).get(date_key, 0.0) or 0.0) + pnl
        state.setdefault("weekly_pnl", {})[week_key] = float(state.get("weekly_pnl", {}).get(week_key, 0.0) or 0.0) + pnl
        state["equity"] = float(state.get("equity", config.PAPER_ACCOUNT_SIZE) or config.PAPER_ACCOUNT_SIZE) + pnl

        if pnl < 0:
            state["consecutive_losses"] = int(state.get("consecutive_losses", 0) or 0) + 1
        else:
            state["consecutive_losses"] = 0

    open_positions = kept_open

    opened_count = 0
    for raw in normalized_signals:
        ticker = str(raw.get("ticker", ""))
        if any(str(pos.get("ticker", "")) == ticker for pos in open_positions):
            continue
        if not can_open_more(len(open_positions), limits):
            break

        ev_cents = float(raw.get("ev_cents", 0.0) or 0.0)
        if ev_cents < float(raw.get("min_ev_cents", config.BOOTSTRAP_MIN_EV_CENTS)):
            continue

        quote_raw = quote_map.get(ticker)
        if not quote_raw:
            continue
        quote = _to_quote(quote_raw)
        if not spread_ok(quote) or not depth_ok(quote):
            continue

        side = str(raw.get("side", "buy_yes"))
        entry_px = _entry_price(quote, side)
        contracts = contracts_for_notional(entry_px, limits.max_position_dollars)
        if contracts <= 0:
            continue

        pos_id = int(state.get("next_position_id", 1) or 1)
        state["next_position_id"] = pos_id + 1

        settlement_ts = raw.get("settlement_ts_utc") or (now_utc + timedelta(days=1)).isoformat()
        max_hold_until = min(
            now_utc + timedelta(hours=config.MAX_HOLD_HOURS),
            datetime.fromisoformat(str(settlement_ts)) - timedelta(hours=config.SETTLEMENT_CUTOFF_HOURS),
        )

        pos = {
            "position_id": f"paper_{pos_id}",
            "ticker": ticker,
            "city": str(raw.get("city", "")),
            "side": side,
            "contracts": contracts,
            "entry_price_dollars": entry_px,
            "entry_fees_dollars": contracts * config.KALSHI_FEE_PER_CONTRACT_DOLLARS,
            "opened_at_utc": now_utc.isoformat(),
            "max_hold_until_utc": max_hold_until.isoformat(),
            "settlement_ts_utc": settlement_ts,
            "status": "open",
            "threshold_f": float(raw.get("threshold_f", 0.0) or 0.0),
            "entry_ev_cents": ev_cents,
        }
        open_positions.append(pos)
        opened_count += 1

    state["open_positions"] = open_positions
    state["closed_positions"] = closed_positions
    safe_write_json_atomic(state_path, state)

    summary = {
        "ts": now_utc.isoformat(),
        "opened": opened_count,
        "closed": closed_count,
        "open_positions": len(open_positions),
        "equity": float(state.get("equity", config.PAPER_ACCOUNT_SIZE) or config.PAPER_ACCOUNT_SIZE),
        "day_pnl": float(state.get("daily_pnl", {}).get(date_key, 0.0) or 0.0),
        "week_pnl": float(state.get("weekly_pnl", {}).get(week_key, 0.0) or 0.0),
    }
    _write_blotter_line(blotter_dir, date_key, summary)
    return summary
