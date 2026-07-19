import logging

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import db
from config import BOT_TOKEN
from handlers import session, access, items, settings
from utils import restricted

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HELP_TEXT = """
*Ghost's Inventory (personal build) \u2013 all commands*

\U0001F47B Main menu \u2013 /start
\U0001F4E5 Start a storage session \u2013 /create [label]
\U0001F6D1 Finish the session \u2013 /stop
\U0001F4C1 Retrieve files by code \u2013 /open <code> [password]
\U0001F4E6 Your sessions \u2013 /list
\U0001F50D Find sessions by label/tag \u2013 /search <term>
\u2139\ufe0f Share details & stats \u2013 /share <code>
\u2795 Reopen a session to add more \u2013 /edit <code>
\u270F\ufe0f Rename active session \u2013 /label <code> <new label>
\u23F1\ufe0f Set expiry \u2013 /duration <code> <24h|7d|30d|90d|off>
\U0001F5D1\ufe0f Delete a session \u2013 /delete <code>
\u2796 Remove one item (reply to it) \u2013 /remove
\U0001F512 Password-protect a share \u2013 /lock <code> <password>
\U0001F513 Remove the password \u2013 /unlock <code>
\U0001F4E5 Cap downloads (1 = one-time) \u2013 /limit <code> <number|off>
\U0001F3F7\ufe0f Tag a session \u2013 /tag <code> <tag1> [tag2 ...]
\U0001F9F9 Clear tags \u2013 /untag <code>
\u2699\ufe0f Your preferences \u2013 /settings
\U0001F4AC Captions on/off \u2013 /togglecaptions
\U0001F5BC\ufe0f Album grouping on/off \u2013 /togglealbum
\u2753 This list \u2013 /help
""".strip()


@restricted
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


def main():
    db.init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # session lifecycle
    app.add_handler(CommandHandler("start", session.start))
    app.add_handler(CommandHandler("create", session.create))
    app.add_handler(CommandHandler("stop", session.stop))
    app.add_handler(CommandHandler("open", session.open_code))
    app.add_handler(CommandHandler("list", session.list_sessions))
    app.add_handler(CommandHandler("search", session.search))
    app.add_handler(CommandHandler("share", session.share_details))
    app.add_handler(CommandHandler("edit", session.edit))
    app.add_handler(CommandHandler("label", session.label))
    app.add_handler(CommandHandler("duration", session.duration))
    app.add_handler(CommandHandler("delete", session.delete))
    app.add_handler(CommandHandler("tag", session.tag))
    app.add_handler(CommandHandler("untag", session.untag))

    # access control
    app.add_handler(CommandHandler("lock", access.lock))
    app.add_handler(CommandHandler("unlock", access.unlock))
    app.add_handler(CommandHandler("limit", access.limit))

    # items
    app.add_handler(CommandHandler("remove", items.remove))
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.ANIMATION | filters.AUDIO | filters.VOICE,
        items.handle_upload,
    ))

    # settings
    app.add_handler(CommandHandler("settings", settings.settings))
    app.add_handler(CommandHandler("togglecaptions", settings.toggle_captions))
    app.add_handler(CommandHandler("togglealbum", settings.toggle_album))

    app.add_handler(CommandHandler("help", help_command))

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
