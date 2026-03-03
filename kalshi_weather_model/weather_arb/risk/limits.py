from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from weather_arb import config
from weather_arb.types import MarketQuote, RiskLimits
from weather_arb.utils.time_utils import day_key_in_zone


def _weekly_stop_from_daily(daily_stop_dollars: float) -> float:
    return round(
        max(
            config.LIVE_WEEKLY_STOP_MIN_DOLLARS,
            daily_stop_dollars * config.LIVE_WEEKLY_STOP_MULTIPLIER,
        ),
        2,
    )


def compute_hybrid_live_limits(equity: float) -> RiskLimits:
    if equity > config.LIVE_PERCENT_SWITCH_EQUITY:
        max_position = max(config.LIVE_MIN_MAX_POSITION_DOLLARS, equity * config.LIVE_MAX_POSITION_PCT)
        daily_stop = max(config.LIVE_MIN_DAILY_STOP_DOLLARS, equity * config.LIVE_DAILY_STOP_PCT)
        return RiskLimits(
            max_position_dollars=round(max_position, 2),
            daily_loss_stop_dollars=round(daily_stop, 2),
            weekly_loss_stop_dollars=_weekly_stop_from_daily(daily_stop),
            max_concurrent_positions=config.LIVE_MAX_CONCURRENT_CAP,
            consecutive_loss_halt=config.LIVE_CONSECUTIVE_LOSS_HALT,
        )

    for tier in config.LIVE_FIXED_TIERS:
        lo = float(tier["min_equity"])
        hi = float(tier["max_equity"])
        if lo <= equity <= hi:
            return RiskLimits(
                max_position_dollars=float(tier["max_position"]),
                daily_loss_stop_dollars=float(tier["daily_stop"]),
                weekly_loss_stop_dollars=_weekly_stop_from_daily(float(tier["daily_stop"])),
                max_concurrent_positions=min(int(tier["max_concurrent"]), config.LIVE_MAX_CONCURRENT_CAP),
                consecutive_loss_halt=config.LIVE_CONSECUTIVE_LOSS_HALT,
            )

    return RiskLimits(
        max_position_dollars=config.LIVE_MIN_MAX_POSITION_DOLLARS,
        daily_loss_stop_dollars=config.LIVE_MIN_DAILY_STOP_DOLLARS,
        weekly_loss_stop_dollars=_weekly_stop_from_daily(config.LIVE_MIN_DAILY_STOP_DOLLARS),
        max_concurrent_positions=config.LIVE_MAX_CONCURRENT_CAP,
        consecutive_loss_halt=config.LIVE_CONSECUTIVE_LOSS_HALT,
    )


def resolve_live_limits_for_day(
    equity: float,
    now_utc: datetime,
    state: dict[str, Any],
    tz_name: str = config.SCHEDULER_TZ,
) -> tuple[RiskLimits, dict[str, Any]]:
    today_key = day_key_in_zone(now_utc, tz_name)
    last_key = str(state.get("last_limits_day", ""))

    if last_key == today_key and isinstance(state.get("live_limits"), dict):
        raw = dict(state["live_limits"])
        limits = RiskLimits(
            max_position_dollars=float(raw.get("max_position_dollars", config.LIVE_MIN_MAX_POSITION_DOLLARS)),
            daily_loss_stop_dollars=float(raw.get("daily_loss_stop_dollars", config.LIVE_MIN_DAILY_STOP_DOLLARS)),
            weekly_loss_stop_dollars=float(
                raw.get(
                    "weekly_loss_stop_dollars",
                    _weekly_stop_from_daily(config.LIVE_MIN_DAILY_STOP_DOLLARS),
                )
            ),
            max_concurrent_positions=int(raw.get("max_concurrent_positions", config.LIVE_MAX_CONCURRENT_CAP)),
            consecutive_loss_halt=int(raw.get("consecutive_loss_halt", config.LIVE_CONSECUTIVE_LOSS_HALT)),
        )
        return limits, state

    limits = compute_hybrid_live_limits(equity)
    next_state = dict(state)
    next_state["last_limits_day"] = today_key
    next_state["live_limits"] = asdict(limits)
    return limits, next_state


def paper_limits() -> RiskLimits:
    return RiskLimits(
        max_position_dollars=config.PAPER_MAX_POSITION_DOLLARS,
        daily_loss_stop_dollars=config.PAPER_DAILY_LOSS_STOP_DOLLARS,
        weekly_loss_stop_dollars=config.PAPER_WEEKLY_LOSS_STOP_DOLLARS,
        max_concurrent_positions=config.PAPER_MAX_CONCURRENT_POSITIONS,
        consecutive_loss_halt=config.PAPER_CONSECUTIVE_LOSS_HALT,
    )


def spread_ok(quote: MarketQuote, side: str = "buy_yes") -> bool:
    if side == "buy_no":
        if quote.no_ask_dollars <= 0:
            return False
        spread = max(0.0, quote.no_ask_dollars - quote.no_bid_dollars)
        spread_pct = spread / max(quote.no_ask_dollars, 1e-9)
    else:
        if quote.yes_ask_dollars <= 0:
            return False
        spread = max(0.0, quote.yes_ask_dollars - quote.yes_bid_dollars)
        spread_pct = spread / max(quote.yes_ask_dollars, 1e-9)
    return spread_pct <= config.MAX_SPREAD_PCT


def depth_ok(quote: MarketQuote) -> bool:
    return (
        quote.yes_bid_size >= config.MIN_BOOK_SIZE
        and quote.yes_ask_size >= config.MIN_BOOK_SIZE
        and quote.no_bid_size >= config.MIN_BOOK_SIZE
        and quote.no_ask_size >= config.MIN_BOOK_SIZE
    )


def contracts_for_notional(price_dollars: float, max_position_dollars: float) -> int:
    if price_dollars <= 0:
        return 0
    return max(int(max_position_dollars // price_dollars), 0)


def top_of_book_size_for_order(quote: MarketQuote, side: str, *, action: str = "entry") -> int:
    side_l = str(side or "").strip().lower()
    action_l = str(action or "").strip().lower()

    if action_l == "entry":
        if side_l == "buy_yes":
            return max(int(quote.yes_ask_size), 0)
        if side_l == "buy_no":
            return max(int(quote.no_ask_size), 0)
        raise ValueError(f"unsupported entry side: {side}")

    if action_l == "exit":
        if side_l == "buy_yes":
            return max(int(quote.yes_bid_size), 0)
        if side_l == "buy_no":
            return max(int(quote.no_bid_size), 0)
        raise ValueError(f"unsupported exit side: {side}")

    raise ValueError(f"unsupported action: {action}")


def cap_contracts_to_top_of_book(
    contracts: int,
    quote: MarketQuote,
    side: str,
    *,
    action: str = "entry",
) -> tuple[int, str | None]:
    requested = max(int(contracts), 0)
    available = top_of_book_size_for_order(quote, side, action=action)
    capped = min(requested, available)
    if capped >= requested:
        return capped, None
    return capped, f"depth_cap_{action}"


def can_open_more(open_positions: int, limits: RiskLimits) -> bool:
    return open_positions < limits.max_concurrent_positions


def daily_stop_hit(day_pnl: float, limits: RiskLimits) -> bool:
    return day_pnl <= -abs(limits.daily_loss_stop_dollars)


def weekly_stop_hit(week_pnl: float, limits: RiskLimits) -> bool:
    return week_pnl <= -abs(limits.weekly_loss_stop_dollars)
