from __future__ import annotations

import argparse
import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from weather_arb import __main__ as main
from weather_arb import config
from weather_arb.governance import model_registry


def _patch_cli_paths(monkeypatch, tmp_path: Path) -> None:
    data = tmp_path / "data"
    gov = data / "governance"
    ev = data / "eval"
    sig = data / "signals"
    paper = data / "paper"

    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "GOVERNANCE_DIR", gov)
    monkeypatch.setattr(config, "EVAL_DIR", ev)
    monkeypatch.setattr(config, "SIGNALS_DIR", sig)
    monkeypatch.setattr(config, "PAPER_DIR", paper)
    monkeypatch.setattr(config, "MODEL_REGISTRY_PATH", gov / "model_registry.json")
    monkeypatch.setattr(config, "GOVERNANCE_LOG_PATH", gov / "governance_log.json")
    monkeypatch.setattr(config, "THRESHOLD_CONFIG_PATH", gov / "thresholds.json")
    monkeypatch.setattr(config, "PAPER_METRICS_DAILY_PATH", paper / "paper_metrics_daily.json")
    monkeypatch.setattr(config, "PAPER_POSITIONS_PATH", paper / "paper_positions.json")
    monkeypatch.setattr(config, "PAPER_BLOTTER_DIR", paper / "paper_blotter")
    config.ensure_dirs()


def _patch_registry_functions(monkeypatch) -> None:
    def _wrap(name: str) -> None:
        original = getattr(model_registry, name)
        param_names = list(inspect.signature(original).parameters)
        path_index = param_names.index("path")

        def _wrapped(*args, **kwargs):
            if "path" not in kwargs and len(args) <= path_index:
                kwargs["path"] = config.MODEL_REGISTRY_PATH
            return original(*args, **kwargs)

        monkeypatch.setattr(model_registry, name, _wrapped)

    for name in (
        "load_registry",
        "save_registry",
        "register_model",
        "get_model",
        "get_models_by_status",
        "update_status",
        "set_paper_metrics",
        "get_champion",
        "promote_champion",
    ):
        _wrap(name)


def _write_passing_signals(now: datetime) -> None:
    rows = []
    for i in range(6):
        day = (now - timedelta(days=i)).date().isoformat()
        for j in range(3):
            rows.append({"generated_at_utc": f"{day}T12:00:00+00:00", "ev_cents": 10.0 + j})
    pd.DataFrame(rows).to_parquet(config.SIGNALS_DIR / "signals_2026-03-14.parquet", index=False)


def _write_passing_backtest(now: datetime) -> None:
    rows = []
    backtest_dir = config.EVAL_DIR / "backtest"
    backtest_dir.mkdir(parents=True, exist_ok=True)
    for i in range(30):
        day = (now - timedelta(days=(i % 10))).date().isoformat()
        pnl = 0.08 if i % 5 else -0.02
        rows.append({"pnl_dollars": pnl, "notional_dollars": 1.0, "date_key": day})
    pd.DataFrame(rows).to_parquet(backtest_dir / "backtest_2026-03-14.parquet", index=False)


def _write_passing_paper_metrics(now: datetime) -> None:
    by_day: dict[str, dict[str, float | int | str]] = {}
    for i in range(20):
        day = (now - timedelta(days=i)).date().isoformat()
        pnl = 0.25 if i != 7 else -0.04
        by_day[day] = {
            "trades": 2,
            "wins": 2 if pnl > 0 else 0,
            "losses": 0 if pnl > 0 else 2,
            "win_rate": 1.0 if pnl > 0 else 0.0,
            "pnl_dollars": pnl,
            "roi_per_trade": 0.03 if pnl > 0 else -0.01,
            "date_key": day,
        }
    with config.PAPER_METRICS_DAILY_PATH.open("w", encoding="utf-8") as f:
        json.dump({"by_day": by_day, "updated_at": now.isoformat()}, f)
    with config.PAPER_POSITIONS_PATH.open("w", encoding="utf-8") as f:
        json.dump({"open_positions": [], "closed_positions": [], "equity": 1000.0}, f)


def test_cmd_governance_eval_does_not_auto_promote(monkeypatch, tmp_path, capsys) -> None:
    _patch_cli_paths(monkeypatch, tmp_path)
    _patch_registry_functions(monkeypatch)
    now = datetime(2026, 3, 14, 23, 10, tzinfo=timezone.utc)
    monkeypatch.setattr(main, "_utc_now", lambda: now)

    entry = model_registry.register_model(
        model_id="r1:weather_temp_high:weather_temp:hybrid:global",
        run_id="r1",
        label_key="weather_temp",
        task_mode="hybrid",
        strategy_id="weather_temp_high",
        status="qualified",
        model_dir=str(config.ROOT_DIR),
        path=config.MODEL_REGISTRY_PATH,
    )
    _write_passing_paper_metrics(now)

    main.cmd_governance_eval(argparse.Namespace())
    out = json.loads(capsys.readouterr().out)
    registry = model_registry.load_registry(config.MODEL_REGISTRY_PATH)
    latest = model_registry.get_model(str(entry["model_id"]), path=config.MODEL_REGISTRY_PATH)

    assert out["result"]["status"] == "paper"
    assert out["promoted"] is None
    assert latest is not None and latest["status"] == "paper"
    assert (registry.get("champion_by_scope") or {}).get("global") is None


def test_hypothetical_cli_pipeline_reaches_paper_without_auto_promotion(monkeypatch, tmp_path, capsys) -> None:
    _patch_cli_paths(monkeypatch, tmp_path)
    _patch_registry_functions(monkeypatch)
    now = datetime(2026, 3, 14, 23, 10, tzinfo=timezone.utc)
    monkeypatch.setattr(main, "_utc_now", lambda: now)
    monkeypatch.setattr(
        main,
        "train_gate_snapshot",
        lambda: {
            "gate": {"gate": "train", "pass": True, "reasons": []},
            "city_stats": {"NYC": {"observations": 90, "missing_pct": 0.0}},
            "days_remaining_by_city": {"NYC": 0},
            "max_days_remaining": 0,
        },
    )

    _write_passing_signals(now)
    _write_passing_backtest(now)
    _write_passing_paper_metrics(now)

    train_out = main.cmd_run_train_gate(argparse.Namespace())
    capsys.readouterr()
    wf_out = main.cmd_run_wf_gate(argparse.Namespace())
    capsys.readouterr()
    backtest_out = main.cmd_run_backtest_gate(argparse.Namespace())
    capsys.readouterr()
    main.cmd_governance_eval(argparse.Namespace())
    gov_out = json.loads(capsys.readouterr().out)

    registry = model_registry.load_registry(config.MODEL_REGISTRY_PATH)
    assert len(list(registry.get("models") or [])) == 1
    latest = list(registry.get("models") or [])[0]

    assert train_out["model_status"] == "validating"
    assert wf_out["model_status"] == "wf_passed"
    assert backtest_out["model_status"] == "qualified"
    assert gov_out["result"]["status"] == "paper"
    assert gov_out["promoted"] is None
    assert latest["status"] == "paper"
    assert (registry.get("champion_by_scope") or {}).get("global") is None
