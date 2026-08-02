from __future__ import annotations

import logging
import math
import random
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from subprocess import PIPE, Popen

from enviropi.config import AppConfig
from enviropi.db import Sample, utc_now

logger = logging.getLogger(__name__)


@dataclass
class Reading:
    temperature: float
    humidity: float
    pressure: float
    lux: float
    noise: float
    gas_reducing: float
    gas_oxidising: float
    gas_nh3: float

    def to_sample(self) -> Sample:
        return Sample(
            ts=utc_now(),
            temperature=self.temperature,
            humidity=self.humidity,
            pressure=self.pressure,
            lux=self.lux,
            noise=self.noise,
            gas_reducing=self.gas_reducing,
            gas_oxidising=self.gas_oxidising,
            gas_nh3=self.gas_nh3,
        )


class SensorReader(ABC):
    @abstractmethod
    def read(self) -> Reading:
        raise NotImplementedError


class MockSensorReader(SensorReader):
    """Deterministic-ish mock readings for off-Pi development."""

    def __init__(self) -> None:
        self._t0 = time.time()

    def read(self) -> Reading:
        t = time.time() - self._t0
        temp = 21.5 + 1.5 * math.sin(t / 600) + random.uniform(-0.1, 0.1)
        humidity = 45 + 5 * math.sin(t / 900) + random.uniform(-0.5, 0.5)
        pressure = 1013.0 + 2 * math.sin(t / 3600)
        lux = max(0.0, 120 + 80 * math.sin(t / 1200) + random.uniform(-5, 5))
        noise = max(0.01, 0.08 + 0.02 * abs(math.sin(t / 30)) + random.uniform(0, 0.01))
        # Resistances in Ohms (typical MICS6814 ballpark)
        gas_reducing = 80_000 + 5_000 * math.sin(t / 1800) + random.uniform(-500, 500)
        gas_oxidising = 40_000 + 3_000 * math.sin(t / 2000) + random.uniform(-400, 400)
        gas_nh3 = 120_000 + 8_000 * math.sin(t / 2200) + random.uniform(-800, 800)
        return Reading(
            temperature=round(temp, 2),
            humidity=round(humidity, 2),
            pressure=round(pressure, 2),
            lux=round(lux, 1),
            noise=round(noise, 4),
            gas_reducing=round(gas_reducing, 1),
            gas_oxidising=round(gas_oxidising, 1),
            gas_nh3=round(gas_nh3, 1),
        )


class EnviroPlusSensorReader(SensorReader):
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._cpu_temps: deque[float] = deque(maxlen=5)
        try:
            from bme280 import BME280
            from smbus2 import SMBus
            from ltr559 import LTR559
            from enviroplus import gas
            from enviroplus.noise import Noise
        except ImportError as exc:
            raise RuntimeError(
                "Hardware libraries not installed. Install enviroplus extras "
                "or set ENVIROPI_MOCK_SENSORS=true"
            ) from exc

        self._bus = SMBus(1)
        self._bme280 = BME280(i2c_dev=self._bus)
        self._ltr559 = LTR559()
        self._gas = gas
        self._noise = Noise()
        # Prime CPU temp buffer
        for _ in range(5):
            self._cpu_temps.append(self._cpu_temperature())

    @staticmethod
    def _cpu_temperature() -> float:
        process = Popen(["vcgencmd", "measure_temp"], stdout=PIPE, universal_newlines=True)
        output, _ = process.communicate()
        return float(output[output.index("=") + 1 : output.rindex("'")])

    def _compensated_temperature(self) -> float:
        cpu_temp = self._cpu_temperature()
        self._cpu_temps.append(cpu_temp)
        avg_cpu = sum(self._cpu_temps) / len(self._cpu_temps)
        raw = self._bme280.get_temperature()
        factor = self.config.temp_compensation_factor
        return raw - ((avg_cpu - raw) / factor)

    def read(self) -> Reading:
        proximity = self._ltr559.get_proximity()
        # When covered, lux reading is unreliable — treat as near-zero like Pimoroni examples
        lux = 1.0 if proximity >= 10 else float(self._ltr559.get_lux())
        gases = self._gas.read_all()
        # Noise: low/mid/high/amp — use overall amplitude
        low, mid, high, amp = self._noise.get_noise_profile()
        return Reading(
            temperature=round(self._compensated_temperature(), 2),
            humidity=round(float(self._bme280.get_humidity()), 2),
            pressure=round(float(self._bme280.get_pressure()), 2),
            lux=round(lux, 1),
            noise=round(float(amp), 4),
            gas_reducing=round(float(gases.reducing), 1),
            gas_oxidising=round(float(gases.oxidising), 1),
            gas_nh3=round(float(gases.nh3), 1),
        )


def create_sensor_reader(config: AppConfig, mock: bool) -> SensorReader:
    if mock:
        logger.info("Using mock sensors")
        return MockSensorReader()
    logger.info("Using Enviro+ hardware sensors")
    return EnviroPlusSensorReader(config)
