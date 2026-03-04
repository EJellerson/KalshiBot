from __future__ import annotations

from datetime import datetime, timedelta, timezone

from weather_arb.execution.live_engine import _to_quote, run_live_cycle
from weather_arb.utils.io_utils import safe_read_json, safe_write_json_atomic


class _FakeAuthClient:
    def __init__(self):
        self.orders: list[dict[str, object]] = []
        self.get_positions_calls = 0

    def get_positions(self):
        self.get_positions_calls += 1
        return {"portfolio": {"equity_dollars": 250.0}}

    def place_order(self, **kwargs):
        self.orders.append(dict(kwargs))
        return {"ok": True, "order_id": "live_test", "request": kwargs}


class _ReconcileAuthClient:
    def __init__(self, positions_payload: dict[str, object]):
        self.positions_payload = positions_payload
        self.get_positions_calls = 0
        self.orders: list[dict[str, object]] = []

    def get_positions(self):
        self.get_positions_calls += 1
        return self.positions_payload

    def place_order(self, **kwargs):
        self.orders.append(dict(kwargs))
        return {"ok": True, "order_id": "live_test", "request": kwargs}


class _SequencedAuthClient:
    def __init__(self, sequence: list[str]):
        self.sequence = list(sequence)
        self.orders: list[dict[str, object]] = []
        self.get_positions_calls = 0

    def get_positions(self):
        self.get_positions_calls += 1
        return {"portfolio": {"equity_dollars": 250.0}}

    def place_order(self, **kwargs):
        self.orders.append(dict(kwargs))
        idx = len(self.orders) - 1
        action = self.sequence[idx] if idx < len(self.sequence) else "ok"
        if action == "raise":
            raise RuntimeError("simulated submit failure")
        if action == "filled":
            return {"status": "executed", "ok": True}
        return {"ok": True, "order_id": "live_test", "request": kwargs}


def _sample_signal(now: datetime) -> list[dict[str, object]]:
    return [
        {
            "ticker": "KXHIGHNYC-26MAR03-T75",
            "city": "NYC",
            "side": "buy_yes",
            "ev_cents": 12.0,
            "min_ev_cents": 6.0,
            "settlement_ts_utc": (now + timedelta(hours=20)).isoformat(),
        }
    ]


def _sample_quotes(now: datetime) -> dict[str, dict[str, object]]:
    return {
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
    }


def _sample_signal_second(now: datetime) -> dict[str, object]:
    return {
        "ticker": "KXHIGHCHI-26MAR03-T68",
        "city": "CHI",
        "side": "buy_yes",
        "ev_cents": 11.0,
        "min_ev_cents": 6.0,
        "settlement_ts_utc": (now + timedelta(hours=18)).isoformat(),
    }


def _sample_quotes_with_second(now: datetime) -> dict[str, dict[str, object]]:
    out = dict(_sample_quotes(now))
    out["KXHIGHCHI-26MAR03-T68"] = {
        "ticker": "KXHIGHCHI-26MAR03-T68",
        "ts_utc": now.isoformat(),
        "yes_bid_dollars": 0.44,
        "yes_ask_dollars": 0.45,
        "no_bid_dollars": 0.54,
        "no_ask_dollars": 0.55,
        "yes_bid_size": 25,
        "yes_ask_size": 25,
        "no_bid_size": 25,
        "no_ask_size": 25,
    }
    return out


def test_run_live_cycle_fail_closed_when_live_routing_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr("weather_arb.config.LIVE_EQUITY_SYNC_ENABLED", False)

    now = datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc)
    state_path = tmp_path / "live_positions.json"
    blotter_dir = tmp_path / "blotter"

    out = run_live_cycle(
        _sample_signal(now),
        _sample_quotes(now),
        now,
        auth_client=None,
        state_path=state_path,
        blotter_dir=blotter_dir,
        live_routing_enabled=False,
    )

    state = safe_read_json(state_path) or {}

    assert out["opened"] == 0
    assert out["blocked"] is True
    assert out["reason"] == "live_routing_disabled"
    assert state.get("open_positions", []) == []


def test_run_live_cycle_routing_disabled_keeps_open_positions(monkeypatch, tmp_path):
    monkeypatch.setattr("weather_arb.config.LIVE_EQUITY_SYNC_ENABLED", False)
    now = datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc)
    state_path = tmp_path / "live_positions.json"
    blotter_dir = tmp_path / "blotter"
    safe_write_json_atomic(
        state_path,
        {
            "equity": 250.0,
            "open_positions": [
                {
                    "position_id": "live_1",
                    "ticker": "KXHIGHNYC-26MAR03-T75",
                    "city": "NYC",
                    "side": "buy_yes",
                    "contracts": 5,
                    "entry_price_dollars": 0.50,
                    "entry_fees_dollars": 0.10,
                    "opened_at_utc": (now - timedelta(hours=2)).isoformat(),
                    "max_hold_until_utc": (now + timedelta(hours=1)).isoformat(),
                    "settlement_ts_utc": (now + timedelta(hours=8)).isoformat(),
                    "status": "open",
                }
            ],
            "closed_positions": [],
            "daily_pnl": {},
            "weekly_pnl": {},
            "consecutive_losses": 0,
            "next_position_id": 2,
            "last_limits_day": "",
            "live_limits": {},
        },
    )

    out = run_live_cycle(
        [
            {
                "ticker": "KXHIGHNYC-26MAR03-T75",
                "side": "buy_yes",
                "ev_cents": 0.0,
                "min_ev_cents": 6.0,
                "settlement_ts_utc": (now + timedelta(hours=8)).isoformat(),
            }
        ],
        _sample_quotes(now),
        now,
        auth_client=None,
        state_path=state_path,
        blotter_dir=blotter_dir,
        live_routing_enabled=False,
    )

    state = safe_read_json(state_path) or {}
    assert out["blocked"] is True
    assert out["reason"] == "live_routing_disabled"
    assert out["closed"] == 0
    assert len(state.get("open_positions", [])) == 1
    assert len(out["exit_orders"]) == 1
    assert out["exit_orders"][0]["error"] == "live_routing_disabled"


def test_run_live_cycle_places_order_when_live_routing_enabled(monkeypatch, tmp_path):
    monkeypatch.setattr("weather_arb.config.LIVE_EQUITY_SYNC_ENABLED", False)

    now = datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc)
    state_path = tmp_path / "live_positions.json"
    blotter_dir = tmp_path / "blotter"

    out = run_live_cycle(
        _sample_signal(now),
        _sample_quotes(now),
        now,
        auth_client=_FakeAuthClient(),
        state_path=state_path,
        blotter_dir=blotter_dir,
        live_routing_enabled=True,
    )

    state = safe_read_json(state_path) or {}
    open_positions = list(state.get("open_positions", []))

    assert out["opened"] == 1
    assert len(open_positions) == 1
    assert bool(open_positions[0].get("live_order_submitted", False)) is True


def test_run_live_cycle_entry_payload_has_explicit_gtc(monkeypatch, tmp_path):
    monkeypatch.setattr("weather_arb.config.LIVE_EQUITY_SYNC_ENABLED", False)
    now = datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc)
    state_path = tmp_path / "live_positions.json"
    blotter_dir = tmp_path / "blotter"
    auth = _FakeAuthClient()

    run_live_cycle(
        _sample_signal(now),
        _sample_quotes(now),
        now,
        auth_client=auth,
        state_path=state_path,
        blotter_dir=blotter_dir,
        live_routing_enabled=True,
    )

    assert len(auth.orders) == 1
    assert auth.orders[0]["time_in_force"] == "gtc"


def test_live_quote_fallback_is_timezone_aware():
    quote = _to_quote(
        {
            "ticker": "TEST",
            "yes_bid_dollars": 0.49,
            "yes_ask_dollars": 0.50,
            "no_bid_dollars": 0.49,
            "no_ask_dollars": 0.50,
            "yes_bid_size": 10,
            "yes_ask_size": 10,
            "no_bid_size": 10,
            "no_ask_size": 10,
        }
    )
    assert quote.ts_utc.tzinfo is not None


def test_live_entry_capped_by_depth(monkeypatch, tmp_path):
    monkeypatch.setattr("weather_arb.config.LIVE_EQUITY_SYNC_ENABLED", False)
    auth = _FakeAuthClient()
    now = datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc)
    state_path = tmp_path / "live_positions.json"
    blotter_dir = tmp_path / "blotter"
    out = run_live_cycle(
        _sample_signal(now),
        {
            "KXHIGHNYC-26MAR03-T75": {
                "ticker": "KXHIGHNYC-26MAR03-T75",
                "ts_utc": now.isoformat(),
                "yes_bid_dollars": 0.49,
                "yes_ask_dollars": 0.50,
                "no_bid_dollars": 0.49,
                "no_ask_dollars": 0.50,
                "yes_bid_size": 6,
                "yes_ask_size": 6,
                "no_bid_size": 6,
                "no_ask_size": 6,
            }
        },
        now,
        auth_client=auth,
        state_path=state_path,
        blotter_dir=blotter_dir,
        live_routing_enabled=True,
    )
    state = safe_read_json(state_path) or {}
    open_pos = list(state.get("open_positions", []))[0]
    assert out["opened"] == 1
    assert out["capped_entries"] == 1
    assert int(open_pos["requested_contracts"]) == 10
    assert int(open_pos["contracts"]) == 6
    assert len(auth.orders) == 1
    assert int(auth.orders[0]["count"]) == 6


def _pending_live_position(now: datetime) -> dict[str, object]:
    return {
        "position_id": "live_1",
        "ticker": "KXHIGHNYC-26MAR03-T75",
        "city": "NYC",
        "side": "buy_yes",
        "contracts": 5,
        "entry_price_dollars": 0.50,
        "entry_fees_dollars": 0.10,
        "opened_at_utc": (now - timedelta(minutes=30)).isoformat(),
        "max_hold_until_utc": (now + timedelta(hours=2)).isoformat(),
        "settlement_ts_utc": (now + timedelta(hours=8)).isoformat(),
        "status": "open",
        "entry_status": "pending_confirmation",
        "entry_submitted_at_utc": (now - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "entry_last_checked_at_utc": (now - timedelta(minutes=20)).isoformat(),
        "entry_reconcile_notes": "awaiting_broker_confirmation",
    }


def test_live_optimistic_entry_starts_pending(monkeypatch, tmp_path):
    monkeypatch.setattr("weather_arb.config.LIVE_EQUITY_SYNC_ENABLED", False)
    auth = _FakeAuthClient()
    now = datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc)
    state_path = tmp_path / "live_positions.json"
    blotter_dir = tmp_path / "blotter"

    out = run_live_cycle(
        _sample_signal(now),
        _sample_quotes(now),
        now,
        auth_client=auth,
        state_path=state_path,
        blotter_dir=blotter_dir,
        live_routing_enabled=True,
    )
    state = safe_read_json(state_path) or {}
    open_pos = list(state.get("open_positions", []))[0]
    assert out["opened"] == 1
    assert open_pos["entry_status"] == "pending_confirmation"
    assert auth.get_positions_calls == 0


def test_live_reconciliation_confirms_after_grace(monkeypatch, tmp_path):
    monkeypatch.setattr("weather_arb.config.LIVE_EQUITY_SYNC_ENABLED", False)
    auth = _ReconcileAuthClient({"positions": [{"ticker": "KXHIGHNYC-26MAR03-T75"}]})
    now = datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc)
    state_path = tmp_path / "live_positions.json"
    blotter_dir = tmp_path / "blotter"
    safe_write_json_atomic(
        state_path,
        {
            "equity": 250.0,
            "open_positions": [_pending_live_position(now)],
            "closed_positions": [],
            "daily_pnl": {},
            "weekly_pnl": {},
            "consecutive_losses": 0,
            "next_position_id": 2,
            "last_limits_day": "",
            "live_limits": {},
        },
    )

    out = run_live_cycle(
        [],
        _sample_quotes(now),
        now,
        auth_client=auth,
        state_path=state_path,
        blotter_dir=blotter_dir,
        live_routing_enabled=True,
    )
    state = safe_read_json(state_path) or {}
    pos = list(state.get("open_positions", []))[0]
    assert pos["entry_status"] == "confirmed"
    assert out["confirmed"] >= 1
    assert auth.get_positions_calls == 1


def test_live_reconciliation_marks_unconfirmed_and_blocks_duplicates(monkeypatch, tmp_path):
    monkeypatch.setattr("weather_arb.config.LIVE_EQUITY_SYNC_ENABLED", False)
    auth = _ReconcileAuthClient({"positions": []})
    now = datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc)
    state_path = tmp_path / "live_positions.json"
    blotter_dir = tmp_path / "blotter"
    safe_write_json_atomic(
        state_path,
        {
            "equity": 250.0,
            "open_positions": [_pending_live_position(now)],
            "closed_positions": [],
            "daily_pnl": {},
            "weekly_pnl": {},
            "consecutive_losses": 0,
            "next_position_id": 2,
            "last_limits_day": "",
            "live_limits": {},
        },
    )

    out = run_live_cycle(
        _sample_signal(now),
        _sample_quotes(now),
        now,
        auth_client=auth,
        state_path=state_path,
        blotter_dir=blotter_dir,
        live_routing_enabled=True,
    )
    state = safe_read_json(state_path) or {}
    pos = list(state.get("open_positions", []))[0]
    assert pos["entry_status"] == "unconfirmed"
    assert out["opened"] == 0
    assert out["unconfirmed"] >= 1
    assert len(auth.orders) == 0
    assert auth.get_positions_calls == 1


def test_live_entry_submit_error_assumes_sent_and_halts_new_entries(monkeypatch, tmp_path):
    monkeypatch.setattr("weather_arb.config.LIVE_EQUITY_SYNC_ENABLED", False)
    now = datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc)
    state_path = tmp_path / "live_positions.json"
    blotter_dir = tmp_path / "blotter"
    auth = _SequencedAuthClient(["raise"])
    signals = _sample_signal(now) + [_sample_signal_second(now)]
    quotes = _sample_quotes_with_second(now)

    out = run_live_cycle(
        signals,
        quotes,
        now,
        auth_client=auth,
        state_path=state_path,
        blotter_dir=blotter_dir,
        live_routing_enabled=True,
    )

    state = safe_read_json(state_path) or {}
    open_positions = list(state.get("open_positions", []))
    assert out["opened"] == 1
    assert len(auth.orders) == 1
    assert len(open_positions) == 1
    assert open_positions[0]["ticker"] == "KXHIGHNYC-26MAR03-T75"
    assert bool(open_positions[0].get("entry_submission_uncertain", False)) is True
    assert str(open_positions[0].get("live_order_submit_error", "")).strip() != ""
    assert any(str(row.get("code")) == "live_entry_submit_error" for row in out.get("alerts", []))


def test_live_exit_persisted_when_followed_by_entry_submit_error(monkeypatch, tmp_path):
    monkeypatch.setattr("weather_arb.config.LIVE_EQUITY_SYNC_ENABLED", False)
    now = datetime(2026, 3, 3, 16, 0, tzinfo=timezone.utc)
    state_path = tmp_path / "live_positions.json"
    blotter_dir = tmp_path / "blotter"
    auth = _SequencedAuthClient(["filled", "raise"])
    safe_write_json_atomic(
        state_path,
        {
            "equity": 250.0,
            "open_positions": [
                {
                    "position_id": "live_1",
                    "ticker": "KXHIGHNYC-26MAR03-T75",
                    "city": "NYC",
                    "side": "buy_yes",
                    "contracts": 5,
                    "entry_price_dollars": 0.50,
                    "entry_fees_dollars": 0.10,
                    "opened_at_utc": (now - timedelta(hours=2)).isoformat(),
                    "max_hold_until_utc": (now + timedelta(hours=1)).isoformat(),
                    "settlement_ts_utc": (now + timedelta(hours=8)).isoformat(),
                    "status": "open",
                }
            ],
            "closed_positions": [],
            "daily_pnl": {},
            "weekly_pnl": {},
            "consecutive_losses": 0,
            "next_position_id": 2,
            "last_limits_day": "",
            "live_limits": {},
        },
    )

    out = run_live_cycle(
        [
            {
                "ticker": "KXHIGHNYC-26MAR03-T75",
                "side": "buy_yes",
                "ev_cents": 0.0,
                "min_ev_cents": 6.0,
                "settlement_ts_utc": (now + timedelta(hours=8)).isoformat(),
            },
            _sample_signal_second(now),
        ],
        _sample_quotes_with_second(now),
        now,
        auth_client=auth,
        state_path=state_path,
        blotter_dir=blotter_dir,
        live_routing_enabled=True,
    )

    state = safe_read_json(state_path) or {}
    assert out["closed"] == 1
    assert out["opened"] == 1
    assert len(auth.orders) == 2
    assert len(state.get("closed_positions", [])) == 1
    assert len(state.get("open_positions", [])) == 1
    assert state["open_positions"][0]["ticker"] == "KXHIGHCHI-26MAR03-T68"
    assert bool(state["open_positions"][0].get("entry_submission_uncertain", False)) is True
