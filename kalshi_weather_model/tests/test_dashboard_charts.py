from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from weather_arb import config
from weather_arb.dashboard import app as dashboard_app
from weather_arb.utils.io_utils import safe_write_json_atomic


def _patch_dashboard_paths(monkeypatch, tmp_path: Path) -> None:
    strategies_dir = tmp_path / "strategies"
    paper_dir = tmp_path / "paper"
    strategies_dir.mkdir(parents=True, exist_ok=True)
    paper_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "STRATEGIES_DIR", strategies_dir)
    monkeypatch.setattr(config, "PAPER_SLEEVES_PATH", paper_dir / "paper_sleeves.json")

    for strategy_id in config.WEATHER_STRATEGY_IDS:
        config.strategy_paper_dir(strategy_id).mkdir(parents=True, exist_ok=True)
        config.strategy_signals_dir(strategy_id).mkdir(parents=True, exist_ok=True)
        config.strategy_quotes_dir(strategy_id).mkdir(parents=True, exist_ok=True)


def test_chart_data_aggregates_per_strategy_metrics(monkeypatch, tmp_path):
    _patch_dashboard_paths(monkeypatch, tmp_path)

    safe_write_json_atomic(
        config.PAPER_SLEEVES_PATH,
        {
            "initial_sleeve_equity": 100.0,
            "sleeves": {},
        },
    )
    safe_write_json_atomic(
        config.strategy_paper_metrics_daily_path("weather_temp_high"),
        {"by_day": {"2026-03-01": {"pnl_dollars": 1.0}, "2026-03-02": {"pnl_dollars": -0.5}}},
    )
    safe_write_json_atomic(
        config.strategy_paper_metrics_daily_path("weather_temp_low"),
        {"by_day": {"2026-03-01": {"pnl_dollars": 2.0}}},
    )
    safe_write_json_atomic(
        config.strategy_paper_metrics_daily_path("weather_temp_bucket"),
        {"by_day": {}},
    )

    pd.DataFrame([{"ticker": "A"}, {"ticker": "B"}]).to_parquet(
        config.strategy_signals_dir("weather_temp_high") / "signals_2026-03-01.parquet",
        index=False,
    )
    pd.DataFrame([{"ticker": "C"}, {"ticker": "D"}, {"ticker": "E"}]).to_parquet(
        config.strategy_signals_dir("weather_temp_low") / "signals_2026-03-01.parquet",
        index=False,
    )

    out = dashboard_app._chart_data()
    equity = out["equity_curve"]
    pnl = out["daily_pnl"]
    signal_count = out["signal_count"]

    assert equity == [
        {"date": "2026-03-01", "equity": 303.0},
        {"date": "2026-03-02", "equity": 302.5},
    ]
    assert pnl == [
        {"date": "2026-03-01", "pnl": 3.0},
        {"date": "2026-03-02", "pnl": -0.5},
    ]
    assert signal_count == [{"date": "2026-03-01", "count": 5}]


def test_latest_signal_and_quote_rows_read_strategy_dirs(monkeypatch, tmp_path):
    _patch_dashboard_paths(monkeypatch, tmp_path)
    now = datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc)

    pd.DataFrame(
        [
            {
                "ticker": "OLD",
                "generated_at_utc": (now - timedelta(minutes=1)).isoformat(),
            }
        ]
    ).to_parquet(
        config.strategy_signals_dir("weather_temp_high") / "signals_2026-03-03.parquet",
        index=False,
    )
    pd.DataFrame(
        [
            {
                "ticker": "NEW",
                "generated_at_utc": now.isoformat(),
            }
        ]
    ).to_parquet(
        config.strategy_signals_dir("weather_temp_low") / "signals_2026-03-03.parquet",
        index=False,
    )
    pd.DataFrame([{"ticker": "QOLD", "ts_utc": (now - timedelta(minutes=1)).isoformat()}]).to_parquet(
        config.strategy_quotes_dir("weather_temp_high") / "quotes_2026-03-03.parquet",
        index=False,
    )
    pd.DataFrame([{"ticker": "QNEW", "ts_utc": now.isoformat()}]).to_parquet(
        config.strategy_quotes_dir("weather_temp_bucket") / "quotes_2026-03-03.parquet",
        index=False,
    )

    latest_signal = dashboard_app._latest_signal_rows(limit=1)
    latest_quote = dashboard_app._latest_quotes_rows(limit=1)

    assert latest_signal and latest_signal[0]["ticker"] == "NEW"
    assert latest_quote and latest_quote[0]["ticker"] == "QNEW"
