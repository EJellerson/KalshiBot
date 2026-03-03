from __future__ import annotations

from typing import Any

from weather_arb import config
from weather_arb.utils.io_utils import safe_read_json


def live_routing_status() -> dict[str, Any]:
    manual_enabled = bool(config.ALLOW_LIVE_TRADING)
    auto_toggle_enabled = bool(config.LIVE_AUTO_ENABLE_ON_CHAMPION)
    tradable = set(config.TRADABLE_WEATHER_STRATEGIES)

    champion_id: str | None = None
    source = "none"
    invalid_champion = False

    if auto_toggle_enabled:
        champion_state = safe_read_json(config.CHAMPION_STATE_PATH) or {}
        strategy_champion = str(champion_state.get("current_champion") or "").strip()
        if strategy_champion:
            source = "strategy_champion_state"
            if strategy_champion in tradable:
                champion_id = strategy_champion
            else:
                invalid_champion = True
                champion_id = None
                source = "champion_invalid"

    auto_enabled = bool(auto_toggle_enabled and champion_id)
    enabled = bool(manual_enabled or auto_enabled)

    if manual_enabled:
        reason = "manual_env_enabled"
    elif not auto_toggle_enabled:
        reason = "auto_disabled_no_manual_override"
    elif invalid_champion:
        reason = "invalid_champion_strategy"
    elif auto_enabled:
        reason = "auto_enabled_on_champion"
    else:
        reason = "no_champion_available"

    return {
        "enabled": enabled,
        "reason": reason,
        "manual_enabled": manual_enabled,
        "auto_enabled": auto_enabled,
        "auto_toggle_enabled": auto_toggle_enabled,
        "champion_id": champion_id,
        "source": source,
    }


def live_routing_enabled() -> bool:
    return bool(live_routing_status().get("enabled", False))
