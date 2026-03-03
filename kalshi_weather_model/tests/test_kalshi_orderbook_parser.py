from __future__ import annotations

import pytest

from weather_arb.connectors.kalshi import parse_dollar_orderbook


def test_parse_orderbook_direct_dollar_fields() -> None:
    payload = {
        "yes_bid_dollars": 0.42,
        "yes_ask_dollars": 0.45,
        "no_bid_dollars": 0.55,
        "no_ask_dollars": 0.58,
        "yes_bid_size": 12,
        "yes_ask_size": 8,
        "no_bid_size": 10,
        "no_ask_size": 9,
    }
    out = parse_dollar_orderbook(payload, "TICK")
    assert out["yes_bid_dollars"] == 0.42
    assert out["yes_ask_dollars"] == 0.45
    assert out["no_bid_dollars"] == 0.55
    assert out["no_ask_dollars"] == 0.58
    assert out["yes_bid_size"] == 12


def test_parse_orderbook_ladder_dollars_fallback() -> None:
    payload = {
        "orderbook": {
            "yes_dollars": [["0.42", 12], ["0.40", 10]],
            "no_dollars": [["0.56", 9], ["0.54", 3]],
        }
    }
    out = parse_dollar_orderbook(payload, "TICK")
    assert out["yes_bid_dollars"] == 0.42
    assert out["no_bid_dollars"] == 0.56
    assert out["yes_ask_dollars"] == pytest.approx(0.44)
    assert out["no_ask_dollars"] == pytest.approx(0.58)
    assert out["yes_ask_size"] == 9
    assert out["no_ask_size"] == 12


def test_parse_orderbook_ladder_cents_fallback() -> None:
    payload = {"orderbook": {"yes": [[42, 12]], "no": [[56, 9]]}}
    out = parse_dollar_orderbook(payload, "TICK")
    assert out["yes_bid_dollars"] == pytest.approx(0.42)
    assert out["no_bid_dollars"] == pytest.approx(0.56)
    assert out["yes_ask_dollars"] == pytest.approx(0.44)
    assert out["no_ask_dollars"] == pytest.approx(0.58)


def test_parse_orderbook_raises_on_empty_payload() -> None:
    with pytest.raises(ValueError):
        parse_dollar_orderbook({"orderbook": {}}, "TICK")
