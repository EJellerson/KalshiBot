from __future__ import annotations

from datetime import datetime, timedelta, timezone

from weather_arb.execution.live_engine import run_live_cycle
from weather_arb.execution.paper_engine import run_paper_cycle
from weather_arb.utils.io_utils import safe_read_json, safe_write_json_atomic


class _LiveAuth:
    def __init__(self) -> None:
        self.orders: list[dict[str, object]] = []

    def get_positions(self):
        return {"portfolio": {"equity_dollars": 100.0}}

    def place_order(self, **kwargs):
        self.orders.append(dict(kwargs))
        return {"status": "executed", "order": {"status": "executed"}}


def _quote(now: datetime) -> dict[str, dict[str, object]]:
    return {
        "KXHIGHNYC-26MAR03-T75": {
            "ticker": "KXHIGHNYC-26MAR03-T75",
            "ts_utc": now.isoformat(),
            "yes_bid_dollars": 0.45,
            "yes_ask_dollars": 0.46,
            "no_bid_dollars": 0.54,
            "no_ask_dollars": 0.55,
            "yes_bid_size": 20,
            "yes_ask_size": 20,
            "no_bid_size": 20,
            "no_ask_size": 20,
        }
    }


def _paper_open_position(now: datetime) -> dict[str, object]:
    return {
        "position_id": "paper_1",
        "ticker": "KXHIGHNYC-26MAR03-T75",
        "city": "NYC",
        "side": "buy_yes",
        "contracts": 10,
        "entry_price_dollars": 0.50,
        "entry_fees_dollars": 0.20,
        "opened_at_utc": (now - timedelta(hours=3)).isoformat(),
        "max_hold_until_utc": (now + timedelta(hours=3)).isoformat(),
        "settlement_ts_utc": (now + timedelta(hours=10)).isoformat(),
        "status": "open",
    }


def _live_open_position(now: datetime) -> dict[str, object]:
    return {
        "position_id": "live_1",
        "ticker": "KXHIGHNYC-26MAR03-T75",
        "city": "NYC",
        "side": "buy_yes",
        "contracts": 10,
        "entry_price_dollars": 0.50,
        "entry_fees_dollars": 0.20,
        "opened_at_utc": (now - timedelta(hours=3)).isoformat(),
        "max_hold_until_utc": (now + timedelta(hours=3)).isoformat(),
        "settlement_ts_utc": (now + timedelta(hours=10)).isoformat(),
        "status": "open",
        "entry_status": "confirmed",
    }


def test_paper_exit_on_negative_ev(tmp_path):
    now = datetime(2026, 3, 3, 16, 0, tzinfo=timezone.utc)
    state_path = tmp_path / "paper.json"
    safe_write_json_atomic(
        state_path,
        {
            "equity": 1000.0,
            "cash": 1000.0,
            "open_positions": [_paper_open_position(now)],
            "closed_positions": [],
            "daily_pnl": {},
            "weekly_pnl": {},
            "consecutive_losses": 0,
            "next_position_id": 2,
        },
    )

    out = run_paper_cycle(
        [
            {
                "ticker": "KXHIGHNYC-26MAR03-T75",
                "side": "buy_yes",
                "ev_cents": -5.0,
                "min_ev_cents": 6.0,
                "settlement_ts_utc": (now + timedelta(hours=10)).isoformat(),
            }
        ],
        _quote(now),
        now,
        state_path=state_path,
        blotter_dir=tmp_path / "blotter",
    )

    assert out["closed"] == 1


def test_paper_ev_sign_preserved_negative_exits_positive_holds(tmp_path):
    now = datetime(2026, 3, 3, 16, 0, tzinfo=timezone.utc)
    state_path = tmp_path / "paper.json"
    safe_write_json_atomic(
        state_path,
        {
            "equity": 1000.0,
            "cash": 1000.0,
            "open_positions": [_paper_open_position(now)],
            "closed_positions": [],
            "daily_pnl": {},
            "weekly_pnl": {},
            "consecutive_losses": 0,
            "next_position_id": 2,
        },
    )

    hold = run_paper_cycle(
        [
            {
                "ticker": "KXHIGHNYC-26MAR03-T75",
                "side": "buy_yes",
                "ev_cents": 5.0,
                "min_ev_cents": 6.0,
                "settlement_ts_utc": (now + timedelta(hours=10)).isoformat(),
            }
        ],
        _quote(now),
        now,
        state_path=state_path,
        blotter_dir=tmp_path / "blotter",
    )
    assert hold["closed"] == 0

    exit_zero = run_paper_cycle(
        [
            {
                "ticker": "KXHIGHNYC-26MAR03-T75",
                "side": "buy_yes",
                "ev_cents": 0.0,
                "min_ev_cents": 6.0,
                "settlement_ts_utc": (now + timedelta(hours=10)).isoformat(),
            }
        ],
        _quote(now),
        now + timedelta(minutes=1),
        state_path=state_path,
        blotter_dir=tmp_path / "blotter",
    )
    assert exit_zero["closed"] == 1


def test_live_exit_on_negative_ev(monkeypatch, tmp_path):
    monkeypatch.setattr("weather_arb.config.LIVE_EQUITY_SYNC_ENABLED", False)
    now = datetime(2026, 3, 3, 16, 0, tzinfo=timezone.utc)
    state_path = tmp_path / "live.json"
    safe_write_json_atomic(
        state_path,
        {
            "equity": 100.0,
            "open_positions": [_live_open_position(now)],
            "closed_positions": [],
            "daily_pnl": {},
            "weekly_pnl": {},
            "consecutive_losses": 0,
            "next_position_id": 2,
            "last_limits_day": "",
            "live_limits": {},
        },
    )
    auth = _LiveAuth()
    out = run_live_cycle(
        [
            {
                "ticker": "KXHIGHNYC-26MAR03-T75",
                "side": "buy_yes",
                "ev_cents": -7.0,
                "min_ev_cents": 6.0,
                "settlement_ts_utc": (now + timedelta(hours=10)).isoformat(),
            }
        ],
        _quote(now),
        now,
        auth_client=auth,
        state_path=state_path,
        blotter_dir=tmp_path / "blotter",
        live_routing_enabled=True,
    )

    state = safe_read_json(state_path) or {}
    assert out["closed"] == 1
    assert len(auth.orders) == 1
    assert len(state.get("closed_positions", [])) == 1


def test_orphan_position_exits_after_threshold_with_z_timestamp(tmp_path):
    now = datetime(2026, 3, 3, 16, 0, tzinfo=timezone.utc)
    state_path = tmp_path / "paper.json"
    stale_opened_at = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    position = _paper_open_position(now)
    position["opened_at_utc"] = stale_opened_at
    safe_write_json_atomic(
        state_path,
        {
            "equity": 1000.0,
            "cash": 1000.0,
            "open_positions": [position],
            "closed_positions": [],
            "daily_pnl": {},
            "weekly_pnl": {},
            "consecutive_losses": 0,
            "next_position_id": 2,
        },
    )

    out = run_paper_cycle([], _quote(now), now, state_path=state_path, blotter_dir=tmp_path / "blotter")
    assert out["closed"] == 1


def test_live_orphan_position_exits_after_threshold(monkeypatch, tmp_path):
    monkeypatch.setattr("weather_arb.config.LIVE_EQUITY_SYNC_ENABLED", False)
    now = datetime(2026, 3, 3, 16, 0, tzinfo=timezone.utc)
    state_path = tmp_path / "live.json"
    orphan = _live_open_position(now)
    orphan["opened_at_utc"] = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    safe_write_json_atomic(
        state_path,
        {
            "equity": 100.0,
            "open_positions": [orphan],
            "closed_positions": [],
            "daily_pnl": {},
            "weekly_pnl": {},
            "consecutive_losses": 0,
            "next_position_id": 2,
            "last_limits_day": "",
            "live_limits": {},
        },
    )
    auth = _LiveAuth()
    out = run_live_cycle(
        [],
        _quote(now),
        now,
        auth_client=auth,
        state_path=state_path,
        blotter_dir=tmp_path / "blotter",
        live_routing_enabled=True,
    )
    assert out["closed"] == 1
    assert len(auth.orders) == 1
