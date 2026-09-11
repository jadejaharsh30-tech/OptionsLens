# optionslens/backend/routers/notifications.py
"""
Notification configuration and testing.

GET  /api/notify/status  → which channels are configured
POST /api/notify/verify  → check Telegram credentials without sending
POST /api/notify/test    → send a real test message

`verify` exists separately from `test` because the two failure modes need
different fixes: a rejected token and a wrong chat_id both look like "it didn't
arrive", and only verify can tell them apart.
"""
import logging

from fastapi import APIRouter, Depends

from auth import get_token
from notify import get_dispatcher
from notify.base import Notification, Severity
from notify.telegram import TelegramChannel

router = APIRouter(prefix="/api/notify", tags=["notifications"])
logger = logging.getLogger(__name__)


@router.get("/status")
def status(token: str = Depends(get_token)):
    """Which channels are live, and how to enable the ones that are not."""
    dispatcher = get_dispatcher()
    telegram = TelegramChannel()
    return {
        "active_channels": dispatcher.active_channels,
        "channels": [
            {
                "name": c.name,
                "configured": c.configured,
                "setup": (
                    "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID. Create a bot "
                    "with @BotFather, message it once, then read chat.id from "
                    "https://api.telegram.org/bot<TOKEN>/getUpdates"
                ) if c.name == "telegram" and not c.configured else None,
            }
            for c in dispatcher.channels
        ],
        "dedupe_window_sec": int(dispatcher.dedupe_window.total_seconds()),
        "min_severity": dispatcher.min_severity.value,
        "telegram_configured": telegram.configured,
    }


@router.post("/verify")
async def verify(token: str = Depends(get_token)):
    """Validate Telegram credentials without sending a message."""
    return await TelegramChannel().verify()


@router.post("/test")
async def test(token: str = Depends(get_token)):
    """Send a real test notification through every configured channel."""
    dispatcher = get_dispatcher()
    if not dispatcher.active_channels:
        return {"sent": {}, "note": "No channels configured — nothing to send."}

    result = await dispatcher.send(Notification(
        title="OptionsLens test",
        body="Notifications are working. Trade and risk alerts will arrive here.",
        severity=Severity.INFO,
        meta={"source": "manual test"},
    ))
    return {"sent": result, "channels": dispatcher.active_channels}
