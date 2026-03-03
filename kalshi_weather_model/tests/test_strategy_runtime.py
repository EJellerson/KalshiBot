from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from weather_arb import config
from weather_arb.strategies import runtime
from weather_arb.utils.io_utils import safe_write_json_atomic


def _patch_config_paths(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    data = root / "data"
    cfg = root / "config"

    monkeypatch.setattr(config, "ROOT_DIR", root)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(config, "CONTRACTS_DIR", data / "contracts")
    monkeypatch.setattr(config, "MARKET_QUOTES_DIR", data / "market_quotes")
    monkeypatch.setattr(config, "FORECAST_SNAPSHOTS_DIR", data / "forecast_snapshots")
    monkeypatch.setattr(config, "OBSERVATIONS_DIR", data / "observations")
    monkeypatch.setattr(config, "SIGNALS_DIR", data / "signals")
    monkeypatch.setattr(config, "STRATEGIES_DIR", data / "strategies")
    monkeypatch.setattr(config, "PAPER_DIR", data / "paper")
    monkeypatch.setattr(config, "LIVE_DIR", data / "live")
    monkeypatch.setattr(config, "GOVERNANCE_DIR", data / "governance")
    monkeypatch.setattr(config, "EVAL_DIR", data / "eval")
    monkeypatch.setattr(config, "REPORTS_DIR", data / "reports")

    monkeypatch.setattr(config, "PAPER_POSITIONS_PATH", config.PAPER_DIR / "paper_positions.json")
    monkeypatch.setattr(config, "PAPER_METRICS_DAILY_PATH", config.PAPER_DIR / "paper_metrics_daily.json")
    monkeypatch.setattr(config, "PAPER_BLOTTER_DIR", config.PAPER_DIR / "paper_blotter")
    monkeypatch.setattr(config, "PAPER_SLEEVES_PATH", config.PAPER_DIR / "paper_sleeves.json")

    monkeypatch.setattr(config, "LIVE_POSITIONS_PATH", config.LIVE_DIR / "live_positions.json")
    monkeypatch.setattr(config, "LIVE_METRICS_DAILY_PATH", config.LIVE_DIR / "live_metrics_daily.json")
    monkeypatch.setattr(config, "LIVE_BLOTTER_DIR", config.LIVE_DIR / "live_blotter")

    monkeypatch.setattr(config, "MODEL_REGISTRY_PATH", config.GOVERNANCE_DIR / "model_registry.json")
    monkeypatch.setattr(config, "LIFECYCLE_STATE_PATH", config.GOVERNANCE_DIR / "lifecycle_state.json")
    monkeypatch.setattr(config, "GOVERNANCE_LOG_PATH", config.GOVERNANCE_DIR / "governance_log.json")
    monkeypatch.setattr(config, "THRESHOLD_CONFIG_PATH", config.GOVERNANCE_DIR / "thresholds.json")
    monkeypatch.setattr(config, "PORTFOLIO_RANKINGS_PATH", config.GOVERNANCE_DIR / "portfolio_rankings.json")
    monkeypatch.setattr(config, "CHAMPION_STATE_PATH", config.GOVERNANCE_DIR / "champion_state.json")

    monkeypatch.setattr(config, "CONTRACTS_ACTIVE_PATH", config.CONTRACTS_DIR / "contracts_active.parquet")
    monkeypatch.setattr(config, "CONTRACTS_HISTORY_PATH", config.CONTRACTS_DIR / "contracts_history.parquet")
    monkeypatch.setattr(config, "CONTRACT_DISCOVERY_CACHE_PATH", config.CONTRACTS_DIR / "discovery_cache.json")

    monkeypatch.setattr(config, "STRATEGY_LIQ_LOOKBACK_DAYS", 1)
    monkeypatch.setattr(config, "STRATEGY_LIQ_MIN_SNAPSHOTS", 50)
    monkeypatch.setattr(config, "STRATEGY_LIQ_MAX_SPREAD_PCT", 0.15)
    monkeypatch.setattr(config, "STRATEGY_LIQ_MIN_BOOK_SIZE", 10)
    monkeypatch.setattr(config, "STRATEGY_DEQUAL_CONSEC_FAILS", 2)

    config.ensure_dirs()


def _write_quotes(strategy_id: str, now_utc: datetime, *, spread: float, depth: int, n: int = 60) -> None:
    rows = []
    yes_bid = 0.50
    yes_ask = yes_bid + spread
    no_bid = 1.0 - yes_ask
    no_ask = 1.0 - yes_bid
    for i in range(n):
        rows.append(
            {
                "ticker": f"TICK{i}",
                "ts_utc": (now_utc - timedelta(minutes=i)).isoformat(timespec="seconds"),
                "yes_bid_dollars": yes_bid,
                "yes_ask_dollars": yes_ask,
                "no_bid_dollars": no_bid,
                "no_ask_dollars": no_ask,
                "yes_bid_size": depth,
                "yes_ask_size": depth,
                "no_bid_size": depth,
                "no_ask_size": depth,
            }
        )

    day_key = now_utc.astimezone(timezone.utc).date().isoformat()
    out_path = config.strategy_quotes_dir(strategy_id) / f"quotes_{day_key}.parquet"
    pd.DataFrame(rows).to_parquet(out_path, index=False)


def test_liquidity_gate_pass_then_dequal(monkeypatch, tmp_path):
    _patch_config_paths(monkeypatch, tmp_path)
    strategy_id = "weather_temp_high"
    base = datetime(2026, 3, 3, 12, 0, tzinfo=timezone.utc)

    _write_quotes(strategy_id, base, spread=0.08, depth=12)
    first = runtime._compute_liquidity_state(strategy_id, base)
    assert first["qualified"] is True

    t1 = base + timedelta(days=1)
    _write_quotes(strategy_id, t1, spread=0.30, depth=4)
    second = runtime._compute_liquidity_state(strategy_id, t1)
    assert second["consecutive_failures"] == 1
    assert second["qualified"] is True

    t2 = base + timedelta(days=2)
    _write_quotes(strategy_id, t2, spread=0.30, depth=4)
    third = runtime._compute_liquidity_state(strategy_id, t2)
    assert third["consecutive_failures"] >= 2
    assert third["qualified"] is False


def test_entry_gate_fail_closed_conditions():
    quality = {"parse_rate": 0.9, "eligible_count": 3}
    freshness = {
        "stale": {
            "contracts": False,
            "quotes": False,
            "signals": False,
            "benchmark": True,
        }
    }
    liquidity = {"qualified": True}

    allowed, reasons, checks = runtime._entry_gate(
        strategy_id="weather_temp_high",
        contract_quality=quality,
        freshness=freshness,
        liquidity=liquidity,
        benchmark_available=False,
    )
    assert allowed is False
    assert "benchmark" in reasons
    assert "freshness" in reasons
    assert checks["tradable"] is True

    allowed2, reasons2, checks2 = runtime._entry_gate(
        strategy_id="weather_precip",
        contract_quality=quality,
        freshness={"stale": {"contracts": False, "quotes": False, "benchmark": False}},
        liquidity={"qualified": True},
        benchmark_available=True,
    )
    assert allowed2 is False
    assert "discovery_only" in reasons2
    assert checks2["tradable"] is False


def test_portfolio_leaderboard_is_deterministic(monkeypatch, tmp_path):
    _patch_config_paths(monkeypatch, tmp_path)

    # Strategy gates: only two tradable strategies are eligible.
    safe_write_json_atomic(
        config.strategy_runtime_gates_path("weather_temp_high"),
        {"eligible_for_challenger": True, "data_health": {"green": True}},
    )
    safe_write_json_atomic(
        config.strategy_runtime_gates_path("weather_temp_low"),
        {"eligible_for_challenger": True, "data_health": {"green": True}},
    )
    safe_write_json_atomic(
        config.strategy_runtime_gates_path("weather_temp_bucket"),
        {"eligible_for_challenger": False, "data_health": {"green": False}},
    )

    by_day_high = {
        f"2026-02-{d:02d}": {"trades": 2, "wins": 1, "pnl_dollars": 1.0, "roi_per_trade": 0.01}
        for d in range(1, 11)
    }
    by_day_low = {
        f"2026-02-{d:02d}": {"trades": 2, "wins": 1, "pnl_dollars": 0.5, "roi_per_trade": 0.005}
        for d in range(1, 11)
    }

    safe_write_json_atomic(config.strategy_paper_metrics_daily_path("weather_temp_high"), {"by_day": by_day_high})
    safe_write_json_atomic(config.strategy_paper_metrics_daily_path("weather_temp_low"), {"by_day": by_day_low})
    safe_write_json_atomic(config.strategy_paper_metrics_daily_path("weather_temp_bucket"), {"by_day": {}})

    first = runtime.compute_portfolio_leaderboard(datetime(2026, 3, 3, tzinfo=timezone.utc))
    second = runtime.compute_portfolio_leaderboard(datetime(2026, 3, 3, tzinfo=timezone.utc))

    first_rows = [(r["strategy_id"], r["rank"], r["score"]) for r in first["rows"]]
    second_rows = [(r["strategy_id"], r["rank"], r["score"]) for r in second["rows"]]
    assert first_rows == second_rows
    assert first_rows[0][0] == "weather_temp_high"
