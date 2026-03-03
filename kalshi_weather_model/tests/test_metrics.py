from __future__ import annotations

from weather_arb.execution.metrics import max_drawdown_from_daily_pnl


def test_max_drawdown_uses_starting_equity_baseline():
    daily = {
        "2026-03-01": -10.0,
        "2026-03-02": -10.0,
    }
    dd = max_drawdown_from_daily_pnl(daily, starting_equity=50.0)
    assert dd == -0.40


def test_max_drawdown_with_recovery():
    daily = {
        "2026-03-01": 10.0,   # equity 110
        "2026-03-02": -5.0,   # equity 105, dd -4.545%
        "2026-03-03": -20.0,  # equity 85, dd -22.727%
    }
    dd = max_drawdown_from_daily_pnl(daily, starting_equity=100.0)
    assert round(dd, 6) == round(-25.0 / 110.0, 6)
