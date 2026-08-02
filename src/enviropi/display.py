from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from enviropi.sensors import Reading

logger = logging.getLogger(__name__)

# Pimoroni all-in-one uses proximity > 1500 as a "tap"/near gesture.
DEFAULT_PROXIMITY_THRESHOLD = 1500
DEFAULT_ON_SEC = 15.0
DEFAULT_POLL_SEC = 0.25


@dataclass
class DisplaySnapshot:
    temperature: float | None = None
    humidity: float | None = None
    pressure: float | None = None
    lux: float | None = None

    @classmethod
    def from_reading(cls, reading: Reading) -> DisplaySnapshot:
        return cls(
            temperature=reading.temperature,
            humidity=reading.humidity,
            pressure=reading.pressure,
            lux=reading.lux,
        )


class ProximityDisplay:
    """Enviro+ ST7735 LCD: backlight off by default, wake on proximity.

    The LCD does not wake by itself when you look at it — software must read
    the LTR-559 proximity sensor and toggle the backlight. This controller
    turns the backlight on while proximity is high, keeps it on briefly after
    you move away, then turns it off again (not permanently on).
    """

    def __init__(
        self,
        *,
        proximity_threshold: int = DEFAULT_PROXIMITY_THRESHOLD,
        on_sec: float = DEFAULT_ON_SEC,
        poll_sec: float = DEFAULT_POLL_SEC,
    ) -> None:
        self.proximity_threshold = proximity_threshold
        self.on_sec = on_sec
        self.poll_sec = poll_sec
        self._lock = threading.Lock()
        self._snapshot = DisplaySnapshot()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._disp: Any = None
        self._ltr: Any = None
        self._img: Any = None
        self._draw: Any = None
        self._font: Any = None
        self._backlight_on = False
        self._wake_until = 0.0

    def start(self) -> None:
        try:
            import st7735
            from fonts.ttf import RobotoMedium as UserFont
            from ltr559 import LTR559
            from PIL import Image, ImageDraw, ImageFont
        except ImportError as exc:
            raise RuntimeError(
                "Display libraries missing (st7735/Pillow/fonts/ltr559). "
                "Install enviroplus hardware deps or set DISPLAY_ENABLED=false"
            ) from exc

        self._disp = st7735.ST7735(
            port=0,
            cs=1,
            dc="GPIO9",
            backlight="GPIO12",
            rotation=270,
            spi_speed_hz=10_000_000,
        )
        self._disp.begin()
        # begin() turns the backlight on — immediately sleep so we are not
        # permanently lit.
        self._disp.set_backlight(0)
        self._backlight_on = False

        self._ltr = LTR559()
        width, height = self._disp.width, self._disp.height
        self._img = Image.new("RGB", (width, height), color=(0, 0, 0))
        self._draw = ImageDraw.Draw(self._img)
        try:
            self._font = ImageFont.truetype(UserFont, 14)
        except OSError:
            self._font = ImageFont.load_default()

        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="enviropi-display", daemon=True)
        self._thread.start()
        logger.info(
            "LCD proximity-wake enabled (threshold=%s, on_sec=%ss); backlight starts off",
            self.proximity_threshold,
            self.on_sec,
        )

    def update(self, reading: Reading) -> None:
        with self._lock:
            self._snapshot = DisplaySnapshot.from_reading(reading)

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        if self._disp is not None:
            try:
                self._disp.set_backlight(0)
            except Exception:
                logger.exception("Failed to turn LCD backlight off on stop")
            self._backlight_on = False

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_sec):
            try:
                self._tick()
            except Exception:
                logger.exception("Display tick failed")

    def _tick(self) -> None:
        proximity = int(self._ltr.get_proximity())
        now = time.monotonic()
        if proximity >= self.proximity_threshold:
            self._wake_until = now + self.on_sec
            if not self._backlight_on:
                self._disp.set_backlight(1)
                self._backlight_on = True
                logger.info("LCD woke (proximity=%s)", proximity)

        if self._backlight_on:
            if now <= self._wake_until:
                self._render()
            else:
                self._disp.set_backlight(0)
                self._backlight_on = False
                logger.info("LCD sleep after idle timeout")

    def _render(self) -> None:
        with self._lock:
            snap = self._snapshot

        draw = self._draw
        img = self._img
        font = self._font
        width, height = self._disp.width, self._disp.height
        draw.rectangle((0, 0, width, height), fill=(0, 20, 30))
        lines = [
            f"T  {snap.temperature:.1f} C" if snap.temperature is not None else "T  --",
            f"H  {snap.humidity:.0f} %" if snap.humidity is not None else "H  --",
            f"P  {snap.pressure:.0f} hPa" if snap.pressure is not None else "P  --",
            f"L  {snap.lux:.0f} lx" if snap.lux is not None else "L  --",
        ]
        y = 4
        for line in lines:
            draw.text((4, y), line, font=font, fill=(220, 240, 255))
            y += 18
        self._disp.display(img)


def create_display(*, enabled: bool, mock_sensors: bool) -> ProximityDisplay | None:
    if not enabled:
        logger.info("LCD disabled (DISPLAY_ENABLED=false)")
        return None
    if mock_sensors:
        logger.info("LCD skipped (ENVIROPI_MOCK_SENSORS=true)")
        return None
    display = ProximityDisplay()
    display.start()
    return display
