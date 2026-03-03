from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from weather_arb.config import (
    ALLOWED_STATUSES,
    MODEL_REGISTRY_PATH,
    MODEL_REGISTRY_SCHEMA_VERSION,
    SCOPE_KEYS,
    TRANSITIONS,
)
from weather_arb.utils.io_utils import read_or_create_json, safe_write_json_atomic


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _infer_strategy_id(entry: dict[str, Any]) -> str:
    existing = str(entry.get("strategy_id") or "").strip()
    if existing:
        return existing
    label_key = str(entry.get("label_key") or "").strip().lower()
    if label_key in {"weather_temp", "weather_temp_high"}:
        return "weather_temp_high"
    if label_key == "weather_temp_low":
        return "weather_temp_low"
    if label_key == "weather_temp_bucket":
        return "weather_temp_bucket"
    return "weather_temp_high"


def make_model_id(
    run_id: str,
    label_key: str,
    task_mode: str,
    scope_key: str = "global",
    strategy_id: str = "weather_temp_high",
) -> str:
    return f"{run_id}:{strategy_id}:{label_key}:{task_mode}:{scope_key}"


def _default_registry(scopes: list[str] | None = None) -> dict[str, Any]:
    use_scopes = list(scopes or SCOPE_KEYS)
    return {
        "schema_version": MODEL_REGISTRY_SCHEMA_VERSION,
        "scope_mode": "global",
        "scopes": use_scopes,
        "champion_by_scope": {scope: None for scope in use_scopes},
        "backup_by_scope": {scope: None for scope in use_scopes},
        "models": [],
        "events": [],
        "updated_at": _iso_now(),
    }


def _validate_transition(old_status: str, new_status: str) -> None:
    if new_status not in ALLOWED_STATUSES:
        raise ValueError(f"invalid target status: {new_status}")
    allowed = TRANSITIONS.get(old_status)
    if allowed is None:
        raise ValueError(f"unknown source status: {old_status}")
    if new_status not in allowed:
        raise ValueError(f"invalid status transition {old_status} -> {new_status}")


def _migrate_registry_if_needed(registry: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    changed = False
    if str(registry.get("schema_version") or "") != MODEL_REGISTRY_SCHEMA_VERSION:
        registry["schema_version"] = MODEL_REGISTRY_SCHEMA_VERSION
        changed = True

    models = list(registry.get("models") or [])
    for model in models:
        if not isinstance(model, dict):
            continue
        strategy_id = _infer_strategy_id(model)
        if str(model.get("strategy_id") or "") != strategy_id:
            model["strategy_id"] = strategy_id
            changed = True
        if "paper_metrics" not in model:
            model["paper_metrics"] = {}
            changed = True
        if "paper_eval_count" not in model:
            model["paper_eval_count"] = 0
            changed = True
        if "gate_results" not in model:
            model["gate_results"] = {}
            changed = True
        if "data_health" not in model:
            model["data_health"] = {}
            changed = True

    return registry, changed


def load_registry(path=MODEL_REGISTRY_PATH) -> dict[str, Any]:
    registry = read_or_create_json(path, _default_registry())
    registry, changed = _migrate_registry_if_needed(registry)
    if changed:
        save_registry(registry, path)
    return registry


def save_registry(registry: dict[str, Any], path=MODEL_REGISTRY_PATH) -> None:
    registry["updated_at"] = _iso_now()
    safe_write_json_atomic(path, registry)


def record_event(registry: dict[str, Any], event_type: str, **payload: Any) -> dict[str, Any]:
    event = {"ts": _iso_now(), "event": event_type, **payload}
    registry.setdefault("events", []).append(event)
    return event


def register_model(
    model_id: str,
    run_id: str,
    label_key: str,
    task_mode: str,
    scope_key: str = "global",
    strategy_id: str = "weather_temp_high",
    status: str = "training",
    model_dir: str | None = None,
    path=MODEL_REGISTRY_PATH,
) -> dict[str, Any]:
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"invalid status: {status}")
    registry = load_registry(path)
    models = registry.setdefault("models", [])
    if any(str(m.get("model_id")) == model_id for m in models):
        raise ValueError(f"model_id already exists: {model_id}")

    entry = {
        "model_id": model_id,
        "run_id": run_id,
        "strategy_id": strategy_id,
        "label_key": label_key,
        "task_mode": task_mode,
        "scope_key": scope_key,
        "status": status,
        "model_dir": model_dir or "",
        "created_at": _iso_now(),
        "updated_at": _iso_now(),
        "paper_metrics": {},
        "paper_eval_count": 0,
        "gate_results": {},
        "data_health": {},
    }
    models.append(entry)
    record_event(registry, "register_model", model_id=model_id, strategy_id=strategy_id, status=status)
    save_registry(registry, path)
    return entry


def get_model(model_id: str, path=MODEL_REGISTRY_PATH) -> dict[str, Any] | None:
    registry = load_registry(path)
    for entry in registry.get("models", []):
        if str(entry.get("model_id")) == model_id:
            return dict(entry)
    return None


def get_models_by_status(
    status: str,
    *,
    strategy_id: str | None = None,
    path=MODEL_REGISTRY_PATH,
) -> list[dict[str, Any]]:
    registry = load_registry(path)
    out: list[dict[str, Any]] = []
    for m in registry.get("models", []):
        if str(m.get("status")) != status:
            continue
        if strategy_id and str(m.get("strategy_id")) != strategy_id:
            continue
        out.append(dict(m))
    return out


def update_status(
    model_id: str,
    new_status: str,
    reason: str = "",
    paper_metrics: dict[str, Any] | None = None,
    gate_results: dict[str, Any] | None = None,
    data_health: dict[str, Any] | None = None,
    path=MODEL_REGISTRY_PATH,
) -> dict[str, Any]:
    registry = load_registry(path)
    for entry in registry.get("models", []):
        if str(entry.get("model_id")) != model_id:
            continue
        old_status = str(entry.get("status", ""))
        _validate_transition(old_status, new_status)
        entry["status"] = new_status
        entry["updated_at"] = _iso_now()
        if paper_metrics:
            merged = dict(entry.get("paper_metrics") or {})
            merged.update(dict(paper_metrics))
            entry["paper_metrics"] = merged
        if gate_results:
            merged_gate = dict(entry.get("gate_results") or {})
            merged_gate.update(dict(gate_results))
            entry["gate_results"] = merged_gate
        if data_health:
            merged_health = dict(entry.get("data_health") or {})
            merged_health.update(dict(data_health))
            entry["data_health"] = merged_health
        record_event(
            registry,
            "update_status",
            model_id=model_id,
            strategy_id=entry.get("strategy_id"),
            old_status=old_status,
            new_status=new_status,
            reason=reason,
        )
        save_registry(registry, path)
        return dict(entry)
    raise ValueError(f"model_id not found: {model_id}")


def set_paper_metrics(
    model_id: str,
    paper_metrics: dict[str, Any],
    increment_eval: bool,
    path=MODEL_REGISTRY_PATH,
) -> dict[str, Any]:
    registry = load_registry(path)
    for entry in registry.get("models", []):
        if str(entry.get("model_id")) != model_id:
            continue
        merged = dict(entry.get("paper_metrics") or {})
        merged.update(dict(paper_metrics))
        entry["paper_metrics"] = merged
        if increment_eval:
            entry["paper_eval_count"] = int(entry.get("paper_eval_count", 0) or 0) + 1
        entry["updated_at"] = _iso_now()
        save_registry(registry, path)
        return dict(entry)
    raise ValueError(f"model_id not found: {model_id}")


def get_champion(scope_key: str = "global", path=MODEL_REGISTRY_PATH) -> dict[str, Any] | None:
    registry = load_registry(path)
    champion_id = registry.get("champion_by_scope", {}).get(scope_key)
    if not champion_id:
        return None
    return get_model(str(champion_id), path=path)


def promote_champion(
    model_id: str,
    scope_key: str = "global",
    reason: str = "",
    path=MODEL_REGISTRY_PATH,
) -> dict[str, Any]:
    registry = load_registry(path)
    current_champion_id = registry.get("champion_by_scope", {}).get(scope_key)
    if current_champion_id and str(current_champion_id) != model_id:
        for entry in registry.get("models", []):
            if str(entry.get("model_id")) == str(current_champion_id):
                old_status = str(entry.get("status", ""))
                if old_status in TRANSITIONS and "backup_standby" in TRANSITIONS[old_status]:
                    entry["status"] = "backup_standby"
                    entry["updated_at"] = _iso_now()

    promoted = None
    for entry in registry.get("models", []):
        if str(entry.get("model_id")) != model_id:
            continue
        old_status = str(entry.get("status", ""))
        if old_status != "paper":
            raise ValueError(f"only paper models can be promoted to champion_live, got: {old_status}")
        _validate_transition(old_status, "champion_live")
        entry["status"] = "champion_live"
        entry["updated_at"] = _iso_now()
        promoted = dict(entry)
        break
    if promoted is None:
        raise ValueError(f"model_id not found: {model_id}")

    registry.setdefault("champion_by_scope", {})[scope_key] = model_id
    record_event(
        registry,
        "promote_champion",
        model_id=model_id,
        strategy_id=(promoted or {}).get("strategy_id"),
        scope_key=scope_key,
        reason=reason,
    )
    save_registry(registry, path)
    return promoted
