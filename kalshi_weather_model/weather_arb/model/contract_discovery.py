from __future__ import annotations

import re
from dataclasses import dataclass
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

_TEMP_F_PATTERN = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:°\s*)?F\b", re.IGNORECASE)
_TEMP_GT_PATTERN = re.compile(r"(?:ABOVE|OVER|AT\s+LEAST|NO\s+LESS\s+THAN|>=?|>)\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_TEMP_LT_PATTERN = re.compile(r"(?:BELOW|UNDER|AT\s+MOST|NO\s+MORE\s+THAN|<=?|<)\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_TEMP_RANGE_PATTERN = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:TO|-)\s*(-?\d+(?:\.\d+)?)(?:\s*(?:°\s*)?F)?", re.IGNORECASE)
_TEMP_TOKEN_T_PATTERN = re.compile(r"(?:^|[-_])T(-?\d+(?:\.\d+)?)(?=$|[-_\s])", re.IGNORECASE)
_TEMP_TOKEN_B_PATTERN = re.compile(r"(?:^|[-_])B(-?\d+(?:\.\d+)?)(?=$|[-_\s])", re.IGNORECASE)

_EVENT_DATE_PATTERN = re.compile(r"(?:^|[-_])(\d{2}[A-Z]{3}\d{2})(?:$|[-_])")
_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}

_TEMP_CONTEXT_KEYWORDS = (
    "TEMPERATURE",
    "TEMP",
    "HIGH TEMP",
    "LOW TEMP",
    "MAX TEMP",
    "MIN TEMP",
    "MAXIMUM TEMP",
    "MINIMUM TEMP",
    "HIGHEST TEMPERATURE",
    "LOWEST TEMPERATURE",
)
_TEMP_TICKER_HINT_PATTERN = re.compile(r"^KX(?:HIGH|LOW)[A-Z0-9-]+$")
_PRECIP_KEYWORDS = ("PRECIP", "RAIN", "RAINFALL")
_SNOW_KEYWORDS = ("SNOW", "SNOWFALL", "BLIZZARD")
_WIND_KEYWORDS = ("WIND", "GUST", "GUSTS")

_TEMP_LOW_HINTS = ("LOWEST", "LOW TEMPERATURE", "LOW TEMP", "OVERNIGHT LOW", "COLDEST")
_TEMP_BUCKET_HINTS = ("BETWEEN", "RANGE", "BUCKET")


@dataclass(slots=True)
class TemperatureSpec:
    comparator: str
    threshold_f: float | None
    lower_f: float | None = None
    upper_f: float | None = None


def _extract_markets(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(payload.get("markets"), list):
        return [m for m in payload["markets"] if isinstance(m, dict)]
    if isinstance(payload.get("data"), list):
        return [m for m in payload["data"] if isinstance(m, dict)]
    if isinstance(payload.get("market"), dict):
        return [payload["market"]]
    return []


def _market_blob(raw: dict[str, Any]) -> str:
    ticker = str(raw.get("ticker") or "").strip()
    title = str(raw.get("title") or "").strip()
    subtitle = str(raw.get("subtitle") or raw.get("sub_title") or "").strip()
    event_title = str(raw.get("event_title") or "").strip()
    event_subtitle = str(raw.get("event_subtitle") or "").strip()
    return " ".join(x for x in [ticker, title, subtitle, event_title, event_subtitle] if x).strip()


def _detect_city(text: str) -> str | None:
    up = text.upper()
    for city, aliases in _CITY_ALIASES.items():
        if any(alias in up for alias in aliases):
            return city
    return None


def _extract_temperature_spec(text: str) -> TemperatureSpec | None:
    up = text.upper()

    bucket_match = _TEMP_TOKEN_B_PATTERN.search(up)
    if bucket_match:
        try:
            value = float(bucket_match.group(1))
            return TemperatureSpec(comparator="between", threshold_f=value, lower_f=value - 0.5, upper_f=value + 0.5)
        except ValueError:
            pass

    range_match = _TEMP_RANGE_PATTERN.search(up)
    if range_match:
        try:
            a = float(range_match.group(1))
            b = float(range_match.group(2))
            lo, hi = min(a, b), max(a, b)
            return TemperatureSpec(comparator="between", threshold_f=lo, lower_f=lo, upper_f=hi)
        except ValueError:
            pass

    lt_match = _TEMP_LT_PATTERN.search(up)
    if lt_match:
        try:
            value = float(lt_match.group(1))
            return TemperatureSpec(comparator="below", threshold_f=value)
        except ValueError:
            pass

    gt_match = _TEMP_GT_PATTERN.search(up)
    if gt_match:
        try:
            value = float(gt_match.group(1))
            return TemperatureSpec(comparator="above", threshold_f=value)
        except ValueError:
            pass

    token_t_match = _TEMP_TOKEN_T_PATTERN.search(up)
    if token_t_match:
        try:
            value = float(token_t_match.group(1))
            comparator = "below" if any(h in up for h in _TEMP_LOW_HINTS) else "above"
            return TemperatureSpec(comparator=comparator, threshold_f=value)
        except ValueError:
            pass

    f_match = _TEMP_F_PATTERN.search(up)
    if f_match:
        try:
            value = float(f_match.group(1))
            comparator = "below" if any(h in up for h in _TEMP_LOW_HINTS) else "above"
            return TemperatureSpec(comparator=comparator, threshold_f=value)
        except ValueError:
            pass

    return None


def classify_weather_strategy(raw: dict[str, Any]) -> str | None:
    blob = _market_blob(raw).upper()
    ticker = str(raw.get("ticker") or "").upper()
    event_ticker = str(raw.get("event_ticker") or "").upper()

    if any(k in blob for k in _PRECIP_KEYWORDS):
        return "weather_precip"
    if any(k in blob for k in _SNOW_KEYWORDS):
        return "weather_snow"
    if any(k in blob for k in _WIND_KEYWORDS):
        return "weather_wind"

    temp_context = any(k in blob for k in _TEMP_CONTEXT_KEYWORDS)
    temp_ticker_hint = bool(_TEMP_TICKER_HINT_PATTERN.search(ticker)) or bool(_TEMP_TICKER_HINT_PATTERN.search(event_ticker))
    weather_context = temp_context or temp_ticker_hint or ("WEATHER" in blob) or ("CLIMATE" in blob)

    has_temp_signal = (
        temp_context
        or bool(_TEMP_TOKEN_T_PATTERN.search(blob))
        or bool(_TEMP_TOKEN_B_PATTERN.search(blob))
        or bool(_TEMP_GT_PATTERN.search(blob))
        or bool(_TEMP_LT_PATTERN.search(blob))
        or bool(_TEMP_RANGE_PATTERN.search(blob))
        or bool(_TEMP_F_PATTERN.search(blob))
    )
    if not weather_context or not has_temp_signal:
        return None

    # Guard against non-weather "high/low" numeric markets leaking into temp variants.
    if not temp_context and not temp_ticker_hint:
        return None

    if any(h in blob for h in _TEMP_LOW_HINTS) or bool(_TEMP_LT_PATTERN.search(blob)):
        return "weather_temp_low"
    if any(h in blob for h in _TEMP_BUCKET_HINTS) or bool(_TEMP_RANGE_PATTERN.search(blob)) or bool(_TEMP_TOKEN_B_PATTERN.search(blob)):
        return "weather_temp_bucket"
    return "weather_temp_high"


def _extract_settlement_ts(raw: dict[str, Any]) -> datetime | None:
    for key in [
        "settlement_time",
        "settlement_ts",
        "close_time",
        "expected_expiration_time",
        "expiration_time",
        "latest_expiration_time",
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


def _parse_event_date_token(token: str) -> str | None:
    up = str(token).upper().strip()
    if len(up) != 7:
        return None
    try:
        yy = int(up[0:2])
        mon = _MONTHS[up[2:5]]
        day = int(up[5:7])
        year = 2000 + yy
        return datetime(year, mon, day).date().isoformat()
    except Exception:
        return None


def _extract_contract_date_local(raw: dict[str, Any], city_tz: str, settlement_ts: datetime | None) -> str | None:
    for key in ["event_ticker", "ticker"]:
        value = str(raw.get(key) or "")
        if not value:
            continue
        match = _EVENT_DATE_PATTERN.search(value.upper())
        if not match:
            continue
        parsed = _parse_event_date_token(match.group(1))
        if parsed:
            return parsed

    if settlement_ts is None:
        return None
    return settlement_ts.astimezone(ZoneInfo(city_tz)).date().isoformat()


def detect_supported_city(raw: dict[str, Any]) -> str | None:
    return _detect_city(_market_blob(raw))


def discover_weather_contracts(
    payload: dict[str, Any],
    *,
    allowed_strategy_ids: set[str] | None = None,
) -> tuple[list[ContractInfo], list[dict[str, Any]]]:
    markets = _extract_markets(payload)
    contracts: list[ContractInfo] = []
    skipped: list[dict[str, Any]] = []

    for raw in markets:
        ticker = str(raw.get("ticker") or "").strip()
        title = str(raw.get("title") or raw.get("subtitle") or raw.get("sub_title") or ticker).strip()
        blob = _market_blob(raw)

        strategy_id = classify_weather_strategy(raw)
        if not strategy_id:
            skipped.append({"ticker": ticker, "reason": "family_unmapped"})
            continue
        if allowed_strategy_ids and strategy_id not in allowed_strategy_ids:
            skipped.append({"ticker": ticker, "reason": "strategy_filtered", "strategy_id": strategy_id})
            continue

        city = detect_supported_city(raw)
        if not city:
            skipped.append({"ticker": ticker, "reason": "city_unmapped", "strategy_id": strategy_id})
            continue

        settlement_ts = _extract_settlement_ts(raw)
        if settlement_ts is None:
            skipped.append({"ticker": ticker, "reason": "settlement_time_missing", "strategy_id": strategy_id})
            continue

        city_tz = str(CITY_CONFIG[city]["tz"])
        contract_date_local = _extract_contract_date_local(raw, city_tz, settlement_ts)
        if not contract_date_local:
            skipped.append({"ticker": ticker, "reason": "contract_date_unparsed", "strategy_id": strategy_id})
            continue

        family = "temperature" if strategy_id.startswith("weather_temp") else "weather"
        comparator = "unknown"
        threshold_f = 0.0
        lower_f: float | None = None
        upper_f: float | None = None

        if family == "temperature":
            spec = _extract_temperature_spec(blob)
            if spec is None or spec.threshold_f is None:
                skipped.append({"ticker": ticker, "reason": "threshold_unparsed", "strategy_id": strategy_id})
                continue
            comparator = spec.comparator
            threshold_f = float(spec.threshold_f)
            lower_f = spec.lower_f
            upper_f = spec.upper_f

            if strategy_id == "weather_temp_low" and comparator == "above":
                comparator = "below"
            if strategy_id == "weather_temp_high" and comparator == "below":
                comparator = "above"

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
                strategy_id=strategy_id,
                family=family,
                comparator=comparator,
                lower_f=lower_f,
                upper_f=upper_f,
                raw=dict(raw),
            )
        )

    return contracts, skipped


def discover_temperature_contracts(payload: dict[str, Any]) -> tuple[list[ContractInfo], list[dict[str, Any]]]:
    return discover_weather_contracts(
        payload,
        allowed_strategy_ids={"weather_temp_high", "weather_temp_low", "weather_temp_bucket"},
    )


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
                "strategy_id": c.strategy_id,
                "family": c.family,
                "comparator": c.comparator,
                "lower_f": c.lower_f,
                "upper_f": c.upper_f,
            }
            for c in contracts
        ]
    )
