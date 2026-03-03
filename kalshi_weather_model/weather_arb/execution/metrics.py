from __future__ import annotations

from statistics import mean
from typing import Any


def compute_day_metrics(closed_positions: list[dict[str, Any]], date_key: str) -> dict[str, Any]:
    rows = [
        p
        for p in closed_positions
        if str(p.get("closed_at_utc", "")).startswith(date_key)
    ]
    trades = len(rows)
    if trades == 0:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "pnl_dollars": 0.0,
            "roi_per_trade": 0.0,
        }

    pnl = [float(r.get("realized_pnl_dollars", 0.0) or 0.0) for r in rows]
    wins = sum(1 for p in pnl if p > 0)
    losses = sum(1 for p in pnl if p <= 0)

    notionals = [
        abs(float(r.get("entry_price_dollars", 0.0) or 0.0) * int(r.get("contracts", 0) or 0))
        for r in rows
    ]
    avg_notional = mean(notionals) if notionals else 1.0
    avg_pnl = mean(pnl)

    return {
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "win_rate": wins / trades,
        "pnl_dollars": sum(pnl),
        "roi_per_trade": avg_pnl / max(avg_notional, 1e-9),
    }


def max_drawdown_from_daily_pnl(
    daily_pnl: dict[str, float],
    *,
    starting_equity: float,
) -> float:
    running = float(starting_equity)
    peak = float(starting_equity)
    max_dd = 0.0
    for day in sorted(daily_pnl.keys()):
        running += float(daily_pnl[day] or 0.0)
        peak = max(peak, running)
        dd = (running - peak) / max(peak, 1e-9)
        max_dd = min(max_dd, dd)
    return max_dd
