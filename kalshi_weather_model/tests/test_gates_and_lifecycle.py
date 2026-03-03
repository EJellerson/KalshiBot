from __future__ import annotations

from weather_arb.governance.gates import evaluate_live_degradation, evaluate_paper_gates
from weather_arb.governance.model_registry import register_model, update_status


def test_evaluate_paper_gates_pass():
    passed, enough, reasons = evaluate_paper_gates(
        {
            "trading_days": 25,
            "trades": 40,
            "win_rate": 0.60,
            "avg_daily_pnl": 2.0,
            "max_drawdown": -0.03,
            "roi_per_trade": 0.02,
        }
    )
    assert passed is True
    assert enough is True
    assert "paper gates passed" in reasons


def test_evaluate_live_degradation_triggered_two_of_three():
    out = evaluate_live_degradation(
        baseline={"win_rate": 0.7, "roi_per_trade": 0.1, "max_drawdown": -0.05},
        current={"closed_trades": 40, "win_rate": 0.55, "roi_per_trade": 0.05, "max_drawdown": -0.10},
    )
    assert out["triggered"] is True


def test_registry_transition_training_to_validating(tmp_path):
    path = tmp_path / "registry.json"
    register_model(
        model_id="r1:l:t:global",
        run_id="r1",
        label_key="l",
        task_mode="t",
        scope_key="global",
        status="training",
        path=path,
    )
    updated = update_status("r1:l:t:global", "validating", path=path)
    assert updated["status"] == "validating"
