from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from enviropi.config import (
    OVERRIDE_KEYS,
    AppConfig,
    effective_threshold_map,
    merge_overrides,
    parse_digest_times,
)
from enviropi.db import Database, to_iso, utc_now

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "EnviroPi bot ready.\n"
    "Commands:\n"
    "/help — this message\n"
    "/status — latest readings + threshold refs\n"
    "/alerts — effective thresholds\n"
    "/digest — show or set status report times\n"
    "/set <key> <value> — override threshold\n"
    "/reset <key> — clear override\n"
    "/mute [hours] — silence alerts\n"
    "/unmute — resume alerts"
)

DIGEST_HELP = (
    "Status digests (current readings + period highs/lows).\n"
    "Usage:\n"
    "/digest — show schedule\n"
    "/digest 08:00 20:00 — set local times\n"
    "/digest on|off — enable or disable\n"
    "/digest reset — restore config.yaml defaults"
)

METRIC_LINES = (
    ("Temp", "temperature", " °C"),
    ("Humidity", "humidity", " %"),
    ("Pressure", "pressure", " hPa"),
    ("Lux", "lux", ""),
    ("Noise", "noise", ""),
    ("Reducing", "gas_reducing", " Ω"),
    ("Oxidising", "gas_oxidising", " Ω"),
    ("NH3", "gas_nh3", " Ω"),
)


def _fmt_ref(value: object) -> str:
    if value is None:
        return "off"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _fmt_num(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:g}"
    return str(value)


def format_status_lines(sample: dict, thresholds: dict) -> list[str]:
    """Latest readings with effective low/high reference thresholds."""

    def line(label: str, value: object, unit: str, key: str) -> str:
        low = thresholds.get(f"{key}.low")
        high = thresholds.get(f"{key}.high")
        # Gas / noise only define .high in config (still show low=off).
        return (
            f"{label}: {value}{unit} "
            f"({_fmt_ref(low)} low, {_fmt_ref(high)} hi)"
        )

    return [
        f"Latest @ {sample['ts']}",
        *[
            line(label, sample[key], unit, key)
            for label, key, unit in METRIC_LINES
        ],
    ]


def format_digest_lines(
    sample: dict,
    thresholds: dict,
    *,
    interval_label: str,
    stats: dict | None,
) -> list[str]:
    """Status digest: current values plus period min/max."""
    lines = format_status_lines(sample, thresholds)
    lines.append("")
    lines.append(f"Since {interval_label}:")
    if not stats:
        lines.append("  (no samples in interval)")
        return lines
    for label, key, unit in METRIC_LINES:
        lo = stats.get(f"{key}_min")
        hi = stats.get(f"{key}_max")
        if lo is None and hi is None:
            continue
        lines.append(f"{label}: {_fmt_num(lo)}–{_fmt_num(hi)}{unit}")
    return lines


def previous_digest_start(slot_local: datetime, times: list[str]) -> datetime:
    """Local datetime of the digest slot immediately before `slot_local`."""
    parsed: list[tuple[int, int]] = []
    for raw in times:
        hh_s, mm_s = raw.split(":", 1)
        parsed.append((int(hh_s), int(mm_s)))
    parsed = sorted(set(parsed))
    if not parsed:
        return slot_local - timedelta(hours=12)

    slot_hm = (slot_local.hour, slot_local.minute)
    # Prefer the configured slot matching this digest; else nearest <= now.
    if slot_hm in parsed:
        idx = parsed.index(slot_hm)
        prev_hm = parsed[idx - 1]
    else:
        earlier = [hm for hm in parsed if hm < slot_hm]
        prev_hm = earlier[-1] if earlier else parsed[-1]

    start = slot_local.replace(
        hour=prev_hm[0], minute=prev_hm[1], second=0, microsecond=0
    )
    if start >= slot_local.replace(second=0, microsecond=0):
        start -= timedelta(days=1)
    return start


def private_alert_user_id(alert_chat_id: str | None) -> int | None:
    """If alert_chat_id is a private chat, return that user id (equals chat id).

    Telegram private chats use a positive id identical to the user's id.
    Groups/supergroups use negative ids and cannot be mapped to one user.
    """
    if not alert_chat_id:
        return None
    try:
        cid = int(str(alert_chat_id).strip())
    except (TypeError, ValueError):
        return None
    return cid if cid > 0 else None


def effective_telegram_allowlist(allowlist: list[int], alert_chat_id: str | None) -> set[int]:
    """Command allowlist: configured user ids plus private alert recipient."""
    result = set(allowlist)
    alert_uid = private_alert_user_id(alert_chat_id)
    if alert_uid is not None:
        result.add(alert_uid)
    return result


class TelegramService:
    def __init__(
        self,
        token: str,
        alert_chat_id: str,
        db: Database,
        base_config: AppConfig,
        allowlist: list[int],
    ) -> None:
        self.token = token
        self.alert_chat_id = alert_chat_id
        self.db = db
        self.base_config = base_config
        configured = set(allowlist)
        self.allowlist = effective_telegram_allowlist(allowlist, alert_chat_id)
        if not configured and self.allowlist:
            logger.info(
                "telegram_allowlist empty; auto-allowing private TELEGRAM_ALERT_CHAT_ID user %s",
                next(iter(self.allowlist)),
            )
        elif not self.allowlist:
            logger.warning(
                "telegram_allowlist is empty and TELEGRAM_ALERT_CHAT_ID is not a "
                "private user chat; denying commands until configured"
            )
        self.app: Application | None = None

    def build(self) -> Application:
        app = Application.builder().token(self.token).build()
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("alerts", self.cmd_alerts))
        app.add_handler(CommandHandler("digest", self.cmd_digest))
        app.add_handler(CommandHandler("set", self.cmd_set))
        app.add_handler(CommandHandler("reset", self.cmd_reset))
        app.add_handler(CommandHandler("mute", self.cmd_mute))
        app.add_handler(CommandHandler("unmute", self.cmd_unmute))
        self.app = app
        return app

    def _authorized(self, update: Update) -> bool:
        user = update.effective_user
        if user is None:
            return False
        return user.id in self.allowlist

    async def _deny(self, update: Update) -> None:
        if not update.message:
            return
        user = update.effective_user
        logger.warning(
            "Telegram command denied for user_id=%s (allowlist empty=%s)",
            user.id if user else None,
            not self.allowlist,
        )
        await update.message.reply_text("Unauthorized.")

    async def send_message(self, text: str) -> None:
        if not self.app or not self.alert_chat_id:
            logger.warning("Cannot send Telegram message: app/chat not configured")
            return
        try:
            await self.app.bot.send_message(chat_id=self.alert_chat_id, text=text)
        except Exception:
            logger.exception("Telegram send failed (chat_id=%s)", self.alert_chat_id)

    def _effective_config(self) -> AppConfig:
        return merge_overrides(self.base_config, self.db.get_overrides())

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._deny(update)
            return
        assert update.message
        await update.message.reply_text(HELP_TEXT)

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._deny(update)
            return
        assert update.message
        await update.message.reply_text(HELP_TEXT)

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._deny(update)
            return
        assert update.message
        sample = self.db.latest_sample()
        if not sample:
            await update.message.reply_text("No samples yet.")
            return
        thresholds = effective_threshold_map(self.base_config, self.db.get_overrides())
        await update.message.reply_text(
            "\n".join(format_status_lines(sample, thresholds))
        )

    async def cmd_alerts(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._deny(update)
            return
        assert update.message
        overrides = self.db.get_overrides()
        effective = effective_threshold_map(self.base_config, overrides)
        lines = ["Effective thresholds:"]
        for key, val in effective.items():
            src = "override" if key in overrides else "config"
            lines.append(f"  {key} = {val} ({src})")
        await update.message.reply_text("\n".join(lines))

    async def cmd_digest(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._deny(update)
            return
        assert update.message
        args = context.args or []
        overrides = self.db.get_overrides()

        if not args:
            cfg = self._effective_config()
            src_times = (
                "override" if "status_report.times" in overrides else "config"
            )
            src_en = (
                "override" if "status_report.enabled" in overrides else "config"
            )
            state = "on" if cfg.status_report.enabled else "off"
            await update.message.reply_text(
                f"Status digest: {state} ({src_en})\n"
                f"Times: {', '.join(cfg.status_report.times)} ({src_times})\n"
                f"Timezone: {cfg.status_report.timezone}\n\n"
                f"{DIGEST_HELP}"
            )
            return

        head = args[0].lower()
        if head in ("help", "?"):
            await update.message.reply_text(DIGEST_HELP)
            return
        if head == "reset":
            self.db.delete_override("status_report.times")
            self.db.delete_override("status_report.enabled")
            cfg = self._effective_config()
            await update.message.reply_text(
                "Digest schedule reset to config defaults:\n"
                f"{'on' if cfg.status_report.enabled else 'off'} @ "
                f"{', '.join(cfg.status_report.times)} "
                f"({cfg.status_report.timezone})"
            )
            return
        if head in ("on", "off", "enable", "disable"):
            enabled = head in ("on", "enable")
            self.db.set_override(
                "status_report.enabled", "true" if enabled else "false", "telegram"
            )
            await update.message.reply_text(
                f"Status digests {'enabled' if enabled else 'disabled'}"
            )
            return

        try:
            times = parse_digest_times(" ".join(args))
        except ValueError as exc:
            await update.message.reply_text(f"{exc}\n\n{DIGEST_HELP}")
            return
        self.db.set_override("status_report.times", ",".join(times), "telegram")
        cfg = self._effective_config()
        try:
            ZoneInfo(cfg.status_report.timezone)
        except ZoneInfoNotFoundError:
            pass
        await update.message.reply_text(
            f"Digest times set to {', '.join(times)} "
            f"({cfg.status_report.timezone})"
        )

    async def cmd_set(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._deny(update)
            return
        assert update.message
        if not context.args or len(context.args) < 2:
            await update.message.reply_text("Usage: /set <key> <value>")
            return
        key = context.args[0]
        value = " ".join(context.args[1:])
        if key not in OVERRIDE_KEYS:
            await update.message.reply_text(
                "Unknown key. Allowed:\n" + ", ".join(OVERRIDE_KEYS)
            )
            return
        if key == "status_report.times":
            try:
                times = parse_digest_times(value)
            except ValueError as exc:
                await update.message.reply_text(str(exc))
                return
            value = ",".join(times)
        self.db.set_override(key, value, "telegram")
        await update.message.reply_text(f"Set {key} = {value}")

    async def cmd_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._deny(update)
            return
        assert update.message
        if not context.args:
            await update.message.reply_text("Usage: /reset <key>")
            return
        key = context.args[0]
        if self.db.delete_override(key):
            await update.message.reply_text(f"Reset {key} to config default")
        else:
            await update.message.reply_text(f"No override for {key}")

    async def cmd_mute(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._deny(update)
            return
        assert update.message
        hours = 1.0
        if context.args:
            try:
                hours = float(context.args[0])
            except ValueError:
                await update.message.reply_text("Usage: /mute [hours]")
                return
        until = utc_now() + timedelta(hours=hours)
        self.db.set_override("mute_until", to_iso(until), "telegram")
        await update.message.reply_text(f"Muted until {to_iso(until)}")

    async def cmd_unmute(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._deny(update)
            return
        assert update.message
        self.db.delete_override("mute_until")
        await update.message.reply_text("Alerts unmuted")
