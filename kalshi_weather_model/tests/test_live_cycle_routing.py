from __future__ import annotations

from datetime import datetime, timedelta, timezone

from weather_arb.execution.live_engine import _to_quote, run_live_cycle
from weather_arb.utils.io_utils import safe_read_json


class _FakeAuthClient:
    def get_positions(self):
        return {"portfolio": {"equity_dollars": 250.0}}

    def place_order(self, **kwargs):
        return {"ok": True, "order_id": "live_test", "request": kwargs}


def _sample_signal(now: datetime) -> list[dict[str, object]]:
    return [
        {
            "ticker": "KXHIGHNYC-26MAR03-T75",
            "city": "NYC",
            "side": "buy_yes",
            "ev_cents": 12.0,
            "min_ev_cents": 6.0,
            "settlement_ts_utc": (now + timedelta(hours=20)).isoformat(),
        }
    ]


def _sample_quotes(now: datetime) -> dict[str, dict[str, object]]:
    return {
        "KXHIGHNYC-26MAR03-T75": {
            "ticker": "KXHIGHNYC-26MAR03-T75",
            "ts_utc": now.isoformat(),
            "yes_bid_dollars": 0.49,
            "yes_ask_dollars": 0.50,
            "no_bid_dollars": 0.49,
            "no_ask_dollars": 0.50,
            "yes_bid_size": 25,
            "yes_ask_size": 25,
            "no_bid_size": 25,
            "no_ask_size": 25,
        }
    }


def test_run_live_cycle_fail_closed_when_live_routing_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr("weather_arb.config.LIVE_EQUITY_SYNC_ENABLED", False)

    now = datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc)
    state_path = tmp_path / "live_positions.json"
    blotter_dir = tmp_path / "blotter"

    out = run_live_cycle(
        _sample_signal(now),
        _sample_quotes(now),
        now,
        auth_client=None,
        state_path=state_path,
        blotter_dir=blotter_dir,
        live_routing_enabled=False,
    )

    state = safe_read_json(state_path) or {}

    assert out["opened"] == 0
    assert out["blocked"] is True
    assert out["reason"] == "live_routing_disabled"
    assert state.get("open_positions", []) == []


def test_run_live_cycle_places_order_when_live_routing_enabled(monkeypatch, tmp_path):
    monkeypatch.setattr("weather_arb.config.LIVE_EQUITY_SYNC_ENABLED", False)

    now = datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc)
    state_path = tmp_path / "live_positions.json"
    blotter_dir = tmp_path / "blotter"

    out = run_live_cycle(
        _sample_signal(now),
        _sample_quotes(now),
        now,
        auth_client=_FakeAuthClient(),
        state_path=state_path,
        blotter_dir=blotter_dir,
        live_routing_enabled=True,
    )

    state = safe_read_json(state_path) or {}
    open_positions = list(state.get("open_positions", []))

    assert out["opened"] == 1
    assert len(open_positions) == 1
    assert bool(open_positions[0].get("live_order_submitted", False)) is True


def test_live_quote_fallback_is_timezone_aware():
    quote = _to_quote(
        {
            "ticker": "TEST",
            "yes_bid_dollars": 0.49,
            "yes_ask_dollars": 0.50,
            "no_bid_dollars": 0.49,
            "no_ask_dollars": 0.50,
            "yes_bid_size": 10,
            "yes_ask_size": 10,
            "no_bid_size": 10,
            "no_ask_size": 10,
        }
    )
    assert quote.ts_utc.tzinfo is not None
