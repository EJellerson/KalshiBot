from __future__ import annotations

from weather_arb.__main__ import _apply_live_closures_to_state


def test_apply_live_closures_updates_equity_and_stops():
    state = {
        "equity": 50.0,
        "daily_pnl": {},
        "weekly_pnl": {},
        "consecutive_losses": 0,
    }
    closed = [
        {"realized_pnl_dollars": -2.5, "closed_at_utc": "2026-03-02T12:00:00+00:00"},
        {"realized_pnl_dollars": 1.0, "closed_at_utc": "2026-03-02T16:00:00+00:00"},
    ]
    out = _apply_live_closures_to_state(state, closed)
    assert out["equity"] == 48.5
    assert out["daily_pnl"]["2026-03-02"] == -1.5
    assert out["weekly_pnl"]["2026-W10"] == -1.5
    assert out["consecutive_losses"] == 0
