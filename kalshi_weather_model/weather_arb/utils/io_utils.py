from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

try:
    import fcntl

    HAVE_FCNTL = True
except Exception:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]
    HAVE_FCNTL = False


def _lock_path(path: Path) -> Path:
    return Path(f"{path}.lock")


def safe_read_json(file_path: str | Path) -> Any:
    path = Path(file_path)
    lock_path = _lock_path(path)
    lock_file = None
    try:
        if lock_path.exists():
            lock_file = lock_path.open("r", encoding="utf-8")
            if HAVE_FCNTL:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)

        if not path.exists():
            return None
        payload = path.read_text(encoding="utf-8").strip()
        if not payload:
            return None
        return json.loads(payload)
    except Exception:
        return None
    finally:
        if lock_file is not None and not lock_file.closed:
            if HAVE_FCNTL:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()


def safe_write_json_atomic(file_path: str | Path, data: Any) -> None:
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path(path)
    tmp_path = Path(f"{path}.tmp")

    with lock_path.open("a+", encoding="utf-8") as lock_file:
        if HAVE_FCNTL:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            with tmp_path.open("w", encoding="utf-8") as wf:
                json.dump(data, wf, indent=2, default=str)
                wf.flush()
                os.fsync(wf.fileno())
            os.replace(tmp_path, path)
        finally:
            if HAVE_FCNTL:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)


def read_or_create_json(file_path: str | Path, default_data: dict[str, Any]) -> dict[str, Any]:
    loaded = safe_read_json(file_path)
    if not isinstance(loaded, dict):
        safe_write_json_atomic(file_path, default_data)
        return dict(default_data)

    changed = False
    data = dict(loaded)
    for key, value in default_data.items():
        if key not in data:
            data[key] = value
            changed = True
    if changed:
        safe_write_json_atomic(file_path, data)
    return data
