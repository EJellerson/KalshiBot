from __future__ import annotations

from dataclasses import dataclass
from statistics import mean
from typing import Any

from weather_arb import config


@dataclass(slots=True)
class CalibrationOutcome:
    min_ev_cents: float
    trades: int
    avg_daily_pnl: float
    roi_per_trade: float
    score: float


def bootstrap_threshold(closed_trades_count: int) -> float:
    if closed_trades_count < config.BOOTSTRAP_MIN_CLOSED_TRADES_PER_CITY:
        return config.BOOTSTRAP_MIN_EV_CENTS
    return config.CALIBRATION_MIN_EV_CENTS


def _frange(start: float, stop: float, step: float) -> list[float]:
    values: list[float] = []
    current = start
    while current <= stop + 1e-9:
        values.append(round(current, 6))
        current += step
    return values


def calibrate_min_ev_threshold(
    rows: list[dict[str, Any]],
    *,
    min_ev: float = config.CALIBRATION_MIN_EV_CENTS,
    max_ev: float = config.CALIBRATION_MAX_EV_CENTS,
    step: float = config.CALIBRATION_STEP_EV_CENTS,
    min_trades: int = 10,
) -> CalibrationOutcome:
    """Select threshold maximizing avg daily pnl with simple guardrails.

    Required row keys:
    - ev_cents
    - realized_pnl_dollars
    - date_key
    """
    candidates = _frange(min_ev, max_ev, step)
    best = CalibrationOutcome(
        min_ev_cents=config.BOOTSTRAP_MIN_EV_CENTS,
        trades=0,
        avg_daily_pnl=0.0,
        roi_per_trade=0.0,
        score=float("-inf"),
    )

    eps = 1e-9
    for threshold in candidates:
        selected = [r for r in rows if float(r.get("ev_cents", 0.0) or 0.0) >= threshold]
        trades = len(selected)
        if trades < min_trades:
            continue

        pnl_values = [float(r.get("realized_pnl_dollars", 0.0) or 0.0) for r in selected]
        total_pnl = sum(pnl_values)

        days = {str(r.get("date_key", "")) for r in selected if str(r.get("date_key", ""))}
        day_count = max(len(days), 1)
        avg_daily_pnl = total_pnl / day_count

        notional = [abs(float(r.get("notional_dollars", 0.0) or 0.0)) for r in selected]
        avg_notional = mean(notional) if notional else 1.0
        roi_per_trade = (mean(pnl_values) / max(avg_notional, 1e-9))

        # Score favors daily pnl and scales directly with ROI quality.
        score = avg_daily_pnl + (50.0 * roi_per_trade)

        if (score > best.score + eps) or (abs(score - best.score) <= eps and threshold > best.min_ev_cents):
            best = CalibrationOutcome(
                min_ev_cents=threshold,
                trades=trades,
                avg_daily_pnl=avg_daily_pnl,
                roi_per_trade=roi_per_trade,
                score=score,
            )

    if best.score == float("-inf"):
        return CalibrationOutcome(
            min_ev_cents=config.BOOTSTRAP_MIN_EV_CENTS,
            trades=0,
            avg_daily_pnl=0.0,
            roi_per_trade=0.0,
            score=0.0,
        )
    return best
