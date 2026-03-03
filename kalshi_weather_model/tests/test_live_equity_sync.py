from __future__ import annotations

from datetime import datetime, timezone

from weather_arb.execution.live_engine import _sync_live_equity_from_api


class _FakeAuthClient:
    def __init__(self, payload):
        self._payload = payload

    def get_positions(self):
        return self._payload


def test_sync_live_equity_uses_payload_value(monkeypatch):
    monkeypatch.setattr("weather_arb.config.ALLOW_LIVE_TRADING", True)
    monkeypatch.setattr("weather_arb.config.LIVE_EQUITY_SYNC_ENABLED", True)
    state = {"equity": 50.0, "last_equity_sync_day": ""}
    now = datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc)
    auth = _FakeAuthClient({"portfolio": {"equity_dollars": 123.45}})
    out = _sync_live_equity_from_api(state, now, auth)
    assert out["equity"] == 123.45
    assert out["equity_source"] == "api"
    assert out["last_equity_sync_day"] == "2026-03-03"


def test_sync_live_equity_skips_when_same_day(monkeypatch):
    monkeypatch.setattr("weather_arb.config.ALLOW_LIVE_TRADING", True)
    monkeypatch.setattr("weather_arb.config.LIVE_EQUITY_SYNC_ENABLED", True)
    state = {"equity": 77.0, "last_equity_sync_day": "2026-03-03"}
    now = datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc)
    auth = _FakeAuthClient({"portfolio": {"equity_dollars": 999.0}})
    out = _sync_live_equity_from_api(state, now, auth)
    assert out["equity"] == 77.0
