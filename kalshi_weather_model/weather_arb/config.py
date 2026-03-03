from __future__ import annotations

import os
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"
CONFIG_DIR = ROOT_DIR / "config"

CONTRACTS_DIR = DATA_DIR / "contracts"
MARKET_QUOTES_DIR = DATA_DIR / "market_quotes"
FORECAST_SNAPSHOTS_DIR = DATA_DIR / "forecast_snapshots"
OBSERVATIONS_DIR = DATA_DIR / "observations"
SIGNALS_DIR = DATA_DIR / "signals"

PAPER_DIR = DATA_DIR / "paper"
LIVE_DIR = DATA_DIR / "live"
GOVERNANCE_DIR = DATA_DIR / "governance"
EVAL_DIR = DATA_DIR / "eval"
REPORTS_DIR = DATA_DIR / "reports"

PAPER_POSITIONS_PATH = PAPER_DIR / "paper_positions.json"
PAPER_METRICS_DAILY_PATH = PAPER_DIR / "paper_metrics_daily.json"
PAPER_BLOTTER_DIR = PAPER_DIR / "paper_blotter"

LIVE_POSITIONS_PATH = LIVE_DIR / "live_positions.json"
LIVE_METRICS_DAILY_PATH = LIVE_DIR / "live_metrics_daily.json"
LIVE_BLOTTER_DIR = LIVE_DIR / "live_blotter"

MODEL_REGISTRY_PATH = GOVERNANCE_DIR / "model_registry.json"
LIFECYCLE_STATE_PATH = GOVERNANCE_DIR / "lifecycle_state.json"
GOVERNANCE_LOG_PATH = GOVERNANCE_DIR / "governance_log.json"
THRESHOLD_CONFIG_PATH = GOVERNANCE_DIR / "thresholds.json"

CONTRACTS_ACTIVE_PATH = CONTRACTS_DIR / "contracts_active.parquet"
CONTRACTS_HISTORY_PATH = CONTRACTS_DIR / "contracts_history.parquet"
CONTRACT_DISCOVERY_CACHE_PATH = CONTRACTS_DIR / "discovery_cache.json"


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return float(default)
    try:
        return float(raw)
    except ValueError:
        return float(default)


KALSHI_API_BASE_URL = os.getenv(
    "KALSHI_API_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2"
)
KALSHI_API_KEY = os.getenv("KALSHI_API_KEY", "")
KALSHI_RSA_KEY_PATH = os.getenv("KALSHI_RSA_KEY_PATH", "")
KALSHI_RSA_PRIVATE_KEY = os.getenv("KALSHI_RSA_PRIVATE_KEY", "")
NWS_USER_AGENT = os.getenv(
    "NWS_USER_AGENT", "kalshi-weather-model/1.0 (contact: local@example.com)"
)

ALLOW_LIVE_TRADING = _env_flag("ALLOW_LIVE_TRADING", False)

SCHEDULER_TZ = "America/New_York"
SCHEDULER_INTERVAL_MINUTES = 15
OBS_SYNC_INTERVAL_MINUTES = 60
SCHEDULER_SETTLEMENT_TIME = "00:20"
SCHEDULER_GATE_EVAL_TIME = "00:30"
SCHEDULER_GOVERNANCE_TIME = "00:40"
SCHEDULER_CALIBRATION_TIME = "23:30"

MARKET_DISCOVERY_MINUTES = 60
EVENT_SCAN_MAX_PAGES = 25
SETTLEMENT_CUTOFF_HOURS = 2
MAX_HOLD_HOURS = 12
OBS_LOOKBACK_DAYS = 120
OBS_MAX_PAGES = 30
OBS_PAGE_SIZE = 500

CITIES = ["NYC", "Chicago", "Dallas", "Miami", "Atlanta", "Seattle"]
CITY_CONFIG = {
    "NYC": {
        "station": "KNYC",
        "tz": "America/New_York",
        "lat": 40.7128,
        "lon": -74.0060,
    },
    "Chicago": {
        "station": "KORD",
        "tz": "America/Chicago",
        "lat": 41.9742,
        "lon": -87.9073,
    },
    "Dallas": {
        "station": "KDFW",
        "tz": "America/Chicago",
        "lat": 32.8998,
        "lon": -97.0403,
    },
    "Miami": {
        "station": "KMIA",
        "tz": "America/New_York",
        "lat": 25.7959,
        "lon": -80.2870,
    },
    "Atlanta": {
        "station": "KATL",
        "tz": "America/New_York",
        "lat": 33.6407,
        "lon": -84.4277,
    },
    "Seattle": {
        "station": "KSEA",
        "tz": "America/Los_Angeles",
        "lat": 47.4502,
        "lon": -122.3088,
    },
}

# Lifecycle / registry
MODEL_REGISTRY_SCHEMA_VERSION = "weather_v1"
SCOPE_KEYS = ["global"]

ALLOWED_STATUSES = {
    "training",
    "validating",
    "wf_passed",
    "backtest_passed",
    "qualified",
    "paper",
    "backup_standby",
    "champion_live",
    "retired",
    "failed",
}

TERMINAL_STATUSES = {"retired", "failed"}
TRANSITIONS: dict[str, set[str]] = {
    "training": {"validating", "failed"},
    "validating": {"wf_passed", "failed"},
    "wf_passed": {"backtest_passed", "retired", "failed"},
    "backtest_passed": {"qualified", "retired", "failed"},
    "qualified": {"paper", "retired", "failed"},
    "paper": {"champion_live", "retired", "failed"},
    "backup_standby": {"champion_live", "retired"},
    "champion_live": {"paper", "backup_standby", "retired", "failed"},
    "retired": set(),
    "failed": set(),
}

# Train / WF / backtest gates
TRAIN_MIN_OBSERVATIONS_PER_CITY = 90
TRAIN_MAX_MISSING_PCT = 0.10

WF_MIN_WINDOWS = 6
WF_MIN_FEASIBLE_RATE = 0.60
WF_MIN_MEDIAN_EV_DAY = 0.0

BACKTEST_MIN_TRADES = 30
BACKTEST_MIN_WIN_RATE = 0.54
BACKTEST_MIN_ROI_PER_TRADE = 0.0
BACKTEST_MIN_EV_DAY = 0.0
BACKTEST_MAX_DRAWDOWN = 0.20

# Paper gate (Hybrid strict)
MIN_PAPER_TRADING_DAYS = 20
MIN_PAPER_TRADES = 30
MIN_PAPER_WIN_RATE = 0.55
MIN_PAPER_AVG_DAILY_PNL = 0.0
MAX_PAPER_DRAWDOWN = -0.06
MIN_PAPER_ROI_PER_TRADE = 0.0
PAPER_FAILURE_MAX_EVALS = 3

# Degradation / rollback
ROLLBACK_MIN_TRADES_FOR_EVAL = 20
ROLLBACK_WIN_RATE_DEGRADATION_THRESHOLD = 0.10
ROLLBACK_ROI_DEGRADATION_THRESHOLD = 0.20
ROLLBACK_DRAWDOWN_INCREASE_THRESHOLD = 0.15
ROLLBACK_CONSECUTIVE_FAILURES = 3

# Signal and thresholding
BOOTSTRAP_MIN_CLOSED_TRADES_PER_CITY = 30
BOOTSTRAP_MIN_EV_CENTS = 6.0
CALIBRATION_MIN_EV_CENTS = 2.0
CALIBRATION_MAX_EV_CENTS = 15.0
CALIBRATION_STEP_EV_CENTS = 0.5
EXIT_EV_CENTS = 1.5

SEASONAL_BANDWIDTH_DAYS = 30.0
MIN_SEASONAL_SAMPLES = 30
RESIDUAL_LOOKBACK_DAYS = 365
RESIDUAL_LEAD_MIN_HOURS = 24.0
RESIDUAL_LEAD_MAX_HOURS = 48.0
RESIDUAL_TARGET_LEAD_HOURS = 36.0

# Market quality
MAX_SPREAD_PCT = 0.15
MIN_BOOK_SIZE = 5

# Fees/slippage
KALSHI_FEE_PER_CONTRACT_DOLLARS = 0.02
DEFAULT_SLIPPAGE_CENTS = 0.5

# Paper risk
PAPER_ACCOUNT_SIZE = 1_000.0
PAPER_MAX_POSITION_DOLLARS = 5.0
PAPER_MAX_CONCURRENT_POSITIONS = 6
PAPER_DAILY_LOSS_STOP_DOLLARS = 10.0
PAPER_WEEKLY_LOSS_STOP_DOLLARS = 30.0
PAPER_CONSECUTIVE_LOSS_HALT = 3

# Live risk hybrid scaling
LIVE_FIXED_TIERS = [
    {"min_equity": 0.0, "max_equity": 99.9999, "max_position": 5.0, "daily_stop": 10.0, "max_concurrent": 3},
    {"min_equity": 100.0, "max_equity": 249.9999, "max_position": 8.0, "daily_stop": 15.0, "max_concurrent": 3},
    {"min_equity": 250.0, "max_equity": 499.9999, "max_position": 12.0, "daily_stop": 25.0, "max_concurrent": 4},
    {"min_equity": 500.0, "max_equity": 500.0, "max_position": 20.0, "daily_stop": 40.0, "max_concurrent": 5},
]
LIVE_PERCENT_SWITCH_EQUITY = 500.0
LIVE_MAX_POSITION_PCT = 0.03
LIVE_DAILY_STOP_PCT = 0.06
LIVE_MIN_MAX_POSITION_DOLLARS = 20.0
LIVE_MIN_DAILY_STOP_DOLLARS = 40.0
LIVE_WEEKLY_STOP_MULTIPLIER = 2.0
LIVE_WEEKLY_STOP_MIN_DOLLARS = 20.0
LIVE_MAX_CONCURRENT_CAP = 5
LIVE_STARTING_EQUITY = _env_float("LIVE_STARTING_EQUITY", 50.0)
LIVE_EQUITY_SYNC_ENABLED = _env_flag("LIVE_EQUITY_SYNC_ENABLED", True)
LIVE_CONSECUTIVE_LOSS_HALT = 3

# Live portfolio notional cap (fraction of account equity)
LIVE_MAX_NOTIONAL_UTILIZATION = _env_float("LIVE_MAX_NOTIONAL_UTILIZATION", 0.60)


def ensure_dirs() -> None:
    for path in [
        DATA_DIR,
        CONFIG_DIR,
        CONTRACTS_DIR,
        MARKET_QUOTES_DIR,
        FORECAST_SNAPSHOTS_DIR,
        OBSERVATIONS_DIR,
        SIGNALS_DIR,
        PAPER_DIR,
        LIVE_DIR,
        GOVERNANCE_DIR,
        EVAL_DIR,
        REPORTS_DIR,
        PAPER_BLOTTER_DIR,
        LIVE_BLOTTER_DIR,
    ]:
        path.mkdir(parents=True, exist_ok=True)
