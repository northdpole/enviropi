from __future__ import annotations

import logging
from datetime import timedelta

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from enviropi.config import OVERRIDE_KEYS, AppConfig, effective_threshold_map, merge_overrides
from enviropi.db import Database, to_iso, utc_now

logger = logging.getLogger(__name__)


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
        self.allowlist = set(allowlist)
        self.app: Application | None = None

    def build(self) -> Application:
        app = Application.builder().token(self.token).build()
        app.add_handler(CommandHandler("start", self.cmd_start))
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
        if not self.allowlist:
            logger.warning("telegram_allowlist is empty; denying commands")
            return False
        return user.id in self.allowlist

    async def _deny(self, update: Update) -> None:
        if update.message:
            await update.message.reply_text("Unauthorized.")

    async def send_message(self, text: str) -> None:
        if not self.app or not self.alert_chat_id:
            logger.warning("Cannot send Telegram message: app/chat not configured")
            return
        await self.app.bot.send_message(chat_id=self.alert_chat_id, text=text)

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._deny(update)
            return
        assert update.message
        await update.message.reply_text(
            "EnviroPi bot ready.\n"
            "Commands: /status /alerts /set /reset /mute /unmute"
        )

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._deny(update)
            return
        assert update.message
        sample = self.db.latest_sample()
        if not sample:
            await update.message.reply_text("No samples yet.")
            return
        lines = [
            f"Latest @ {sample['ts']}",
            f"Temp: {sample['temperature']} °C",
            f"Humidity: {sample['humidity']} %",
            f"Pressure: {sample['pressure']} hPa",
            f"Lux: {sample['lux']}",
            f"Noise: {sample['noise']}",
            f"Reducing: {sample['gas_reducing']} Ω",
            f"Oxidising: {sample['gas_oxidising']} Ω",
            f"NH3: {sample['gas_nh3']} Ω",
        ]
        await update.message.reply_text("\n".join(lines))

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
