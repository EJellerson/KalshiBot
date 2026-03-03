from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from weather_arb.config import CITY_CONFIG
from weather_arb.types import ContractInfo
from weather_arb.utils.time_utils import parse_iso_datetime


_CITY_ALIASES = {
    "NYC": ["NYC", "NEW YORK", "NEW YORK CITY"],
    "Chicago": ["CHICAGO", "ORD"],
    "Dallas": ["DALLAS", "DFW"],
    "Miami": ["MIAMI"],
    "Atlanta": ["ATLANTA"],
    "Seattle": ["SEATTLE"],
}

_TEMP_PATTERNS = [
    re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:°\s*)?F\b", re.IGNORECASE),
    re.compile(r"(?:ABOVE|OVER|AT\s+LEAST|>=?)\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE),
    re.compile(r"(?:BELOW|UNDER|<=?)\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE),
    re.compile(r"(?:^|[-_])T(-?\d+(?:\.\d+)?)(?:$|[-_])", re.IGNORECASE),
]


def _extract_markets(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(payload.get("markets"), list):
        return [m for m in payload["markets"] if isinstance(m, dict)]
    if isinstance(payload.get("data"), list):
        return [m for m in payload["data"] if isinstance(m, dict)]
    if isinstance(payload.get("market"), dict):
        return [payload["market"]]
    return []


def _detect_city(text: str) -> str | None:
    up = text.upper()
    for city, aliases in _CITY_ALIASES.items():
        if any(alias in up for alias in aliases):
            return city
    return None


def _extract_threshold(text: str) -> float | None:
    for pattern in _TEMP_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                continue
    return None


def _extract_settlement_ts(raw: dict[str, Any]) -> datetime | None:
    for key in [
        "settlement_time",
        "settlement_ts",
        "expiration_time",
        "close_time",
        "end_date",
    ]:
        value = raw.get(key)
        if not value:
            continue
        try:
            return parse_iso_datetime(str(value))
        except Exception:
            continue
    return None


def discover_temperature_contracts(payload: dict[str, Any]) -> tuple[list[ContractInfo], list[dict[str, Any]]]:
    markets = _extract_markets(payload)
    contracts: list[ContractInfo] = []
    skipped: list[dict[str, Any]] = []

    for raw in markets:
        ticker = str(raw.get("ticker") or "").strip()
        title = str(raw.get("title") or raw.get("subtitle") or ticker).strip()
        blob = f"{ticker} {title}".strip()

        city = _detect_city(blob)
        if not city:
            skipped.append({"ticker": ticker, "reason": "city_unmapped"})
            continue
        threshold_f = _extract_threshold(blob)
        if threshold_f is None:
            skipped.append({"ticker": ticker, "reason": "threshold_unparsed"})
            continue
        settlement_ts = _extract_settlement_ts(raw)
        if settlement_ts is None:
            skipped.append({"ticker": ticker, "reason": "settlement_time_missing"})
            continue

        city_tz = CITY_CONFIG[city]["tz"]
        contract_date_local = settlement_ts.astimezone(ZoneInfo(city_tz)).date().isoformat()

        contracts.append(
            ContractInfo(
                market_id=str(raw.get("id") or ticker),
                ticker=ticker,
                title=title,
                city=city,
                threshold_f=threshold_f,
                settlement_ts_utc=settlement_ts,
                contract_date_local=contract_date_local,
                status=str(raw.get("status") or "unknown"),
                raw=dict(raw),
            )
        )

    return contracts, skipped


def contracts_to_frame(contracts: list[ContractInfo]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "market_id": c.market_id,
                "ticker": c.ticker,
                "title": c.title,
                "city": c.city,
                "threshold_f": c.threshold_f,
                "settlement_ts_utc": c.settlement_ts_utc.isoformat(),
                "contract_date_local": c.contract_date_local,
                "status": c.status,
            }
            for c in contracts
        ]
    )
