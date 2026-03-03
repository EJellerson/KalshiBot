from __future__ import annotations

from weather_arb.governance.live_routing import live_routing_status
from weather_arb.utils.io_utils import safe_write_json_atomic


def test_live_routing_manual_override(monkeypatch, tmp_path):
    champion_path = tmp_path / "champion_state.json"
    registry_path = tmp_path / "model_registry.json"

    monkeypatch.setattr("weather_arb.config.CHAMPION_STATE_PATH", champion_path)
    monkeypatch.setattr("weather_arb.config.MODEL_REGISTRY_PATH", registry_path)
    monkeypatch.setattr("weather_arb.config.ALLOW_LIVE_TRADING", True)
    monkeypatch.setattr("weather_arb.config.LIVE_AUTO_ENABLE_ON_CHAMPION", False)

    out = live_routing_status()
    assert out["enabled"] is True
    assert out["reason"] == "manual_env_enabled"
    assert out["champion_id"] is None


def test_live_routing_auto_uses_strategy_champion(monkeypatch, tmp_path):
    champion_path = tmp_path / "champion_state.json"

    safe_write_json_atomic(champion_path, {"current_champion": "weather_temp_high"})

    monkeypatch.setattr("weather_arb.config.CHAMPION_STATE_PATH", champion_path)
    monkeypatch.setattr("weather_arb.config.ALLOW_LIVE_TRADING", False)
    monkeypatch.setattr("weather_arb.config.LIVE_AUTO_ENABLE_ON_CHAMPION", True)
    monkeypatch.setattr("weather_arb.config.TRADABLE_WEATHER_STRATEGIES", ["weather_temp_high", "weather_temp_low"])

    out = live_routing_status()
    assert out["enabled"] is True
    assert out["auto_enabled"] is True
    assert out["reason"] == "auto_enabled_on_champion"
    assert out["champion_id"] == "weather_temp_high"
    assert out["source"] == "strategy_champion_state"


def test_live_routing_auto_invalid_champion_fails_closed(monkeypatch, tmp_path):
    champion_path = tmp_path / "champion_state.json"
    safe_write_json_atomic(champion_path, {"current_champion": "weather_unknown"})

    monkeypatch.setattr("weather_arb.config.CHAMPION_STATE_PATH", champion_path)
    monkeypatch.setattr("weather_arb.config.ALLOW_LIVE_TRADING", False)
    monkeypatch.setattr("weather_arb.config.LIVE_AUTO_ENABLE_ON_CHAMPION", True)
    monkeypatch.setattr("weather_arb.config.TRADABLE_WEATHER_STRATEGIES", ["weather_temp_high", "weather_temp_low"])

    out = live_routing_status()
    assert out["enabled"] is False
    assert out["auto_enabled"] is False
    assert out["champion_id"] is None
    assert out["source"] == "champion_invalid"
    assert out["reason"] == "invalid_champion_strategy"


def test_live_routing_disabled_without_champion(monkeypatch, tmp_path):
    champion_path = tmp_path / "champion_state.json"

    monkeypatch.setattr("weather_arb.config.CHAMPION_STATE_PATH", champion_path)
    monkeypatch.setattr("weather_arb.config.ALLOW_LIVE_TRADING", False)
    monkeypatch.setattr("weather_arb.config.LIVE_AUTO_ENABLE_ON_CHAMPION", True)

    out = live_routing_status()
    assert out["enabled"] is False
    assert out["reason"] == "no_champion_available"
