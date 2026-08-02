from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from enviropi.alerts import AlertEvaluator
from enviropi.config import AppConfig, merge_overrides
from enviropi.db import Database, Sample, to_iso, utc_now
from enviropi.display import DisplaySnapshot, create_display
from enviropi.sensors import MockSensorReader, Reading
from enviropi.telegram_bot import (
    effective_telegram_allowlist,
    format_status_lines,
    private_alert_user_id,
)


def test_mock_sensor_reading():
    r = MockSensorReader().read()
    assert r.temperature is not None
    assert r.gas_reducing > 0


def test_display_skipped_when_mock_or_disabled():
    assert create_display(enabled=False, mock_sensors=False) is None
    assert create_display(enabled=True, mock_sensors=True) is None


def test_display_snapshot_from_reading():
    snap = DisplaySnapshot.from_reading(
        Reading(
            temperature=21.5,
            humidity=40.0,
            pressure=1013.0,
            lux=100.0,
            noise=0.1,
            gas_reducing=1.0,
            gas_oxidising=1.0,
            gas_nh3=1.0,
        )
    )
    assert snap.temperature == 21.5
    assert snap.humidity == 40.0


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


def test_private_alert_user_id():
    assert private_alert_user_id("1112223334") == 1112223334
    assert private_alert_user_id("-1001234567890") is None
    assert private_alert_user_id("") is None
    assert private_alert_user_id("not-a-number") is None


def test_format_status_includes_threshold_refs():
    sample = {
        "ts": "2026-08-02T12:00:00+00:00",
        "temperature": 21.5,
        "humidity": 40.0,
        "pressure": 1013.0,
        "lux": 100.0,
        "noise": 0.1,
        "gas_reducing": 80000.0,
        "gas_oxidising": 40000.0,
        "gas_nh3": 120000.0,
    }
    thresholds = {
        "temperature.low": 10.0,
        "temperature.high": 28.0,
        "humidity.low": 30.0,
        "humidity.high": 70.0,
        "pressure.low": None,
        "pressure.high": None,
        "lux.low": None,
        "lux.high": None,
        "noise.high": None,
        "gas_reducing.high": 50_000.0,
        "gas_oxidising.high": None,
        "gas_nh3.high": None,
    }
    text = "\n".join(format_status_lines(sample, thresholds))
    assert "Temp: 21.5 °C (10 low, 28 hi)" in text
    assert "Reducing: 80000.0 Ω (off low, 50000 hi)" in text
    assert "Pressure: 1013.0 hPa (off low, off hi)" in text


def test_effective_telegram_allowlist_owner_only():
    # Empty allowlist + private alert chat => only that user (not public)
    assert effective_telegram_allowlist([], "1112223334") == {1112223334}
    # Empty allowlist + no/group alert chat => deny everyone
    assert effective_telegram_allowlist([], "") == set()
    assert effective_telegram_allowlist([], "-100123") == set()
    # Explicit allowlist plus alert recipient
    assert effective_telegram_allowlist([111], "1112223334") == {111, 1112223334}
    # Stranger never implied
    assert 999 not in effective_telegram_allowlist([], "1112223334")


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
