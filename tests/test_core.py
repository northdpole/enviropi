from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from enviropi.alerts import AlertEvaluator
from enviropi.config import AppConfig, merge_overrides
from enviropi.db import Database, Sample, to_iso, utc_now
from enviropi.sensors import MockSensorReader


def test_mock_sensor_reading():
    r = MockSensorReader().read()
    assert r.temperature is not None
    assert r.gas_reducing > 0


def test_insert_and_latest(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    sample = MockSensorReader().read().to_sample()
    db.insert_sample(sample)
    latest = db.latest_sample()
    assert latest is not None
    assert latest["temperature"] == sample.temperature


def test_overrides_merge(tmp_path: Path):
    base = AppConfig()
    merged = merge_overrides(base, {"temperature.high": "30"})
    assert merged.temperature.high == 30.0
    assert merged.temperature.low == 10.0


def test_alert_temperature_high(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    cfg = AppConfig()
    cfg.temperature.high = 25.0
    cfg.cooldown_sec = 0
    ev = AlertEvaluator(db, cfg)
    sample = Sample(ts=utc_now(), temperature=30.0, humidity=40.0, pressure=1013.0)
    events = ev.evaluate(sample)
    assert any(e.condition_key == "temperature.high" for e in events)
    # cooldown / still active — second fire suppressed when cooldown > 0
    cfg.cooldown_sec = 1800
    ev2 = AlertEvaluator(db, cfg)
    events2 = ev2.evaluate(sample)
    assert events2 == []


def test_gas_reducing_below_floor(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    cfg = AppConfig()
    cfg.gas_reducing.high = 50_000
    cfg.cooldown_sec = 0
    ev = AlertEvaluator(db, cfg)
    sample = Sample(ts=utc_now(), gas_reducing=40_000.0)
    events = ev.evaluate(sample)
    assert any(e.condition_key == "gas_reducing.high" for e in events)


def test_rollup_and_prune(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    past = utc_now() - timedelta(hours=2)
    db.insert_sample(
        Sample(
            ts=past,
            temperature=20.0,
            humidity=40.0,
            pressure=1010.0,
            lux=10.0,
            noise=0.1,
            gas_reducing=1.0,
            gas_oxidising=1.0,
            gas_nh3=1.0,
        )
    )
    # Force timestamp string for older hour
    with db.cursor() as cur:
        cur.execute(
            "UPDATE samples SET ts = ? WHERE temperature = 20.0",
            (to_iso(past.replace(minute=15, second=0, microsecond=0)),),
        )
    n = db.rollup_hourly()
    assert n >= 1
    hist = db.history(since=utc_now() - timedelta(days=1), use_hourly=True)
    assert len(hist) >= 1
