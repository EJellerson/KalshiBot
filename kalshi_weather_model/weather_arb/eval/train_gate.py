from __future__ import annotations

from typing import Any

from weather_arb.config import TRAIN_MAX_MISSING_PCT, TRAIN_MIN_OBSERVATIONS_PER_CITY


def evaluate_train_gate(city_stats: dict[str, dict[str, Any]]) -> dict[str, Any]:
    reasons: list[str] = []
    details: dict[str, Any] = {}

    for city, stats in city_stats.items():
        n_obs = int(stats.get("observations", 0) or 0)
        missing_pct = float(stats.get("missing_pct", 0.0) or 0.0)
        details[city] = {"observations": n_obs, "missing_pct": missing_pct}

        if n_obs < TRAIN_MIN_OBSERVATIONS_PER_CITY:
            reasons.append(f"{city}: observations {n_obs} < {TRAIN_MIN_OBSERVATIONS_PER_CITY}")
        if missing_pct > TRAIN_MAX_MISSING_PCT:
            reasons.append(f"{city}: missing_pct {missing_pct:.2%} > {TRAIN_MAX_MISSING_PCT:.2%}")

    return {
        "gate": "train",
        "pass": len(reasons) == 0,
        "reasons": reasons or ["train gate passed"],
        "details": details,
    }
