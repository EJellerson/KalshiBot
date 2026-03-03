from __future__ import annotations

from weather_arb.model.contract_discovery import discover_temperature_contracts


def test_discover_temperature_contracts_parses_city_threshold_and_time():
    payload = {
        "markets": [
            {
                "id": "m1",
                "ticker": "WX_NYC_T75",
                "title": "NYC above 75F",
                "status": "open",
                "settlement_time": "2026-03-03T23:00:00Z",
            }
        ]
    }
    contracts, skipped = discover_temperature_contracts(payload)
    assert not skipped
    assert len(contracts) == 1
    c = contracts[0]
    assert c.city == "NYC"
    assert c.threshold_f == 75.0
    assert c.ticker == "WX_NYC_T75"


def test_discover_temperature_contracts_skips_unmapped_city():
    payload = {
        "markets": [
            {
                "id": "m2",
                "ticker": "WX_NOWHERE_T70",
                "title": "Nowhere above 70F",
                "status": "open",
                "settlement_time": "2026-03-03T23:00:00Z",
            }
        ]
    }
    contracts, skipped = discover_temperature_contracts(payload)
    assert not contracts
    assert skipped and skipped[0]["reason"] == "city_unmapped"
