from __future__ import annotations

import asyncio
import logging
import signal
import sys
from datetime import datetime, timezone

from enviropi.alerts import AlertEvaluator
from enviropi.config import get_config, get_env
from enviropi.db import Database
from enviropi.display import create_display
from enviropi.sensors import create_sensor_reader
from enviropi.telegram_bot import TelegramService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("enviropi.collector")


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
                    await self.telegram.send_message(event.message)

            self._maybe_maintain()
        except Exception:
            logger.exception("Collector tick failed")

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


def main() -> None:
    try:
        asyncio.run(Collector().run())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
