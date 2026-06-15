# Kalshi Weather Model

Self-contained weather mean-reversion pipeline for Kalshi, built to run entirely inside this repository.

## Safety
- Paper-first by default.
- Live routing is gated behind `ALLOW_LIVE_TRADING=1`.
- Status path mirrors staged promotion:
  `training -> validating -> wf_passed -> backtest_passed -> qualified -> paper -> champion_live`

## Quick start
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m weather_arb bootstrap
```

## Core commands
```bash
python -m weather_arb api-check
python -m weather_arb ingest-forecasts
python -m weather_arb sync-observations
python -m weather_arb discover-contracts
python -m weather_arb ingest --start 2026-03-01 --end 2026-03-03
python -m weather_arb run-train-gate
python -m weather_arb run-wf-gate
python -m weather_arb run-backtest-gate
python -m weather_arb run-daily-gates
python -m weather_arb paper-cycle
python -m weather_arb governance-eval
python -m weather_arb live-cycle
python -m weather_arb report --date 2026-03-03
python -m weather_arb dashboard --host 127.0.0.1 --port 8077
```

## Dashboard monitoring
- Web UI: `http://127.0.0.1:8077`
- Main API payload: `GET /api/monitoring`
- Dashboard now includes:
  - train-gate progress by city (`observations`, `missing%`, `days_remaining`)
  - estimated train-ready date in local scheduler timezone
  - stream freshness (`forecasts/observations/quotes/signals`) with stale detection
  - scheduler heartbeat (minutes since key events)
  - operational alerts (blocked train gate, stale streams, cycle gaps, no-signal warnings)

## Dashboard security
- Bind to localhost only (`--host 127.0.0.1`). Never expose the dashboard on a public interface.
- Mutating service-control endpoints (`POST /api/ops/restart|shutdown|start-scheduler`) are gated:
  - Set `WEATHER_ARB_DASHBOARD_ADMIN_TOKEN` to enable them; if unset they return `403`.
  - Requests must send the token in the `X-Admin-Token` header.
- CORS is restricted via `WEATHER_ARB_DASHBOARD_CORS_ORIGINS` (comma-separated). Defaults to localhost origins only; no wildcard.

## launchd (24/7 local runtime)
```bash
cd /path/to/KalshiBot/kalshi_weather_model
chmod +x ops/run_scheduler.sh ops/run_dashboard.sh ops/launchd/install_launchd.sh ops/launchd/uninstall_launchd.sh
./ops/launchd/install_launchd.sh
```

Check status:
```bash
launchctl print "gui/$(id -u)/com.kalshi-weather.scheduler" | head -n 30
launchctl print "gui/$(id -u)/com.kalshi-weather.dashboard" | head -n 30
curl -sS http://127.0.0.1:8077/health
```

Stop/remove:
```bash
./ops/launchd/uninstall_launchd.sh
```

## Risk scaling
- Live risk uses hybrid scaling:
  - fixed tiers through `$500` equity
  - percentage-based sizing above `$500` (`max_position=3%`, `daily_stop=6%`)
  - `weekly_stop` scales as `2x daily_stop` (min `$20`)
  - hard cap `max_concurrent_positions=5`
  - limits recalc once per day (ET)
  - when live auth is enabled, equity is synced from Kalshi once/day (`LIVE_EQUITY_SYNC_ENABLED=1`)
  - default fallback baseline is `LIVE_STARTING_EQUITY` (set to `$50` in `.env.example`)

## Data Cadence
- `15m`: NOAA forecast vintages (`ingest-forecasts`) + market scan/signal cycle + `live-cycle`.
- `60m`: NOAA observation sync (`sync-observations`) with dedupe by `city + obs_date_local`.
- `daily 00:30 ET`: automatic gate pipeline (`run-daily-gates` = train -> wf -> backtest).
- This lets you capture intraday divergence while keeping daily outcome labels clean for training gates.

## Isolation constraint
- No runtime dependency on Newton_Bot/Newton_ADS.
- All state/artifacts are under `kalshi_weather_model/data/`.

## Kalshi key setup
- Preferred: keep private key in a local `.pem` file and set `KALSHI_RSA_KEY_PATH` to that path.
- Supported alternative: set `KALSHI_RSA_PRIVATE_KEY` with PEM content (escaped `\n` line breaks).
- Do not commit secrets; `.env` is ignored by git.
