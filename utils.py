import functools
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.ext import ContextTypes

from config import ALLOWED_USER_IDS

DURATION_PRESETS = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
}


def restricted(func):
    """Reject anyone not in ALLOWED_USER_IDS. Wrap every command handler with this."""
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id if update.effective_user else None
        if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
            if update.effective_message:
                await update.effective_message.reply_text("This bot is private.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper


def parse_duration(token: str) -> datetime | None:
    """Turn '24h'/'7d'/'30d'/'90d' (or 'off') into an absolute expiry datetime, or None."""
    token = token.strip().lower()
    if token in ("off", "none", "never"):
        return None
    delta = DURATION_PRESETS.get(token)
    if not delta:
        raise ValueError(f"Unknown duration '{token}'. Use one of: 24h, 7d, 30d, 90d, off")
    return datetime.now(timezone.utc) + delta


def is_expired(session: dict) -> bool:
    exp = session.get("expires_at")
    return bool(exp and exp < datetime.now(timezone.utc))


def file_type_and_id(message):
    """Return (file_type, file_id) for whichever attachment is on this message, or (None, None)."""
    if message.photo:
        return "photo", message.photo[-1].file_id
    if message.video:
        return "video", message.video.file_id
    if message.document:
        return "document", message.document.file_id
    if message.animation:
        return "animation", message.animation.file_id
    if message.audio:
        return "audio", message.audio.file_id
    if message.voice:
        return "voice", message.voice.file_id
    return None, None
