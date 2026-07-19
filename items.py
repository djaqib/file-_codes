from telegram import Update
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


@restricted
async def handle_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fires on any incoming photo/video/document/etc. Requires an open session."""
    owner_id = update.effective_user.id
    message = update.message

    session = db.get_active_session(owner_id)
    if not session:
        await message.reply_text("No open session. /create one first, then send files.")
        return

    file_type, file_id = file_type_and_id(message)
    if not file_id:
        return  # not a file we handle (e.g. plain text) -- ignore silently

    settings = db.get_settings(owner_id)
    caption = message.caption if settings["captions_enabled"] else None

    # Forward the actual bytes into the private vault channel so the file
    # survives even if this bot / this chat is later deleted or banned.
    send = getattr(context.bot, SEND_METHOD[file_type])
    vault_message = await send(chat_id=VAULT_CHANNEL_ID, **{file_type: file_id}, caption=caption)

    db.add_item(session["id"], VAULT_CHANNEL_ID, vault_message.message_id, file_type, caption)
    await message.reply_text("\u2705 Saved to current session.")


async def deliver_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/open <code> [password] -- called from handlers/session.py:open_code."""
    if not context.args:
        await update.message.reply_text("Usage: /open <code> [password]")
        return

    code = context.args[0]
    password = context.args[1] if len(context.args) > 1 else None

    session = db.get_session_by_code(code)
    if not session:
        await update.message.reply_text("No session found with that code.")
        return

    if is_expired(session):
        await update.message.reply_text("This share has expired.")
        return

    if session["password_hash"]:
        if not password or db.hash_password(password) != session["password_hash"]:
            await update.message.reply_text("This share is password protected. Usage: /open <code> <password>")
            return

    limit = session["download_limit"]
    if limit and session["downloads_used"] >= limit:
        await update.message.reply_text("This share has hit its download limit.")
        return

    items = db.get_items(session["id"])
    if not items:
        await update.message.reply_text("This session has no items.")
        return

    for item in items:
        send = getattr(context.bot, SEND_METHOD[item["file_type"]])
        await context.bot.forward_message(
            chat_id=update.effective_chat.id,
            from_chat_id=item["vault_chat_id"],
            message_id=item["vault_message_id"],
        )

    db.increment_downloads(session["id"])
    await update.message.reply_text(f"\u2705 Delivered {len(items)} item(s) from `{code}`.", parse_mode="Markdown")


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
