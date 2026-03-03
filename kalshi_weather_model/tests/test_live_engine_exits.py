from __future__ import annotations

from datetime import datetime, timedelta, timezone

from weather_arb.execution.live_engine import run_live_cycle
from weather_arb.utils.io_utils import safe_read_json, safe_write_json_atomic


class _ExitAuthClient:
    def __init__(self, *, fill_status: str = "executed"):
        self.fill_status = fill_status
        self.orders: list[dict[str, object]] = []

    def get_positions(self):
        return {"portfolio": {"equity_dollars": 50.0}}

    def place_order(self, **kwargs):
        self.orders.append(dict(kwargs))
        return {"status": self.fill_status, "order": {"status": self.fill_status}, "ok": True}


def _quote(now: datetime) -> dict[str, dict[str, object]]:
    return {
        "KXHIGHNYC-26MAR03-T75": {
            "ticker": "KXHIGHNYC-26MAR03-T75",
            "ts_utc": now.isoformat(),
            "yes_bid_dollars": 0.45,
            "yes_ask_dollars": 0.46,
            "no_bid_dollars": 0.54,
            "no_ask_dollars": 0.55,
            "yes_bid_size": 50,
            "yes_ask_size": 50,
            "no_bid_size": 50,
            "no_ask_size": 50,
        }
    }


def _open_position(now: datetime) -> dict[str, object]:
    return {
        "position_id": "live_1",
        "ticker": "KXHIGHNYC-26MAR03-T75",
        "city": "NYC",
        "side": "buy_yes",
        "contracts": 10,
        "entry_price_dollars": 0.50,
        "entry_fees_dollars": 0.20,
        "opened_at_utc": (now - timedelta(hours=2)).isoformat(),
        "max_hold_until_utc": (now + timedelta(hours=1)).isoformat(),
        "settlement_ts_utc": (now + timedelta(hours=8)).isoformat(),
        "status": "open",
    }


def test_live_exit_on_ev_drop_places_sell_reduce_order(monkeypatch, tmp_path):
    monkeypatch.setattr("weather_arb.config.LIVE_EQUITY_SYNC_ENABLED", False)
    now = datetime(2026, 3, 3, 16, 0, tzinfo=timezone.utc)
    state_path = tmp_path / "live_positions.json"
    blotter_dir = tmp_path / "blotter"
    auth = _ExitAuthClient(fill_status="executed")

    safe_write_json_atomic(
        state_path,
        {
            "equity": 50.0,
            "open_positions": [_open_position(now)],
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
                "ev_cents": 1.0,  # below EXIT_EV_CENTS
                "min_ev_cents": 6.0,
                "settlement_ts_utc": (now + timedelta(hours=8)).isoformat(),
            }
        ],
        _quote(now),
        now,
        auth_client=auth,
        state_path=state_path,
        blotter_dir=blotter_dir,
        live_routing_enabled=True,
    )

    state = safe_read_json(state_path) or {}
    assert out["closed"] == 1
    assert len(auth.orders) == 1
    order = auth.orders[0]
    assert order["action"] == "sell"
    assert order["side"] == "yes"
    assert order["reduce_only"] is True
    assert order["time_in_force"] == "fill_or_kill"
    assert len(state.get("open_positions", [])) == 0
    assert len(state.get("closed_positions", [])) == 1


def test_live_exit_on_time_hold_limit(monkeypatch, tmp_path):
    monkeypatch.setattr("weather_arb.config.LIVE_EQUITY_SYNC_ENABLED", False)
    now = datetime(2026, 3, 3, 16, 0, tzinfo=timezone.utc)
    state_path = tmp_path / "live_positions.json"
    blotter_dir = tmp_path / "blotter"
    auth = _ExitAuthClient(fill_status="executed")

    pos = _open_position(now)
    pos["max_hold_until_utc"] = (now - timedelta(minutes=1)).isoformat()
    safe_write_json_atomic(
        state_path,
        {
            "equity": 50.0,
            "open_positions": [pos],
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
        _quote(now),
        now,
        auth_client=auth,
        state_path=state_path,
        blotter_dir=blotter_dir,
        live_routing_enabled=True,
    )
    assert out["closed"] == 1
    assert len(auth.orders) == 1


def test_live_exit_unfilled_keeps_position_open(monkeypatch, tmp_path):
    monkeypatch.setattr("weather_arb.config.LIVE_EQUITY_SYNC_ENABLED", False)
    now = datetime(2026, 3, 3, 16, 0, tzinfo=timezone.utc)
    state_path = tmp_path / "live_positions.json"
    blotter_dir = tmp_path / "blotter"
    auth = _ExitAuthClient(fill_status="canceled")

    safe_write_json_atomic(
        state_path,
        {
            "equity": 50.0,
            "open_positions": [_open_position(now)],
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
                "ev_cents": 0.5,
                "min_ev_cents": 6.0,
                "settlement_ts_utc": (now + timedelta(hours=8)).isoformat(),
            }
        ],
        _quote(now),
        now,
        auth_client=auth,
        state_path=state_path,
        blotter_dir=blotter_dir,
        live_routing_enabled=True,
    )

    state = safe_read_json(state_path) or {}
    assert out["closed"] == 0
    assert len(state.get("open_positions", [])) == 1
    assert len(state.get("closed_positions", [])) == 0
