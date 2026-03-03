from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from weather_arb.config import (
    KALSHI_API_BASE_URL,
    KALSHI_API_KEY,
    KALSHI_RSA_KEY_PATH,
    KALSHI_RSA_PRIVATE_KEY,
)


class KalshiAPIError(RuntimeError):
    pass


@dataclass(slots=True)
class KalshiCredentials:
    api_key: str
    rsa_private_key_path: str = ""
    rsa_private_key_pem: str = ""


class KalshiPublicClient:
    def __init__(
        self,
        base_url: str = KALSHI_API_BASE_URL,
        session: requests.Session | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout_seconds = timeout_seconds

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        max_retries: int = 3,
    ) -> dict[str, Any]:
        method_u = method.upper()
        path = path if path.startswith("/") else f"/{path}"
        url = f"{self.base_url}{path}"
        req_headers = {"Content-Type": "application/json"}
        if headers:
            req_headers.update(headers)

        for attempt in range(max_retries + 1):
            resp = self.session.request(
                method_u,
                url,
                params=params,
                json=json_body,
                headers=req_headers,
                timeout=self.timeout_seconds,
            )
            if resp.status_code in {429, 500, 502, 503, 504} and attempt < max_retries:
                sleep_s = (2 ** attempt) * 0.25
                time.sleep(sleep_s)
                continue
            if resp.status_code >= 400:
                raise KalshiAPIError(f"{method_u} {path} failed [{resp.status_code}]: {resp.text[:500]}")
            try:
                return resp.json()
            except Exception:
                return {"raw_text": resp.text}

        raise KalshiAPIError(f"{method_u} {path} exhausted retries")

    def get_markets(self, *, status: str | None = None, limit: int = 200) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        return self._request("GET", "/markets", params=params)

    def get_market(self, ticker: str) -> dict[str, Any]:
        return self._request("GET", f"/markets/{ticker}")

    def get_market_orderbook(self, ticker: str) -> dict[str, Any]:
        return self._request("GET", f"/markets/{ticker}/orderbook")

    def get_events(
        self,
        *,
        status: str | None = None,
        limit: int = 200,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/events", params=params)

    def get_event(self, event_ticker: str) -> dict[str, Any]:
        return self._request("GET", f"/events/{event_ticker}")


class KalshiAuthClient(KalshiPublicClient):
    def __init__(
        self,
        credentials: KalshiCredentials | None = None,
        base_url: str = KALSHI_API_BASE_URL,
        session: requests.Session | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        super().__init__(base_url=base_url, session=session, timeout_seconds=timeout_seconds)
        creds = credentials or KalshiCredentials(
            api_key=KALSHI_API_KEY,
            rsa_private_key_path=KALSHI_RSA_KEY_PATH,
            rsa_private_key_pem=KALSHI_RSA_PRIVATE_KEY,
        )
        if not creds.api_key:
            raise ValueError("KALSHI_API_KEY is required for auth client")
        if not creds.rsa_private_key_path and not creds.rsa_private_key_pem:
            raise ValueError("KALSHI_RSA_KEY_PATH or KALSHI_RSA_PRIVATE_KEY is required for auth client")
        self.credentials = creds
        parsed = urlparse(self.base_url)
        self._api_base_path = parsed.path.rstrip("/")
        self._private_key = self._load_private_key(
            rsa_private_key_path=creds.rsa_private_key_path,
            rsa_private_key_pem=creds.rsa_private_key_pem,
        )

    @staticmethod
    def _load_private_key(*, rsa_private_key_path: str, rsa_private_key_pem: str):
        pem_text = str(rsa_private_key_pem or "").strip()
        path_text = str(rsa_private_key_path or "").strip()
        payload: bytes

        if pem_text:
            payload = pem_text.replace("\\n", "\n").encode("utf-8")
        elif "BEGIN" in path_text and "PRIVATE KEY" in path_text:
            payload = path_text.replace("\\n", "\n").encode("utf-8")
        else:
            path = Path(path_text)
            if not path.exists():
                raise FileNotFoundError(f"Kalshi private key file not found: {path}")
            payload = path.read_bytes()
        return load_pem_private_key(payload, password=None)

    @staticmethod
    def canonical_signing_path(
        path: str,
        params: dict[str, Any] | None = None,
        api_base_path: str = "",
    ) -> str:
        base = path if path.startswith("/") else f"/{path}"
        prefix = str(api_base_path or "").rstrip("/")
        full_path = f"{prefix}{base}" if prefix else base
        if not params:
            return full_path
        # Kalshi signing path does not include query params.
        return full_path

    def build_signature(self, timestamp_ms: str, method: str, signing_path: str) -> str:
        message = f"{timestamp_ms}{method.upper()}{signing_path}".encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _auth_headers(self, method: str, path: str, params: dict[str, Any] | None = None) -> dict[str, str]:
        timestamp_ms = str(int(time.time() * 1000))
        signing_path = self.canonical_signing_path(
            path,
            params=params,
            api_base_path=self._api_base_path,
        )
        signature = self.build_signature(timestamp_ms, method, signing_path)
        return {
            "KALSHI-ACCESS-KEY": self.credentials.api_key,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": signature,
        }

    def auth_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        max_retries: int = 3,
    ) -> dict[str, Any]:
        headers = self._auth_headers(method, path, params=params)
        return self._request(
            method,
            path,
            params=params,
            json_body=json_body,
            headers=headers,
            max_retries=max_retries,
        )

    def get_positions(self) -> dict[str, Any]:
        return self.auth_request("GET", "/portfolio/positions")

    def get_settlements(self, *, limit: int = 200) -> dict[str, Any]:
        return self.auth_request("GET", "/portfolio/settlements", params={"limit": limit})

    def place_order(
        self,
        *,
        ticker: str,
        side: str,
        count: int,
        yes_price_dollars: float | None = None,
        no_price_dollars: float | None = None,
        order_type: str = "limit",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ticker": ticker,
            "action": side,
            "count": count,
            "type": order_type,
        }
        if yes_price_dollars is not None:
            payload["yes_price_dollars"] = yes_price_dollars
        if no_price_dollars is not None:
            payload["no_price_dollars"] = no_price_dollars
        return self.auth_request("POST", "/portfolio/orders", json_body=payload)


def parse_dollar_orderbook(payload: dict[str, Any], ticker: str) -> dict[str, Any]:
    """Parse orderbook payload into normalized dollar-denominated top-of-book fields.

    Supports both:
    1) direct top-of-book fields (`yes_bid_dollars`, `yes_ask_dollars`, ...)
    2) ladder payloads (`orderbook.yes_dollars`, `orderbook.no_dollars`, with cent fallback)
    """
    candidates = []
    if isinstance(payload, dict):
        candidates.append(payload)
        if isinstance(payload.get("orderbook"), dict):
            candidates.append(payload["orderbook"])

    required = [
        "yes_bid_dollars",
        "yes_ask_dollars",
        "no_bid_dollars",
        "no_ask_dollars",
    ]
    chosen: dict[str, Any] | None = None
    for obj in candidates:
        if all(k in obj for k in required):
            chosen = obj
            break

    if chosen is None:
        def _levels(raw: Any, *, cents: bool) -> list[tuple[float, int]]:
            out: list[tuple[float, int]] = []
            if not isinstance(raw, list):
                return out
            for row in raw:
                if not isinstance(row, (list, tuple)) or len(row) < 2:
                    continue
                try:
                    price = float(row[0])
                    size = int(float(row[1]))
                except Exception:
                    continue
                if cents:
                    price = price / 100.0
                if price < 0:
                    continue
                out.append((price, max(size, 0)))
            return out

        def _best_bid(levels: list[tuple[float, int]]) -> tuple[float, int]:
            if not levels:
                return 0.0, 0
            price, size = max(levels, key=lambda x: x[0])
            return float(price), int(size)

        yes_levels: list[tuple[float, int]] = []
        no_levels: list[tuple[float, int]] = []
        for obj in candidates:
            if not yes_levels:
                yes_levels = _levels(obj.get("yes_dollars"), cents=False)
            if not yes_levels:
                yes_levels = _levels(obj.get("yes"), cents=True)
            if not no_levels:
                no_levels = _levels(obj.get("no_dollars"), cents=False)
            if not no_levels:
                no_levels = _levels(obj.get("no"), cents=True)

        yes_bid, yes_bid_size = _best_bid(yes_levels)
        no_bid, no_bid_size = _best_bid(no_levels)
        yes_ask = (1.0 - no_bid) if no_bid > 0 else 1.0
        no_ask = (1.0 - yes_bid) if yes_bid > 0 else 1.0
        yes_ask = max(min(float(yes_ask), 1.0), 0.0)
        no_ask = max(min(float(no_ask), 1.0), 0.0)
        yes_ask = max(yes_ask, yes_bid)
        no_ask = max(no_ask, no_bid)

        if (yes_bid <= 0 and no_bid <= 0) and (yes_ask >= 1.0 and no_ask >= 1.0):
            raise ValueError("Orderbook payload missing supported top-of-book and ladder fields.")

        return {
            "ticker": ticker,
            "yes_bid_dollars": yes_bid,
            "yes_ask_dollars": yes_ask,
            "no_bid_dollars": no_bid,
            "no_ask_dollars": no_ask,
            "yes_bid_size": yes_bid_size,
            "yes_ask_size": no_bid_size if no_bid > 0 else 0,
            "no_bid_size": no_bid_size,
            "no_ask_size": yes_bid_size if yes_bid > 0 else 0,
        }

    def _int(name: str) -> int:
        return int(chosen.get(name, 0) or 0)

    return {
        "ticker": ticker,
        "yes_bid_dollars": float(chosen["yes_bid_dollars"]),
        "yes_ask_dollars": float(chosen["yes_ask_dollars"]),
        "no_bid_dollars": float(chosen["no_bid_dollars"]),
        "no_ask_dollars": float(chosen["no_ask_dollars"]),
        "yes_bid_size": _int("yes_bid_size"),
        "yes_ask_size": _int("yes_ask_size"),
        "no_bid_size": _int("no_bid_size"),
        "no_ask_size": _int("no_ask_size"),
    }
