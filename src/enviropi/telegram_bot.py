from __future__ import annotations

import logging
from datetime import timedelta

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from enviropi.config import OVERRIDE_KEYS, AppConfig, effective_threshold_map
from enviropi.db import Database, to_iso, utc_now

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "EnviroPi bot ready.\n"
    "Commands:\n"
    "/help — this message\n"
    "/status — latest readings + threshold refs\n"
    "/alerts — effective thresholds\n"
    "/set <key> <value> — override threshold\n"
    "/reset <key> — clear override\n"
    "/mute [hours] — silence alerts\n"
    "/unmute — resume alerts"
)


def _fmt_ref(value: object) -> str:
    if value is None:
        return "off"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
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
        line("Temp", sample["temperature"], " °C", "temperature"),
        line("Humidity", sample["humidity"], " %", "humidity"),
        line("Pressure", sample["pressure"], " hPa", "pressure"),
        line("Lux", sample["lux"], "", "lux"),
        line("Noise", sample["noise"], "", "noise"),
        line("Reducing", sample["gas_reducing"], " Ω", "gas_reducing"),
        line("Oxidising", sample["gas_oxidising"], " Ω", "gas_oxidising"),
        line("NH3", sample["gas_nh3"], " Ω", "gas_nh3"),
    ]


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

    async def cmd_set(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._deny(update)
            return
        assert update.message
        if not context.args or len(context.args) < 2:
            await update.message.reply_text("Usage: /set <key> <value>")
            return
        key = context.args[0]
        value = context.args[1]
        if key not in OVERRIDE_KEYS:
            await update.message.reply_text(
                "Unknown key. Allowed:\n" + ", ".join(OVERRIDE_KEYS)
            )
            return
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
