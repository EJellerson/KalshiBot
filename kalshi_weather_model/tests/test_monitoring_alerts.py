from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from weather_arb.analytics import monitoring


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

    out = monitoring.operational_alerts_snapshot(train, inventory, events)
    codes = {row["code"] for row in out["alerts"]}
    info_codes = {row["code"] for row in out.get("info", [])}

    assert out["status"] == "critical"
    assert "no_contracts" in codes
    assert "train_gate_blocked" not in codes
    assert "train_gate_blocked" in info_codes
    assert int((out.get("suppressed") or {}).get("by_reason", {}).get("warmup_train_gate", 0)) == 1


def test_operational_alerts_ok_when_healthy() -> None:
    train = {"gate": {"pass": True}, "max_days_remaining": 0}
    inventory = {
        "stale_streams": [],
        "ingest_gap_streams": [],
        "ingest_gap_threshold_minutes": 2880.0,
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

    out = monitoring.operational_alerts_snapshot(train, inventory, events)
    assert out["status"] == "ok"
    assert out["alerts"] == []
    assert out.get("info", []) == []


def test_operational_alerts_emit_ingest_stale_when_stream_gap_exceeds_48h() -> None:
    train = {"gate": {"pass": True}, "max_days_remaining": 0}
    inventory = {
        "stale_streams": ["observations", "quotes"],
        "ingest_gap_streams": ["observations", "quotes"],
        "ingest_gap_threshold_minutes": 2880.0,
        "stream_age_minutes": {"observations": 3000.0, "quotes": 3005.0},
        "freshness_threshold_minutes": {"observations": 130.0, "quotes": 45.0},
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

    out = monitoring.operational_alerts_snapshot(train, inventory, events)
    codes = {row["code"] for row in out["alerts"]}

    assert "ingest_stale_observations" in codes
    assert "ingest_stale_quotes" in codes
    assert "stale_observations" not in codes
    assert "stale_quotes" not in codes


def test_variant_alerts_route_discovery_only_to_info(monkeypatch) -> None:
    def fake_strategies_health_snapshot() -> dict:
        return {
            "ts_utc": "2026-03-03T00:00:00+00:00",
            "rows": [
                {
                    "strategy_id": "weather_temp_high",
                    "mode": "tradable",
                    "train_gate_pass": True,
                    "benchmark_available": True,
                    "alerts": [
                        {
                            "severity": "warn",
                            "code": "stale_quotes_weather_temp_high",
                            "message": "Quotes stream is stale.",
                        }
                    ],
                    "contract_quality": {"parse_rate": 0.5, "raw_count": 50, "parse_alert_sample_count": 50, "eligible_count": 0},
                    "freshness": {},
                },
                {
                    "strategy_id": "weather_precip",
                    "mode": "discovery_only",
                    "train_gate_pass": False,
                    "benchmark_available": False,
                    "alerts": [
                        {
                            "severity": "warn",
                            "code": "stale_quotes_weather_precip",
                            "message": "Quotes stream is stale.",
                        }
                    ],
                    "contract_quality": {"parse_rate": 0.0, "raw_count": 0, "parse_alert_sample_count": 0, "eligible_count": 0},
                    "freshness": {},
                },
            ],
        }

    monkeypatch.setattr(monitoring, "strategies_health_snapshot", fake_strategies_health_snapshot)

    out = monitoring.variant_operational_alerts_snapshot()

    actionable_codes = {row["code"] for row in out["alerts"]}
    info_codes = {row["code"] for row in out.get("info", [])}

    assert "stale_quotes_weather_temp_high" in actionable_codes
    assert "contract_parse_degraded_weather_temp_high" in actionable_codes
    assert "contract_eligible_low_weather_temp_high" in actionable_codes

    assert "stale_quotes_weather_precip" not in actionable_codes
    assert "contract_parse_degraded_weather_precip" not in actionable_codes
    assert "contract_eligible_low_weather_precip" not in actionable_codes

    assert "stale_quotes_weather_precip" in info_codes
    assert "contract_parse_degraded_weather_precip" in info_codes
    assert "contract_eligible_low_weather_precip" in info_codes

    assert out["status"] == "warn"
    assert int((out.get("suppressed") or {}).get("by_reason", {}).get("discovery_only", 0)) >= 3


def test_variant_parse_warmup_routed_to_info_for_tradable(monkeypatch) -> None:
    monkeypatch.setattr(monitoring.config, "STRATEGY_PARSE_ALERT_MIN_RAW", 25)

    def fake_strategies_health_snapshot() -> dict:
        return {
            "ts_utc": "2026-03-03T00:00:00+00:00",
            "rows": [
                {
                    "strategy_id": "weather_temp_high",
                    "mode": "tradable",
                    "train_gate_pass": True,
                    "benchmark_available": True,
                    "alerts": [],
                    "contract_quality": {
                        "parse_rate": 0.4,
                        "raw_count": 12,
                        "parse_alert_sample_count": 12,
                        "eligible_count": 12,
                    },
                    "freshness": {},
                }
            ],
        }

    monkeypatch.setattr(monitoring, "strategies_health_snapshot", fake_strategies_health_snapshot)

    out = monitoring.variant_operational_alerts_snapshot()

    actionable_codes = {row["code"] for row in out["alerts"]}
    info_codes = {row["code"] for row in out.get("info", [])}

    assert "contract_parse_degraded_weather_temp_high" not in actionable_codes
    assert "contract_parse_degraded_weather_temp_high" in info_codes
    assert int((out.get("suppressed") or {}).get("by_reason", {}).get("warmup_parse_sample", 0)) >= 1


def test_variant_warmup_train_blockers_routed_to_info(monkeypatch) -> None:
    def fake_strategies_health_snapshot() -> dict:
        return {
            "ts_utc": "2026-03-04T00:00:00+00:00",
            "rows": [
                {
                    "strategy_id": "weather_temp_high",
                    "mode": "tradable",
                    "train_gate_pass": False,
                    "benchmark_available": False,
                    "alerts": [
                        {
                            "severity": "warn",
                            "code": "train_gate_blocked_weather_temp_high",
                            "message": "Train gate blocked entries.",
                        },
                        {
                            "severity": "warn",
                            "code": "liquidity_blocked_weather_temp_high",
                            "message": "Liquidity gate blocked entries.",
                        },
                        {
                            "severity": "warn",
                            "code": "stale_benchmark_weather_temp_high",
                            "message": "Benchmark unavailable during train warmup; entries remain blocked.",
                        },
                        {
                            "severity": "warn",
                            "code": "stale_quotes_weather_temp_high",
                            "message": "Quotes stream is stale.",
                        },
                    ],
                    "contract_quality": {
                        "parse_rate": 1.0,
                        "raw_count": 100,
                        "parse_alert_sample_count": 100,
                        "eligible_count": 0,
                    },
                    "freshness": {},
                }
            ],
        }

    monkeypatch.setattr(monitoring, "strategies_health_snapshot", fake_strategies_health_snapshot)

    out = monitoring.variant_operational_alerts_snapshot()
    actionable_codes = {row["code"] for row in out["alerts"]}
    info_codes = {row["code"] for row in out.get("info", [])}

    assert "stale_quotes_weather_temp_high" in actionable_codes

    assert "train_gate_blocked_weather_temp_high" not in actionable_codes
    assert "liquidity_blocked_weather_temp_high" not in actionable_codes
    assert "contract_eligible_low_weather_temp_high" not in actionable_codes
    assert "stale_benchmark_weather_temp_high" not in actionable_codes

    assert "train_gate_blocked_weather_temp_high" in info_codes
    assert "liquidity_blocked_weather_temp_high" in info_codes
    assert "contract_eligible_low_weather_temp_high" in info_codes
    assert "stale_benchmark_weather_temp_high" in info_codes

    assert int((out.get("suppressed") or {}).get("by_reason", {}).get("warmup_train_blockers", 0)) >= 4


def test_variant_stale_benchmark_data_remains_actionable_pre_train(monkeypatch) -> None:
    def fake_strategies_health_snapshot() -> dict:
        return {
            "ts_utc": "2026-03-04T00:00:00+00:00",
            "rows": [
                {
                    "strategy_id": "weather_temp_high",
                    "mode": "tradable",
                    "train_gate_pass": False,
                    "benchmark_available": True,
                    "alerts": [
                        {
                            "severity": "warn",
                            "code": "stale_benchmark_weather_temp_high",
                            "message": "Benchmark data is stale (last successful benchmark update exceeded threshold).",
                        }
                    ],
                    "contract_quality": {
                        "parse_rate": 1.0,
                        "raw_count": 100,
                        "parse_alert_sample_count": 100,
                        "eligible_count": 12,
                    },
                    "freshness": {},
                }
            ],
        }

    monkeypatch.setattr(monitoring, "strategies_health_snapshot", fake_strategies_health_snapshot)

    out = monitoring.variant_operational_alerts_snapshot()
    actionable_codes = {row["code"] for row in out["alerts"]}
    info_codes = {row["code"] for row in out.get("info", [])}

    assert "stale_benchmark_weather_temp_high" in actionable_codes
    assert "stale_benchmark_weather_temp_high" not in info_codes
    assert int((out.get("suppressed") or {}).get("by_reason", {}).get("warmup_train_blockers", 0)) == 0


def _patch_monitoring_paths(monkeypatch, tmp_path: Path) -> None:
    forecasts = tmp_path / "forecast_snapshots"
    observations = tmp_path / "observations"
    quotes = tmp_path / "market_quotes"
    signals = tmp_path / "signals"
    contracts = tmp_path / "contracts"
    strategies = tmp_path / "strategies"

    for path in [forecasts, observations, quotes, signals, contracts, strategies]:
        path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(monitoring.config, "FORECAST_SNAPSHOTS_DIR", forecasts)
    monkeypatch.setattr(monitoring.config, "OBSERVATIONS_DIR", observations)
    monkeypatch.setattr(monitoring.config, "MARKET_QUOTES_DIR", quotes)
    monkeypatch.setattr(monitoring.config, "SIGNALS_DIR", signals)
    monkeypatch.setattr(monitoring.config, "CONTRACTS_DIR", contracts)
    monkeypatch.setattr(monitoring.config, "CONTRACTS_ACTIVE_PATH", contracts / "contracts_active.parquet")
    monkeypatch.setattr(monitoring.config, "STRATEGIES_DIR", strategies)

    for strategy_id in monitoring.config.WEATHER_STRATEGY_IDS:
        monitoring.config.strategy_contracts_dir(strategy_id).mkdir(parents=True, exist_ok=True)
        monitoring.config.strategy_quotes_dir(strategy_id).mkdir(parents=True, exist_ok=True)
        monitoring.config.strategy_signals_dir(strategy_id).mkdir(parents=True, exist_ok=True)


def test_inventory_contracts_use_strategy_aggregate_first(monkeypatch, tmp_path) -> None:
    _patch_monitoring_paths(monkeypatch, tmp_path)

    pd.DataFrame([{"ticker": "LEGACY"}]).to_parquet(monitoring.config.CONTRACTS_ACTIVE_PATH, index=False)
    pd.DataFrame([{"ticker": "H1"}, {"ticker": "H2"}]).to_parquet(
        monitoring.config.strategy_contracts_active_path("weather_temp_high"),
        index=False,
    )
    pd.DataFrame([{"ticker": "L1"}]).to_parquet(
        monitoring.config.strategy_contracts_active_path("weather_temp_low"),
        index=False,
    )

    snapshot = monitoring.data_inventory_snapshot()
    assert int(snapshot.get("contracts_active", 0)) == 3
    assert snapshot.get("contracts_source") == "strategy_aggregate"
    assert snapshot.get("contracts_latest_mtime_utc") is not None
    assert snapshot.get("contracts_age_minutes") is not None


def test_inventory_empty_streams_are_marked_stale(monkeypatch, tmp_path) -> None:
    _patch_monitoring_paths(monkeypatch, tmp_path)

    snapshot = monitoring.data_inventory_snapshot()
    stale_streams = set(snapshot.get("stale_streams") or [])

    assert "forecasts" in stale_streams
    assert "observations" in stale_streams
    assert "quotes" in stale_streams
    assert "signals" in stale_streams


def test_inventory_ingest_gap_detects_stale_and_fresh_streams(monkeypatch, tmp_path) -> None:
    _patch_monitoring_paths(monkeypatch, tmp_path)

    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=49)

    forecast_path = monitoring.config.FORECAST_SNAPSHOTS_DIR / "forecast_2026-03-03.parquet"
    obs_path = monitoring.config.OBSERVATIONS_DIR / "obs_2026-03-03.parquet"
    quote_path = monitoring.config.strategy_quotes_dir("weather_temp_high") / "quotes_2026-03-03.parquet"

    pd.DataFrame([{"city": "NYC"}]).to_parquet(forecast_path, index=False)
    pd.DataFrame([{"city": "NYC", "obs_date_local": "2026-03-03"}]).to_parquet(obs_path, index=False)
    pd.DataFrame(
        [
            {
                "ticker": "T1",
                "ts_utc": now.isoformat(timespec="seconds"),
                "yes_bid_dollars": 0.01,
                "yes_ask_dollars": 0.02,
                "no_bid_dollars": 0.98,
                "no_ask_dollars": 0.99,
                "yes_bid_size": 12,
                "yes_ask_size": 12,
                "no_bid_size": 12,
                "no_ask_size": 12,
            }
        ]
    ).to_parquet(quote_path, index=False)

    old_ts = old.timestamp()
    os.utime(obs_path, (old_ts, old_ts))
    os.utime(quote_path, (now.timestamp(), now.timestamp()))
    os.utime(forecast_path, (now.timestamp(), now.timestamp()))

    snapshot = monitoring.data_inventory_snapshot()
    ingest_gap_streams = set(snapshot.get("ingest_gap_streams") or [])

    assert "observations" in ingest_gap_streams
    assert "quotes" not in ingest_gap_streams
    assert "forecasts" not in ingest_gap_streams
