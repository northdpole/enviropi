from __future__ import annotations

import asyncio
import logging
import signal
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from enviropi.alerts import AlertEvaluator
from enviropi.config import get_config, get_env, effective_threshold_map, merge_overrides
from enviropi.db import Database
from enviropi.display import create_display
from enviropi.sensors import create_sensor_reader
from enviropi.telegram_bot import (
    TelegramService,
    format_digest_lines,
    previous_digest_start,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("enviropi.collector")

STATUS_REPORT_SLOT_KEY = "status_report_last_slot"


class Collector:
    def __init__(self) -> None:
        self.env = get_env()
        self.config = get_config(self.env)
        self.db = Database(self.env.enviropi_db)
        self.sensors = create_sensor_reader(self.config, self.env.enviropi_mock_sensors)
        self.evaluator = AlertEvaluator(self.db, self.config)
        self.display = None
        self.telegram: TelegramService | None = None
        self._stop = asyncio.Event()
        self._last_rollup_day: str | None = None

        try:
            self.display = create_display(
                enabled=self.env.display_enabled,
                mock_sensors=self.env.enviropi_mock_sensors,
            )
        except Exception:
            logger.exception("LCD init failed; continuing without display")
            self.display = None

        if self.env.telegram_bot_token:
            self.telegram = TelegramService(
                token=self.env.telegram_bot_token,
                alert_chat_id=self.env.telegram_alert_chat_id,
                db=self.db,
                base_config=self.config,
                allowlist=self.config.telegram_allowlist,
            )

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except NotImplementedError:
                # Windows
                pass

        tg_task = None
        if self.telegram:
            app = self.telegram.build()
            await app.initialize()
            await app.start()
            if app.updater:
                await app.updater.start_polling(drop_pending_updates=True)
            tg_task = asyncio.create_task(self._idle())
            if self.config.notify_on_startup and self.env.telegram_alert_chat_id:
                await self.telegram.send_message("EnviroPi collector started.")

        logger.info(
            "Collector running (poll=%ss, mock=%s, db=%s)",
            self.config.poll_interval_sec,
            self.env.enviropi_mock_sensors,
            self.env.enviropi_db,
        )

        try:
            while not self._stop.is_set():
                await self._tick()
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=self.config.poll_interval_sec,
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            if self.display:
                await asyncio.to_thread(self.display.stop)
            if self.telegram and self.telegram.app:
                app = self.telegram.app
                if app.updater:
                    await app.updater.stop()
                await app.stop()
                await app.shutdown()
            if tg_task:
                tg_task.cancel()

    async def _idle(self) -> None:
        await self._stop.wait()

    async def _tick(self) -> None:
        try:
            reading = await asyncio.to_thread(self.sensors.read)
            sample = reading.to_sample()
            self.db.insert_sample(sample)
            if self.display:
                self.display.update(reading)
            logger.info(
                "Sample T=%.1f H=%.1f P=%.1f lux=%.0f",
                sample.temperature or 0,
                sample.humidity or 0,
                sample.pressure or 0,
                sample.lux or 0,
            )

            events = self.evaluator.evaluate(sample)
            if events and self.telegram:
                for event in events:
                    logger.warning(
                        "Sending %s: %s",
                        "catastrophe" if event.catastrophe else (
                            "resolve" if event.resolved else "alert"
                        ),
                        event.condition_key,
                    )
                    await self.telegram.send_message(event.message)

            await self._maybe_status_report(sample.as_dict())
            self._maybe_maintain()
        except Exception:
            logger.exception("Collector tick failed")

    async def _maybe_status_report(self, sample: dict) -> None:
        overrides = self.db.get_overrides()
        cfg = merge_overrides(self.config, overrides)
        report = cfg.status_report
        if not report.enabled or not self.telegram or not self.env.telegram_alert_chat_id:
            return

        try:
            tz = ZoneInfo(report.timezone)
        except ZoneInfoNotFoundError:
            logger.error("Invalid status_report.timezone=%s", report.timezone)
            return

        now_local = datetime.now(tz)
        slot = _due_status_slot(
            now_local,
            report.times,
            poll_interval_sec=self.config.poll_interval_sec,
        )
        if not slot:
            return

        last = overrides.get(STATUS_REPORT_SLOT_KEY)
        if last == slot:
            return

        # slot id is "YYYY-MM-DD HH:MM"
        try:
            date_s, time_s = slot.split(" ", 1)
            y, mo, d = (int(x) for x in date_s.split("-"))
            hh, mm = (int(x) for x in time_s.split(":"))
            slot_local = datetime(y, mo, d, hh, mm, tzinfo=tz)
        except ValueError:
            logger.exception("Bad status report slot id %r", slot)
            return

        interval_start_local = previous_digest_start(slot_local, report.times)
        stats = self.db.interval_stats(
            since=interval_start_local.astimezone(timezone.utc),
            until=slot_local.astimezone(timezone.utc),
        )
        thresholds = effective_threshold_map(self.config, overrides)
        body = "\n".join(
            format_digest_lines(
                sample,
                thresholds,
                interval_label=interval_start_local.strftime("%H:%M"),
                stats=stats,
            )
        )
        text = f"EnviroPi status report ({slot})\n{body}"
        await self.telegram.send_message(text)
        self.db.set_override(STATUS_REPORT_SLOT_KEY, slot, "collector")
        logger.info("Sent status report for slot %s", slot)

    def _maybe_maintain(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # Roll up every tick is cheap; prune once per UTC day
        try:
            hours = self.db.rollup_hourly()
            if hours:
                logger.debug("Rolled up %s hourly buckets", hours)
        except Exception:
            logger.exception("Hourly rollup failed")

        if self._last_rollup_day != today:
            try:
                deleted = self.db.prune_raw_samples(self.config.raw_retention_days)
                logger.info("Pruned %s raw samples (retention %sd)", deleted, self.config.raw_retention_days)
                self.db.purge_expired_sessions()
                self._last_rollup_day = today
            except Exception:
                logger.exception("Daily maintenance failed")


def _due_status_slot(
    now_local: datetime,
    times: list[str],
    *,
    poll_interval_sec: int,
) -> str | None:
    """Return slot id if local time is within one poll window after a configured HH:MM."""
    for raw in times:
        try:
            hh_s, mm_s = raw.strip().split(":", 1)
            hh, mm = int(hh_s), int(mm_s)
        except ValueError:
            logger.warning("Ignoring invalid status_report time %r", raw)
            continue
        target = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        delta = (now_local - target).total_seconds()
        if 0 <= delta < poll_interval_sec:
            return f"{now_local.date().isoformat()} {hh:02d}:{mm:02d}"
    return None


def main() -> None:
    try:
        asyncio.run(Collector().run())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
