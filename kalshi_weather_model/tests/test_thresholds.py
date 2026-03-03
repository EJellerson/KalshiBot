from __future__ import annotations

from weather_arb.model.thresholds import bootstrap_threshold, calibrate_min_ev_threshold


def test_bootstrap_threshold():
    assert bootstrap_threshold(0) == 6.0


def test_calibrate_min_ev_threshold_selects_profitable_cutoff():
    rows = [
        {"ev_cents": 3.0, "realized_pnl_dollars": -1.0, "notional_dollars": 5.0, "date_key": "2026-03-01"},
        {"ev_cents": 8.0, "realized_pnl_dollars": 1.2, "notional_dollars": 5.0, "date_key": "2026-03-01"},
        {"ev_cents": 9.0, "realized_pnl_dollars": 1.1, "notional_dollars": 5.0, "date_key": "2026-03-02"},
        {"ev_cents": 10.0, "realized_pnl_dollars": 1.0, "notional_dollars": 5.0, "date_key": "2026-03-02"},
    ]
    out = calibrate_min_ev_threshold(rows, min_ev=2.0, max_ev=10.0, step=1.0, min_trades=2)
    assert out.min_ev_cents >= 8.0
    assert out.trades >= 2
