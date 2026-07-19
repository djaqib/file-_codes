import os

# --- Telegram ---
BOT_TOKEN = os.environ["BOT_TOKEN"]

# Only these Telegram user IDs may use the bot at all.
# Comma-separated in the env var, e.g. "111111,222222"
ALLOWED_USER_IDS = {
    int(uid.strip())
    for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if uid.strip()
}

# Private channel the bot forwards files into for permanent storage.
# The bot account must be an admin of this channel.
# e.g. -1001234567890
VAULT_CHANNEL_ID = int(os.environ["VAULT_CHANNEL_ID"])

# --- Database ---
DATABASE_URL = os.environ["DATABASE_URL"]

# --- Behavior defaults ---
DEFAULT_CAPTIONS_ENABLED = True
DEFAULT_ALBUM_GROUPING = True
SHARE_CODE_LENGTH = 8
SHARE_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I to avoid confusion

# Every generated code looks like FILEQQ_XXXXXXXX. The fixed prefix lets you
# (or the bot) recognize a pasted code at a glance without needing /open.
FILE_CODE_PREFIX = "FILEQQ_"
