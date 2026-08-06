import logging
import os
import re
import threading

from flask import Flask
from telegram import Update, BotCommand
from telegram.request import HTTPXRequest
from telegram.error import TimedOut
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
*Ghost's Inventory (personal build) – all commands*

👻 Main menu – /start
📥 Start a storage session – /create [label]
🛑 Finish the session – /stop
📁 Retrieve files by code – /open <code> [password]
📦 Your sessions – /list
🔍 Find sessions by label/tag – /search <term>
ℹ️ Share details & stats – /share <code>
➕ Reopen a session to add more – /edit <code>
✏️ Rename active session – /label <code> <new label>
⏱ Set expiry – /duration <code> <24h|7d|30d|90d|off>
🗑 Delete a session – /delete <code>
➖ Remove one item (reply to it) – /remove
🔒 Password-protect a share – /lock <code> <password>
🔓 Remove the password – /unlock <code>
📥 Cap downloads (1 = one-time) – /limit <code> <number|off>
🏷 Tag a session – /tag <code> <tag1> [tag2 ...]
🧹 Clear tags – /untag <code>
⚙️ Your preferences (tap to toggle) – /settings
📦 Download session as ZIP – /zip <code>
🧹 Bulk delete items – /clean <code>
❓ This list – /help
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
    BotCommand("zip", "Download session as ZIP"),
    BotCommand("clean", "Bulk delete items from session"),
    BotCommand("settings", "Your preferences"),
    BotCommand("help", "All commands"),
]


async def post_init(app: Application):
    await app.bot.set_my_commands(BOT_COMMANDS)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled exception", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong handling that. Check the Render logs for details."
            )
        except Exception:
            pass


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
web_app = Flask(__name__)


@web_app.route("/")
def health():
    return "ok", 200


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)


def main():
    db.init_db()

    threading.Thread(target=run_web_server, daemon=True).start()

    request = HTTPXRequest(
        connect_timeout=30,
        read_timeout=60,
        write_timeout=60,
        pool_timeout=30,
    )
    app = Application.builder().token(BOT_TOKEN).request(request).post_init(post_init).build()
    app.add_error_handler(error_handler)

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

    # /create conversation
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
    app.add_handler(CommandHandler("zip", items.zip_download))
    app.add_handler(CommandHandler("clean", items.clean_start))
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.ANIMATION | filters.AUDIO | filters.VOICE,
        items.handle_upload,
    ))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(re.compile(rf"^{re.escape(FILE_CODE_PREFIX)}", re.IGNORECASE)),
        items.auto_open,
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, items.handle_text_message))
    
    # inline buttons
    app.add_handler(CallbackQueryHandler(items.open_share_callback, pattern=r"^open_share:"))
    app.add_handler(CallbackQueryHandler(items.cancel_share_callback, pattern=r"^cancel_share$"))
    app.add_handler(CallbackQueryHandler(items.card_view_callback, pattern=r"^card_view:"))
    app.add_handler(CallbackQueryHandler(items.card_info_callback, pattern=r"^card_info:"))
    app.add_handler(CallbackQueryHandler(items.card_edit_callback, pattern=r"^card_edit:"))
    app.add_handler(CallbackQueryHandler(items.card_delete_callback, pattern=r"^card_delete:"))
    app.add_handler(CallbackQueryHandler(items.page_callback, pattern=r"^page:"))
    app.add_handler(CallbackQueryHandler(items.filter_callback, pattern=r"^filter:"))
    app.add_handler(CallbackQueryHandler(items.clean_callback, pattern=r"^clean_del:"))
    app.add_handler(CallbackQueryHandler(items.clean_page_callback, pattern=r"^clean_page:"))
    app.add_handler(CallbackQueryHandler(items.noop_callback, pattern=r"^noop$"))

    # settings
    app.add_handler(CommandHandler("settings", settings.settings))
    app.add_handler(CallbackQueryHandler(settings.toggle_setting_callback, pattern=r"^setting:"))

    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(menu_router, pattern=r"^menu:(cloud|profile|opencode|settings|help|root)$"))

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
