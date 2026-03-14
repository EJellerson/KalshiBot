from __future__ import annotations

from decimal import Decimal, ROUND_CEILING

from weather_arb import config
from weather_arb.risk.limits import contracts_for_notional


_ONE_CENT = Decimal("0.01")
_ONE_DOLLAR = Decimal("1")


def kalshi_trading_fee_dollars(contracts: int, price_dollars: float, *, maker: bool = False) -> float:
    count = max(int(contracts), 0)
    if count <= 0:
        return 0.0

    price = max(0.0, min(float(price_dollars), 1.0))
    rate = config.KALSHI_MAKER_FEE_RATE if maker else config.KALSHI_TAKER_FEE_RATE

    raw_fee = Decimal(str(rate)) * Decimal(count) * Decimal(str(price)) * (_ONE_DOLLAR - Decimal(str(price)))
    return float(raw_fee.quantize(_ONE_CENT, rounding=ROUND_CEILING))


def kalshi_fee_per_contract_dollars(contracts: int, price_dollars: float, *, maker: bool = False) -> float:
    count = max(int(contracts), 0)
    if count <= 0:
        return 0.0
    return kalshi_trading_fee_dollars(count, price_dollars, maker=maker) / float(count)


def estimated_entry_cost_cents(
    price_dollars: float,
    *,
    max_position_dollars: float,
    available_contracts: int | None = None,
    slippage_cents: float = 0.0,
    maker: bool = False,
) -> float:
    requested_contracts = contracts_for_notional(float(price_dollars), float(max_position_dollars))
    if requested_contracts <= 0:
        return float(slippage_cents)

    if available_contracts is None or int(available_contracts) <= 0:
        fee_contracts = requested_contracts
    else:
        fee_contracts = min(requested_contracts, int(available_contracts))

    fee_cents = kalshi_fee_per_contract_dollars(fee_contracts, float(price_dollars), maker=maker) * 100.0
    return fee_cents + float(slippage_cents)


def split_entry_fees_dollars(
    entry_fees_dollars: float,
    total_contracts: int,
    closed_contracts: int,
) -> tuple[float, float]:
    total = max(int(total_contracts), 0)
    closed = min(max(int(closed_contracts), 0), total)
    if total <= 0 or closed <= 0:
        return 0.0, float(entry_fees_dollars or 0.0)

    entry_fees = float(entry_fees_dollars or 0.0)
    closed_fees = entry_fees * (closed / float(total))
    remaining_fees = max(entry_fees - closed_fees, 0.0)
    return closed_fees, remaining_fees
