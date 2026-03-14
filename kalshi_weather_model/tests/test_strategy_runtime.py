from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from weather_arb import config
from weather_arb.fees import estimated_entry_cost_cents
from weather_arb.model.fair_value import compute_ev_cents
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


def _write_quotes(
    strategy_id: str,
    now_utc: datetime,
    *,
    spread: float,
    depth: int,
    n: int = 60,
    depths: tuple[int, int, int, int] | None = None,
) -> None:
    rows = []
    yes_bid = 0.50
    yes_ask = yes_bid + spread
    no_bid = 1.0 - yes_ask
    no_ask = 1.0 - yes_bid
    size_tuple = depths or (depth, depth, depth, depth)
    for i in range(n):
        rows.append(
            {
                "ticker": f"TICK{i}",
                "ts_utc": (now_utc - timedelta(minutes=i)).isoformat(timespec="seconds"),
                "yes_bid_dollars": yes_bid,
                "yes_ask_dollars": yes_ask,
                "no_bid_dollars": no_bid,
                "no_ask_dollars": no_ask,
                "yes_bid_size": size_tuple[0],
                "yes_ask_size": size_tuple[1],
                "no_bid_size": size_tuple[2],
                "no_ask_size": size_tuple[3],
            }
        )

    day_key = now_utc.astimezone(timezone.utc).date().isoformat()
    out_path = config.strategy_quotes_dir(strategy_id) / f"quotes_{day_key}.parquet"
    pd.DataFrame(rows).to_parquet(out_path, index=False)


def _write_penny_quotes(
    strategy_id: str,
    now_utc: datetime,
    *,
    yes_bid: float,
    yes_ask: float,
    n: int = 60,
    depth: int = 12,
) -> None:
    rows = []
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

    _write_quotes(strategy_id, base, spread=0.06, depth=12)
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


def test_liquidity_gate_accepts_one_tick_penny_books(monkeypatch, tmp_path):
    _patch_config_paths(monkeypatch, tmp_path)
    strategy_id = "weather_temp_high"
    now_utc = datetime(2026, 3, 3, 12, 0, tzinfo=timezone.utc)

    _write_penny_quotes(
        strategy_id,
        now_utc,
        yes_bid=0.01,
        yes_ask=0.02,
        n=60,
        depth=12,
    )
    out = runtime._compute_liquidity_state(strategy_id, now_utc)
    last_window = dict(out.get("last_window") or {})

    assert out["qualified"] is True
    assert round(float(last_window.get("median_spread", 0.0) or 0.0), 6) == 1.0
    assert round(float(last_window.get("median_spread_abs_dollars", 0.0) or 0.0), 6) == 0.01
    assert round(float(last_window.get("median_spread_pct", 0.0) or 0.0), 6) > 0.15


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
        train_gate_pass=True,
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
        train_gate_pass=True,
        benchmark_available=True,
    )
    assert allowed2 is False
    assert "discovery_only" in reasons2
    assert checks2["tradable"] is False


def test_entry_gate_blocks_on_train_gate_failure():
    quality = {"parse_rate": 0.9, "eligible_count": 3}
    freshness = {"stale": {"contracts": False, "quotes": False, "signals": False, "benchmark": False}}
    liquidity = {"qualified": True}

    allowed, reasons, checks = runtime._entry_gate(
        strategy_id="weather_temp_high",
        contract_quality=quality,
        freshness=freshness,
        liquidity=liquidity,
        train_gate_pass=False,
        benchmark_available=True,
    )
    assert allowed is False
    assert "train_gate" in reasons
    assert checks["train_ok"] is False


def test_compute_freshness_uses_last_successful_benchmark_timestamp(monkeypatch, tmp_path):
    _patch_config_paths(monkeypatch, tmp_path)
    strategy_id = "weather_temp_high"
    now_utc = datetime(2026, 3, 3, 12, 0, tzinfo=timezone.utc)
    success_ts = (now_utc - timedelta(hours=4)).isoformat(timespec="seconds")
    attempt_ts = now_utc.isoformat(timespec="seconds")

    safe_write_json_atomic(
        config.strategy_benchmark_latest_path(strategy_id),
        {
            "strategy_id": strategy_id,
            "updated_at": attempt_ts,
            "last_attempted_at_utc": attempt_ts,
            "last_successful_updated_at_utc": success_ts,
            "available": False,
            "reason": "benchmark_fetch_failed: timeout",
        },
    )

    out = runtime._compute_freshness(strategy_id, now_utc)
    stale = dict(out.get("stale") or {})
    ages = dict(out.get("ages_minutes") or {})

    assert stale["benchmark"] is True
    assert stale["benchmark_attempt"] is False
    assert float(ages.get("benchmark", 0.0) or 0.0) >= 240.0
    assert float(ages.get("benchmark_attempt", 0.0) or 0.0) < 1.0


def test_compute_freshness_legacy_unavailable_without_success_is_stale(monkeypatch, tmp_path):
    _patch_config_paths(monkeypatch, tmp_path)
    strategy_id = "weather_temp_high"
    now_utc = datetime(2026, 3, 3, 12, 0, tzinfo=timezone.utc)
    attempt_ts = now_utc.isoformat(timespec="seconds")

    safe_write_json_atomic(
        config.strategy_benchmark_latest_path(strategy_id),
        {
            "strategy_id": strategy_id,
            "updated_at": attempt_ts,
            "available": False,
            "reason": "legacy_error",
        },
    )

    out = runtime._compute_freshness(strategy_id, now_utc)
    stale = dict(out.get("stale") or {})
    assert stale["benchmark"] is True
    assert stale["benchmark_attempt"] is False


def test_compute_freshness_legacy_available_falls_back_to_updated_at(monkeypatch, tmp_path):
    _patch_config_paths(monkeypatch, tmp_path)
    strategy_id = "weather_temp_high"
    now_utc = datetime(2026, 3, 3, 12, 0, tzinfo=timezone.utc)
    updated_ts = (now_utc - timedelta(minutes=30)).isoformat(timespec="seconds")

    safe_write_json_atomic(
        config.strategy_benchmark_latest_path(strategy_id),
        {
            "strategy_id": strategy_id,
            "updated_at": updated_ts,
            "available": True,
            "source": "legacy_noaa",
        },
    )

    out = runtime._compute_freshness(strategy_id, now_utc)
    stale = dict(out.get("stale") or {})
    assert stale["benchmark"] is False


def test_liquidity_uses_worst_side_depth(monkeypatch, tmp_path):
    _patch_config_paths(monkeypatch, tmp_path)
    strategy_id = "weather_temp_high"
    now_utc = datetime(2026, 3, 3, 12, 0, tzinfo=timezone.utc)

    _write_quotes(
        strategy_id,
        now_utc,
        spread=0.08,
        depth=12,
        depths=(12, 12, 12, 1),
    )
    out = runtime._compute_liquidity_state(strategy_id, now_utc)
    last_window = dict(out.get("last_window") or {})

    assert float(last_window.get("median_depth", 0.0) or 0.0) == 1.0
    assert out["qualified"] is False


def test_wf_feasibility_requires_min_signals_per_window(monkeypatch, tmp_path):
    _patch_config_paths(monkeypatch, tmp_path)
    strategy_id = "weather_temp_high"
    monkeypatch.setattr(config, "WF_MIN_WINDOWS", 6)
    monkeypatch.setattr(config, "WF_MIN_SIGNALS_PER_WINDOW", 3)

    base = datetime(2026, 2, 1, 12, 0, tzinfo=timezone.utc)
    rows = []
    for d in range(6):
        day = base + timedelta(days=d)
        for i in range(2):  # below threshold of 3
            rows.append(
                {
                    "ticker": f"T{d}_{i}",
                    "generated_at_utc": (day + timedelta(hours=i)).isoformat(timespec="seconds"),
                    "ev_cents": 12.0,
                }
            )

    pd.DataFrame(rows).to_parquet(config.strategy_signals_dir(strategy_id) / "signals_2026-02-06.parquet", index=False)
    safe_write_json_atomic(
        config.strategy_runtime_cycle_path(strategy_id),
        {
            "entry_gate": {
                "checks": {
                    "parse_ok": True,
                    "freshness_ok": True,
                    "benchmark_ok": True,
                    "liquidity_ok": True,
                }
            }
        },
    )
    monkeypatch.setattr(runtime, "evaluate_train_gate", lambda _x: {"pass": True, "reasons": []})
    monkeypatch.setattr(runtime, "evaluate_backtest_gate", lambda _x: {"pass": True, "reasons": []})
    monkeypatch.setattr(runtime, "evaluate_paper_gates", lambda _x: (True, True, []))

    out = runtime.evaluate_strategy_gates(strategy_id, now_utc=datetime(2026, 2, 8, tzinfo=timezone.utc))
    assert out["wf"]["details"]["windows"] == 6
    assert out["wf"]["details"]["feasible_rate"] == 0.0
    assert out["wf"]["pass"] is False


def test_run_strategy_cycle_persists_live_input_artifact(monkeypatch, tmp_path):
    _patch_config_paths(monkeypatch, tmp_path)
    strategy_id = "weather_temp_high"
    now = datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc)

    class _ResidualModel:
        def p_exceeds(self, **_kwargs):
            return 0.8

    class _FakePublicClient:
        def get_market_orderbook(self, _ticker: str):
            return {"orderbook": []}

    monkeypatch.setattr(
        runtime,
        "parse_dollar_orderbook",
        lambda _raw, ticker: {
            "ticker": ticker,
            "yes_bid_dollars": 0.49,
            "yes_ask_dollars": 0.50,
            "no_bid_dollars": 0.49,
            "no_ask_dollars": 0.50,
            "yes_bid_size": 25,
            "yes_ask_size": 25,
            "no_bid_size": 25,
            "no_ask_size": 25,
        },
    )
    monkeypatch.setattr(runtime, "_compute_liquidity_state", lambda *_args, **_kwargs: {"qualified": True})
    monkeypatch.setattr(runtime, "evaluate_train_gate", lambda _x: {"pass": True, "reasons": []})
    monkeypatch.setattr(
        runtime,
        "run_paper_cycle",
        lambda *_args, **_kwargs: {"opened": 0, "closed": 0, "open_positions": 0, "equity": 1000.0},
    )

    payload = {
        "markets": [
            {
                "id": "m1",
                "ticker": "KXHIGHNYC-26MAR03-T75",
                "event_ticker": "KXHIGHNYC-26MAR03",
                "title": "NYC highest temperature above 75F",
                "status": "open",
                "settlement_time": "2026-03-03T23:00:00Z",
            }
        ]
    }
    context = runtime.StrategyContext(
        forecast_extremes={"NYC": {"2026-03-03": {"max_f": 80.0, "min_f": 60.0}}},
        residual_model=_ResidualModel(),
        thresholds={"global_min_ev_cents": 6.0, "by_city": {"NYC": 6.0}},
    )

    out = runtime.run_strategy_cycle(
        strategy_id,
        now_utc=now,
        market_payload=payload,
        public_client=_FakePublicClient(),
        context=context,
    )
    live_input = runtime.safe_read_json(config.strategy_live_input_path(strategy_id)) or {}

    assert out["signals"]["count"] == 1
    assert live_input["strategy_id"] == strategy_id
    assert isinstance(live_input.get("signals"), list) and len(live_input["signals"]) == 1
    assert "KXHIGHNYC-26MAR03-T75" in dict(live_input.get("quote_map") or {})


def test_run_strategy_cycle_pre_train_blocks_opening_new_paper_entries(monkeypatch, tmp_path):
    _patch_config_paths(monkeypatch, tmp_path)
    strategy_id = "weather_temp_high"
    now = datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc)
    captured: dict[str, object] = {}

    class _ResidualModel:
        def p_exceeds(self, **_kwargs):
            return 0.8

    class _FakePublicClient:
        def get_market_orderbook(self, _ticker: str):
            return {"orderbook": []}

    monkeypatch.setattr(
        runtime,
        "parse_dollar_orderbook",
        lambda _raw, ticker: {
            "ticker": ticker,
            "yes_bid_dollars": 0.49,
            "yes_ask_dollars": 0.50,
            "no_bid_dollars": 0.49,
            "no_ask_dollars": 0.50,
            "yes_bid_size": 25,
            "yes_ask_size": 25,
            "no_bid_size": 25,
            "no_ask_size": 25,
        },
    )
    monkeypatch.setattr(runtime, "_compute_liquidity_state", lambda *_args, **_kwargs: {"qualified": True})
    monkeypatch.setattr(
        runtime,
        "evaluate_train_gate",
        lambda _x: {"pass": False, "reasons": ["NYC: observations 0 < 90"]},
    )

    def _capture_paper_cycle(signals, *_args, **_kwargs):
        captured["signals"] = list(signals)
        captured["allow_new_entries"] = _kwargs.get("allow_new_entries")
        return {"opened": 0, "closed": 0, "open_positions": 0, "equity": 1000.0}

    monkeypatch.setattr(runtime, "run_paper_cycle", _capture_paper_cycle)

    payload = {
        "markets": [
            {
                "id": "m1",
                "ticker": "KXHIGHNYC-26MAR03-T75",
                "event_ticker": "KXHIGHNYC-26MAR03",
                "title": "NYC highest temperature above 75F",
                "status": "open",
                "settlement_time": "2026-03-03T23:00:00Z",
            }
        ]
    }
    context = runtime.StrategyContext(
        forecast_extremes={"NYC": {"2026-03-03": {"max_f": 80.0, "min_f": 60.0}}},
        residual_model=_ResidualModel(),
        thresholds={"global_min_ev_cents": 6.0, "by_city": {"NYC": 6.0}},
    )

    out = runtime.run_strategy_cycle(
        strategy_id,
        now_utc=now,
        market_payload=payload,
        public_client=_FakePublicClient(),
        context=context,
    )
    assert out["signals"]["count"] == 1
    assert out["entry_gate"]["allowed"] is False
    assert "train_gate" in list(out["entry_gate"]["blocked_reasons"])
    assert out["entry_gate"]["checks"]["train_ok"] is False
    assert out["train_gate"]["pass"] is False
    assert captured["signals"] == []
    assert captured["allow_new_entries"] is False


def test_run_strategy_cycle_signal_ev_uses_formula_based_fee(monkeypatch, tmp_path):
    _patch_config_paths(monkeypatch, tmp_path)
    strategy_id = "weather_temp_high"
    now = datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc)

    class _ResidualModel:
        def p_exceeds(self, **_kwargs):
            return 0.2

    class _FakePublicClient:
        def get_market_orderbook(self, _ticker: str):
            return {"orderbook": []}

    monkeypatch.setattr(
        runtime,
        "parse_dollar_orderbook",
        lambda _raw, ticker: {
            "ticker": ticker,
            "yes_bid_dollars": 0.00,
            "yes_ask_dollars": 0.01,
            "no_bid_dollars": 0.98,
            "no_ask_dollars": 0.99,
            "yes_bid_size": 499,
            "yes_ask_size": 499,
            "no_bid_size": 499,
            "no_ask_size": 499,
        },
    )
    monkeypatch.setattr(runtime, "_compute_liquidity_state", lambda *_args, **_kwargs: {"qualified": True})
    monkeypatch.setattr(runtime, "evaluate_train_gate", lambda _x: {"pass": True, "reasons": []})
    monkeypatch.setattr(
        runtime,
        "run_paper_cycle",
        lambda *_args, **_kwargs: {"opened": 0, "closed": 0, "open_positions": 0, "equity": 1000.0},
    )

    payload = {
        "markets": [
            {
                "id": "m1",
                "ticker": "KXHIGHNYC-26MAR03-T75",
                "event_ticker": "KXHIGHNYC-26MAR03",
                "title": "NYC highest temperature above 75F",
                "status": "open",
                "settlement_time": "2026-03-03T23:00:00Z",
            }
        ]
    }
    context = runtime.StrategyContext(
        forecast_extremes={"NYC": {"2026-03-03": {"max_f": 80.0, "min_f": 60.0}}},
        residual_model=_ResidualModel(),
        thresholds={"global_min_ev_cents": 6.0, "by_city": {"NYC": 6.0}},
    )

    out = runtime.run_strategy_cycle(
        strategy_id,
        now_utc=now,
        market_payload=payload,
        public_client=_FakePublicClient(),
        context=context,
    )

    signal_rows = pd.read_parquet(config.strategy_signals_dir(strategy_id) / "signals_2026-03-03.parquet")
    ev_row = signal_rows.to_dict(orient="records")[0]
    expected_ev = compute_ev_cents(
        p_fair=0.2,
        p_market=0.01,
        est_cost_cents=estimated_entry_cost_cents(
            0.01,
            max_position_dollars=config.PAPER_MAX_POSITION_DOLLARS,
            available_contracts=499,
            slippage_cents=config.DEFAULT_SLIPPAGE_CENTS,
        ),
    )

    assert out["signals"]["count"] == 1
    assert round(float(ev_row["ev_cents"]), 6) == round(expected_ev, 6)


def test_run_strategy_cycle_benchmark_artifact_tracks_attempt_and_success(monkeypatch, tmp_path):
    _patch_config_paths(monkeypatch, tmp_path)
    strategy_id = "weather_temp_high"
    now = datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc)

    class _ResidualModel:
        def p_exceeds(self, **_kwargs):
            return 0.8

    class _FakePublicClient:
        def get_market_orderbook(self, _ticker: str):
            return {"orderbook": []}

    monkeypatch.setattr(
        runtime,
        "parse_dollar_orderbook",
        lambda _raw, ticker: {
            "ticker": ticker,
            "yes_bid_dollars": 0.49,
            "yes_ask_dollars": 0.50,
            "no_bid_dollars": 0.49,
            "no_ask_dollars": 0.50,
            "yes_bid_size": 25,
            "yes_ask_size": 25,
            "no_bid_size": 25,
            "no_ask_size": 25,
        },
    )
    monkeypatch.setattr(runtime, "_compute_liquidity_state", lambda *_args, **_kwargs: {"qualified": True})
    monkeypatch.setattr(runtime, "evaluate_train_gate", lambda _x: {"pass": True, "reasons": []})
    monkeypatch.setattr(
        runtime,
        "run_paper_cycle",
        lambda *_args, **_kwargs: {"opened": 0, "closed": 0, "open_positions": 0, "equity": 1000.0},
    )

    payload = {
        "markets": [
            {
                "id": "m1",
                "ticker": "KXHIGHNYC-26MAR03-T75",
                "event_ticker": "KXHIGHNYC-26MAR03",
                "title": "NYC highest temperature above 75F",
                "status": "open",
                "settlement_time": "2026-03-03T23:00:00Z",
            }
        ]
    }
    context = runtime.StrategyContext(
        forecast_extremes={"NYC": {"2026-03-03": {"max_f": 80.0, "min_f": 60.0}}},
        residual_model=_ResidualModel(),
        thresholds={"global_min_ev_cents": 6.0, "by_city": {"NYC": 6.0}},
    )

    runtime.run_strategy_cycle(
        strategy_id,
        now_utc=now,
        market_payload=payload,
        public_client=_FakePublicClient(),
        context=context,
    )
    benchmark = runtime.safe_read_json(config.strategy_benchmark_latest_path(strategy_id)) or {}
    expected_ts = now.isoformat(timespec="seconds")
    assert benchmark.get("available") is True
    assert benchmark.get("updated_at") == expected_ts
    assert benchmark.get("last_attempted_at_utc") == expected_ts
    assert benchmark.get("last_successful_updated_at_utc") == expected_ts


def test_run_strategy_cycle_failure_preserves_last_successful_benchmark_timestamp(monkeypatch, tmp_path):
    _patch_config_paths(monkeypatch, tmp_path)
    strategy_id = "weather_temp_high"
    now = datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc)
    previous_success = "2026-03-02T15:00:00+00:00"

    class _FakePublicClient:
        def get_market_orderbook(self, _ticker: str):
            return {"orderbook": []}

    safe_write_json_atomic(
        config.strategy_benchmark_latest_path(strategy_id),
        {
            "strategy_id": strategy_id,
            "updated_at": previous_success,
            "available": True,
            "source": "legacy_noaa",
        },
    )

    monkeypatch.setattr(
        runtime,
        "parse_dollar_orderbook",
        lambda _raw, ticker: {
            "ticker": ticker,
            "yes_bid_dollars": 0.49,
            "yes_ask_dollars": 0.50,
            "no_bid_dollars": 0.49,
            "no_ask_dollars": 0.50,
            "yes_bid_size": 25,
            "yes_ask_size": 25,
            "no_bid_size": 25,
            "no_ask_size": 25,
        },
    )
    monkeypatch.setattr(runtime, "_compute_liquidity_state", lambda *_args, **_kwargs: {"qualified": True})
    monkeypatch.setattr(runtime, "evaluate_train_gate", lambda _x: {"pass": False, "reasons": ["NYC: observations 0 < 90"]})
    monkeypatch.setattr(runtime, "build_strategy_context", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("noaa down")))
    monkeypatch.setattr(
        runtime,
        "run_paper_cycle",
        lambda *_args, **_kwargs: {"opened": 0, "closed": 0, "open_positions": 0, "equity": 1000.0},
    )

    payload = {
        "markets": [
            {
                "id": "m1",
                "ticker": "KXHIGHNYC-26MAR03-T75",
                "event_ticker": "KXHIGHNYC-26MAR03",
                "title": "NYC highest temperature above 75F",
                "status": "open",
                "settlement_time": "2026-03-03T23:00:00Z",
            }
        ]
    }

    runtime.run_strategy_cycle(
        strategy_id,
        now_utc=now,
        market_payload=payload,
        public_client=_FakePublicClient(),
        context=None,
    )
    benchmark = runtime.safe_read_json(config.strategy_benchmark_latest_path(strategy_id)) or {}
    expected_ts = now.isoformat(timespec="seconds")
    assert benchmark.get("available") is False
    assert benchmark.get("updated_at") == expected_ts
    assert benchmark.get("last_attempted_at_utc") == expected_ts
    assert benchmark.get("last_successful_updated_at_utc") == previous_success


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


def test_portfolio_leaderboard_does_not_promote_discovery_only_over_tradable(monkeypatch, tmp_path):
    _patch_config_paths(monkeypatch, tmp_path)

    safe_write_json_atomic(
        config.strategy_paper_metrics_daily_path("weather_temp_high"),
        {"by_day": {"2026-03-01": {"trades": 2, "wins": 1, "pnl_dollars": -1.0, "roi_per_trade": -0.01}}},
    )
    safe_write_json_atomic(
        config.strategy_paper_metrics_daily_path("weather_temp_low"),
        {"by_day": {"2026-03-01": {"trades": 2, "wins": 0, "pnl_dollars": -2.0, "roi_per_trade": -0.02}}},
    )
    safe_write_json_atomic(
        config.strategy_paper_metrics_daily_path("weather_temp_bucket"),
        {"by_day": {"2026-03-01": {"trades": 2, "wins": 0, "pnl_dollars": -3.0, "roi_per_trade": -0.03}}},
    )

    board = runtime.compute_portfolio_leaderboard(datetime(2026, 3, 3, tzinfo=timezone.utc))
    rows = list(board.get("rows", []))
    assert rows
    assert str(rows[0].get("mode")) == "tradable"

    for row in rows:
        if str(row.get("mode")) != "tradable":
            assert float(row.get("score", 0.0) or 0.0) == 0.0


def test_leaderboard_uses_cycle_data_health_freshness(monkeypatch, tmp_path):
    _patch_config_paths(monkeypatch, tmp_path)
    strategy_id = "weather_temp_high"
    safe_write_json_atomic(
        config.strategy_paper_metrics_daily_path(strategy_id),
        {
            "by_day": {
                "2026-03-01": {
                    "trades": 2,
                    "wins": 1,
                    "pnl_dollars": 1.0,
                    "roi_per_trade": 0.01,
                }
            }
        },
    )
    safe_write_json_atomic(
        config.strategy_runtime_gates_path(strategy_id),
        {
            "eligible_for_challenger": True,
            "data_health": {"green": True},
            "ts_utc": "2026-03-01T00:00:00+00:00",
        },
    )
    safe_write_json_atomic(
        config.strategy_runtime_cycle_path(strategy_id),
        {
            "ts_utc": "2026-03-03T12:15:00+00:00",
            "entry_gate": {
                "checks": {
                    "parse_ok": True,
                    "eligible_ok": True,
                    "freshness_ok": False,
                    "liquidity_ok": True,
                    "benchmark_ok": True,
                }
            },
        },
    )

    row = runtime._leaderboard_row(strategy_id)
    assert row["eligible"] is True
    assert row["data_health"] == 0.0
    assert row["data_health_green"] is False
    assert row["data_health_source"] == "cycle_entry_checks"
    assert row["data_health_source_ts_utc"] == "2026-03-03T12:15:00+00:00"


def test_variant_alerts_include_liquidity_block_details():
    alerts = runtime._variant_alerts(
        strategy_id="weather_temp_high",
        contract_quality={
            "parse_rate": 1.0,
            "parse_alert_sample_count": 30,
            "eligible_count": 5,
        },
        freshness={"stale": {"quotes": False, "signals": False, "benchmark": False}},
        liquidity={
            "qualified": False,
            "last_window": {
                "median_spread": 1.24,
                "median_spread_pct": 0.62,
                "median_spread_abs_dollars": 0.01,
                "median_depth": 3.0,
                "thresholds": {"max_spread": 1.0, "max_spread_pct": 0.15, "max_spread_abs_dollars": 0.01, "min_depth": 10},
            },
        },
        train_gate_pass=True,
        train_gate_reasons=[],
        benchmark_available=True,
    )

    codes = {str(a.get("code")) for a in alerts}
    assert "liquidity_blocked_weather_temp_high" in codes
    msg = next(str(a.get("message")) for a in alerts if str(a.get("code")) == "liquidity_blocked_weather_temp_high")
    assert "hybrid_spread=1.240 > 1.000 (fail)" in msg
    assert "abs=$0.010 vs max($0.010, 0.150*mid)" in msg
    assert "pct=0.620" in msg
    assert "depth=3.0 < 10.0 (fail)" in msg


def test_variant_alerts_benchmark_unavailable_downgraded_during_train_warmup():
    alerts = runtime._variant_alerts(
        strategy_id="weather_temp_high",
        contract_quality={
            "parse_rate": 1.0,
            "parse_alert_sample_count": 30,
            "eligible_count": 5,
        },
        freshness={"stale": {"quotes": False, "signals": False, "benchmark": False}},
        liquidity={"qualified": True, "last_window": {}},
        train_gate_pass=False,
        train_gate_reasons=["NYC: observations 10 < 90"],
        benchmark_available=False,
    )
    match = next(a for a in alerts if str(a.get("code")) == "stale_benchmark_weather_temp_high")
    assert match["severity"] == "warn"
    assert "train warmup" in str(match.get("message", ""))


def test_variant_alerts_benchmark_unavailable_critical_after_train_pass():
    alerts = runtime._variant_alerts(
        strategy_id="weather_temp_high",
        contract_quality={
            "parse_rate": 1.0,
            "parse_alert_sample_count": 30,
            "eligible_count": 5,
        },
        freshness={"stale": {"quotes": False, "signals": False, "benchmark": False}},
        liquidity={"qualified": True, "last_window": {}},
        train_gate_pass=True,
        train_gate_reasons=[],
        benchmark_available=False,
    )
    match = next(a for a in alerts if str(a.get("code")) == "stale_benchmark_weather_temp_high")
    assert match["severity"] == "critical"


def test_variant_alerts_benchmark_unavailable_emits_single_alert_when_stale():
    alerts = runtime._variant_alerts(
        strategy_id="weather_temp_high",
        contract_quality={
            "parse_rate": 1.0,
            "parse_alert_sample_count": 30,
            "eligible_count": 5,
        },
        freshness={"stale": {"quotes": False, "signals": False, "benchmark": True}},
        liquidity={"qualified": True, "last_window": {}},
        train_gate_pass=False,
        train_gate_reasons=["NYC: observations 10 < 90"],
        benchmark_available=False,
    )
    matches = [a for a in alerts if str(a.get("code")) == "stale_benchmark_weather_temp_high"]
    assert len(matches) == 1
    assert "train warmup" in str(matches[0].get("message", ""))


def test_strategies_summary_benchmark_fresh_requires_availability(monkeypatch, tmp_path):
    _patch_config_paths(monkeypatch, tmp_path)
    strategy_id = "weather_temp_high"
    safe_write_json_atomic(
        config.strategy_runtime_cycle_path(strategy_id),
        {
            "entry_gate": {"allowed": False, "blocked_reasons": ["benchmark"]},
            "freshness": {"stale": {"benchmark": False}},
            "benchmark": {"available": False},
            "alerts": [],
            "ts_utc": "2026-03-03T12:00:00+00:00",
        },
    )
    safe_write_json_atomic(config.strategy_runtime_gates_path(strategy_id), {"train": {"pass": False}, "paper": {"pass": False}})

    out = runtime.strategies_summary_snapshot()
    rows = {str(r.get("strategy_id")): r for r in list(out.get("rows", []))}
    row = rows[strategy_id]
    assert row["benchmark_available"] is False
    assert row["benchmark_data_fresh"] is True
    assert row["benchmark_stream_fresh"] is True
    assert row["benchmark_fresh"] is False


def test_strategies_summary_benchmark_field_matrix(monkeypatch, tmp_path):
    _patch_config_paths(monkeypatch, tmp_path)

    fixtures = [
        (
            "weather_temp_high",
            {
                "freshness": {"stale": {"benchmark": False, "benchmark_attempt": False}},
                "benchmark": {"available": True, "reason": None},
            },
            {"benchmark_data_fresh": True, "benchmark_stream_fresh": True, "benchmark_fresh": True},
        ),
        (
            "weather_temp_low",
            {
                "freshness": {"stale": {"benchmark": False, "benchmark_attempt": False}},
                "benchmark": {"available": False, "reason": "fetch_failed"},
            },
            {"benchmark_data_fresh": True, "benchmark_stream_fresh": True, "benchmark_fresh": False},
        ),
        (
            "weather_temp_bucket",
            {
                "freshness": {"stale": {"benchmark": True, "benchmark_attempt": False}},
                "benchmark": {"available": True, "reason": None},
            },
            {"benchmark_data_fresh": False, "benchmark_stream_fresh": True, "benchmark_fresh": False},
        ),
    ]

    for strategy_id, cycle_payload, _expected in fixtures:
        safe_write_json_atomic(
            config.strategy_runtime_cycle_path(strategy_id),
            {
                "entry_gate": {"allowed": False, "blocked_reasons": ["benchmark"]},
                "alerts": [],
                "ts_utc": "2026-03-03T12:00:00+00:00",
                **cycle_payload,
            },
        )
        safe_write_json_atomic(config.strategy_runtime_gates_path(strategy_id), {"train": {"pass": False}, "paper": {"pass": False}})

    out = runtime.strategies_summary_snapshot()
    rows = {str(r.get("strategy_id")): r for r in list(out.get("rows", []))}

    for strategy_id, _payload, expected in fixtures:
        row = rows[strategy_id]
        assert row["benchmark_data_fresh"] is expected["benchmark_data_fresh"]
        assert row["benchmark_stream_fresh"] is expected["benchmark_stream_fresh"]
        assert row["benchmark_fresh"] is expected["benchmark_fresh"]
