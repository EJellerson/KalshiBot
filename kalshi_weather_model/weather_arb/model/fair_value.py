from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from weather_arb.config import MIN_SEASONAL_SAMPLES, SEASONAL_BANDWIDTH_DAYS


@dataclass(slots=True)
class ResidualSample:
    day_of_year: int
    residual_f: float


def _normal_cdf(x: float, mean: float, std: float) -> float:
    if std <= 1e-9:
        return 1.0 if x >= mean else 0.0
    z = (x - mean) / (std * math.sqrt(2.0))
    return 0.5 * (1.0 + math.erf(z))


def _circular_day_distance(a: int, b: int) -> int:
    raw = abs(a - b)
    return min(raw, 366 - raw)


def _gaussian_weight(distance_days: int, bandwidth_days: float) -> float:
    if bandwidth_days <= 0:
        return 0.0
    return math.exp(-0.5 * (distance_days / bandwidth_days) ** 2)


def _weighted_mean_std(values: list[float], weights: list[float]) -> tuple[float, float]:
    if not values or not weights or len(values) != len(weights):
        return 0.0, 1.0
    w_sum = sum(weights)
    if w_sum <= 1e-12:
        return 0.0, 1.0
    mean = sum(v * w for v, w in zip(values, weights)) / w_sum
    var = sum(w * (v - mean) ** 2 for v, w in zip(values, weights)) / w_sum
    std = math.sqrt(max(var, 1e-6))
    return mean, std


class SeasonalResidualModel:
    def __init__(self, bandwidth_days: float = SEASONAL_BANDWIDTH_DAYS) -> None:
        self.bandwidth_days = bandwidth_days
        self.by_city: dict[str, list[ResidualSample]] = {}
        self.pooled: list[ResidualSample] = []

    def fit(self, rows: list[dict[str, Any]]) -> None:
        by_city: dict[str, list[ResidualSample]] = {}
        pooled: list[ResidualSample] = []

        for row in rows:
            city = str(row.get("city") or "").strip()
            if not city:
                continue
            forecast = row.get("forecast_temp_f")
            actual = row.get("actual_temp_f")
            target = row.get("target_date")
            if forecast is None or actual is None or target is None:
                continue
            try:
                forecast_f = float(forecast)
                actual_f = float(actual)
                day = self._day_of_year(target)
            except Exception:
                continue

            sample = ResidualSample(day_of_year=day, residual_f=(actual_f - forecast_f))
            by_city.setdefault(city, []).append(sample)
            pooled.append(sample)

        self.by_city = by_city
        self.pooled = pooled

    @staticmethod
    def _day_of_year(target: str | date | datetime) -> int:
        if isinstance(target, datetime):
            return int(target.timetuple().tm_yday)
        if isinstance(target, date):
            return int(target.timetuple().tm_yday)
        dt = datetime.fromisoformat(str(target))
        return int(dt.timetuple().tm_yday)

    def _distribution(self, city: str, target_day: int) -> tuple[float, float, int]:
        city_samples = list(self.by_city.get(city, []))
        samples = city_samples if len(city_samples) >= MIN_SEASONAL_SAMPLES else list(self.pooled)
        if not samples:
            return 0.0, 5.0, 0

        values: list[float] = []
        weights: list[float] = []
        for sample in samples:
            dist = _circular_day_distance(target_day, sample.day_of_year)
            w = _gaussian_weight(dist, self.bandwidth_days)
            values.append(sample.residual_f)
            weights.append(w)

        mean, std = _weighted_mean_std(values, weights)
        return mean, max(std, 1.0), len(samples)

    def p_exceeds(self, city: str, forecast_temp_f: float, threshold_f: float, target_date: str | date | datetime) -> float:
        target_day = self._day_of_year(target_date)
        mean_resid, std_resid, _ = self._distribution(city, target_day)
        mean_temp = forecast_temp_f + mean_resid
        p_leq = _normal_cdf(threshold_f, mean_temp, std_resid)
        return max(0.0, min(1.0, 1.0 - p_leq))


def market_probability_from_price(side: str, yes_bid: float, yes_ask: float, no_bid: float, no_ask: float) -> float:
    side_l = side.lower()
    if side_l == "buy_yes":
        return max(0.0, min(1.0, yes_ask))
    if side_l == "buy_no":
        # Buying NO is economically equivalent to YES probability being 1 - no_ask
        return max(0.0, min(1.0, 1.0 - no_ask))
    raise ValueError(f"unsupported side: {side}")


def compute_ev_cents(p_fair: float, p_market: float, est_cost_cents: float) -> float:
    return ((p_fair - p_market) * 100.0) - est_cost_cents


def _parse_iso_utc(value: Any) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def build_residual_training_rows(
    forecast_rows: list[dict[str, Any]],
    observation_rows: list[dict[str, Any]],
    city_timezones: dict[str, str],
    *,
    lead_min_hours: float = 24.0,
    lead_max_hours: float = 48.0,
    target_lead_hours: float = 36.0,
    lookback_days: int = 365,
    as_of_utc: datetime | None = None,
) -> list[dict[str, Any]]:
    now_utc = as_of_utc or datetime.now(timezone.utc)
    cutoff_date = now_utc.date().toordinal() - int(max(0, lookback_days))

    obs_map: dict[tuple[str, str], tuple[float, datetime]] = {}
    for row in observation_rows:
        city = str(row.get("city", "")).strip()
        day_key = str(row.get("obs_date_local", "")).strip()
        if not city or not day_key:
            continue
        try:
            actual = float(row.get("max_temp_f"))
            observed_ts = _parse_iso_utc(row.get("observed_at_utc"))
            day_ord = datetime.fromisoformat(day_key).date().toordinal()
        except Exception:
            continue
        if day_ord < cutoff_date:
            continue
        key = (city, day_key)
        prev = obs_map.get(key)
        if prev is None or observed_ts > prev[1]:
            obs_map[key] = (actual, observed_ts)

    # Aggregate hourly forecast rows into one daily max per forecast vintage.
    vintage_daily: dict[tuple[str, str, str], dict[str, float | str | datetime]] = {}
    for row in forecast_rows:
        city = str(row.get("city", "")).strip()
        if not city or city not in city_timezones:
            continue
        try:
            fetched_at = _parse_iso_utc(row.get("fetched_at_utc"))
            forecast_at = _parse_iso_utc(row.get("forecast_time_utc"))
            temp_f = float(row.get("temperature_f"))
        except Exception:
            continue

        lead_hours = (forecast_at - fetched_at).total_seconds() / 3600.0
        if lead_hours < lead_min_hours or lead_hours > lead_max_hours:
            continue

        tz_name = city_timezones[city]
        target_day = forecast_at.astimezone(ZoneInfo(tz_name)).date().isoformat()
        try:
            day_ord = datetime.fromisoformat(target_day).date().toordinal()
        except Exception:
            continue
        if day_ord < cutoff_date:
            continue

        vkey = (city, target_day, fetched_at.isoformat())
        entry = vintage_daily.get(vkey)
        if entry is None:
            vintage_daily[vkey] = {
                "city": city,
                "target_day": target_day,
                "fetched_at": fetched_at,
                "max_temp_f": temp_f,
                "lead_sum": lead_hours,
                "lead_count": 1.0,
            }
        else:
            entry["max_temp_f"] = max(float(entry["max_temp_f"]), temp_f)
            entry["lead_sum"] = float(entry["lead_sum"]) + lead_hours
            entry["lead_count"] = float(entry["lead_count"]) + 1.0

    # Choose one representative forecast vintage per city/day.
    chosen: dict[tuple[str, str], dict[str, float | str | datetime]] = {}
    for entry in vintage_daily.values():
        city = str(entry["city"])
        day_key = str(entry["target_day"])
        avg_lead = float(entry["lead_sum"]) / max(float(entry["lead_count"]), 1.0)
        score = abs(avg_lead - target_lead_hours)
        key = (city, day_key)
        current = chosen.get(key)
        if current is None:
            chosen[key] = {**entry, "score": score}
            continue
        current_score = float(current.get("score", 1e9))
        current_fetched = current.get("fetched_at")
        new_fetched = entry.get("fetched_at")
        choose_new = False
        if score < current_score:
            choose_new = True
        elif score == current_score and isinstance(new_fetched, datetime) and isinstance(current_fetched, datetime):
            choose_new = new_fetched > current_fetched
        if choose_new:
            chosen[key] = {**entry, "score": score}

    rows: list[dict[str, Any]] = []
    for (city, day_key), entry in chosen.items():
        actual_pair = obs_map.get((city, day_key))
        if actual_pair is None:
            continue
        rows.append(
            {
                "city": city,
                "forecast_temp_f": float(entry["max_temp_f"]),
                "actual_temp_f": float(actual_pair[0]),
                "target_date": day_key,
            }
        )

    rows.sort(key=lambda r: (str(r.get("city", "")), str(r.get("target_date", ""))))
    return rows
