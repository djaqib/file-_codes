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
    """Return (file_type, file_id, file_unique_id) for whichever attachment is
    on this message, or (None, None, None). file_unique_id is stable across
    re-uploads of the exact same file (unlike file_id, which can differ), so
    it's what dedup checks are based on."""
    if message.photo:
        f = message.photo[-1]
        return "photo", f.file_id, f.file_unique_id
    if message.video:
        f = message.video
        return "video", f.file_id, f.file_unique_id
    if message.document:
        f = message.document
        return "document", f.file_id, f.file_unique_id
    if message.animation:
        f = message.animation
        return "animation", f.file_id, f.file_unique_id
    if message.audio:
        f = message.audio
        return "audio", f.file_id, f.file_unique_id
    if message.voice:
        f = message.voice
        return "voice", f.file_id, f.file_unique_id
    return None, None, None
