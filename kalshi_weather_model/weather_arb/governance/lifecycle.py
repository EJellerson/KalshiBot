from __future__ import annotations

from typing import Any

from weather_arb.config import (
    LIFECYCLE_STATE_PATH,
    PAPER_FAILURE_MAX_EVALS,
    ROLLBACK_CONSECUTIVE_FAILURES,
)
from weather_arb.governance import model_registry
from weather_arb.governance.gates import evaluate_live_degradation, evaluate_paper_gates
from weather_arb.utils.io_utils import read_or_create_json, safe_write_json_atomic


def default_lifecycle_state() -> dict[str, Any]:
    return {
        "active_model_id": None,
        "degradation_consecutive": 0,
        "last_eval": None,
        "notes": [],
    }


def load_lifecycle_state(path=LIFECYCLE_STATE_PATH) -> dict[str, Any]:
    return read_or_create_json(path, default_lifecycle_state())


def save_lifecycle_state(payload: dict[str, Any], path=LIFECYCLE_STATE_PATH) -> None:
    safe_write_json_atomic(path, payload)


def apply_train_gate(model_id: str, passed: bool, reason: str = "") -> dict[str, Any]:
    if passed:
        return model_registry.update_status(model_id, "validating", reason=reason or "train_gate_passed")
    return model_registry.update_status(model_id, "failed", reason=reason or "train_gate_failed")


def apply_wf_gate(model_id: str, passed: bool, reason: str = "") -> dict[str, Any]:
    if passed:
        return model_registry.update_status(model_id, "wf_passed", reason=reason or "wf_gate_passed")
    return model_registry.update_status(model_id, "failed", reason=reason or "wf_gate_failed")


def apply_backtest_gate(model_id: str, passed: bool, reason: str = "") -> dict[str, Any]:
    if passed:
        out = model_registry.update_status(model_id, "backtest_passed", reason=reason or "backtest_gate_passed")
        return model_registry.update_status(model_id, "qualified", reason="qualified_after_backtest") if out else out
    return model_registry.update_status(model_id, "failed", reason=reason or "backtest_gate_failed")


def apply_paper_metrics(model_id: str, metrics: dict[str, Any]) -> dict[str, Any]:
    passed, enough_data, reasons = evaluate_paper_gates(metrics)
    model_registry.set_paper_metrics(model_id, metrics, increment_eval=bool(enough_data and not passed))
    entry = model_registry.get_model(model_id) or {}
    status = str(entry.get("status", ""))

    if passed:
        if status in {"paper", "champion_live"}:
            return {
                "model_id": model_id,
                "status": status,
                "paper_gate_pass": True,
                "reasons": reasons,
            }
        return model_registry.update_status(model_id, "paper", reason="paper_gates_passed", paper_metrics=metrics)

    eval_count = int((entry or {}).get("paper_eval_count", 0) or 0)
    if enough_data and eval_count >= PAPER_FAILURE_MAX_EVALS:
        return model_registry.update_status(
            model_id,
            "failed",
            reason=f"paper_gates_failed_max_evals: {', '.join(reasons)}",
            paper_metrics=metrics,
        )
    return {
        "model_id": model_id,
        "status": (entry or {}).get("status"),
        "pending": True,
        "enough_data": enough_data,
        "reasons": reasons,
        "paper_eval_count": eval_count,
    }


def auto_promote_if_ready(model_id: str, scope_key: str = "global") -> dict[str, Any] | None:
    entry = model_registry.get_model(model_id)
    if not entry:
        return None
    if str(entry.get("status")) != "paper":
        return None
    return model_registry.promote_champion(model_id, scope_key=scope_key, reason="auto_promotion")


def apply_live_degradation(
    model_id: str,
    baseline_metrics: dict[str, Any],
    current_metrics: dict[str, Any],
) -> dict[str, Any]:
    state = load_lifecycle_state()
    result = evaluate_live_degradation(baseline_metrics, current_metrics)

    if result.get("triggered", False):
        state["degradation_consecutive"] = int(state.get("degradation_consecutive", 0) or 0) + 1
    else:
        state["degradation_consecutive"] = 0

    state["active_model_id"] = model_id
    state["last_eval"] = result
    save_lifecycle_state(state)

    if state["degradation_consecutive"] >= ROLLBACK_CONSECUTIVE_FAILURES:
        demoted = model_registry.update_status(
            model_id,
            "paper",
            reason="live_degradation_rollback",
            paper_metrics=current_metrics,
        )
        return {
            "rollback": True,
            "degradation_consecutive": state["degradation_consecutive"],
            "result": result,
            "model": demoted,
        }
    return {
        "rollback": False,
        "degradation_consecutive": state["degradation_consecutive"],
        "result": result,
    }
