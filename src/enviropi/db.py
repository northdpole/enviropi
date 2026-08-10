from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS samples (
    ts TEXT NOT NULL PRIMARY KEY,
    temperature REAL,
    humidity REAL,
    pressure REAL,
    lux REAL,
    noise REAL,
    gas_reducing REAL,
    gas_oxidising REAL,
    gas_nh3 REAL
);

CREATE TABLE IF NOT EXISTS samples_hourly (
    hour_ts TEXT NOT NULL PRIMARY KEY,
    temperature REAL,
    humidity REAL,
    pressure REAL,
    lux REAL,
    noise REAL,
    gas_reducing REAL,
    gas_oxidising REAL,
    gas_nh3 REAL,
    temperature_min REAL,
    temperature_max REAL,
    humidity_min REAL,
    humidity_max REAL,
    sample_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings_overrides (
    key TEXT NOT NULL PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_state (
    condition_key TEXT NOT NULL PRIMARY KEY,
    last_fired_at TEXT,
    last_value REAL,
    active INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS users (
    sub TEXT NOT NULL PRIMARY KEY,
    email TEXT NOT NULL,
    name TEXT,
    last_login TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT NOT NULL PRIMARY KEY,
    sub TEXT NOT NULL,
    email TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY (sub) REFERENCES users(sub)
);

CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
CREATE INDEX IF NOT EXISTS idx_hourly_ts ON samples_hourly(hour_ts);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
"""

SAMPLE_COLUMNS = (
    "temperature",
    "humidity",
    "pressure",
    "lux",
    "noise",
    "gas_reducing",
    "gas_oxidising",
    "gas_nh3",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    """UTC timestamp string that SQLite datetime functions accept."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Sample:
    ts: datetime
    temperature: float | None = None
    humidity: float | None = None
    pressure: float | None = None
    lux: float | None = None
    noise: float | None = None
    gas_reducing: float | None = None
    gas_oxidising: float | None = None
    gas_nh3: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": to_iso(self.ts),
            "temperature": self.temperature,
            "humidity": self.humidity,
            "pressure": self.pressure,
            "lux": self.lux,
            "noise": self.noise,
            "gas_reducing": self.gas_reducing,
            "gas_oxidising": self.gas_oxidising,
            "gas_nh3": self.gas_nh3,
        }


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Cursor]:
        conn = self.connect()
        try:
            cur = conn.cursor()
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self.cursor() as cur:
            cur.executescript(SCHEMA)

    def insert_sample(self, sample: Sample) -> None:
        d = sample.as_dict()
        cols = ", ".join(["ts", *SAMPLE_COLUMNS])
        placeholders = ", ".join(["?"] * (1 + len(SAMPLE_COLUMNS)))
        values = [d["ts"], *[d[c] for c in SAMPLE_COLUMNS]]
        with self.cursor() as cur:
            cur.execute(
                f"INSERT OR REPLACE INTO samples ({cols}) VALUES ({placeholders})",
                values,
            )

    def latest_sample(self) -> dict[str, Any] | None:
        with self.cursor() as cur:
            row = cur.execute("SELECT * FROM samples ORDER BY ts DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def get_overrides(self) -> dict[str, str]:
        with self.cursor() as cur:
            rows = cur.execute("SELECT key, value FROM settings_overrides").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def set_override(self, key: str, value: str, updated_by: str) -> None:
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO settings_overrides (key, value, updated_at, updated_by)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at,
                    updated_by=excluded.updated_by
                """,
                (key, value, to_iso(utc_now()), updated_by),
            )

    def delete_override(self, key: str) -> bool:
        with self.cursor() as cur:
            cur.execute("DELETE FROM settings_overrides WHERE key = ?", (key,))
            return cur.rowcount > 0

    def get_alert_state(self, condition_key: str) -> dict[str, Any] | None:
        with self.cursor() as cur:
            row = cur.execute(
                "SELECT * FROM alert_state WHERE condition_key = ?",
                (condition_key,),
            ).fetchone()
        return dict(row) if row else None

    def upsert_alert_state(
        self,
        condition_key: str,
        *,
        last_fired_at: str | None = None,
        last_value: float | None = None,
        active: bool = False,
    ) -> None:
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO alert_state (condition_key, last_fired_at, last_value, active)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(condition_key) DO UPDATE SET
                    last_fired_at=COALESCE(excluded.last_fired_at, alert_state.last_fired_at),
                    last_value=excluded.last_value,
                    active=excluded.active
                """,
                (condition_key, last_fired_at, last_value, 1 if active else 0),
            )

    def rollup_hourly(self, until: datetime | None = None) -> int:
        """Aggregate completed hours from samples into samples_hourly. Returns hours written."""
        until = until or utc_now()
        # Only roll up hours strictly before the current hour
        current_hour = until.replace(minute=0, second=0, microsecond=0)
        with self.cursor() as cur:
            rows = cur.execute(
                """
                SELECT
                    strftime('%Y-%m-%dT%H:00:00Z', ts) AS hour_ts,
                    AVG(temperature) AS temperature,
                    AVG(humidity) AS humidity,
                    AVG(pressure) AS pressure,
                    AVG(lux) AS lux,
                    AVG(noise) AS noise,
                    AVG(gas_reducing) AS gas_reducing,
                    AVG(gas_oxidising) AS gas_oxidising,
                    AVG(gas_nh3) AS gas_nh3,
                    MIN(temperature) AS temperature_min,
                    MAX(temperature) AS temperature_max,
                    MIN(humidity) AS humidity_min,
                    MAX(humidity) AS humidity_max,
                    COUNT(*) AS sample_count
                FROM samples
                WHERE ts < ?
                GROUP BY 1
                """,
                (to_iso(current_hour),),
            ).fetchall()
            count = 0
            for row in rows:
                cur.execute(
                    """
                    INSERT INTO samples_hourly (
                        hour_ts, temperature, humidity, pressure, lux, noise,
                        gas_reducing, gas_oxidising, gas_nh3,
                        temperature_min, temperature_max, humidity_min, humidity_max,
                        sample_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(hour_ts) DO UPDATE SET
                        temperature=excluded.temperature,
                        humidity=excluded.humidity,
                        pressure=excluded.pressure,
                        lux=excluded.lux,
                        noise=excluded.noise,
                        gas_reducing=excluded.gas_reducing,
                        gas_oxidising=excluded.gas_oxidising,
                        gas_nh3=excluded.gas_nh3,
                        temperature_min=excluded.temperature_min,
                        temperature_max=excluded.temperature_max,
                        humidity_min=excluded.humidity_min,
                        humidity_max=excluded.humidity_max,
                        sample_count=excluded.sample_count
                    """,
                    (
                        row["hour_ts"],
                        row["temperature"],
                        row["humidity"],
                        row["pressure"],
                        row["lux"],
                        row["noise"],
                        row["gas_reducing"],
                        row["gas_oxidising"],
                        row["gas_nh3"],
                        row["temperature_min"],
                        row["temperature_max"],
                        row["humidity_min"],
                        row["humidity_max"],
                        row["sample_count"],
                    ),
                )
                count += 1
        return count

    def prune_raw_samples(self, retention_days: int) -> int:
        cutoff = utc_now().timestamp() - retention_days * 86400
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        with self.cursor() as cur:
            cur.execute("DELETE FROM samples WHERE ts < ?", (cutoff_iso,))
            return cur.rowcount

    def history(
        self,
        *,
        since: datetime,
        until: datetime | None = None,
        use_hourly: bool = False,
    ) -> list[dict[str, Any]]:
        until = until or utc_now()
        table = "samples_hourly" if use_hourly else "samples"
        ts_col = "hour_ts" if use_hourly else "ts"
        with self.cursor() as cur:
            rows = cur.execute(
                f"""
                SELECT * FROM {table}
                WHERE {ts_col} >= ? AND {ts_col} <= ?
                ORDER BY {ts_col} ASC
                """,
                (to_iso(since), to_iso(until)),
            ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            if use_hourly:
                d["ts"] = d.pop("hour_ts")
            result.append(d)
        return result

    def upsert_user(self, sub: str, email: str, name: str | None) -> None:
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (sub, email, name, last_login)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(sub) DO UPDATE SET
                    email=excluded.email,
                    name=excluded.name,
                    last_login=excluded.last_login
                """,
                (sub, email.lower(), name, to_iso(utc_now())),
            )

    def create_session(self, session_id: str, sub: str, email: str, expires_at: datetime) -> None:
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sessions (session_id, sub, email, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, sub, email.lower(), to_iso(utc_now()), to_iso(expires_at)),
            )

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self.cursor() as cur:
            row = cur.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        if d["expires_at"] < to_iso(utc_now()):
            self.delete_session(session_id)
            return None
        return d

    def delete_session(self, session_id: str) -> None:
        with self.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    def purge_expired_sessions(self) -> None:
        with self.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE expires_at < ?", (to_iso(utc_now()),))

    def recent_gas_baseline(self, metric: str, minutes: int = 60) -> float | None:
        if metric not in SAMPLE_COLUMNS:
            return None
        since = datetime.fromtimestamp(utc_now().timestamp() - minutes * 60, tz=timezone.utc)
        with self.cursor() as cur:
            row = cur.execute(
                f"SELECT AVG({metric}) AS avg_val FROM samples WHERE ts >= ?",
                (to_iso(since),),
            ).fetchone()
        if not row or row["avg_val"] is None:
            return None
        return float(row["avg_val"])

    def sample_near(
        self, *, minutes_ago: int, max_skew_min: int | None = None
    ) -> dict[str, Any] | None:
        """Return the newest sample at or before (now - minutes_ago).

        If max_skew_min is set, reject priors older than minutes_ago + max_skew_min
        (avoids comparing to pre-downtime samples after a restart gap).
        """
        if minutes_ago <= 0:
            return self.latest_sample()
        now = utc_now()
        target = datetime.fromtimestamp(now.timestamp() - minutes_ago * 60, tz=timezone.utc)
        with self.cursor() as cur:
            row = cur.execute(
                "SELECT * FROM samples WHERE ts <= ? ORDER BY ts DESC LIMIT 1",
                (to_iso(target),),
            ).fetchone()
        if not row:
            return None
        sample = dict(row)
        if max_skew_min is not None:
            try:
                ts = datetime.fromisoformat(str(sample["ts"]).replace("Z", "+00:00"))
            except ValueError:
                return None
            age_min = (now - ts).total_seconds() / 60.0
            if age_min > minutes_ago + max_skew_min:
                return None
        return sample

    def interval_stats(
        self, *, since: datetime, until: datetime | None = None
    ) -> dict[str, Any] | None:
        """Min/max per metric between since (exclusive) and until (inclusive)."""
        until = until or utc_now()
        aggs = ", ".join(
            f"MIN({c}) AS {c}_min, MAX({c}) AS {c}_max" for c in SAMPLE_COLUMNS
        )
        with self.cursor() as cur:
            row = cur.execute(
                f"""
                SELECT COUNT(*) AS sample_count, {aggs}
                FROM samples
                WHERE ts > ? AND ts <= ?
                """,
                (to_iso(since), to_iso(until)),
            ).fetchone()
        if not row or not row["sample_count"]:
            return None
        return dict(row)
