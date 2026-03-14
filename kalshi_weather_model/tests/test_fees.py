from __future__ import annotations

from weather_arb.fees import (
    estimated_entry_cost_cents,
    kalshi_fee_per_contract_dollars,
    kalshi_trading_fee_dollars,
    split_entry_fees_dollars,
)
from weather_arb.execution.settlement import realized_pnl_from_settlement


def test_kalshi_fee_rounds_small_penny_order_up_to_one_cent():
    assert kalshi_trading_fee_dollars(1, 0.01) == 0.01
    assert kalshi_fee_per_contract_dollars(1, 0.01) == 0.01


def test_kalshi_fee_matches_midrange_taker_formula():
    assert kalshi_trading_fee_dollars(10, 0.50) == 0.18
    assert kalshi_fee_per_contract_dollars(10, 0.50) == 0.018


def test_kalshi_fee_matches_large_order_without_extra_rounding():
    assert kalshi_trading_fee_dollars(1000, 0.60) == 16.8
    assert round(kalshi_fee_per_contract_dollars(1000, 0.60), 6) == 0.0168


def test_kalshi_maker_fee_uses_lower_official_rate():
    assert kalshi_trading_fee_dollars(10, 0.50, maker=True) == 0.05


def test_estimated_entry_cost_cents_uses_available_size_when_present():
    cost_cents = estimated_entry_cost_cents(
        0.01,
        max_position_dollars=5.0,
        available_contracts=100,
        slippage_cents=0.5,
    )
    assert round(cost_cents, 4) == 0.57


def test_split_entry_fees_prorates_closed_and_remaining_contracts():
    closed, remaining = split_entry_fees_dollars(0.18, 10, 3)
    assert round(closed, 6) == 0.054
    assert round(remaining, 6) == 0.126


def test_settlement_pnl_uses_stored_entry_fee_dollars():
    pnl = realized_pnl_from_settlement(
        side="buy_yes",
        entry_price_dollars=0.50,
        contracts=10,
        settled_yes=True,
        entry_fees_dollars=0.18,
    )
    assert pnl == 4.82
