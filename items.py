import logging
import tempfile
import zipfile
import os
from telegram import InputMediaPhoto, InputMediaVideo

logger = logging.getLogger(__name__)
ALBUM_TYPES = {"photo", "video"}
CLEAN_PAGE_SIZE = 6
import asyncio
from urllib.parse import quote

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.error import TimedOut, RetryAfter

import db
from config import VAULT_CHANNEL_ID
from utils import restricted, file_type_and_id, is_expired, back_to_menu_keyboard, md

SEND_METHOD = {
    "photo": "send_photo",
    "video": "send_video",
    "document": "send_document",
    "animation": "send_animation",
    "audio": "send_audio",
    "voice": "send_voice",
}

PAGE_SIZE = 10
JUMP_SIZE = 30
FLOOD_DELAY = 10  # seconds to wait between API calls to avoid flood control


async def _reply(update: Update, text: str, **kwargs):
    """Works whether this update came from a regular message or a button tap."""
    if update.callback_query:
        await update.callback_query.message.reply_text(text, **kwargs)
    else:
        await update.message.reply_text(text, **kwargs)


@restricted
async def handle_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner_id = update.effective_user.id
    message = update.message

    file_type, file_id, file_unique_id = file_type_and_id(message)
    if not file_type:
        return

    settings = db.get_settings(owner_id)

    if file_type == "photo" and not settings["accept_photos_enabled"]:
        await message.reply_text("🚫 Photo uploads are currently disabled in /settings.")
        return
    if file_type == "document" and not settings["accept_documents_enabled"]:
        await message.reply_text("🚫 Document uploads are currently disabled in /settings.")
        return

    session = db.get_active_session(owner_id)
    if not session:
        session = db.create_session(owner_id)
        await message.reply_text(
            f"📥 No open session — started one automatically "
            f"(code `{session['code']}`). Send /stop when done.",
            parse_mode="Markdown",
        )

    if settings["dedup_enabled"] and db.is_duplicate_in_session(session["id"], file_unique_id):
        await message.reply_text("⚠️ Duplicate — already in this session, skipped.")
        return

    if settings["global_dedup_enabled"] and db.is_duplicate_globally(owner_id, file_unique_id):
        await message.reply_text("⚠️ Duplicate — already exists somewhere in your cloud, skipped.")
        return

    caption = message.caption if settings["captions_enabled"] else None

    send = getattr(context.bot, SEND_METHOD[file_type])
    vault_message = None
    for attempt in range(3):
        try:
            vault_message = await send(chat_id=VAULT_CHANNEL_ID, **{file_type: file_id}, caption=caption)
            break
        except TimedOut:
            if attempt == 2:
                await message.reply_text("⚠️ Upload timed out — file too large or connection too slow. Try again later.")
                return
            await message.reply_text(f"⏳ Retry {attempt + 1}/2 — upload timed out, waiting before retry...")
            await asyncio.sleep(5)
        except RetryAfter as e:
            wait_time = e.retry_after + 1
            await message.reply_text(f"⏳ Rate limited — waiting {wait_time}s before retry...")
            await asyncio.sleep(wait_time)
            vault_message = await send(chat_id=VAULT_CHANNEL_ID, **{file_type: file_id}, caption=caption)
            break

    # Extract the vault-side file_id so we can send_media_group / zip later
    vault_file_id = file_id
    if vault_message:
        if file_type == "photo" and vault_message.photo:
            vault_file_id = vault_message.photo[-1].file_id
        elif file_type == "video" and vault_message.video:
            vault_file_id = vault_message.video.file_id
        elif file_type == "document" and vault_message.document:
            vault_file_id = vault_message.document.file_id
        elif file_type == "animation" and vault_message.animation:
            vault_file_id = vault_message.animation.file_id
        elif file_type == "audio" and vault_message.audio:
            vault_file_id = vault_message.audio.file_id
        elif file_type == "voice" and vault_message.voice:
            vault_file_id = vault_message.voice.file_id

    db.add_item(session["id"], VAULT_CHANNEL_ID, vault_message.message_id, file_type, caption, file_unique_id, vault_file_id)
    await message.reply_text("✅ Saved to current session.")



@restricted
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fires on plain text that isn't a command and isn't a pasted code --
    saves it as a text item in the active session, if text uploads are
    enabled in Settings."""
    owner_id = update.effective_user.id
    message = update.message
    settings = db.get_settings(owner_id)

    if not settings["accept_text_enabled"]:
        return  # quietly ignored so it doesn't clutter the chat

    session = db.get_active_session(owner_id)
    if not session:
        session = db.create_session(owner_id)
        await message.reply_text(
            f"\U0001F4E5 No open session \u2014 started one automatically "
            f"(code `{session['code']}`). Send /stop when done.",
            parse_mode="Markdown",
        )

    vault_message = await context.bot.send_message(chat_id=VAULT_CHANNEL_ID, text=message.text)
    db.add_item(session["id"], VAULT_CHANNEL_ID, vault_message.message_id, "text", message.text, None)
    await message.reply_text("\u2705 Saved to current session.")


@restricted
async def auto_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fires when a plain text message starts with FILE_CODE_PREFIX (or is a
    bare code the owner pastes). Shows the same confirmation card as /share
    -- delivery only happens once Open is tapped."""
    code = update.message.text.strip().split()[0]
    await send_share_card(update, context, code)


def build_pagination_keyboard(code: str, page: int, total_pages: int, file_type: str | None = None) -> InlineKeyboardMarkup | None:
    rows = []

    # Filter bar
    filters = ["all", "photo", "video", "document", "text"]
    filter_buttons = []
    current = file_type or "all"
    for ft in filters:
        label = f"·{ft}·" if current == ft else ft
        filter_buttons.append(InlineKeyboardButton(label, callback_data=f"filter:{code}:{ft}:{page}"))
    rows.append(filter_buttons[:3])
    if len(filter_buttons) > 3:
        rows.append(filter_buttons[3:])

    # Page nav
    if total_pages > 1:
        nav_row = []
        if page > 1:
            nav_row.append(InlineKeyboardButton("◀", callback_data=f"page:{code}:{page-1}:{current}"))
        window = 1
        start = max(1, page - window)
        end = min(total_pages, page + window)
        if start > 1:
            nav_row.append(InlineKeyboardButton("1", callback_data=f"page:{code}:1:{current}"))
            if start > 2:
                nav_row.append(InlineKeyboardButton("…", callback_data="noop"))
        for p in range(start, end + 1):
            label = f"·{p}·" if p == page else str(p)
            nav_row.append(InlineKeyboardButton(label, callback_data=f"page:{code}:{p}:{current}"))
        if end < total_pages:
            if end < total_pages - 1:
                nav_row.append(InlineKeyboardButton("…", callback_data="noop"))
            nav_row.append(InlineKeyboardButton(str(total_pages), callback_data=f"page:{code}:{total_pages}:{current}"))
        if page < total_pages:
            nav_row.append(InlineKeyboardButton("▶", callback_data=f"page:{code}:{page+1}:{current}"))
        rows.append(nav_row)

    if total_pages > 50:
        jump_row = []
        if page > JUMP_SIZE:
            jump_row.append(InlineKeyboardButton(f"«{JUMP_SIZE}", callback_data=f"page:{code}:{max(1, page - JUMP_SIZE)}:{current}"))
        if page + JUMP_SIZE <= total_pages:
            jump_row.append(InlineKeyboardButton(f"{JUMP_SIZE}»", callback_data=f"page:{code}:{min(total_pages, page + JUMP_SIZE)}:{current}"))
        if jump_row:
            rows.append(jump_row)

    return InlineKeyboardMarkup(rows)


async def _send_page(update: Update, context: ContextTypes.DEFAULT_TYPE, session: dict, code: str, page: int, file_type: str | None = None):
    all_items = db.get_items_by_type(session["id"], file_type)
    total = len(all_items)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * PAGE_SIZE
    chunk = all_items[start:start + PAGE_SIZE]

    chat_id = update.effective_chat.id
    owner_settings = db.get_settings(session["owner_id"])
    use_albums = owner_settings["album_grouping"]

    album_buffer = []
    album_items = []
    sends_done = 0

    async def flush_album():
        nonlocal album_buffer, album_items, sends_done
        if not album_buffer:
            return
        try:
            msgs = await context.bot.send_media_group(chat_id=chat_id, media=album_buffer)
            for msg, item in zip(msgs, album_items):
                db.add_delivered_item(item["id"], chat_id, msg.message_id)
        except Exception as e:
            logger.warning("Album send failed, falling back to individual copy: %s", e)
            for item in album_items:
                try:
                    sent = await context.bot.copy_message(
                        chat_id=chat_id,
                        from_chat_id=item["vault_chat_id"],
                        message_id=item["vault_message_id"],
                        caption="",
                    )
                    db.add_delivered_item(item["id"], chat_id, sent.message_id)
                except Exception:
                    pass
        album_buffer = []
        album_items = []

    for item in chunk:
        is_album = item["file_type"] in ALBUM_TYPES and item.get("file_id") and use_albums

        if is_album:
            if item["file_type"] == "photo":
                media = InputMediaPhoto(media=item["file_id"], caption="")
            else:
                media = InputMediaVideo(media=item["file_id"], caption="")
            album_buffer.append(media)
            album_items.append(item)
            if len(album_buffer) >= 10:
                if sends_done > 0:
                    await asyncio.sleep(FLOOD_DELAY)
                await flush_album()
                sends_done += 1
        else:
            if album_buffer:
                if sends_done > 0:
                    await asyncio.sleep(FLOOD_DELAY)
                await flush_album()
                sends_done += 1
            if sends_done > 0:
                await asyncio.sleep(FLOOD_DELAY)
            try:
                sent = await context.bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=item["vault_chat_id"],
                    message_id=item["vault_message_id"],
                    caption="",
                )
                db.add_delivered_item(item["id"], chat_id, sent.message_id)
            except RetryAfter as e:
                wait_time = e.retry_after + 1
                await _reply(update, f"⏳ Rate limited — waiting {wait_time}s before continuing...")
                await asyncio.sleep(wait_time)
                sent = await context.bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=item["vault_chat_id"],
                    message_id=item["vault_message_id"],
                    caption="",
                )
                db.add_delivered_item(item["id"], chat_id, sent.message_id)
            sends_done += 1

    if album_buffer:
        if sends_done > 0:
            await asyncio.sleep(FLOOD_DELAY)
        await flush_album()

    keyboard = build_pagination_keyboard(code, page, total_pages, file_type)
    filter_text = f" | filter: {file_type}" if file_type else ""
    text = f"Page {page}/{total_pages}{filter_text} — items {start + 1}–{min(start + PAGE_SIZE, total)} of {total}"
    await _reply(update, text, reply_markup=keyboard)


@restricted
async def filter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, code, file_type, page_str = query.data.split(":", 3)
    session = db.get_session_by_code(code)
    if not session:
        await query.message.reply_text("No session found with that code.")
        return
    if is_expired(session):
        await query.message.reply_text("This share has expired.")
        return
    ft = file_type if file_type != "all" else None
    await _send_page(update, context, session, code, int(page_str), file_type=ft)



@restricted
async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The '…' ellipsis button -- purely visual, does nothing."""
    await update.callback_query.answer()


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

    db.increment_downloads(session["id"])
    await _send_page(update, context, session, code, page=1)


def build_share_card(session: dict, bot_username: str):
    """Card layout matching the reference bot: stats + a deep link + Open/Cancel buttons."""
    items = db.get_items(session["id"])
    code = session["code"]
    label = session["label"] or "Untitled"
    expires = session["expires_at"] or "never"
    limit = session["download_limit"]
    downloads_str = f"{session['downloads_used']}/{limit}" if limit else str(session["downloads_used"])

    text = (
        f"\U0001F4E6 *{md(label)}*\n"
        f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        f"\u25B8 Items \u2014 {len(items)}\n"
        f"\u25B8 Expires \u2014 {expires}\n"
        f"\u25B8 Downloads \u2014 {downloads_str}\n"
        f"\u25B8 Code \u2014 {md(code)}\n"
        f"\u25B8 \U0001F517 t.me/{md(bot_username)}?start={md(code)}\n"
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


def build_management_card(session: dict, bot_username: str, saved: bool = False):
    """Owner-facing card: Items/Code/Link + Open/Info/Share link/Edit/Delete.
    Used by /create, /stop, /share, and My Cloud."""
    items = db.get_items(session["id"])
    code = session["code"]
    label = session["label"] or "Untitled"
    header = f"\u2705 *{md(label)} saved*" if saved else f"\U0001F4E6 *{md(label)}*"

    text = (
        f"{header}\n"
        f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        f"\u25B8 Items \u2014 {len(items)}\n"
        f"\u25B8 Code \u2014 {md(code)}\n"
        f"\u25B8 \U0001F517 t.me/{md(bot_username)}?start={md(code)}\n"
        f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        f"Share the link, or manage it below \u2193"
    )
    share_url = f"https://t.me/share/url?url={quote(f'https://t.me/{bot_username}?start={code}')}"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001F4C1 Open", callback_data=f"open_share:{code}"),
         InlineKeyboardButton("\u2139\ufe0f Info", callback_data=f"card_info:{code}")],
        [InlineKeyboardButton("\u2197\ufe0f Share link", url=share_url)],
        [InlineKeyboardButton("\u270F\ufe0f Edit", callback_data=f"card_edit:{code}"),
         InlineKeyboardButton("\U0001F5D1\ufe0f Delete", callback_data=f"card_delete:{code}")],
    ])
    return text, keyboard


async def send_management_card(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str, saved: bool = False):
    owner_id = update.effective_user.id
    session = db.get_session_by_code(code)
    if not session or session["owner_id"] != owner_id:
        await _reply(update, "Session not found.")
        return
    bot = await context.bot.get_me()
    text, keyboard = build_management_card(session, bot.username, saved=saved)
    await update.effective_message.reply_text(
        text, parse_mode="Markdown", reply_markup=keyboard, disable_web_page_preview=False
    )


async def card_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fires when a session row is tapped in My Cloud -- shows its management card."""
    query = update.callback_query
    await query.answer()
    code = query.data.split(":", 1)[1]
    await send_management_card(update, context, code, saved=False)


async def card_info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fires when Info is tapped on a management card -- shows full stats."""
    query = update.callback_query
    await query.answer()
    code = query.data.split(":", 1)[1]
    owner_id = update.effective_user.id
    session = db.get_session_by_code(code)
    if not session or session["owner_id"] != owner_id:
        await query.message.reply_text("Session not found.")
        return
    items = db.get_items(session["id"])
    tags = db.get_tags(session["id"])
    limit = session["download_limit"]
    limit_str = f"{session['downloads_used']}/{limit}" if limit else "unlimited"
    lock_str = "yes" if session["password_hash"] else "no"
    await query.message.reply_text(
        f"\u2139\ufe0f *Session Info*\n\n"
        f"Code: `{code}`\n"
        f"Label: {md(session['label']) if session['label'] else '(none)'}\n"
        f"Description: {md(session['description']) if session['description'] else '(none)'}\n"
        f"Items: {len(items)}\n"
        f"Tags: {md(', '.join(tags)) if tags else '(none)'}\n"
        f"Password protected: {lock_str}\n"
        f"Downloads used: {limit_str}\n"
        f"Expires: {session['expires_at'] or 'never'}\n"
        f"Status: {session['status']}",
        parse_mode="Markdown",
    )


async def card_edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fires when Edit is tapped -- reopens the session so more files can be added."""
    query = update.callback_query
    await query.answer()
    code = query.data.split(":", 1)[1]
    owner_id = update.effective_user.id
    session = db.get_session_by_code(code)
    if not session or session["owner_id"] != owner_id:
        await query.message.reply_text("Session not found.")
        return
    db.reopen_session(owner_id, session["id"])
    await query.message.reply_text(f"Reopened `{code}`. Send more files, then /stop.", parse_mode="Markdown")


async def card_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fires when Delete is tapped -- removes the session and its items."""
    query = update.callback_query
    await query.answer()
    code = query.data.split(":", 1)[1]
    owner_id = update.effective_user.id
    session = db.get_session_by_code(code)
    if not session or session["owner_id"] != owner_id:
        await query.message.reply_text("Session not found.")
        return
    db.delete_session(owner_id, session["id"])
    await query.edit_message_text(f"\U0001F5D1\ufe0f Deleted `{code}`.", parse_mode="Markdown")


async def cloud_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The 'My Cloud' screen -- tap a session to manage it, or go back to the menu."""
    owner_id = update.effective_user.id
    sessions = db.list_sessions(owner_id, limit=50)

    rows = []
    for s in sessions:
        marker = "\U0001F7E2" if s["status"] == "open" else "\u26AA"
        label = s["label"] or "Untitled"
        rows.append([InlineKeyboardButton(f"{marker} {label} ({s['code']})", callback_data=f"card_view:{s['code']}")])
    rows.append([InlineKeyboardButton("\u25C0 Menu", callback_data="menu:root")])
    keyboard = InlineKeyboardMarkup(rows)

    if not sessions:
        text = (
            "\U0001F4C1 *My Cloud*\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
            "Nothing here yet \u2014 tap \U0001F4E5 New Upload!"
        )
    else:
        text = (
            "\U0001F4C1 *My Cloud*\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
            "Tap a session to manage it:"
        )
    await update.effective_message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


@restricted
async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message.reply_to_message:
        await message.reply_text("Reply to a delivered item with /remove to delete it.")
        return

    reply = message.reply_to_message

    # NEW: look up by delivery record (works on any message this bot sent to this chat)
    item = db.get_item_by_delivery(reply.chat.id, reply.message_id)
    if item:
        db.delete_item(item["id"])
        await message.reply_text("🗑 Removed.")
        return

    # FALLBACK: old forward-origin method (for legacy items delivered before this update)
    forward_origin = message.reply_to_message.forward_origin
    if forward_origin and hasattr(forward_origin, "message_id"):
        item = db.get_item_by_vault_message(forward_origin.message_id)
        if item:
            db.delete_item(item["id"])
            await message.reply_text("🗑 Removed.")
            return

    await message.reply_text("Couldn't identify that item. /remove only works on files this bot delivered to you in this chat.")

def _file_ext(file_type: str) -> str:
    return {
        "photo": ".jpg",
        "video": ".mp4",
        "document": "",
        "animation": ".mp4",
        "audio": ".mp3",
        "voice": ".ogg",
    }.get(file_type, "")


@restricted
async def zip_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /zip <code>")
        return

    code = context.args[0]
    owner_id = update.effective_user.id
    session = db.get_session_by_code(code)
    if not session or session["owner_id"] != owner_id:
        await update.message.reply_text("Session not found.")
        return

    items_list = db.get_items(session["id"])
    file_items = [i for i in items_list if i.get("file_id") and i["file_type"] != "text"]

    if not file_items:
        await update.message.reply_text("No downloadable files in this session.")
        return
    if len(file_items) > 50:
        await update.message.reply_text("ZIP download is limited to 50 files. Use /open to browse.")
        return

    status_msg = await update.message.reply_text("📦 Downloading and zipping… this may take a moment.")

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = os.path.join(tmpdir, f"{code}.zip")
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for idx, item in enumerate(file_items):
                    try:
                        file_obj = await context.bot.get_file(item["file_id"])
                        ext = _file_ext(item["file_type"])
                        item_path = os.path.join(tmpdir, f"item_{idx+1:03d}{ext}")
                        await file_obj.download_to_drive(item_path)
                        zf.write(item_path, os.path.basename(item_path))
                    except Exception as e:
                        logger.warning("Skipping item %s in ZIP: %s", item["id"], e)

            await status_msg.delete()
            with open(zip_path, "rb") as zf:
                await update.message.reply_document(
                    document=zf,
                    caption=f"📦 `{code}` ({len(file_items)} files)",
                    parse_mode="Markdown",
                )
    except Exception:
        logger.exception("ZIP creation failed")
        await status_msg.edit_text("❌ Failed to create ZIP. Files may be too large.")


# ---------- /clean — interactive bulk delete ----------

async def _send_clean_page(update: Update, context: ContextTypes.DEFAULT_TYPE, session: dict, code: str, page: int):
    items_list = db.get_items(session["id"])
    total = len(items_list)
    total_pages = max(1, (total + CLEAN_PAGE_SIZE - 1) // CLEAN_PAGE_SIZE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * CLEAN_PAGE_SIZE
    chunk = items_list[start:start + CLEAN_PAGE_SIZE]

    text_lines = [f"🧹 *Clean Items* — `{code}`\nPage {page}/{total_pages}\n_Tap 🗑 to delete instantly:_\n"]
    keyboard_rows = []

    for idx, item in enumerate(chunk):
        label = f"{start + idx + 1}. {item['file_type']}"
        if item.get("caption"):
            c = item["caption"][:18] + "…" if len(item["caption"]) > 18 else item["caption"]
            label += f" ({c})"
        text_lines.append(label)
        keyboard_rows.append([
            InlineKeyboardButton(f"🗑 {item['file_type'][:8]}", callback_data=f"clean_del:{code}:{item['id']}")
        ])

    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("◀ Prev", callback_data=f"clean_page:{code}:{page-1}"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("▶ Next", callback_data=f"clean_page:{code}:{page+1}"))
    if nav_row:
        keyboard_rows.append(nav_row)

    keyboard_rows.append([InlineKeyboardButton("◀ Menu", callback_data="menu:root")])
    text = "\n".join(text_lines)

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard_rows)
        )
    else:
        await update.effective_message.reply_text(
            text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard_rows)
        )


@restricted
async def clean_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /clean <code>")
        return
    code = context.args[0]
    owner_id = update.effective_user.id
    session = db.get_session_by_code(code)
    if not session or session["owner_id"] != owner_id:
        await update.message.reply_text("Session not found.")
        return
    await _send_clean_page(update, context, session, code, page=1)


@restricted
async def clean_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, code, item_id_str = query.data.split(":", 2)
    owner_id = update.effective_user.id
    session = db.get_session_by_code(code)
    if not session or session["owner_id"] != owner_id:
        await query.message.reply_text("Session not found.")
        return

    db.delete_item(int(item_id_str))
    await _send_clean_page(update, context, session, code, page=1)


@restricted
async def clean_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, code, page_str = query.data.split(":", 2)
    owner_id = update.effective_user.id
    session = db.get_session_by_code(code)
    if not session or session["owner_id"] != owner_id:
        await query.message.reply_text("Session not found.")
        return
    await _send_clean_page(update, context, session, code, int(page_str))
