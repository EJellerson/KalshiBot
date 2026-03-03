from __future__ import annotations

from weather_arb.utils.io_utils import read_or_create_json, safe_read_json, safe_write_json_atomic


def test_safe_write_and_read(tmp_path):
    path = tmp_path / "state.json"
    safe_write_json_atomic(path, {"a": 1, "b": "x"})
    loaded = safe_read_json(path)
    assert loaded == {"a": 1, "b": "x"}


def test_read_or_create_backfills_defaults(tmp_path):
    path = tmp_path / "cfg.json"
    data = read_or_create_json(path, {"x": 1, "y": 2})
    assert data == {"x": 1, "y": 2}

    safe_write_json_atomic(path, {"x": 9})
    data2 = read_or_create_json(path, {"x": 1, "y": 2})
    assert data2["x"] == 9
    assert data2["y"] == 2
