from __future__ import annotations

from weather_arb.analytics.monitoring import operational_alerts_snapshot


def test_operational_alerts_no_contracts_is_critical() -> None:
    train = {
        "gate": {"pass": False},
        "max_days_remaining": 10,
        "estimated_ready_date_local": "2026-03-12",
    }
    inventory = {
        "stale_streams": [],
        "stream_age_minutes": {},
        "freshness_threshold_minutes": {},
        "contracts_active": 0,
        "contracts_age_minutes": None,
        "today_rows": {"signals": 0},
    }
    events = {"minutes_since_by_event": {}}

    out = operational_alerts_snapshot(train, inventory, events)
    codes = {row["code"] for row in out["alerts"]}

    assert out["status"] == "critical"
    assert "no_contracts" in codes
    assert "train_gate_blocked" in codes


def test_operational_alerts_ok_when_healthy() -> None:
    train = {"gate": {"pass": True}, "max_days_remaining": 0}
    inventory = {
        "stale_streams": [],
        "stream_age_minutes": {},
        "freshness_threshold_minutes": {},
        "contracts_active": 6,
        "contracts_age_minutes": 20.0,
        "today_rows": {"signals": 4},
    }
    events = {
        "minutes_since_by_event": {
            "ingest_forecasts": 10.0,
            "sync_observations": 40.0,
            "paper_cycle": 10.0,
        }
    }

    out = operational_alerts_snapshot(train, inventory, events)
    assert out["status"] == "ok"
    assert out["alerts"] == []
