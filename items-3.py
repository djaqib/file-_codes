from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

import db
from config import VAULT_CHANNEL_ID
from utils import restricted, file_type_and_id, is_expired

SEND_METHOD = {
    "photo": "send_photo",
    "video": "send_video",
    "document": "send_document",
    "animation": "send_animation",
    "audio": "send_audio",
    "voice": "send_voice",
}


async def _reply(update: Update, text: str, **kwargs):
    """Works whether this update came from a regular message or a button tap."""
    if update.callback_query:
        await update.callback_query.message.reply_text(text, **kwargs)
    else:
        await update.message.reply_text(text, **kwargs)


@restricted
async def handle_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fires on any incoming photo/video/document/etc. Auto-creates a
    session if you forgot to /create one first."""
    owner_id = update.effective_user.id
    message = update.message

    session = db.get_active_session(owner_id)
    if not session:
        session = db.create_session(owner_id)
        await message.reply_text(
            f"\U0001F4E5 No open session \u2014 started one automatically "
            f"(code `{session['code']}`). Send /stop when done.",
            parse_mode="Markdown",
        )

    file_type, file_id, file_unique_id = file_type_and_id(message)
    if not file_id:
        return  # not a file we handle (e.g. plain text) -- ignore silently

    settings = db.get_settings(owner_id)

    # Dedup only ever looks within THIS active session, and only for the
    # file types you've enabled it for.
    dedup_applies = (
        (file_type == "photo" and settings["dedup_photos_enabled"])
        or (file_type == "document" and settings["dedup_documents_enabled"])
    )
    if dedup_applies and db.is_duplicate_in_session(session["id"], file_unique_id):
        await message.reply_text("\u26A0\ufe0f Duplicate \u2014 already in this session, skipped.")
        return

    caption = message.caption if settings["captions_enabled"] else None

    # Forward the actual bytes into the private vault channel so the file
    # survives even if this bot / this chat is later deleted or banned.
    send = getattr(context.bot, SEND_METHOD[file_type])
    vault_message = await send(chat_id=VAULT_CHANNEL_ID, **{file_type: file_id}, caption=caption)

    db.add_item(session["id"], VAULT_CHANNEL_ID, vault_message.message_id, file_type, caption, file_unique_id)
    await message.reply_text("\u2705 Saved to current session.")


@restricted
async def auto_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fires when a plain text message starts with FILE_CODE_PREFIX, so you
    can just paste a code (optionally with a password after it) instead of
    typing /open every time."""
    context.args = update.message.text.strip().split()
    await deliver_session(update, context)


async def deliver_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/open <code> [password] -- called from handlers/session.py:open_code,
    directly from auto_open, or from the Open button on a share card."""
    if not context.args:
        await _reply(update, "Usage: /open <code> [password]")
        return

    code = context.args[0]
    password = context.args[1] if len(context.args) > 1 else None

    session = db.get_session_by_code(code)
    if not session:
        await _reply(update, "No session found with that code.")
        return

    if is_expired(session):
        await _reply(update, "This share has expired.")
        return

    if session["password_hash"]:
        if not password or db.hash_password(password) != session["password_hash"]:
            await _reply(update, "This share is password protected. Usage: /open <code> <password>")
            return

    limit = session["download_limit"]
    if limit and session["downloads_used"] >= limit:
        await _reply(update, "This share has hit its download limit.")
        return

    items = db.get_items(session["id"])
    if not items:
        await _reply(update, "This session has no items.")
        return

    chat_id = update.effective_chat.id
    for item in items:
        await context.bot.forward_message(
            chat_id=chat_id,
            from_chat_id=item["vault_chat_id"],
            message_id=item["vault_message_id"],
        )

    db.increment_downloads(session["id"])
    await _reply(update, f"\u2705 Delivered {len(items)} item(s) from `{code}`.", parse_mode="Markdown")


def build_share_card(session: dict, bot_username: str):
    """Card layout matching the reference bot: stats + a deep link + Open/Cancel buttons."""
    items = db.get_items(session["id"])
    code = session["code"]
    label = session["label"] or "Untitled"
    expires = session["expires_at"] or "never"
    limit = session["download_limit"]
    downloads_str = f"{session['downloads_used']}/{limit}" if limit else str(session["downloads_used"])

    text = (
        f"\U0001F4E6 *{label}*\n"
        f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        f"\u25B8 Items \u2014 {len(items)}\n"
        f"\u25B8 Expires \u2014 {expires}\n"
        f"\u25B8 Downloads \u2014 {downloads_str}\n"
        f"\u25B8 Code \u2014 {code}\n"
        f"\u25B8 \U0001F517 t.me/{bot_username}?start={code}\n"
        f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        f"Open this share? \u2193"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("\U0001F4E7 Open", callback_data=f"open_share:{code}"),
        InlineKeyboardButton("\u274C Cancel", callback_data="cancel_share"),
    ]])
    return text, keyboard


async def send_share_card(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str):
    owner_id = update.effective_user.id
    session = db.get_session_by_code(code)
    if not session or session["owner_id"] != owner_id:
        await _reply(update, "Session not found.")
        return
    bot = await context.bot.get_me()
    text, keyboard = build_share_card(session, bot.username)
    await _reply(update, text, parse_mode="Markdown", reply_markup=keyboard)


async def open_share_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fires when the Open button on a share card is tapped."""
    query = update.callback_query
    await query.answer()
    code = query.data.split(":", 1)[1]
    context.args = [code]
    await deliver_session(update, context)


async def cancel_share_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fires when the Cancel button on a share card is tapped."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Cancelled.")


@restricted
async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/remove -- reply to a message the bot sent you (from an /open delivery)
    to delete that item from its session. Matching is done by forwarded
    vault message id, so this only works within the same chat where the
    item was delivered."""
    message = update.message
    if not message.reply_to_message:
        await message.reply_text("Reply to a delivered item with /remove to delete it.")
        return

    forward_origin = message.reply_to_message.forward_origin
    # NOTE: matching a forwarded message back to its vault copy requires the
    # forward_origin's message id, which Telegram only exposes for channel
    # forwards. This works for items delivered via /open (forwarded from the
    # vault channel).
    if not forward_origin or not hasattr(forward_origin, "message_id"):
        await message.reply_text("Couldn't identify that item. /remove only works on delivered files.")
        return

    item = db.get_item_by_vault_message(forward_origin.message_id)
    if not item:
        await message.reply_text("Couldn't find that item in any session.")
        return

    db.delete_item(item["id"])
    await message.reply_text("\U0001F5D1\ufe0f Removed.")
