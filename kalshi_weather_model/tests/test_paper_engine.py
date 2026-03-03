from __future__ import annotations

from datetime import datetime, timedelta, timezone

from weather_arb.execution.paper_engine import _to_quote, run_paper_cycle
from weather_arb.utils.io_utils import safe_read_json, safe_write_json_atomic


def test_to_quote_fallback_is_timezone_aware():
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


def test_run_paper_cycle_exits_time_based_position_without_quote(tmp_path):
    now = datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc)
    state_path = tmp_path / "paper_positions.json"
    blotter_dir = tmp_path / "paper_blotter"

    safe_write_json_atomic(
        state_path,
        {
            "equity": 1000.0,
            "cash": 1000.0,
            "open_positions": [
                {
                    "position_id": "paper_1",
                    "ticker": "KXHIGHNYC-26MAR03-T75",
                    "city": "NYC",
                    "side": "buy_yes",
                    "contracts": 10,
                    "entry_price_dollars": 0.50,
                    "entry_fees_dollars": 0.20,
                    "opened_at_utc": (now - timedelta(hours=5)).isoformat(),
                    "max_hold_until_utc": (now - timedelta(minutes=1)).isoformat(),
                    "settlement_ts_utc": (now + timedelta(hours=10)).isoformat(),
                    "status": "open",
                }
            ],
            "closed_positions": [],
            "daily_pnl": {},
            "weekly_pnl": {},
            "consecutive_losses": 0,
            "next_position_id": 2,
        },
    )

    out = run_paper_cycle([], {}, now, state_path=state_path, blotter_dir=blotter_dir)
    state = safe_read_json(state_path) or {}

    assert out["closed"] == 1
    assert out["open_positions"] == 0
    assert len(state.get("closed_positions", [])) == 1
    closed = state["closed_positions"][0]
    assert closed["close_reason"] == "timeout_no_quote"
    assert float(closed["realized_pnl_dollars"]) == -0.2
