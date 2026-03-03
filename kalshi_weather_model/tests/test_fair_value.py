from __future__ import annotations

from datetime import datetime, timezone

from weather_arb.model.fair_value import (
    SeasonalResidualModel,
    build_residual_training_rows,
    compute_ev_cents,
)


def test_seasonal_model_prob_bounds():
    model = SeasonalResidualModel()
    model.fit([])
    p = model.p_exceeds("NYC", forecast_temp_f=75.0, threshold_f=70.0, target_date="2026-06-01")
    assert 0.0 <= p <= 1.0


def test_compute_ev_cents_sign():
    ev = compute_ev_cents(p_fair=0.7, p_market=0.5, est_cost_cents=1.0)
    assert ev > 0


def test_build_residual_training_rows_uses_city_day_alignment():
    forecast_rows = [
        {
            "city": "NYC",
            "fetched_at_utc": "2026-03-01T00:00:00+00:00",
            "forecast_time_utc": "2026-03-02T06:00:00+00:00",
            "temperature_f": 61.0,
        },
        {
            "city": "NYC",
            "fetched_at_utc": "2026-03-01T00:00:00+00:00",
            "forecast_time_utc": "2026-03-02T12:00:00+00:00",
            "temperature_f": 63.0,
        },
    ]
    observation_rows = [
        {
            "city": "NYC",
            "obs_date_local": "2026-03-02",
            "max_temp_f": 64.0,
            "observed_at_utc": "2026-03-03T01:00:00+00:00",
        }
    ]
    rows = build_residual_training_rows(
        forecast_rows=forecast_rows,
        observation_rows=observation_rows,
        city_timezones={"NYC": "America/New_York"},
        lead_min_hours=24.0,
        lead_max_hours=48.0,
        target_lead_hours=36.0,
        lookback_days=365,
        as_of_utc=datetime(2026, 3, 4, tzinfo=timezone.utc),
    )
    assert len(rows) == 1
    assert rows[0]["city"] == "NYC"
    assert rows[0]["target_date"] == "2026-03-02"
    assert rows[0]["forecast_temp_f"] == 63.0
    assert rows[0]["actual_temp_f"] == 64.0
