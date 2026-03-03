from __future__ import annotations

from datetime import datetime
from typing import Any

from weather_arb.config import KALSHI_FEE_PER_CONTRACT_DOLLARS


def payout_per_contract(side: str, settled_yes: bool) -> float:
    side_l = side.lower()
    if side_l == "buy_yes":
        return 1.0 if settled_yes else 0.0
    if side_l == "buy_no":
        return 1.0 if not settled_yes else 0.0
    raise ValueError(f"unsupported side: {side}")


def realized_pnl_from_settlement(
    side: str,
    entry_price_dollars: float,
    contracts: int,
    settled_yes: bool,
    fee_per_contract: float = KALSHI_FEE_PER_CONTRACT_DOLLARS,
) -> float:
    payout = payout_per_contract(side, settled_yes)
    gross = (payout - entry_price_dollars) * contracts
    fees = fee_per_contract * contracts
    return gross - fees


def apply_settlements_to_positions(
    open_positions: list[dict[str, Any]],
    settlements: dict[str, bool],
    ts_utc: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    still_open: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []

    for pos in open_positions:
        ticker = str(pos.get("ticker", ""))
        if ticker not in settlements:
            still_open.append(pos)
            continue

        settled_yes = bool(settlements[ticker])
        contracts = int(pos.get("contracts", 0) or 0)
        entry_price = float(pos.get("entry_price_dollars", 0.0) or 0.0)
        side = str(pos.get("side", ""))
        pnl = realized_pnl_from_settlement(
            side=side,
            entry_price_dollars=entry_price,
            contracts=contracts,
            settled_yes=settled_yes,
        )

        out = dict(pos)
        out["status"] = "closed"
        out["closed_at_utc"] = ts_utc.isoformat()
        out["close_price_dollars"] = 1.0 if payout_per_contract(side, settled_yes) > 0 else 0.0
        out["realized_pnl_dollars"] = float(pnl)
        out["close_reason"] = "settlement"
        closed.append(out)

    return still_open, closed


def parse_settlements_payload(payload: dict[str, Any]) -> dict[str, bool]:
    out: dict[str, bool] = {}
    rows = payload.get("settlements") if isinstance(payload.get("settlements"), list) else payload.get("data")
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or row.get("market_ticker") or "").strip()
        if not ticker:
            continue
        yes = row.get("yes_settled")
        if yes is None:
            # Fallback to price-based resolution if explicit bool absent.
            settlement_price = row.get("yes_settlement_price_dollars")
            if settlement_price is None:
                settlement_price = row.get("yes_settlement_price")
            if settlement_price is None:
                continue
            try:
                yes = float(settlement_price) >= 0.5
            except Exception:
                continue
        out[ticker] = bool(yes)
    return out
