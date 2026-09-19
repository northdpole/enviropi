from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


METRIC_KEYS = (
    "temperature",
    "humidity",
    "pressure",
    "lux",
    "noise",
    "gas_reducing",
    "gas_oxidising",
    "gas_nh3",
)

# Flat override keys supported by Telegram / dashboard
OVERRIDE_KEYS = (
    "temperature.high",
    "temperature.low",
    "humidity.high",
    "humidity.low",
    "pressure.high",
    "pressure.low",
    "gas_reducing.high",
    "gas_oxidising.high",
    "gas_nh3.high",
    "noise.high",
    "lux.high",
    "lux.low",
    "status_report.times",
    "status_report.enabled",
    "mute_until",
)


class ThresholdPair(BaseModel):
    high: float | None = None
    low: float | None = None


class GasHighOnly(BaseModel):
    high: float | None = None


class GasConfig(BaseModel):
    baseline_warmup_min: int = 30
    relative_change_pct: float | None = None


class StatusReportConfig(BaseModel):
    """Scheduled Telegram digests with current readings."""

    enabled: bool = True
    # Local wall-clock times (HH:MM) in `timezone`
    times: list[str] = Field(default_factory=lambda: ["08:00", "20:00"])
    timezone: str = "Europe/London"


class CatastropheConfig(BaseModel):
    """Sudden rate-of-change alerts (fire / flood / extreme gas)."""

    enabled: bool = True
    window_min: int = 5
    # Absolute rises within the window
    temperature_rise: float = 5.0
    humidity_rise: float = 25.0
    # Optional sudden pressure drop (hPa); null = off
    pressure_drop: float | None = 8.0
    # Gas resistance % move vs sample ~window_min ago
    gas_drop_pct: float = 50.0  # reducing / NH3: lower Ω = more gas
    gas_rise_pct: float = 50.0  # oxidising: higher Ω = more NO2-like
    # Optional sudden lux jump; null = off (no PM sensor — lux is a weak fire proxy)
    lux_rise: float | None = None
    # After a catastrophe clears, wait this long before the same metric can fire again
    cooldown_sec: int = 1800


class AppConfig(BaseModel):
    poll_interval_sec: int = 60
    raw_retention_days: int = 14
    dashboard_url: str = "http://127.0.0.1:8000"
    temp_compensation_factor: float = 2.25
    # Kept for older config.yaml files; threshold alerts are edge-triggered (unused).
    cooldown_sec: int = 1800
    hysteresis: dict[str, float] = Field(
        default_factory=lambda: {
            "temperature": 1.5,
            "humidity": 3.0,
            "pressure": 1.0,
            "gas_reducing": 1000.0,
            "gas_oxidising": 1000.0,
            "gas_nh3": 1000.0,
            "noise": 0.05,
            "lux": 50.0,
        }
    )
    gas: GasConfig = Field(default_factory=GasConfig)
    status_report: StatusReportConfig = Field(default_factory=StatusReportConfig)
    catastrophe: CatastropheConfig = Field(default_factory=CatastropheConfig)
    temperature: ThresholdPair = Field(default_factory=lambda: ThresholdPair(high=33.0, low=10.0))
    humidity: ThresholdPair = Field(default_factory=lambda: ThresholdPair(high=70.0, low=20.0))
    pressure: ThresholdPair = Field(default_factory=ThresholdPair)
    gas_reducing: GasHighOnly = Field(default_factory=GasHighOnly)
    gas_oxidising: GasHighOnly = Field(default_factory=GasHighOnly)
    gas_nh3: GasHighOnly = Field(default_factory=GasHighOnly)
    noise: GasHighOnly = Field(default_factory=GasHighOnly)
    lux: ThresholdPair = Field(default_factory=ThresholdPair)
    telegram_allowlist: list[int] = Field(default_factory=list)
    notify_on_startup: bool = True


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    enviropi_config: Path = Path("./config.yaml")
    enviropi_db: Path = Path("./data/enviropi.db")
    enviropi_mock_sensors: bool = True
    # Deploy identity (also used by push-to-pi.sh). App uses host for dashboard_url.
    enviropi_service_user: str = ""
    enviropi_tailscale_host: str = ""
    # Optional full URL override; else derived from ENVIROPI_TAILSCALE_HOST + WEB_PORT.
    enviropi_dashboard_url: str = ""
    telegram_bot_token: str = ""
    telegram_alert_chat_id: str = ""
    # Comma-separated Telegram numeric user ids for bot commands (optional;
    # private TELEGRAM_ALERT_CHAT_ID is also auto-allowed).
    telegram_allowlist: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    oauth_redirect_uri: str = "http://127.0.0.1:8000/auth/callback"
    oauth_allowlist: str = ""
    session_secret: str = "change-me"
    # When false, enviropi-web exits without binding; collector keeps running.
    dashboard_enabled: bool = True
    # Enviro+ LCD: proximity-wake only (not permanently on). Ignored when mock.
    display_enabled: bool = True
    web_host: str = "127.0.0.1"
    web_port: int = 8000
    # MQTT publisher (collector → Mosquitto). Password lives in .env, never git.
    mqtt_enabled: bool = False
    mqtt_host: str = "homeserver.example.ts.net"
    mqtt_port: int = 8883
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_topic: str = "enviropi/enviroplus/state"
    mqtt_tls: bool = True
    mqtt_tls_insecure: bool = False

    @property
    def oauth_emails(self) -> set[str]:
        return {e.strip().lower() for e in self.oauth_allowlist.split(",") if e.strip()}


def parse_telegram_allowlist(raw: str | None) -> list[int]:
    """Parse TELEGRAM_ALLOWLIST=id1,id2 into integer user ids."""
    if not raw:
        return []
    out: list[int] = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            continue
    return out


def load_yaml_config(path: Path) -> AppConfig:
    if not path.exists():
        return AppConfig()
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    return AppConfig.model_validate(data)


def _set_nested(cfg: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur: dict[str, Any] = cfg
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def parse_digest_times(raw: str) -> list[str]:
    """Parse '08:00,20:00' or '8:00 20:00' into normalized HH:MM list."""
    parts = [p.strip() for p in raw.replace(",", " ").split() if p.strip()]
    if not parts:
        raise ValueError("at least one HH:MM time required")
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        try:
            hh_s, mm_s = part.split(":", 1)
            hh, mm = int(hh_s), int(mm_s)
        except ValueError as exc:
            raise ValueError(f"invalid time {part!r}; use HH:MM") from exc
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError(f"invalid time {part!r}; use HH:MM")
        norm = f"{hh:02d}:{mm:02d}"
        if norm not in seen:
            seen.add(norm)
            out.append(norm)
    return sorted(out)


def merge_overrides(base: AppConfig, overrides: dict[str, str]) -> AppConfig:
    """Apply SQLite string overrides on top of YAML config."""
    data = base.model_dump()
    for key, raw in overrides.items():
        if key == "mute_until":
            # Stored separately in alert flow; keep on root for effective settings display
            data["mute_until"] = raw
            continue
        if key == "status_report_last_slot":
            continue
        if key not in OVERRIDE_KEYS and key != "mute_until":
            continue
        if key == "status_report.times":
            try:
                parsed: Any = parse_digest_times(raw)
            except ValueError:
                continue
            _set_nested(data, key, parsed)
            continue
        if key == "status_report.enabled":
            _set_nested(data, key, raw.lower() in ("1", "true", "yes", "on"))
            continue
        if raw.lower() in ("null", "none", ""):
            parsed = None
        else:
            try:
                parsed = float(raw)
            except ValueError:
                parsed = raw
        _set_nested(data, key, parsed)
    # mute_until is not an AppConfig field — strip before validate
    data.pop("mute_until", None)
    return AppConfig.model_validate(data)


@lru_cache
def get_env() -> EnvSettings:
    load_dotenv()
    return EnvSettings()


def get_config(env: EnvSettings | None = None) -> AppConfig:
    env = env or get_env()
    cfg = load_yaml_config(env.enviropi_config)
    updates: dict[str, Any] = {}

    env_ids = parse_telegram_allowlist(env.telegram_allowlist)
    if env_ids:
        merged = list(dict.fromkeys([*cfg.telegram_allowlist, *env_ids]))
        updates["telegram_allowlist"] = merged

    dash = (env.enviropi_dashboard_url or "").strip()
    if dash:
        updates["dashboard_url"] = dash.rstrip("/")
    else:
        host = (env.enviropi_tailscale_host or "").strip()
        if host:
            updates["dashboard_url"] = f"http://{host}:{env.web_port}"

    if updates:
        cfg = cfg.model_copy(update=updates)
    return cfg


def effective_threshold_map(cfg: AppConfig, overrides: dict[str, str]) -> dict[str, Any]:
    """Flat map of effective threshold keys for display."""
    merged = merge_overrides(cfg, overrides)
    out: dict[str, Any] = {
        "temperature.high": merged.temperature.high,
        "temperature.low": merged.temperature.low,
        "humidity.high": merged.humidity.high,
        "humidity.low": merged.humidity.low,
        "pressure.high": merged.pressure.high,
        "pressure.low": merged.pressure.low,
        "gas_reducing.high": merged.gas_reducing.high,
        "gas_oxidising.high": merged.gas_oxidising.high,
        "gas_nh3.high": merged.gas_nh3.high,
        "noise.high": merged.noise.high,
        "lux.high": merged.lux.high,
        "lux.low": merged.lux.low,
        "status_report.times": ",".join(merged.status_report.times),
        "status_report.enabled": merged.status_report.enabled,
        "mute_until": overrides.get("mute_until"),
    }
    return out


def project_root() -> Path:
    return Path(os.getcwd())
