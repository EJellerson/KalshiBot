from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from weather_arb import config
from weather_arb.utils.time_utils import day_key_in_zone


def append_rows_to_parquet(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    frame_new = pd.DataFrame(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        frame_old = pd.read_parquet(path)
        frame = pd.concat([frame_old, frame_new], ignore_index=True)
    else:
        frame = frame_new
    frame.to_parquet(path, index=False)


def write_contract_snapshots(active_rows: list[dict[str, Any]], history_rows: list[dict[str, Any]]) -> None:
    if active_rows:
        pd.DataFrame(active_rows).to_parquet(config.CONTRACTS_ACTIVE_PATH, index=False)
    append_rows_to_parquet(config.CONTRACTS_HISTORY_PATH, history_rows)


def append_forecast_rows(rows: list[dict[str, Any]], now_utc: datetime) -> Path:
    day_key = day_key_in_zone(now_utc, config.SCHEDULER_TZ)
    file_path = config.FORECAST_SNAPSHOTS_DIR / f"forecast_{day_key}.parquet"
    append_rows_to_parquet(file_path, rows)
    return file_path


def append_quote_rows(rows: list[dict[str, Any]], now_utc: datetime) -> Path:
    day_key = day_key_in_zone(now_utc, config.SCHEDULER_TZ)
    file_path = config.MARKET_QUOTES_DIR / f"quotes_{day_key}.parquet"
    append_rows_to_parquet(file_path, rows)
    return file_path


def append_signal_rows(rows: list[dict[str, Any]], now_utc: datetime) -> Path:
    day_key = day_key_in_zone(now_utc, config.SCHEDULER_TZ)
    file_path = config.SIGNALS_DIR / f"signals_{day_key}.parquet"
    append_rows_to_parquet(file_path, rows)
    return file_path


def append_observation_rows(rows: list[dict[str, Any]], now_utc: datetime) -> Path:
    day_key = day_key_in_zone(now_utc, config.SCHEDULER_TZ)
    file_path = config.OBSERVATIONS_DIR / f"obs_{day_key}.parquet"
    if not rows:
        return file_path

    frame_new = pd.DataFrame(rows)
    dedupe_cols = [c for c in ["city", "obs_date_local"] if c in frame_new.columns]

    if file_path.exists():
        frame_old = pd.read_parquet(file_path)
        frame = pd.concat([frame_old, frame_new], ignore_index=True)
    else:
        frame = frame_new

    if dedupe_cols:
        keep_cols = [c for c in ["observed_at_utc"] if c in frame.columns]
        if keep_cols:
            frame = frame.sort_values(by=keep_cols)
        frame = frame.drop_duplicates(subset=dedupe_cols, keep="last")

    file_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(file_path, index=False)
    return file_path
