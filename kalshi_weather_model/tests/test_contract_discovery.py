from __future__ import annotations

from weather_arb.model.contract_discovery import (
    classify_weather_strategy,
    discover_temperature_contracts,
    discover_weather_contracts,
)


def _base_market(**overrides):
    out = {
        "id": "m1",
        "ticker": "KXHIGHNYC-26MAR03-T75",
        "event_ticker": "KXHIGHNYC-26MAR03",
        "title": "NYC highest temperature above 75F",
        "status": "open",
        "settlement_time": "2026-03-03T23:00:00Z",
    }
    out.update(overrides)
    return out


def test_discover_temperature_contracts_parses_city_threshold_and_time():
    payload = {"markets": [_base_market()]}
    contracts, skipped = discover_temperature_contracts(payload)
    assert not skipped
    assert len(contracts) == 1
    c = contracts[0]
    assert c.city == "NYC"
    assert c.threshold_f == 75.0
    assert c.strategy_id == "weather_temp_high"
    assert c.contract_date_local == "2026-03-03"


def test_discover_temperature_contracts_skips_unmapped_city():
    payload = {
        "markets": [
            _base_market(
                ticker="WX_NOWHERE_T70",
                title="Nowhere above 70F",
            )
        ]
    }
    contracts, skipped = discover_temperature_contracts(payload)
    assert not contracts
    assert skipped and skipped[0]["reason"] == "city_unmapped"


def test_parse_b_token_bucket_contract():
    payload = {
        "markets": [
            _base_market(
                ticker="KXHIGHCHI-26MAR03-B39.5",
                title="Chicago highest temperature 39-40F",
            )
        ]
    }
    contracts, skipped = discover_temperature_contracts(payload)
    assert not skipped
    assert len(contracts) == 1
    c = contracts[0]
    assert c.strategy_id == "weather_temp_bucket"
    assert c.comparator == "between"
    assert c.lower_f is not None
    assert c.upper_f is not None


def test_parse_range_contract_39_to_40():
    payload = {
        "markets": [
            _base_market(
                ticker="KXHIGHSEA-26MAR03-RANGE",
                title="Seattle highest temperature 39-40F",
            )
        ]
    }
    contracts, skipped = discover_temperature_contracts(payload)
    assert not skipped
    assert len(contracts) == 1
    c = contracts[0]
    assert c.strategy_id == "weather_temp_bucket"
    assert c.comparator == "between"
    assert c.lower_f == 39.0
    assert c.upper_f == 40.0


def test_parse_lt_contract_low_variant():
    payload = {
        "markets": [
            _base_market(
                ticker="KXLOWMIA-26MAR03-T39",
                title="Miami lowest temperature <39F",
            )
        ]
    }
    contracts, skipped = discover_temperature_contracts(payload)
    assert not skipped
    assert len(contracts) == 1
    c = contracts[0]
    assert c.strategy_id == "weather_temp_low"
    assert c.comparator == "below"
    assert c.threshold_f == 39.0


def test_parse_t_token_contract_high_variant():
    payload = {
        "markets": [
            _base_market(
                ticker="KXHIGHATL-26MAR03-T39",
                title="Atlanta highest temperature T39",
            )
        ]
    }
    contracts, skipped = discover_temperature_contracts(payload)
    assert not skipped
    assert len(contracts) == 1
    c = contracts[0]
    assert c.strategy_id == "weather_temp_high"
    assert c.comparator == "above"
    assert c.threshold_f == 39.0


def test_discover_weather_contracts_supports_precip_discovery_only():
    payload = {
        "markets": [
            {
                "id": "m2",
                "ticker": "KXRAINNYC-26MAR03-T1",
                "event_ticker": "KXRAINNYC-26MAR03",
                "title": "NYC precipitation above 1in",
                "status": "open",
                "settlement_time": "2026-03-03T23:00:00Z",
            }
        ]
    }
    contracts, skipped = discover_weather_contracts(payload)
    assert not skipped
    assert len(contracts) == 1
    assert contracts[0].strategy_id == "weather_precip"


def test_classify_weather_strategy_handles_temp_low_bucket_and_wind():
    low = classify_weather_strategy(_base_market(title="Chicago lowest temperature below 25F"))
    bucket = classify_weather_strategy(_base_market(title="Chicago highest temperature 25-26F", ticker="KXHIGHCHI-26MAR03-B25.5"))
    wind = classify_weather_strategy(_base_market(title="Chicago wind gust above 35mph", ticker="KXWINDCHI-26MAR03-T35"))
    assert low == "weather_temp_low"
    assert bucket == "weather_temp_bucket"
    assert wind == "weather_wind"


def test_classify_weather_strategy_ignores_non_weather_high_low_markets():
    ev = classify_weather_strategy(
        {
            "ticker": "EVSHARE-30JAN-20",
            "title": "EV market share in 2030?",
            "subtitle": "Will EV market share be above 20%?",
        }
    )
    era = classify_weather_strategy(
        {
            "ticker": "KXLEADERMLBERA-26-MFRI",
            "title": "Pro Baseball Lowest ERA",
            "subtitle": "Will Max Fried lead Pro Baseball in ERA?",
        }
    )
    quake = classify_weather_strategy(
        {
            "ticker": "KXEARTHQUAKECALIFORNIA-28",
            "title": "8.0 magnitude earthquake in California before 2028?",
            "subtitle": "Will there be an at least 8.0 magnitude earthquake in California before 2028?",
        }
    )

    assert ev is None
    assert era is None
    assert quake is None
