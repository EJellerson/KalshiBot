from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class ContractInfo:
    market_id: str
    ticker: str
    title: str
    city: str
    threshold_f: float
    settlement_ts_utc: datetime
    contract_date_local: str
    status: str
    strategy_id: str = "weather_temp_high"
    family: str = "temperature"
    comparator: str = "above"  # above | below | between | unknown
    lower_f: float | None = None
    upper_f: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MarketQuote:
    ticker: str
    ts_utc: datetime
    yes_bid_dollars: float
    yes_ask_dollars: float
    no_bid_dollars: float
    no_ask_dollars: float
    yes_bid_size: int
    yes_ask_size: int
    no_bid_size: int
    no_ask_size: int


@dataclass(slots=True)
class ForecastSnapshot:
    city: str
    fetched_at_utc: datetime
    forecast_time_utc: datetime
    temperature_f: float
    source: str = "nws"


@dataclass(slots=True)
class ObservationRecord:
    city: str
    obs_date_local: str
    max_temp_f: float
    observed_at_utc: datetime


@dataclass(slots=True)
class FairValueSignal:
    ticker: str
    city: str
    side: str  # buy_yes | buy_no
    p_fair: float
    p_mkt: float
    ev_cents: float
    threshold_f: float
    generated_at_utc: datetime


@dataclass(slots=True)
class OrderIntent:
    ticker: str
    side: str
    contracts: int
    limit_price_dollars: float
    reason: str
    generated_at_utc: datetime


@dataclass(slots=True)
class Fill:
    fill_id: str
    ticker: str
    side: str
    contracts: int
    price_dollars: float
    fees_dollars: float
    ts_utc: datetime
    mode: str  # paper | live


@dataclass(slots=True)
class PositionState:
    position_id: str
    ticker: str
    city: str
    side: str
    contracts: int
    entry_price_dollars: float
    entry_fees_dollars: float
    opened_at_utc: datetime
    status: str
    max_hold_until_utc: datetime
    settlement_ts_utc: datetime
    realized_pnl_dollars: float = 0.0
    close_price_dollars: float | None = None
    closed_at_utc: datetime | None = None


@dataclass(slots=True)
class DailyMetrics:
    date_key: str
    mode: str
    trades: int
    wins: int
    losses: int
    win_rate: float
    roi_per_trade: float
    pnl_dollars: float
    avg_daily_pnl: float
    max_drawdown: float


@dataclass(slots=True)
class GovernanceDecision:
    model_id: str
    from_status: str
    to_status: str
    reasons: list[str]
    ts_utc: datetime


@dataclass(slots=True)
class RiskLimits:
    max_position_dollars: float
    daily_loss_stop_dollars: float
    weekly_loss_stop_dollars: float
    max_concurrent_positions: int
    consecutive_loss_halt: int
