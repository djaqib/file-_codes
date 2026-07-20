import logging
import os
import re
import threading

from flask import Flask
from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

import db
from config import BOT_TOKEN, FILE_CODE_PREFIX
import session, access, items, settings
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
\U0001F501 Photo dedup on/off (active session) \u2013 /toggledupphotos
\U0001F501 Document dedup on/off (active session) \u2013 /toggledupdocs
\u2753 This list \u2013 /help
""".strip()


BOT_COMMANDS = [
    BotCommand("start", "Main menu"),
    BotCommand("create", "Start a storage session"),
    BotCommand("stop", "Finish the session"),
    BotCommand("open", "Retrieve files by code"),
    BotCommand("list", "Your sessions"),
    BotCommand("search", "Find sessions by label/tag"),
    BotCommand("share", "Share details & stats"),
    BotCommand("profile", "Your profile"),
    BotCommand("edit", "Reopen a session to add more"),
    BotCommand("label", "Rename active session"),
    BotCommand("duration", "Set expiry"),
    BotCommand("delete", "Delete a session"),
    BotCommand("remove", "Remove one item (reply to it)"),
    BotCommand("lock", "Password-protect a share"),
    BotCommand("unlock", "Remove the password"),
    BotCommand("limit", "Cap downloads (1 = one-time)"),
    BotCommand("tag", "Tag a session"),
    BotCommand("untag", "Clear tags"),
    BotCommand("settings", "Your preferences"),
    BotCommand("togglecaptions", "Captions on/off"),
    BotCommand("togglealbum", "Album grouping on/off"),
    BotCommand("toggledupphotos", "Photo dedup on/off (active session)"),
    BotCommand("toggledupdocs", "Document dedup on/off (active session)"),
    BotCommand("help", "All commands"),
]


async def post_init(app: Application):
    # Populates the "/" command menu Telegram shows in the chat window.
    await app.bot.set_my_commands(BOT_COMMANDS)


@restricted
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP_TEXT, parse_mode="Markdown")


@restricted
async def open_code_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        f"Send /open <code>, or just paste a code starting with {FILE_CODE_PREFIX}"
    )


@restricted
async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the main menu's inline buttons (except 'New Upload', which is
    a ConversationHandler entry point registered separately)."""
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "menu:root":
        await session.start(update, context)
    elif data == "menu:cloud":
        await items.cloud_view(update, context)
    elif data == "menu:profile":
        await session.profile(update, context)
    elif data == "menu:opencode":
        await open_code_prompt(update, context)
    elif data == "menu:settings":
        await settings.settings(update, context)
    elif data == "menu:help":
        await help_command(update, context)


# ---------- keep-alive web server ----------
# Render's free instance type only exists for Web Services (not Background
# Workers), and free Web Services spin down after 15 minutes with no inbound
# HTTP traffic. This tiny Flask app gives Render something to route to, and
# an external pinger (e.g. UptimeRobot hitting "/" every ~10 min) keeps the
# service awake so the bot's polling loop keeps running.
web_app = Flask(__name__)


@web_app.route("/")
def health():
    return "ok", 200


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)


def main():
    db.init_db()

    # Flask runs in a background thread; the bot's polling loop owns the
    # main thread below.
    threading.Thread(target=run_web_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # session lifecycle
    app.add_handler(CommandHandler("start", session.start))
    app.add_handler(CommandHandler("stop", session.stop))
    app.add_handler(CommandHandler("open", session.open_code))
    app.add_handler(CommandHandler("list", session.list_sessions))
    app.add_handler(CommandHandler("search", session.search))
    app.add_handler(CommandHandler("share", session.share_details))
    app.add_handler(CommandHandler("profile", session.profile))
    app.add_handler(CommandHandler("edit", session.edit))
    app.add_handler(CommandHandler("label", session.label))
    app.add_handler(CommandHandler("duration", session.duration))
    app.add_handler(CommandHandler("delete", session.delete))
    app.add_handler(CommandHandler("tag", session.tag))
    app.add_handler(CommandHandler("untag", session.untag))

    # /create as a name -> description -> confirm conversation. Also
    # reachable by tapping "New Upload" on the main menu.
    create_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("create", session.create_start),
            CallbackQueryHandler(session.create_start, pattern=r"^menu:upload$"),
        ],
        states={
            session.NAME: [
                CallbackQueryHandler(session.create_skip_name, pattern=r"^create:skipname$"),
                CallbackQueryHandler(session.create_cancel, pattern=r"^create:cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, session.create_receive_name),
            ],
            session.DESC: [
                CallbackQueryHandler(session.create_skip_desc, pattern=r"^create:skipdesc$"),
                CallbackQueryHandler(session.create_cancel, pattern=r"^create:cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, session.create_receive_desc),
            ],
        },
        fallbacks=[CommandHandler("cancel", session.create_cancel)],
    )
    app.add_handler(create_conversation)

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
    # Paste a code directly (e.g. "FILEQQ_7K3XM9QT") without typing /open first.
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(re.compile(rf"^{re.escape(FILE_CODE_PREFIX)}", re.IGNORECASE)),
        items.auto_open,
    ))
    # Inline Open / Cancel buttons on the recipient-facing share card.
    app.add_handler(CallbackQueryHandler(items.open_share_callback, pattern=r"^open_share:"))
    app.add_handler(CallbackQueryHandler(items.cancel_share_callback, pattern=r"^cancel_share$"))
    # Buttons on the owner-facing management card (My Cloud, /share, /create, /stop).
    app.add_handler(CallbackQueryHandler(items.card_view_callback, pattern=r"^card_view:"))
    app.add_handler(CallbackQueryHandler(items.card_info_callback, pattern=r"^card_info:"))
    app.add_handler(CallbackQueryHandler(items.card_edit_callback, pattern=r"^card_edit:"))
    app.add_handler(CallbackQueryHandler(items.card_delete_callback, pattern=r"^card_delete:"))

    # settings
    app.add_handler(CommandHandler("settings", settings.settings))
    app.add_handler(CommandHandler("togglecaptions", settings.toggle_captions))
    app.add_handler(CommandHandler("togglealbum", settings.toggle_album))
    app.add_handler(CommandHandler("toggledupphotos", settings.toggle_dedup_photos))
    app.add_handler(CommandHandler("toggledupdocs", settings.toggle_dedup_documents))

    app.add_handler(CommandHandler("help", help_command))

    # Main menu's inline buttons (New Upload is handled by create_conversation above).
    app.add_handler(CallbackQueryHandler(menu_router, pattern=r"^menu:(cloud|profile|opencode|settings|help|root)$"))

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
