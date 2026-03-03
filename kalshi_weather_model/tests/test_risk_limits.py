from __future__ import annotations

from datetime import datetime, timezone

from weather_arb.risk.limits import (
    cap_contracts_to_top_of_book,
    compute_hybrid_live_limits,
    resolve_live_limits_for_day,
    spread_ok,
)
from weather_arb.types import MarketQuote


def test_compute_hybrid_live_limits_tiers():
    l1 = compute_hybrid_live_limits(50)
    assert l1.max_position_dollars == 5.0
    assert l1.daily_loss_stop_dollars == 10.0
    assert l1.weekly_loss_stop_dollars == 20.0
    assert l1.max_concurrent_positions == 3

    l2 = compute_hybrid_live_limits(150)
    assert l2.max_position_dollars == 8.0
    assert l2.daily_loss_stop_dollars == 15.0
    assert l2.weekly_loss_stop_dollars == 30.0
    assert l2.max_concurrent_positions == 3

    l3 = compute_hybrid_live_limits(300)
    assert l3.max_position_dollars == 12.0
    assert l3.daily_loss_stop_dollars == 25.0
    assert l3.weekly_loss_stop_dollars == 50.0
    assert l3.max_concurrent_positions == 4


def test_compute_hybrid_live_limits_percent_above_switch():
    l = compute_hybrid_live_limits(1_000)
    assert l.max_position_dollars == 30.0  # 3%
    assert l.daily_loss_stop_dollars == 60.0  # 6%
    assert l.weekly_loss_stop_dollars == 120.0
    assert l.max_concurrent_positions == 5


def test_resolve_live_limits_daily_boundary_only():
    now = datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc)
    state = {}
    limits1, state1 = resolve_live_limits_for_day(1000, now, state)
    limits2, state2 = resolve_live_limits_for_day(2000, now, state1)
    assert limits1.max_position_dollars == limits2.max_position_dollars
    assert state2["last_limits_day"] == state1["last_limits_day"]


def test_spread_ok_is_side_aware_for_buy_no():
    quote = MarketQuote(
        ticker="TEST",
        ts_utc=datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc),
        yes_bid_dollars=0.49,
        yes_ask_dollars=0.50,  # tight yes spread (2%)
        no_bid_dollars=0.20,
        no_ask_dollars=0.50,  # wide no spread (60%)
        yes_bid_size=20,
        yes_ask_size=20,
        no_bid_size=20,
        no_ask_size=20,
    )
    assert spread_ok(quote, side="buy_yes") is True
    assert spread_ok(quote, side="buy_no") is False


def test_cap_to_book_depth_entry_and_exit_sides():
    quote = MarketQuote(
        ticker="TEST",
        ts_utc=datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc),
        yes_bid_dollars=0.49,
        yes_ask_dollars=0.50,
        no_bid_dollars=0.49,
        no_ask_dollars=0.50,
        yes_bid_size=3,
        yes_ask_size=8,
        no_bid_size=4,
        no_ask_size=9,
    )

    entry_yes, reason_entry_yes = cap_contracts_to_top_of_book(20, quote, "buy_yes", action="entry")
    entry_no, reason_entry_no = cap_contracts_to_top_of_book(20, quote, "buy_no", action="entry")
    exit_yes, reason_exit_yes = cap_contracts_to_top_of_book(20, quote, "buy_yes", action="exit")
    exit_no, reason_exit_no = cap_contracts_to_top_of_book(20, quote, "buy_no", action="exit")

    assert entry_yes == 8 and reason_entry_yes == "depth_cap_entry"
    assert entry_no == 9 and reason_entry_no == "depth_cap_entry"
    assert exit_yes == 3 and reason_exit_yes == "depth_cap_exit"
    assert exit_no == 4 and reason_exit_no == "depth_cap_exit"


def test_cap_to_book_depth_zero_available_returns_zero():
    quote = MarketQuote(
        ticker="TEST",
        ts_utc=datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc),
        yes_bid_dollars=0.49,
        yes_ask_dollars=0.50,
        no_bid_dollars=0.49,
        no_ask_dollars=0.50,
        yes_bid_size=0,
        yes_ask_size=0,
        no_bid_size=0,
        no_ask_size=0,
    )
    capped, reason = cap_contracts_to_top_of_book(5, quote, "buy_yes", action="exit")
    assert capped == 0
    assert reason == "depth_cap_exit"
