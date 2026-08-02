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
    "cooldown_sec",
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


class AppConfig(BaseModel):
    poll_interval_sec: int = 60
    raw_retention_days: int = 14
    dashboard_url: str = "http://127.0.0.1:8000"
    temp_compensation_factor: float = 2.25
    cooldown_sec: int = 1800
    hysteresis: dict[str, float] = Field(default_factory=dict)
    gas: GasConfig = Field(default_factory=GasConfig)
    temperature: ThresholdPair = Field(default_factory=lambda: ThresholdPair(high=28.0, low=10.0))
    humidity: ThresholdPair = Field(default_factory=lambda: ThresholdPair(high=70.0, low=30.0))
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
    telegram_bot_token: str = ""
    telegram_alert_chat_id: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    oauth_redirect_uri: str = "http://127.0.0.1:8000/auth/callback"
    oauth_allowlist: str = ""
    session_secret: str = "change-me"
    web_host: str = "127.0.0.1"
    web_port: int = 8000

    @property
    def oauth_emails(self) -> set[str]:
        return {e.strip().lower() for e in self.oauth_allowlist.split(",") if e.strip()}


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


def merge_overrides(base: AppConfig, overrides: dict[str, str]) -> AppConfig:
    """Apply SQLite string overrides on top of YAML config."""
    data = base.model_dump()
    for key, raw in overrides.items():
        if key == "mute_until":
            # Stored separately in alert flow; keep on root for effective settings display
            data["mute_until"] = raw
            continue
        if key not in OVERRIDE_KEYS and key != "mute_until":
            continue
        if raw.lower() in ("null", "none", ""):
            parsed: Any = None
        else:
            try:
                parsed = int(raw) if key == "cooldown_sec" else float(raw)
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
    return load_yaml_config(env.enviropi_config)


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
        "cooldown_sec": merged.cooldown_sec,
        "mute_until": overrides.get("mute_until"),
    }
    return out


def project_root() -> Path:
    return Path(os.getcwd())
