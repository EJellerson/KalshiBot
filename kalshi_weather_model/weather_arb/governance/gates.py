from __future__ import annotations

from typing import Any

from weather_arb.config import (
    MAX_PAPER_DRAWDOWN,
    MIN_PAPER_AVG_DAILY_PNL,
    MIN_PAPER_ROI_PER_TRADE,
    MIN_PAPER_TRADES,
    MIN_PAPER_TRADING_DAYS,
    MIN_PAPER_WIN_RATE,
    ROLLBACK_DRAWDOWN_INCREASE_THRESHOLD,
    ROLLBACK_MIN_TRADES_FOR_EVAL,
    ROLLBACK_ROI_DEGRADATION_THRESHOLD,
    ROLLBACK_WIN_RATE_DEGRADATION_THRESHOLD,
)


def evaluate_paper_gates(metrics: dict[str, Any]) -> tuple[bool, bool, list[str]]:
    reasons: list[str] = []

    trading_days = int(metrics.get("trading_days", 0) or 0)
    trades = int(metrics.get("trades", 0) or 0)
    win_rate = float(metrics.get("win_rate", 0.0) or 0.0)
    avg_daily_pnl = float(metrics.get("avg_daily_pnl", 0.0) or 0.0)
    max_drawdown = float(metrics.get("max_drawdown", 0.0) or 0.0)
    roi_per_trade = float(metrics.get("roi_per_trade", 0.0) or 0.0)

    enough_data = trading_days >= MIN_PAPER_TRADING_DAYS and trades >= MIN_PAPER_TRADES
    if not enough_data:
        if trading_days < MIN_PAPER_TRADING_DAYS:
            reasons.append(f"insufficient trading_days ({trading_days}/{MIN_PAPER_TRADING_DAYS})")
        if trades < MIN_PAPER_TRADES:
            reasons.append(f"insufficient trades ({trades}/{MIN_PAPER_TRADES})")
        return False, False, reasons

    passed = True
    if win_rate < MIN_PAPER_WIN_RATE:
        passed = False
        reasons.append(f"win_rate {win_rate:.3f} < {MIN_PAPER_WIN_RATE:.3f}")
    if avg_daily_pnl <= MIN_PAPER_AVG_DAILY_PNL:
        passed = False
        reasons.append(f"avg_daily_pnl {avg_daily_pnl:.2f} <= {MIN_PAPER_AVG_DAILY_PNL:.2f}")
    if max_drawdown < MAX_PAPER_DRAWDOWN:
        passed = False
        reasons.append(f"max_drawdown {max_drawdown:.4f} < {MAX_PAPER_DRAWDOWN:.4f}")
    if roi_per_trade <= MIN_PAPER_ROI_PER_TRADE:
        passed = False
        reasons.append(f"roi_per_trade {roi_per_trade:.6f} <= {MIN_PAPER_ROI_PER_TRADE:.6f}")

    if passed:
        reasons.append("paper gates passed")
    return passed, True, reasons


def evaluate_live_degradation(
    baseline: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    closed_trades = int(current.get("closed_trades", 0) or 0)
    if closed_trades < ROLLBACK_MIN_TRADES_FOR_EVAL:
        return {
            "triggered": False,
            "reason": "insufficient live trades for degradation evaluation",
            "flags": {},
            "closed_trades": closed_trades,
        }

    baseline_wr = float(baseline.get("win_rate", 0.0) or 0.0)
    current_wr = float(current.get("win_rate", 0.0) or 0.0)
    wr_drop = baseline_wr - current_wr

    baseline_roi = float(baseline.get("roi_per_trade", 0.0) or 0.0)
    current_roi = float(current.get("roi_per_trade", 0.0) or 0.0)
    roi_drop_ratio = (baseline_roi - current_roi) / max(abs(baseline_roi), 1e-9)

    baseline_dd = float(baseline.get("max_drawdown", 0.0) or 0.0)
    current_dd = float(current.get("max_drawdown", 0.0) or 0.0)
    dd_increase_ratio = (current_dd - baseline_dd) / max(abs(baseline_dd), 1.0)

    flags = {
        "win_rate_drop": wr_drop >= ROLLBACK_WIN_RATE_DEGRADATION_THRESHOLD,
        "roi_drop": roi_drop_ratio >= ROLLBACK_ROI_DEGRADATION_THRESHOLD,
        "drawdown_increase": dd_increase_ratio >= ROLLBACK_DRAWDOWN_INCREASE_THRESHOLD,
    }
    triggered = sum(int(v) for v in flags.values()) >= 2

    return {
        "triggered": bool(triggered),
        "flags": flags,
        "closed_trades": closed_trades,
        "baseline": {
            "win_rate": baseline_wr,
            "roi_per_trade": baseline_roi,
            "max_drawdown": baseline_dd,
        },
        "current": {
            "win_rate": current_wr,
            "roi_per_trade": current_roi,
            "max_drawdown": current_dd,
        },
    }
