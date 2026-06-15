# KalshiBot

[![CI](https://github.com/EJellerson/KalshiBot/actions/workflows/ci.yml/badge.svg)](https://github.com/EJellerson/KalshiBot/actions/workflows/ci.yml)

KalshiBot is a standalone public experiment exploring whether NOAA weather forecasts can be used to identify potential opportunities in Kalshi weather markets.

This repository is **not** my production trading infrastructure, is **not** representative of my deployed trading architecture, and is **not** a complete trading platform. My production trading systems, execution systems, and ML infrastructure remain private. This project was extracted and published as a self-contained research/engineering artifact.

## What It Does

- Ingests NOAA forecast and observation data for selected US cities.
- Discovers and parses Kalshi weather-market contracts.
- Estimates simple weather-market fair values and expected value signals.
- Runs paper/live-oriented state machines with fail-closed live-routing controls.
- Provides local monitoring and dashboard tooling for data freshness, strategy health, and operational state.
- Includes a pytest suite covering parsing, pricing, risk limits, lifecycle gates, routing controls, and dashboard data paths.

## What It Is Not

- Not a deployed production strategy.
- Not a representation of my private trading architecture.
- Not a full execution platform or portfolio system.
- Not intended to include live credentials, private keys, proprietary infrastructure, or production model code.

## Project Layout

```text
kalshi_weather_model/
  weather_arb/      # package code
  tests/            # pytest suite
  ops/              # local launchd helpers
  config/           # public config fixtures
  README.md         # detailed local setup and command reference
```

## Quick Start

```bash
cd kalshi_weather_model
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q
```

For operational commands, dashboard usage, and local runtime details, see [`kalshi_weather_model/README.md`](kalshi_weather_model/README.md).

## Local Operation And Security

No live credentials are included. `.env`, private keys, generated data, and local runtime artifacts are ignored by git.

Dashboard operations are designed for local use. The dashboard should bind to `127.0.0.1`, and mutating dashboard operations require an admin token when enabled.
