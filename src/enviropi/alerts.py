from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from enviropi.config import AppConfig, merge_overrides
from enviropi.db import Database, Sample, to_iso, utc_now

logger = logging.getLogger(__name__)


@dataclass
class AlertEvent:
    condition_key: str
    message: str
    value: float
    threshold: float


# Metrics where "high" means reading rose above threshold
HIGH_IS_BAD = {
    "temperature",
    "humidity",
    "pressure",
    "gas_oxidising",
    "noise",
    "lux",
}

# Metrics where "high" config means resistance floor: alert when reading DROPS below
LOW_RESISTANCE_IS_BAD = {"gas_reducing", "gas_nh3"}


def _parse_mute_until(overrides: dict[str, str]) -> datetime | None:
    raw = overrides.get("mute_until")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


class AlertEvaluator:
    def __init__(self, db: Database, base_config: AppConfig) -> None:
        self.db = db
        self.base_config = base_config
        self._started_at = utc_now()

    def _cfg(self) -> tuple[AppConfig, dict[str, str]]:
        overrides = self.db.get_overrides()
        return merge_overrides(self.base_config, overrides), overrides

    def evaluate(self, sample: Sample) -> list[AlertEvent]:
        cfg, overrides = self._cfg()
        mute_until = _parse_mute_until(overrides)
        if mute_until and utc_now() < mute_until:
            logger.debug("Alerts muted until %s", mute_until.isoformat())
            return []

        events: list[AlertEvent] = []
        checks: list[tuple[str, float | None, float | None, str]] = []

        # (condition_key, value, threshold, direction)
        # direction: "above" | "below"
        def add_pair(metric: str, high: float | None, low: float | None, value: float | None) -> None:
            if value is None:
                return
            if high is not None:
                if metric in LOW_RESISTANCE_IS_BAD:
                    checks.append((f"{metric}.high", value, high, "below"))
                else:
                    checks.append((f"{metric}.high", value, high, "above"))
            if low is not None:
                checks.append((f"{metric}.low", value, low, "below"))

        add_pair("temperature", cfg.temperature.high, cfg.temperature.low, sample.temperature)
        add_pair("humidity", cfg.humidity.high, cfg.humidity.low, sample.humidity)
        add_pair("pressure", cfg.pressure.high, cfg.pressure.low, sample.pressure)
        add_pair("gas_reducing", cfg.gas_reducing.high, None, sample.gas_reducing)
        add_pair("gas_oxidising", cfg.gas_oxidising.high, None, sample.gas_oxidising)
        add_pair("gas_nh3", cfg.gas_nh3.high, None, sample.gas_nh3)
        add_pair("noise", cfg.noise.high, None, sample.noise)
        add_pair("lux", cfg.lux.high, cfg.lux.low, sample.lux)

        # Optional relative gas change vs rolling baseline after warmup
        warmup = cfg.gas.baseline_warmup_min * 60
        elapsed = (utc_now() - self._started_at).total_seconds()
        if cfg.gas.relative_change_pct is not None and elapsed >= warmup:
            pct = cfg.gas.relative_change_pct
            for metric, value in (
                ("gas_reducing", sample.gas_reducing),
                ("gas_oxidising", sample.gas_oxidising),
                ("gas_nh3", sample.gas_nh3),
            ):
                if value is None:
                    continue
                baseline = self.db.recent_gas_baseline(metric, minutes=60)
                if baseline is None or baseline == 0:
                    continue
                change = abs(value - baseline) / abs(baseline) * 100.0
                if change >= pct:
                    checks.append((f"{metric}.relative", value, baseline, "relative"))

        for condition_key, value, threshold, direction in checks:
            event = self._maybe_fire(cfg, condition_key, value, threshold, direction)
            if event:
                events.append(event)

        return events

    def _maybe_fire(
        self,
        cfg: AppConfig,
        condition_key: str,
        value: float,
        threshold: float,
        direction: str,
    ) -> AlertEvent | None:
        metric = condition_key.split(".", 1)[0]
        hyst = cfg.hysteresis.get(metric, 0.0)
        state = self.db.get_alert_state(condition_key) or {}
        was_active = bool(state.get("active"))
        last_fired = state.get("last_fired_at")

        if direction == "above":
            breached = value >= threshold
            recovered = value < (threshold - hyst)
        elif direction == "below":
            breached = value <= threshold
            recovered = value > (threshold + hyst)
        else:  # relative
            breached = True  # already filtered
            recovered = False

        now = utc_now()
        if breached:
            if was_active and last_fired:
                try:
                    last_dt = datetime.fromisoformat(last_fired)
                    if (now - last_dt).total_seconds() < cfg.cooldown_sec:
                        self.db.upsert_alert_state(
                            condition_key, last_value=value, active=True
                        )
                        return None
                except ValueError:
                    pass

            # First breach or cooldown elapsed
            msg = self._format_message(condition_key, value, threshold, direction, cfg)
            self.db.upsert_alert_state(
                condition_key,
                last_fired_at=to_iso(now),
                last_value=value,
                active=True,
            )
            return AlertEvent(condition_key, msg, value, threshold)

        if was_active and recovered:
            self.db.upsert_alert_state(condition_key, last_value=value, active=False)
            logger.info("Condition cleared: %s", condition_key)
        elif not breached:
            self.db.upsert_alert_state(condition_key, last_value=value, active=False)

        return None

    def _format_message(
        self,
        condition_key: str,
        value: float,
        threshold: float,
        direction: str,
        cfg: AppConfig,
    ) -> str:
        units = {
            "temperature": "°C",
            "humidity": "%",
            "pressure": "hPa",
            "gas_reducing": "Ω",
            "gas_oxidising": "Ω",
            "gas_nh3": "Ω",
            "noise": "",
            "lux": "lux",
        }
        metric = condition_key.split(".", 1)[0]
        unit = units.get(metric, "")
        if direction == "relative":
            detail = f"changed vs baseline {threshold:.1f}{unit}"
        elif direction == "above":
            detail = f"above {threshold:g}{unit}"
        else:
            detail = f"below {threshold:g}{unit}"
        return (
            f"EnviroPi alert: {condition_key}\n"
            f"Value {value:g}{unit} is {detail}.\n"
            f"Dashboard: {cfg.dashboard_url}"
        )


async def send_alerts(
    events: list[AlertEvent],
    send: Callable[[str], object],
) -> None:
    for event in events:
        logger.warning("Alert: %s", event.condition_key)
        result = send(event.message)
        if hasattr(result, "__await__"):
            await result  # type: ignore[misc]
