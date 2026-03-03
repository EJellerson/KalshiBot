from __future__ import annotations

from typing import Any

from weather_arb.config import (
    BACKTEST_MAX_DRAWDOWN,
    BACKTEST_MIN_EV_DAY,
    BACKTEST_MIN_ROI_PER_TRADE,
    BACKTEST_MIN_TRADES,
    BACKTEST_MIN_WIN_RATE,
)


def _max_drawdown(pnl_series: list[float]) -> float:
    equity = 0.0
    high_water = 0.0
    max_dd = 0.0
    for pnl in pnl_series:
        equity += pnl
        high_water = max(high_water, equity)
        dd = equity - high_water
        max_dd = min(max_dd, dd)
    return max_dd


def evaluate_backtest_gate(trades: list[dict[str, Any]]) -> dict[str, Any]:
    reasons: list[str] = []
    n = len(trades)
    pnl = [float(t.get("pnl_dollars", 0.0) or 0.0) for t in trades]
    wins = sum(1 for x in pnl if x > 0)
    win_rate = wins / max(n, 1)
    avg_pnl = sum(pnl) / max(n, 1)

    notional = [abs(float(t.get("notional_dollars", 0.0) or 0.0)) for t in trades]
    avg_notional = (sum(notional) / max(len(notional), 1)) if notional else 1.0
    roi_per_trade = avg_pnl / max(avg_notional, 1e-9)

    days = {str(t.get("date_key", "")) for t in trades if str(t.get("date_key", ""))}
    ev_day = sum(pnl) / max(len(days), 1)

    dd_dollars = _max_drawdown(pnl)
    peak = max(sum(pnl[:i + 1]) for i in range(len(pnl))) if pnl else 0.0
    dd_ratio = (dd_dollars / max(abs(peak), 1.0)) if peak != 0 else 0.0

    if n < BACKTEST_MIN_TRADES:
        reasons.append(f"trades {n} < {BACKTEST_MIN_TRADES}")
    if win_rate < BACKTEST_MIN_WIN_RATE:
        reasons.append(f"win_rate {win_rate:.3f} < {BACKTEST_MIN_WIN_RATE:.3f}")
    if roi_per_trade <= BACKTEST_MIN_ROI_PER_TRADE:
        reasons.append(f"roi_per_trade {roi_per_trade:.6f} <= {BACKTEST_MIN_ROI_PER_TRADE:.6f}")
    if ev_day <= BACKTEST_MIN_EV_DAY:
        reasons.append(f"ev_day {ev_day:.4f} <= {BACKTEST_MIN_EV_DAY:.4f}")
    if abs(dd_ratio) > BACKTEST_MAX_DRAWDOWN:
        reasons.append(f"drawdown {abs(dd_ratio):.2%} > {BACKTEST_MAX_DRAWDOWN:.2%}")

    return {
        "gate": "backtest",
        "pass": len(reasons) == 0,
        "reasons": reasons or ["backtest gate passed"],
        "details": {
            "trades": n,
            "win_rate": win_rate,
            "roi_per_trade": roi_per_trade,
            "ev_day": ev_day,
            "max_drawdown": -abs(dd_ratio),
        },
    }
