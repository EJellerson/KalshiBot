from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import schedule

from weather_arb import config


@dataclass
class SchedulerHooks:
    cycle_hook: Callable[[], Any]
    ingest_hook: Callable[[], Any]
    obs_sync_hook: Callable[[], Any]
    gate_eval_hook: Callable[[], Any]
    settlement_hook: Callable[[], Any]
    governance_hook: Callable[[], Any]
    calibration_hook: Callable[[], Any]


@dataclass
class WeatherScheduler:
    hooks: SchedulerHooks
    jobs: list[schedule.Job] = field(default_factory=list)

    def _run_hook_safe(self, hook_name: str, hook: Callable[[], Any]) -> Any:
        try:
            return hook()
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "ts_utc": now_utc().isoformat(timespec="seconds"),
                        "event": "scheduler_hook_error",
                        "hook": hook_name,
                        "error": str(exc),
                    }
                )
            )
            return None

    def configure(self) -> None:
        self.jobs.append(
            schedule.every(config.SCHEDULER_INTERVAL_MINUTES).minutes.do(
                self._run_hook_safe,
                "ingest_hook",
                self.hooks.ingest_hook,
            )
        )
        self.jobs.append(
            schedule.every(config.SCHEDULER_INTERVAL_MINUTES).minutes.do(
                self._run_hook_safe,
                "cycle_hook",
                self.hooks.cycle_hook,
            )
        )
        self.jobs.append(
            schedule.every(config.OBS_SYNC_INTERVAL_MINUTES).minutes.do(
                self._run_hook_safe,
                "obs_sync_hook",
                self.hooks.obs_sync_hook,
            )
        )
        self.jobs.append(
            schedule.every(config.SCHEDULER_INTERVAL_MINUTES).minutes.do(
                self._run_hook_safe,
                "settlement_hook",
                self.hooks.settlement_hook,
            )
        )
        self.jobs.append(
            schedule.every().day.at(config.SCHEDULER_GATE_EVAL_TIME).do(
                self._run_hook_safe,
                "gate_eval_hook",
                self.hooks.gate_eval_hook,
            )
        )
        self.jobs.append(
            schedule.every().day.at(config.SCHEDULER_GOVERNANCE_TIME).do(
                self._run_hook_safe,
                "governance_hook",
                self.hooks.governance_hook,
            )
        )
        self.jobs.append(
            schedule.every().sunday.at(config.SCHEDULER_CALIBRATION_TIME).do(
                self._run_hook_safe,
                "calibration_hook",
                self.hooks.calibration_hook,
            )
        )

    def run_forever(self) -> None:
        if not self.jobs:
            self.configure()
        while True:
            schedule.run_pending()
            time.sleep(30)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
