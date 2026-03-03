from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time
from typing import Any

import requests

from weather_arb.config import NWS_USER_AGENT


class NOAAAPIError(RuntimeError):
    pass


class NOAAClient:
    def __init__(
        self,
        session: requests.Session | None = None,
        user_agent: str = NWS_USER_AGENT,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout_seconds = timeout_seconds
        self.headers = {
            "User-Agent": user_agent,
            "Accept": "application/geo+json",
        }

    def _request(self, url: str, *, max_retries: int = 3) -> dict[str, Any]:
        for attempt in range(max_retries + 1):
            resp = self.session.get(url, headers=self.headers, timeout=self.timeout_seconds)
            if resp.status_code in {429, 500, 502, 503, 504} and attempt < max_retries:
                time.sleep((2 ** attempt) * 0.25)
                continue
            if resp.status_code >= 400:
                raise NOAAAPIError(f"GET {url} failed [{resp.status_code}]: {resp.text[:500]}")
            return resp.json()
        raise NOAAAPIError(f"GET {url} exhausted retries")

    def get_point_metadata(self, lat: float, lon: float) -> dict[str, Any]:
        return self._request(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}")

    def get_hourly_forecast(self, lat: float, lon: float) -> dict[str, Any]:
        meta = self.get_point_metadata(lat, lon)
        forecast_url = (
            meta.get("properties", {}).get("forecastHourly")
            or meta.get("properties", {}).get("forecast")
        )
        if not forecast_url:
            raise NOAAAPIError("point metadata missing forecast URL")
        return self._request(str(forecast_url))

    def get_station_observations(self, station_id: str, *, limit: int = 500) -> dict[str, Any]:
        url = f"https://api.weather.gov/stations/{station_id}/observations?limit={int(limit)}"
        return self._request(url)

    @staticmethod
    def _parse_obs_ts(value: Any) -> datetime | None:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def get_station_observations_history(
        self,
        station_id: str,
        *,
        lookback_days: int,
        page_limit: int = 30,
        page_size: int = 500,
    ) -> dict[str, Any]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(lookback_days)))
        url = f"https://api.weather.gov/stations/{station_id}/observations?limit={int(page_size)}"

        features: list[dict[str, Any]] = []
        pages = 0
        while pages < max(1, int(page_limit)):
            data = self._request(url)
            rows = data.get("features", [])
            if not isinstance(rows, list) or not rows:
                break
            features.extend([r for r in rows if isinstance(r, dict)])
            pages += 1

            oldest_ts: datetime | None = None
            for feat in rows:
                ts = self._parse_obs_ts((feat.get("properties") or {}).get("timestamp"))
                if ts is None:
                    continue
                oldest_ts = ts if oldest_ts is None else min(oldest_ts, ts)
            if oldest_ts is not None and oldest_ts <= cutoff:
                break

            next_url = ((data.get("pagination") or {}).get("next"))
            if not next_url:
                break
            url = str(next_url)

        filtered: list[dict[str, Any]] = []
        for feat in features:
            ts = self._parse_obs_ts((feat.get("properties") or {}).get("timestamp"))
            if ts is None or ts < cutoff:
                continue
            filtered.append(feat)

        return {
            "type": "FeatureCollection",
            "features": filtered,
            "meta": {
                "cutoff_utc": cutoff.isoformat(),
                "pages_fetched": pages,
            },
        }
