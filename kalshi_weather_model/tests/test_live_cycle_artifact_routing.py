from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from weather_arb import config
from weather_arb import __main__ as main
from weather_arb.utils.io_utils import safe_write_json_atomic


def _patch_live_cycle_paths(monkeypatch, tmp_path: Path) -> None:
    live_dir = tmp_path / "live"
    paper_dir = tmp_path / "paper"
    gov_dir = tmp_path / "governance"
    strategies_dir = tmp_path / "strategies"
    signals_dir = tmp_path / "signals"
    quotes_dir = tmp_path / "market_quotes"
    for path in [live_dir, paper_dir, gov_dir, strategies_dir, signals_dir, quotes_dir]:
        path.mkdir(parents=True, exist_ok=True)
    (live_dir / "live_blotter").mkdir(parents=True, exist_ok=True)
    (paper_dir / "paper_blotter").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(config, "LIVE_DIR", live_dir)
    monkeypatch.setattr(config, "PAPER_DIR", paper_dir)
    monkeypatch.setattr(config, "GOVERNANCE_DIR", gov_dir)
    monkeypatch.setattr(config, "STRATEGIES_DIR", strategies_dir)
    monkeypatch.setattr(config, "SIGNALS_DIR", signals_dir)
    monkeypatch.setattr(config, "MARKET_QUOTES_DIR", quotes_dir)
    monkeypatch.setattr(config, "LIVE_POSITIONS_PATH", live_dir / "live_positions.json")
    monkeypatch.setattr(config, "LIVE_METRICS_DAILY_PATH", live_dir / "live_metrics_daily.json")
    monkeypatch.setattr(config, "LIVE_BLOTTER_DIR", live_dir / "live_blotter")
    monkeypatch.setattr(config, "PAPER_POSITIONS_PATH", paper_dir / "paper_positions.json")
    monkeypatch.setattr(config, "PAPER_METRICS_DAILY_PATH", paper_dir / "paper_metrics_daily.json")
    monkeypatch.setattr(config, "PAPER_BLOTTER_DIR", paper_dir / "paper_blotter")
    monkeypatch.setattr(config, "GOVERNANCE_LOG_PATH", gov_dir / "governance_log.json")
    monkeypatch.setattr(config, "CHAMPION_STATE_PATH", gov_dir / "champion_state.json")
    monkeypatch.setattr(config, "SCHEDULER_INTERVAL_MINUTES", 15)


def test_cmd_live_cycle_invalid_champion_fails_closed(monkeypatch, tmp_path, capsys):
    _patch_live_cycle_paths(monkeypatch, tmp_path)
    called = {"run_live_cycle": False}

    monkeypatch.setattr(
        main,
        "live_routing_status",
        lambda: {
            "enabled": True,
            "reason": "auto_enabled_on_champion",
            "champion_id": "weather_unknown",
            "manual_enabled": False,
            "auto_enabled": True,
            "auto_toggle_enabled": True,
            "source": "strategy_champion_state",
        },
    )
    monkeypatch.setattr(main, "_latest_model_for_status", lambda _status: None)
    monkeypatch.setattr(main, "append_quote_rows", lambda _rows, _now: None)
    monkeypatch.setattr(main, "append_signal_rows", lambda _rows, _now: None)

    def _never_called(*_args, **_kwargs):
        called["run_live_cycle"] = True
        return {}

    monkeypatch.setattr(main, "run_live_cycle", _never_called)
    main.cmd_live_cycle(argparse.Namespace())

    out = json.loads(capsys.readouterr().out)
    assert out["skipped"] is True
    assert out["reason"] == "invalid_champion_strategy"
    assert called["run_live_cycle"] is False


def test_cmd_live_cycle_stale_strategy_live_input_blocks_entries_but_runs_cycle(monkeypatch, tmp_path, capsys):
    _patch_live_cycle_paths(monkeypatch, tmp_path)
    captured: dict[str, object] = {}
    champion_id = "weather_temp_high"
    now = datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(main, "_utc_now", lambda: now)
    monkeypatch.setattr(
        main,
        "live_routing_status",
        lambda: {
            "enabled": False,
            "reason": "no_champion_available",
            "champion_id": champion_id,
            "manual_enabled": False,
            "auto_enabled": False,
            "auto_toggle_enabled": True,
            "source": "strategy_champion_state",
        },
    )
    monkeypatch.setattr(main, "_latest_model_for_status", lambda _status: None)
    monkeypatch.setattr(main, "append_quote_rows", lambda _rows, _now: None)
    monkeypatch.setattr(main, "append_signal_rows", lambda _rows, _now: None)

    safe_write_json_atomic(
        config.strategy_live_input_path(champion_id),
        {
            "strategy_id": champion_id,
            "ts_utc": (now - timedelta(minutes=31)).isoformat(),
            "signals": [],
            "quote_map": {},
            "entry_allowed": True,
        },
    )

    def _capture_run_live_cycle(sig, qmap, *_args, **kwargs):
        captured["signals"] = sig
        captured["quote_map"] = qmap
        captured["allow_new_entries"] = kwargs.get("allow_new_entries")
        return {"opened": 0, "closed": 0, "alerts": []}

    monkeypatch.setattr(main, "run_live_cycle", _capture_run_live_cycle)
    main.cmd_live_cycle(argparse.Namespace())

    out = json.loads(capsys.readouterr().out)
    assert out["entry_blocked"] is True
    assert "strategy_live_input_stale" in out["entry_block_reasons"]
    assert captured["signals"] == []
    assert captured["quote_map"] == {}
    assert captured["allow_new_entries"] is False


def test_cmd_live_cycle_entry_gate_blocked_runs_exits_only(monkeypatch, tmp_path, capsys):
    _patch_live_cycle_paths(monkeypatch, tmp_path)
    captured: dict[str, object] = {}
    champion_id = "weather_temp_high"
    now = datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc)
    signals = [
        {
            "strategy_id": champion_id,
            "ticker": "KXHIGHNYC-26MAR03-T75",
            "city": "NYC",
            "side": "buy_yes",
            "ev_cents": 12.0,
            "min_ev_cents": 6.0,
            "settlement_ts_utc": (now + timedelta(hours=10)).isoformat(),
        }
    ]
    quote_map = {
        "KXHIGHNYC-26MAR03-T75": {
            "ticker": "KXHIGHNYC-26MAR03-T75",
            "ts_utc": now.isoformat(),
            "yes_bid_dollars": 0.45,
            "yes_ask_dollars": 0.46,
            "no_bid_dollars": 0.54,
            "no_ask_dollars": 0.55,
            "yes_bid_size": 25,
            "yes_ask_size": 25,
            "no_bid_size": 25,
            "no_ask_size": 25,
        }
    }

    monkeypatch.setattr(main, "_utc_now", lambda: now)
    monkeypatch.setattr(
        main,
        "live_routing_status",
        lambda: {
            "enabled": False,
            "reason": "no_champion_available",
            "champion_id": champion_id,
            "manual_enabled": False,
            "auto_enabled": False,
            "auto_toggle_enabled": True,
            "source": "strategy_champion_state",
        },
    )
    monkeypatch.setattr(main, "_latest_model_for_status", lambda _status: None)
    monkeypatch.setattr(main, "append_quote_rows", lambda _rows, _now: None)
    monkeypatch.setattr(main, "append_signal_rows", lambda _rows, _now: None)
    safe_write_json_atomic(
        config.strategy_live_input_path(champion_id),
        {
            "strategy_id": champion_id,
            "ts_utc": now.isoformat(),
            "signals": signals,
            "quote_map": quote_map,
            "entry_allowed": False,
            "blocked_reasons": ["train_gate"],
        },
    )

    def _capture_run_live_cycle(sig, qmap, *_args, **kwargs):
        captured["signals"] = sig
        captured["quote_map"] = qmap
        captured["allow_new_entries"] = kwargs.get("allow_new_entries")
        return {"opened": 0, "closed": 0, "alerts": []}

    monkeypatch.setattr(main, "run_live_cycle", _capture_run_live_cycle)
    main.cmd_live_cycle(argparse.Namespace())

    out = json.loads(capsys.readouterr().out)
    assert out["entry_blocked"] is True
    assert "strategy_entry_gate_blocked" in out["entry_block_reasons"]
    assert out["blocked_reasons"] == ["train_gate"]
    assert captured["signals"] == []
    assert captured["quote_map"] == quote_map
    assert captured["allow_new_entries"] is False


def test_cmd_live_cycle_routes_champion_artifact_signals(monkeypatch, tmp_path, capsys):
    _patch_live_cycle_paths(monkeypatch, tmp_path)
    champion_id = "weather_temp_low"
    now = datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc)
    captured: dict[str, object] = {}

    signals = [
        {
            "strategy_id": champion_id,
            "ticker": "KXLOWNYC-26MAR03-T39",
            "city": "NYC",
            "side": "buy_no",
            "comparator": "below",
            "ev_cents": 10.0,
            "min_ev_cents": 6.0,
            "settlement_ts_utc": (now + timedelta(hours=12)).isoformat(),
        },
        {
            "strategy_id": champion_id,
            "ticker": "KXHIGHCHI-26MAR03-B39.5",
            "city": "Chicago",
            "side": "buy_yes",
            "comparator": "between",
            "ev_cents": 9.0,
            "min_ev_cents": 6.0,
            "settlement_ts_utc": (now + timedelta(hours=14)).isoformat(),
        },
    ]
    quote_map = {
        "KXLOWNYC-26MAR03-T39": {
            "ticker": "KXLOWNYC-26MAR03-T39",
            "ts_utc": now.isoformat(),
            "yes_bid_dollars": 0.30,
            "yes_ask_dollars": 0.31,
            "no_bid_dollars": 0.69,
            "no_ask_dollars": 0.70,
            "yes_bid_size": 25,
            "yes_ask_size": 25,
            "no_bid_size": 25,
            "no_ask_size": 25,
        },
        "KXHIGHCHI-26MAR03-B39.5": {
            "ticker": "KXHIGHCHI-26MAR03-B39.5",
            "ts_utc": now.isoformat(),
            "yes_bid_dollars": 0.45,
            "yes_ask_dollars": 0.46,
            "no_bid_dollars": 0.54,
            "no_ask_dollars": 0.55,
            "yes_bid_size": 25,
            "yes_ask_size": 25,
            "no_bid_size": 25,
            "no_ask_size": 25,
        },
    }

    safe_write_json_atomic(
        config.strategy_live_input_path(champion_id),
        {
            "strategy_id": champion_id,
            "ts_utc": now.isoformat(),
            "signals": signals,
            "quote_map": quote_map,
            "entry_allowed": True,
        },
    )

    monkeypatch.setattr(main, "_utc_now", lambda: now)
    monkeypatch.setattr(
        main,
        "live_routing_status",
        lambda: {
            "enabled": False,
            "reason": "auto_enabled_on_champion",
            "champion_id": champion_id,
            "manual_enabled": False,
            "auto_enabled": True,
            "auto_toggle_enabled": True,
            "source": "strategy_champion_state",
        },
    )
    monkeypatch.setattr(main, "_latest_model_for_status", lambda _status: None)
    monkeypatch.setattr(main, "append_quote_rows", lambda _rows, _now: None)
    monkeypatch.setattr(main, "append_signal_rows", lambda _rows, _now: None)
    monkeypatch.setattr(main, "_build_signals_and_quotes", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("legacy path should not be used")))

    def _capture_run_live_cycle(sig, qmap, *_args, **_kwargs):
        captured["signals"] = sig
        captured["quote_map"] = qmap
        return {"opened": 0, "orders": []}

    monkeypatch.setattr(main, "run_live_cycle", _capture_run_live_cycle)
    main.cmd_live_cycle(argparse.Namespace())

    out = json.loads(capsys.readouterr().out)
    assert out["summary"]["opened"] == 0
    assert out["signals"] == 2
    assert captured["signals"] == signals
    assert captured["quote_map"] == quote_map


def test_cmd_paper_cycle_pre_train_blocks_new_entries(monkeypatch, tmp_path, capsys):
    _patch_live_cycle_paths(monkeypatch, tmp_path)
    now = datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc)
    captured: dict[str, object] = {}

    monkeypatch.setattr(main, "_utc_now", lambda: now)
    monkeypatch.setattr(
        main,
        "_build_signals_and_quotes",
        lambda _now: (
            [
                {
                    "ticker": "KXHIGHNYC-26MAR03-T75",
                    "city": "NYC",
                    "side": "buy_yes",
                    "ev_cents": 12.0,
                    "min_ev_cents": 6.0,
                    "settlement_ts_utc": (now + timedelta(hours=10)).isoformat(),
                }
            ],
            {
                "KXHIGHNYC-26MAR03-T75": {
                    "ticker": "KXHIGHNYC-26MAR03-T75",
                    "ts_utc": now.isoformat(),
                    "yes_bid_dollars": 0.49,
                    "yes_ask_dollars": 0.50,
                    "no_bid_dollars": 0.49,
                    "no_ask_dollars": 0.50,
                    "yes_bid_size": 25,
                    "yes_ask_size": 25,
                    "no_bid_size": 25,
                    "no_ask_size": 25,
                }
            },
            [],
        ),
    )
    monkeypatch.setattr(main, "append_quote_rows", lambda _rows, _now: None)
    monkeypatch.setattr(main, "append_signal_rows", lambda _rows, _now: None)
    monkeypatch.setattr(main, "train_gate_snapshot", lambda: {"gate": {"pass": False}})
    monkeypatch.setattr(main, "_latest_model_for_status", lambda status: {"model_id": "m1", "status": status} if status == "wf_passed" else None)

    def _capture_run_paper_cycle(signals, quote_map, *_args, **kwargs):
        captured["signals"] = list(signals)
        captured["quote_map"] = dict(quote_map)
        captured["allow_new_entries"] = kwargs.get("allow_new_entries")
        return {"opened": 0, "closed": 0, "entries_allowed": False}

    monkeypatch.setattr(main, "run_paper_cycle", _capture_run_paper_cycle)
    main.cmd_paper_cycle(argparse.Namespace())

    out = json.loads(capsys.readouterr().out)
    assert out["entry_blocked"] is True
    assert out["entry_block_reasons"] == ["train_gate"]
    assert out["train_gate_pass"] is False
    assert out["model_status"] == "wf_passed"
    assert captured["allow_new_entries"] is False
    assert len(captured["signals"]) == 1


def test_cmd_paper_cycle_train_pass_allows_entries(monkeypatch, tmp_path, capsys):
    _patch_live_cycle_paths(monkeypatch, tmp_path)
    now = datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc)
    captured: dict[str, object] = {}

    monkeypatch.setattr(main, "_utc_now", lambda: now)
    monkeypatch.setattr(main, "_build_signals_and_quotes", lambda _now: ([], {}, []))
    monkeypatch.setattr(main, "append_quote_rows", lambda _rows, _now: None)
    monkeypatch.setattr(main, "append_signal_rows", lambda _rows, _now: None)
    monkeypatch.setattr(main, "train_gate_snapshot", lambda: {"gate": {"pass": True}})
    monkeypatch.setattr(main, "_latest_model_for_status", lambda status: {"model_id": "m1", "status": status} if status == "wf_passed" else None)

    def _capture_run_paper_cycle(*_args, **kwargs):
        captured["allow_new_entries"] = kwargs.get("allow_new_entries")
        return {"opened": 1, "closed": 0, "entries_allowed": True}

    monkeypatch.setattr(main, "run_paper_cycle", _capture_run_paper_cycle)
    main.cmd_paper_cycle(argparse.Namespace())

    out = json.loads(capsys.readouterr().out)
    assert out["entry_blocked"] is False
    assert out["entry_block_reasons"] == []
    assert out["train_gate_pass"] is True
    assert out["summary"]["opened"] == 1
    assert captured["allow_new_entries"] is True


def test_cmd_paper_cycle_without_model_still_manages_positions(monkeypatch, tmp_path, capsys):
    _patch_live_cycle_paths(monkeypatch, tmp_path)
    now = datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc)
    captured: dict[str, object] = {}

    monkeypatch.setattr(main, "_utc_now", lambda: now)
    monkeypatch.setattr(main, "_build_signals_and_quotes", lambda _now: ([], {}, []))
    monkeypatch.setattr(main, "append_quote_rows", lambda _rows, _now: None)
    monkeypatch.setattr(main, "append_signal_rows", lambda _rows, _now: None)
    monkeypatch.setattr(main, "train_gate_snapshot", lambda: {"gate": {"pass": True}})
    monkeypatch.setattr(main, "_latest_model_for_status", lambda _status: None)

    def _capture_run_paper_cycle(*_args, **kwargs):
        captured["allow_new_entries"] = kwargs.get("allow_new_entries")
        return {"opened": 0, "closed": 1, "entries_allowed": False}

    monkeypatch.setattr(main, "run_paper_cycle", _capture_run_paper_cycle)
    main.cmd_paper_cycle(argparse.Namespace())

    out = json.loads(capsys.readouterr().out)
    assert out["entry_blocked"] is True
    assert out["entry_block_reasons"] == ["no_eligible_model"]
    assert out["model_status"] is None
    assert out["summary"]["closed"] == 1
    assert captured["allow_new_entries"] is False
