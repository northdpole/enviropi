from __future__ import annotations

import json
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from enviropi.alerts import AlertEvaluator
from enviropi.config import (
    AppConfig,
    EnvSettings,
    get_config,
    merge_overrides,
    parse_digest_times,
    parse_telegram_allowlist,
)
from enviropi.db import Database, Sample, to_iso, utc_now
from enviropi.display import DisplaySnapshot, create_display
from enviropi.mqtt import MqttPublisher, encode_payload
from enviropi.sensors import MockSensorReader, Reading
from enviropi.telegram_bot import (
    effective_telegram_allowlist,
    format_digest_lines,
    format_status_lines,
    previous_digest_start,
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


def test_alert_temperature_high_edge_triggered(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    cfg = AppConfig()
    cfg.temperature.high = 25.0
    cfg.catastrophe.enabled = False
    ev = AlertEvaluator(db, cfg)
    sample = Sample(ts=utc_now(), temperature=30.0, humidity=40.0, pressure=1013.0)
    events = ev.evaluate(sample)
    assert any(e.condition_key == "temperature.high" for e in events)
    # Still breached — no repeat notify
    events2 = ev.evaluate(sample)
    assert events2 == []


def test_alert_resolves_once(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    cfg = AppConfig()
    cfg.temperature.high = 25.0
    cfg.hysteresis = {"temperature": 0.5}
    cfg.catastrophe.enabled = False
    ev = AlertEvaluator(db, cfg)
    hot = Sample(ts=utc_now(), temperature=30.0, humidity=40.0, pressure=1013.0)
    assert any(e.condition_key == "temperature.high" for e in ev.evaluate(hot))
    cool = Sample(ts=utc_now(), temperature=24.0, humidity=40.0, pressure=1013.0)
    resolved = ev.evaluate(cool)
    assert len(resolved) == 1
    assert resolved[0].resolved
    assert resolved[0].condition_key == "temperature.high"
    assert ev.evaluate(cool) == []


def test_gas_reducing_below_floor(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    cfg = AppConfig()
    cfg.gas_reducing.high = 50_000
    cfg.catastrophe.enabled = False
    ev = AlertEvaluator(db, cfg)
    sample = Sample(ts=utc_now(), gas_reducing=40_000.0)
    events = ev.evaluate(sample)
    assert any(e.condition_key == "gas_reducing.high" for e in events)


def test_catastrophe_temperature_rise(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    past = utc_now() - timedelta(minutes=5)
    db.insert_sample(
        Sample(ts=past, temperature=20.0, humidity=40.0, pressure=1013.0)
    )
    cfg = AppConfig()
    cfg.temperature.high = 100.0  # avoid normal threshold
    cfg.temperature.low = -50.0
    cfg.humidity.high = 100.0
    cfg.humidity.low = 0.0
    cfg.catastrophe.window_min = 5
    cfg.catastrophe.temperature_rise = 5.0
    cfg.catastrophe.humidity_rise = 100.0
    cfg.catastrophe.pressure_drop = None
    cfg.catastrophe.gas_drop_pct = 100.0
    cfg.catastrophe.gas_rise_pct = 100.0
    ev = AlertEvaluator(db, cfg)
    now = Sample(ts=utc_now(), temperature=26.0, humidity=40.0, pressure=1013.0)
    events = ev.evaluate(now)
    assert any(e.catastrophe and e.condition_key == "catastrophe.temperature" for e in events)
    # Still active — edge-triggered, no repeat
    assert not any(
        e.condition_key == "catastrophe.temperature" for e in ev.evaluate(now)
    )


def test_catastrophe_rejects_stale_prior(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    stale = utc_now() - timedelta(hours=2)
    db.insert_sample(
        Sample(ts=stale, temperature=20.0, humidity=40.0, pressure=1013.0)
    )
    cfg = AppConfig()
    cfg.temperature.high = 100.0
    cfg.temperature.low = -50.0
    cfg.humidity.high = 100.0
    cfg.humidity.low = 0.0
    cfg.catastrophe.pressure_drop = None
    cfg.catastrophe.gas_drop_pct = 100.0
    cfg.catastrophe.gas_rise_pct = 100.0
    ev = AlertEvaluator(db, cfg)
    now = Sample(ts=utc_now(), temperature=30.0, humidity=40.0, pressure=1013.0)
    assert ev.evaluate(now) == []


def test_catastrophe_clears_without_rearm_spam(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    past = utc_now() - timedelta(minutes=5)
    db.insert_sample(
        Sample(ts=past, temperature=20.0, humidity=40.0, pressure=1013.0)
    )
    cfg = AppConfig()
    cfg.temperature.high = 100.0
    cfg.temperature.low = -50.0
    cfg.humidity.high = 100.0
    cfg.humidity.low = 0.0
    cfg.catastrophe.cooldown_sec = 3600
    cfg.catastrophe.pressure_drop = None
    cfg.catastrophe.gas_drop_pct = 100.0
    cfg.catastrophe.gas_rise_pct = 100.0
    ev = AlertEvaluator(db, cfg)
    hot = Sample(ts=utc_now(), temperature=26.0, humidity=40.0, pressure=1013.0)
    assert any(e.condition_key == "catastrophe.temperature" for e in ev.evaluate(hot))
    # Condition gone → clear active silently
    db.insert_sample(
        Sample(ts=utc_now() - timedelta(minutes=5), temperature=25.5, humidity=40.0, pressure=1013.0)
    )
    cool = Sample(ts=utc_now(), temperature=26.0, humidity=40.0, pressure=1013.0)
    assert ev.evaluate(cool) == []
    state = db.get_alert_state("catastrophe.temperature")
    assert state is not None and not state["active"]
    # Spike again within cooldown → suppressed (not armed)
    db.insert_sample(
        Sample(ts=utc_now() - timedelta(minutes=5), temperature=20.0, humidity=40.0, pressure=1013.0)
    )
    again = Sample(ts=utc_now(), temperature=28.0, humidity=40.0, pressure=1013.0)
    assert ev.evaluate(again) == []
    assert not db.get_alert_state("catastrophe.temperature")["active"]


def test_due_status_slot():
    from enviropi.collector import _due_status_slot
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/London")
    now = datetime(2026, 8, 8, 8, 0, 30, tzinfo=tz)
    assert _due_status_slot(now, ["08:00", "20:00"], poll_interval_sec=60) == (
        "2026-08-08 08:00"
    )
    later = datetime(2026, 8, 8, 8, 2, 0, tzinfo=tz)
    assert _due_status_slot(later, ["08:00", "20:00"], poll_interval_sec=60) is None


def test_parse_digest_times():
    assert parse_digest_times("08:00,20:00") == ["08:00", "20:00"]
    assert parse_digest_times("20:00 8:00") == ["08:00", "20:00"]
    try:
        parse_digest_times("25:00")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_previous_digest_start_wraps_midnight():
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/London")
    slot = datetime(2026, 8, 8, 8, 0, tzinfo=tz)
    prev = previous_digest_start(slot, ["08:00", "20:00"])
    assert prev == datetime(2026, 8, 7, 20, 0, tzinfo=tz)
    evening = datetime(2026, 8, 8, 20, 0, tzinfo=tz)
    assert previous_digest_start(evening, ["08:00", "20:00"]) == datetime(
        2026, 8, 8, 8, 0, tzinfo=tz
    )


def test_merge_digest_times_override():
    base = AppConfig()
    merged = merge_overrides(base, {"status_report.times": "07:30,19:15"})
    assert merged.status_report.times == ["07:30", "19:15"]
    off = merge_overrides(base, {"status_report.enabled": "false"})
    assert off.status_report.enabled is False


def test_interval_stats_and_digest_format(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    t0 = utc_now() - timedelta(hours=2)
    t1 = utc_now() - timedelta(hours=1)
    db.insert_sample(Sample(ts=t0, temperature=18.0, humidity=40.0, pressure=1010.0))
    db.insert_sample(Sample(ts=t1, temperature=24.0, humidity=55.0, pressure=1015.0))
    stats = db.interval_stats(since=t0 - timedelta(minutes=1), until=utc_now())
    assert stats is not None
    assert stats["temperature_min"] == 18.0
    assert stats["temperature_max"] == 24.0
    sample = {
        "ts": to_iso(utc_now()),
        "temperature": 22.0,
        "humidity": 50.0,
        "pressure": 1012.0,
        "lux": 10.0,
        "noise": 0.1,
        "gas_reducing": 1.0,
        "gas_oxidising": 1.0,
        "gas_nh3": 1.0,
    }
    text = "\n".join(
        format_digest_lines(
            sample,
            {"temperature.low": 10, "temperature.high": 28},
            interval_label="20:00",
            stats=stats,
        )
    )
    assert "Since 20:00:" in text
    assert "Temp: 18–24 °C" in text
    assert "Humidity: 40–55 %" in text


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


def test_parse_telegram_allowlist_env():
    assert parse_telegram_allowlist("") == []
    assert parse_telegram_allowlist("9998887776") == [9998887776]
    assert parse_telegram_allowlist("111, 222") == [111, 222]
    assert parse_telegram_allowlist("bad,333") == [333]


def test_get_config_merges_env_identity(tmp_path: Path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "dashboard_url: \"http://127.0.0.1:8000\"\ntelegram_allowlist: []\n"
    )
    monkeypatch.chdir(tmp_path)
    # Isolate from developer .env leaking into the process environment
    for key in (
        "ENVIROPI_DASHBOARD_URL",
        "ENVIROPI_TAILSCALE_HOST",
        "TELEGRAM_ALLOWLIST",
        "WEB_PORT",
    ):
        monkeypatch.delenv(key, raising=False)
    env = EnvSettings(
        enviropi_config=cfg_path,
        enviropi_dashboard_url="",
        enviropi_tailscale_host="enviropi.example.ts.net",
        telegram_allowlist="111,222",
        web_port=8000,
    )
    cfg = get_config(env)
    assert cfg.dashboard_url == "http://enviropi.example.ts.net:8000"
    assert cfg.telegram_allowlist == [111, 222]


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


def _sample_dict() -> dict:
    return Sample(
        ts=datetime(2026, 9, 19, 14, 30, tzinfo=timezone.utc),
        temperature=21.5,
        humidity=40.0,
        pressure=1013.0,
        lux=100.0,
        noise=0.1,
        gas_reducing=80000.0,
        gas_oxidising=40000.0,
        gas_nh3=120000.0,
    ).as_dict()


def test_mqtt_payload_shape():
    sample = _sample_dict()
    data = json.loads(encode_payload(sample))
    assert data == {
        "ts": "2026-09-19T14:30:00Z",
        "temperature": 21.5,
        "humidity": 40.0,
        "pressure": 1013.0,
        "lux": 100.0,
        "noise": 0.1,
        "gas_reducing": 80000.0,
        "gas_oxidising": 40000.0,
        "gas_nh3": 120000.0,
    }


def test_mqtt_env_defaults():
    fields = EnvSettings.model_fields
    assert fields["mqtt_enabled"].default is False
    assert fields["mqtt_host"].default == "homeserver.example.ts.net"
    assert fields["mqtt_port"].default == 8883
    assert fields["mqtt_topic"].default == "enviropi/enviroplus/state"
    assert fields["mqtt_tls"].default is True
    assert fields["mqtt_tls_insecure"].default is False


class _FakeMqttClient:
    """Stand-in for paho Client so tests never touch a broker."""

    instances: list["_FakeMqttClient"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        self.auth: tuple[str, str] | None = None
        self.tls_kwargs: dict | None = None
        self.tls_insecure: bool | None = None
        self.connect_args: tuple | None = None
        self.loop_started = False
        self.loop_stopped = False
        self.disconnected = False
        self.publishes: list[dict] = []
        self.on_connect = None
        self.on_disconnect = None
        type(self).instances.append(self)

    def username_pw_set(self, username, password=None) -> None:
        self.auth = (username, password)

    def tls_set(self, **kwargs) -> None:
        self.tls_kwargs = kwargs

    def tls_insecure_set(self, insecure: bool) -> None:
        self.tls_insecure = insecure

    def reconnect_delay_set(self, min_delay=1, max_delay=120) -> None:
        self.reconnect = (min_delay, max_delay)

    def connect_async(self, host, port, keepalive=60) -> None:
        self.connect_args = (host, port, keepalive)

    def loop_start(self) -> None:
        self.loop_started = True

    def loop_stop(self) -> None:
        self.loop_stopped = True

    def disconnect(self) -> None:
        self.disconnected = True

    def publish(self, topic, payload, qos=0, retain=False):
        self.publishes.append(
            {"topic": topic, "payload": payload, "qos": qos, "retain": retain}
        )
        return SimpleNamespace(rc=0)


def test_mqtt_disabled_is_noop(monkeypatch):
    _FakeMqttClient.instances = []
    monkeypatch.setattr("enviropi.mqtt.Client", _FakeMqttClient)
    env = EnvSettings(
        mqtt_enabled=False,
        mqtt_host="homeserver.example.ts.net",
        mqtt_port=8883,
        mqtt_username="enviropi",
        mqtt_password="not-used",
        mqtt_topic="enviropi/enviroplus/state",
        mqtt_tls=True,
        mqtt_tls_insecure=False,
    )
    pub = MqttPublisher(env)
    pub.publish(_sample_dict())
    pub.stop()
    assert _FakeMqttClient.instances == []


def test_mqtt_publishes_retained_json(monkeypatch):
    _FakeMqttClient.instances = []
    monkeypatch.setattr("enviropi.mqtt.Client", _FakeMqttClient)
    env = EnvSettings(
        mqtt_enabled=True,
        mqtt_host="homeserver.example.ts.net",
        mqtt_port=8883,
        mqtt_username="enviropi",
        mqtt_password="test-password",
        mqtt_topic="enviropi/enviroplus/state",
        mqtt_tls=True,
        mqtt_tls_insecure=False,
    )
    pub = MqttPublisher(env)
    assert len(_FakeMqttClient.instances) == 1
    client = _FakeMqttClient.instances[0]
    assert client.auth == ("enviropi", "test-password")
    assert client.connect_args == ("homeserver.example.ts.net", 8883, 60)
    assert client.tls_kwargs is not None
    assert client.tls_kwargs.get("cert_reqs") == ssl.CERT_REQUIRED
    assert client.tls_insecure is False
    assert client.loop_started is True

    sample = _sample_dict()
    pub.publish(sample)
    assert len(client.publishes) == 1
    sent = client.publishes[0]
    assert sent["topic"] == "enviropi/enviroplus/state"
    assert sent["qos"] == 1
    assert sent["retain"] is True
    assert json.loads(sent["payload"]) == json.loads(encode_payload(sample))

    pub.stop()
    assert client.disconnected is True
    assert client.loop_stopped is True


def test_mqtt_publish_failure_does_not_raise(monkeypatch):
    class BoomClient(_FakeMqttClient):
        def publish(self, topic, payload, qos=0, retain=False):
            raise OSError("broker down")

    BoomClient.instances = []
    monkeypatch.setattr("enviropi.mqtt.Client", BoomClient)
    env = EnvSettings(
        mqtt_enabled=True,
        mqtt_host="homeserver.example.ts.net",
        mqtt_port=8883,
        mqtt_username="enviropi",
        mqtt_password="test-password",
        mqtt_topic="enviropi/enviroplus/state",
        mqtt_tls=True,
        mqtt_tls_insecure=False,
    )
    pub = MqttPublisher(env)
    pub.publish(_sample_dict())  # must not raise
    pub.stop()
