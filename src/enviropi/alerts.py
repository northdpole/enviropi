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
    resolved: bool = False
    catastrophe: bool = False


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

UNITS = {
    "temperature": "°C",
    "humidity": "%",
    "pressure": "hPa",
    "gas_reducing": "Ω",
    "gas_oxidising": "Ω",
    "gas_nh3": "Ω",
    "noise": "",
    "lux": "lux",
}


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
        muted = bool(mute_until and utc_now() < mute_until)
        if muted:
            logger.debug("Alerts muted until %s", mute_until.isoformat())

        events: list[AlertEvent] = []
        if not muted:
            events.extend(self._evaluate_thresholds(cfg, sample))

        # Catastrophe always evaluated; bypasses mute (fire / flood / extreme gas)
        if cfg.catastrophe.enabled:
            events.extend(self._evaluate_catastrophe(cfg, sample))

        return events

    def _evaluate_thresholds(self, cfg: AppConfig, sample: Sample) -> list[AlertEvent]:
        events: list[AlertEvent] = []
        checks: list[tuple[str, float | None, float | None, str]] = []

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
                else:
                    # Allow recovery path when change has subsided
                    checks.append((f"{metric}.relative", value, baseline, "relative_ok"))

        for condition_key, value, threshold, direction in checks:
            event = self._maybe_fire(cfg, condition_key, value, threshold, direction)
            if event:
                events.append(event)

        return events

    def _evaluate_catastrophe(self, cfg: AppConfig, sample: Sample) -> list[AlertEvent]:
        cat = cfg.catastrophe
        # Prior must be near the window (not hours-old pre-downtime samples).
        past = self.db.sample_near(
            minutes_ago=cat.window_min,
            max_skew_min=max(2, cat.window_min // 2),
        )
        if not past:
            return []

        events: list[AlertEvent] = []
        window = cat.window_min
        warmup = cfg.gas.baseline_warmup_min * 60
        gas_ready = (utc_now() - self._started_at).total_seconds() >= warmup

        def condition_met(
            current: float | None,
            prior: float | None,
            *,
            delta: float | None,
            kind: str,
        ) -> bool:
            if current is None or prior is None or delta is None:
                return False
            if kind == "rise":
                return (current - prior) >= delta
            if kind == "drop":
                return (prior - current) >= delta
            if kind == "pct_drop":
                return prior != 0 and ((prior - current) / abs(prior) * 100.0) >= delta
            if kind == "pct_rise":
                return prior != 0 and ((current - prior) / abs(prior) * 100.0) >= delta
            return False

        def maybe(
            key: str,
            metric: str,
            current: float | None,
            prior: float | None,
            *,
            delta: float | None,
            kind: str,
            hint: str,
        ) -> None:
            condition_key = f"catastrophe.{key}"
            met = condition_met(current, prior, delta=delta, kind=kind)
            if not met:
                # Edge clear — no resolve Telegram for catastrophe
                state = self.db.get_alert_state(condition_key) or {}
                if state.get("active"):
                    self.db.upsert_alert_state(
                        condition_key,
                        last_value=current if current is not None else state.get("last_value"),
                        active=False,
                    )
                return
            assert current is not None and prior is not None and delta is not None

            unit = UNITS.get(metric, "")
            if kind.startswith("pct"):
                change = abs(current - prior) / abs(prior) * 100.0
                detail = f"{change:.0f}% change in {window} min ({prior:g}{unit} → {current:g}{unit})"
                thresh = float(delta)
            else:
                signed = current - prior
                detail = f"{signed:+.1f}{unit} in {window} min (now {current:g}{unit})"
                thresh = float(delta)

            event = self._maybe_catastrophe_fire(
                cfg,
                condition_key=condition_key,
                value=current,
                threshold=thresh,
                detail=detail,
                hint=hint,
            )
            if event:
                events.append(event)

        maybe(
            "temperature",
            "temperature",
            sample.temperature,
            past.get("temperature"),
            delta=cat.temperature_rise,
            kind="rise",
            hint="Possible fire / extreme heat",
        )
        maybe(
            "humidity",
            "humidity",
            sample.humidity,
            past.get("humidity"),
            delta=cat.humidity_rise,
            kind="rise",
            hint="Possible flooding / steam / water event",
        )
        maybe(
            "pressure",
            "pressure",
            sample.pressure,
            past.get("pressure"),
            delta=cat.pressure_drop,
            kind="drop",
            hint="Sudden pressure drop",
        )
        if gas_ready:
            maybe(
                "gas_reducing",
                "gas_reducing",
                sample.gas_reducing,
                past.get("gas_reducing"),
                delta=cat.gas_drop_pct,
                kind="pct_drop",
                hint="Extreme reducing-gas spike (smoke / CO-like)",
            )
            maybe(
                "gas_nh3",
                "gas_nh3",
                sample.gas_nh3,
                past.get("gas_nh3"),
                delta=cat.gas_drop_pct,
                kind="pct_drop",
                hint="Extreme NH3 / related gas spike",
            )
            maybe(
                "gas_oxidising",
                "gas_oxidising",
                sample.gas_oxidising,
                past.get("gas_oxidising"),
                delta=cat.gas_rise_pct,
                kind="pct_rise",
                hint="Extreme oxidising-gas spike (NO2-like)",
            )
        maybe(
            "lux",
            "lux",
            sample.lux,
            past.get("lux"),
            delta=cat.lux_rise,
            kind="rise",
            hint="Sudden light surge",
        )

        return events

    def _maybe_catastrophe_fire(
        self,
        cfg: AppConfig,
        *,
        condition_key: str,
        value: float,
        threshold: float,
        detail: str,
        hint: str,
    ) -> AlertEvent | None:
        """Edge-triggered catastrophe: one message while active; cooldown after clear."""
        state = self.db.get_alert_state(condition_key) or {}
        was_active = bool(state.get("active"))
        now = utc_now()

        if was_active:
            self.db.upsert_alert_state(condition_key, last_value=value, active=True)
            return None

        last_fired = state.get("last_fired_at")
        if last_fired:
            try:
                last_dt = datetime.fromisoformat(last_fired)
                if (now - last_dt).total_seconds() < cfg.catastrophe.cooldown_sec:
                    # Post-clear cooldown: ignore new edges until it expires
                    return None
            except ValueError:
                pass

        msg = (
            f"EnviroPi CATASTROPHE: {condition_key.split('.', 1)[1]}\n"
            f"{detail}.\n"
            f"{hint}.\n"
            f"Dashboard: {cfg.dashboard_url}"
        )
        self.db.upsert_alert_state(
            condition_key,
            last_fired_at=to_iso(now),
            last_value=value,
            active=True,
        )
        return AlertEvent(
            condition_key,
            msg,
            value,
            threshold,
            catastrophe=True,
        )

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

        if direction == "above":
            breached = value >= threshold
            recovered = value < (threshold - hyst)
        elif direction == "below":
            breached = value <= threshold
            recovered = value > (threshold + hyst)
        elif direction == "relative":
            breached = True
            recovered = False
        elif direction == "relative_ok":
            breached = False
            recovered = True
        else:
            return None

        now = utc_now()
        if breached:
            self.db.upsert_alert_state(
                condition_key,
                last_value=value,
                active=True,
                last_fired_at=to_iso(now) if not was_active else None,
            )
            if was_active:
                # Still breached — edge-triggered: do not re-notify
                return None
            msg = self._format_message(condition_key, value, threshold, direction, cfg)
            return AlertEvent(condition_key, msg, value, threshold)

        if was_active and recovered:
            self.db.upsert_alert_state(condition_key, last_value=value, active=False)
            logger.info("Condition cleared: %s", condition_key)
            unit = UNITS.get(metric, "")
            msg = (
                f"EnviroPi resolved: {condition_key}\n"
                f"Value {value:g}{unit} is back within limits.\n"
                f"Dashboard: {cfg.dashboard_url}"
            )
            return AlertEvent(condition_key, msg, value, threshold, resolved=True)

        if not breached:
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
        metric = condition_key.split(".", 1)[0]
        unit = UNITS.get(metric, "")
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
