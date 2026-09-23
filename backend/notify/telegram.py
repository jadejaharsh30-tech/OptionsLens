# optionslens/backend/notify/telegram.py
"""
Telegram delivery.

Chosen as the first channel because the setup cost is a bot token and one HTTP
POST — no business verification, no paid intermediary, and it reaches a phone
that is already in your pocket during market hours.

Setup:
  1. Message @BotFather on Telegram, send /newbot, follow the prompts.
  2. Put the token in TELEGRAM_BOT_TOKEN.
  3. Message your new bot once, then open
     https://api.telegram.org/bot<TOKEN>/getUpdates and read chat.id.
  4. Put that in TELEGRAM_CHAT_ID.

Both are read from the environment. A missing token disables the channel
quietly rather than raising, so an unconfigured deployment still runs.
"""
import html
import logging
import os
from typing import Optional

from notify.base import Notification, NotificationChannel, Severity

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
TIMEOUT_SEC = 10.0

# Telegram hard-limits messages to 4096 characters.
MAX_MESSAGE_LEN = 4000


class TelegramChannel(NotificationChannel):
    """Sends via the Telegram Bot API."""

    def __init__(self, bot_token: Optional[str] = None,
                 chat_id: Optional[str] = None):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id   = chat_id   or os.getenv("TELEGRAM_CHAT_ID", "")

    @property
    def name(self) -> str:
        return "telegram"

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def _format(self, n: Notification) -> str:
        """
        HTML-formatted message body.

        User-supplied strings are escaped: a strike description containing an
        angle bracket would otherwise make Telegram reject the whole message,
        and the failure would look like a delivery problem rather than a
        formatting one.
        """
        icon = {
            Severity.INFO: "ℹ️", Severity.SIGNAL: "📊", Severity.TRADE: "💼",
            Severity.WARNING: "⚠️", Severity.CRITICAL: "🚨",
        }[n.severity]

        lines = [f"{icon} <b>{html.escape(n.title)}</b>", "",
                 html.escape(n.body)]

        if n.meta:
            lines.append("")
            for k, v in n.meta.items():
                lines.append(f"<code>{html.escape(str(k))}: "
                             f"{html.escape(str(v))}</code>")

        text = "\n".join(lines)
        if len(text) > MAX_MESSAGE_LEN:
            text = text[:MAX_MESSAGE_LEN - 20] + "\n… (truncated)"
        return text

    async def send(self, notification: Notification) -> bool:
        if not self.configured:
            return False

        # Imported lazily: a missing notification dependency must never be able
        # to prevent the API from starting.
        import httpx

        url = f"{API_BASE}/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": self._format(notification),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SEC) as client:
                resp = await client.post(url, json=payload)
            if resp.status_code == 200 and resp.json().get("ok"):
                return True
            # Log the body: Telegram's errors ("chat not found", "bot was
            # blocked") are specific and actionable, unlike the status code.
            logger.error(f"Telegram send failed [{resp.status_code}]: {resp.text}")
            return False
        except Exception as e:
            logger.error(f"Telegram request error: {e!r}")
            return False

    async def verify(self) -> dict:
        """
        Check the credentials without sending anything.

        Returns a diagnosis rather than a bare bool — 'token valid but chat_id
        wrong' and 'token invalid' need different fixes, and the distinction is
        exactly what a user setting this up for the first time needs.
        """
        if not self.bot_token:
            return {"ok": False, "error": "TELEGRAM_BOT_TOKEN is not set"}

        import httpx

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SEC) as client:
                resp = await client.get(f"{API_BASE}/bot{self.bot_token}/getMe")
            data = resp.json()
            if not data.get("ok"):
                return {"ok": False,
                        "error": f"Token rejected: {data.get('description')}"}

            bot = data["result"]
            if not self.chat_id:
                return {
                    "ok": False, "bot_username": bot.get("username"),
                    "error": "TELEGRAM_CHAT_ID is not set. Message your bot, "
                             "then read chat.id from /getUpdates.",
                }
            return {"ok": True, "bot_username": bot.get("username"),
                    "chat_id": self.chat_id}
        except Exception as e:
            return {"ok": False, "error": f"Could not reach Telegram: {e!r}"}
