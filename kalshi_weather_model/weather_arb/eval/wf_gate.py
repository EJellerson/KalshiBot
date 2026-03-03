from __future__ import annotations

from statistics import median
from typing import Any

from weather_arb.config import WF_MIN_FEASIBLE_RATE, WF_MIN_MEDIAN_EV_DAY, WF_MIN_WINDOWS


def evaluate_wf_gate(windows: list[dict[str, Any]]) -> dict[str, Any]:
    reasons: list[str] = []
    n_windows = len(windows)
    feasible = [w for w in windows if bool(w.get("feasible", False))]
    feasible_rate = len(feasible) / max(n_windows, 1)
    ev_days = [float(w.get("ev_day", 0.0) or 0.0) for w in feasible]
    median_ev = median(ev_days) if ev_days else 0.0

    if n_windows < WF_MIN_WINDOWS:
        reasons.append(f"windows {n_windows} < {WF_MIN_WINDOWS}")
    if feasible_rate < WF_MIN_FEASIBLE_RATE:
        reasons.append(f"feasible_rate {feasible_rate:.2%} < {WF_MIN_FEASIBLE_RATE:.2%}")
    if median_ev <= WF_MIN_MEDIAN_EV_DAY:
        reasons.append(f"median_ev_day {median_ev:.4f} <= {WF_MIN_MEDIAN_EV_DAY:.4f}")

    return {
        "gate": "wf",
        "pass": len(reasons) == 0,
        "reasons": reasons or ["wf gate passed"],
        "details": {
            "windows": n_windows,
            "feasible_rate": feasible_rate,
            "median_ev_day": median_ev,
        },
    }
