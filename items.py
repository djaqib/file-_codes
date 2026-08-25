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
FLOOD_DELAY = 3.5
ALBUM_DELAY = 0.2

# ---------- batched upload confirmations ----------
UPLOAD_BATCH_DELAY = 2.0
_pending_uploads = {}


async def _send_batched_confirmation(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    if chat_id not in _pending_uploads:
        return
    count = _pending_uploads[chat_id]["count"]
    del _pending_uploads[chat_id]
    text = f"✅ Saved {count} item{'s' if count > 1 else ''} to current session."
    await context.bot.send_message(chat_id=chat_id, text=text)


def _schedule_upload_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in _pending_uploads:
        _pending_uploads[chat_id] = {"count": 0, "job": None}
    _pending_uploads[chat_id]["count"] += 1
    if _pending_uploads[chat_id]["job"]:
        _pending_uploads[chat_id]["job"].schedule_removal()
    job = context.job_queue.run_once(
        _send_batched_confirmation,
        UPLOAD_BATCH_DELAY,
        data={"chat_id": chat_id},
    )
    _pending_uploads[chat_id]["job"] = job
# -----------------------------------------------


async def _reply(update: Update, text: str, **kwargs):
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

    if settings["dedup_enabled"] and file_unique_id and db.is_duplicate_in_session(session["id"], file_unique_id):
        await message.reply_text("⚠️ Duplicate — already in this session, skipped.")
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

    if vault_message is None:
        await message.reply_text("⚠️ Upload failed after multiple retries. Please try again later.")
        return

    db.add_item(session["id"], VAULT_CHANNEL_ID, vault_message.message_id, file_type, caption, file_unique_id)
    _schedule_upload_confirmation(update, context)


@restricted
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner_id = update.effective_user.id
    message = update.message
    settings = db.get_settings(owner_id)

    if not settings["accept_text_enabled"]:
        return

    session = db.get_active_session(owner_id)
    if not session:
        session = db.create_session(owner_id)
        await message.reply_text(
            f"📥 No open session — started one automatically "
            f"(code `{session['code']}`). Send /stop when done.",
            parse_mode="Markdown",
        )

    vault_message = None
    for attempt in range(3):
        try:
            vault_message = await context.bot.send_message(chat_id=VAULT_CHANNEL_ID, text=message.text)
            break
        except TimedOut:
            if attempt == 2:
                await message.reply_text("⚠️ Upload timed out. Please try again later.")
                return
            await asyncio.sleep(2)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)

    if vault_message is None:
        await message.reply_text("⚠️ Failed to save text. Please try again.")
        return

    db.add_item(session["id"], VAULT_CHANNEL_ID, vault_message.message_id, "text", message.text, None)
    _schedule_upload_confirmation(update, context)


@restricted
async def auto_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip().split()[0]
    await send_share_card(update, context, code)


def build_pagination_keyboard(code: str, page: int, total_pages: int) -> InlineKeyboardMarkup | None:
    if total_pages <= 1:
        return None

    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("◀", callback_data=f"page:{code}:{page-1}"))

    window = 1
    start = max(1, page - window)
    end = min(total_pages, page + window)

    if start > 1:
        nav_row.append(InlineKeyboardButton("1", callback_data=f"page:{code}:1"))
        if start > 2:
            nav_row.append(InlineKeyboardButton("…", callback_data="noop"))
    for p in range(start, end + 1):
        label = f"·{p}·" if p == page else str(p)
        nav_row.append(InlineKeyboardButton(label, callback_data=f"page:{code}:{p}"))
    if end < total_pages:
        if end < total_pages - 1:
            nav_row.append(InlineKeyboardButton("…", callback_data="noop"))
        nav_row.append(InlineKeyboardButton(str(total_pages), callback_data=f"page:{code}:{total_pages}"))

    if page < total_pages:
        nav_row.append(InlineKeyboardButton("▶", callback_data=f"page:{code}:{page+1}"))

    rows = [nav_row]

    if total_pages > 50:
        jump_row = []
        if page > JUMP_SIZE:
            jump_row.append(InlineKeyboardButton(f"«{JUMP_SIZE}", callback_data=f"page:{code}:{max(1, page - JUMP_SIZE)}"))
        if page + JUMP_SIZE <= total_pages:
            jump_row.append(InlineKeyboardButton(f"{JUMP_SIZE}»", callback_data=f"page:{code}:{min(total_pages, page + JUMP_SIZE)}"))
        if jump_row:
            rows.append(jump_row)

    return InlineKeyboardMarkup(rows)


async def _send_page(update: Update, context: ContextTypes.DEFAULT_TYPE, session: dict, code: str, page: int):
    all_items = db.get_items(session["id"])
    total = len(all_items)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * PAGE_SIZE
    chunk = all_items[start:start + PAGE_SIZE]

    owner_id = update.effective_user.id
    settings = db.get_settings(owner_id)
    chat_id = update.effective_chat.id
    
    groupable_types = {"photo", "video"}
    idx = 0
    
    while idx < len(chunk):
        item = chunk[idx]
        
        if idx > 0:
            await asyncio.sleep(FLOOD_DELAY)
        
        try:
            if settings["album_grouping"] and item["file_type"] in groupable_types:
                await context.bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=item["vault_chat_id"],
                    message_id=item["vault_message_id"],
                    caption="",
                )
                idx += 1
                
                while idx < len(chunk) and chunk[idx]["file_type"] in groupable_types:
                    await asyncio.sleep(ALBUM_DELAY)
                    try:
                        await context.bot.copy_message(
                            chat_id=chat_id,
                            from_chat_id=chunk[idx]["vault_chat_id"],
                            message_id=chunk[idx]["vault_message_id"],
                            caption="",
                        )
                    except RetryAfter as e:
                        wait_time = e.retry_after + 1
                        await _reply(update, f"⏳ Rate limited — waiting {wait_time}s before continuing delivery...")
                        await asyncio.sleep(wait_time)
                        try:
                            await context.bot.copy_message(
                                chat_id=chat_id,
                                from_chat_id=chunk[idx]["vault_chat_id"],
                                message_id=chunk[idx]["vault_message_id"],
                                caption="",
                            )
                        except Exception:
                            pass
                    except TimedOut:
                        await _reply(update, "⚠️ Delivery timed out. Skipping item...")
                    idx += 1
            else:
                await context.bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=item["vault_chat_id"],
                    message_id=item["vault_message_id"],
                    caption="",
                )
                idx += 1
                
        except RetryAfter as e:
            wait_time = e.retry_after + 1
            await _reply(update, f"⏳ Rate limited — waiting {wait_time}s before continuing delivery...")
            await asyncio.sleep(wait_time)
            try:
                await context.bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=item["vault_chat_id"],
                    message_id=item["vault_message_id"],
                    caption="",
                )
                idx += 1
            except Exception:
                idx += 1
        except TimedOut:
            await _reply(update, "⚠️ Delivery timed out. Skipping item...")
            idx += 1

    keyboard = build_pagination_keyboard(code, page, total_pages)
    text = f"Page {page}/{total_pages} — items {start + 1}–{min(start + PAGE_SIZE, total)} of {total}"
    await _reply(update, text, reply_markup=keyboard)


@restricted
async def page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, code, page_str = query.data.split(":", 2)
    session = db.get_session_by_code(code)
    if not session:
        await query.message.reply_text("No session found with that code.")
        return
    if is_expired(session):
        await query.message.reply_text("This share has expired.")
        return
    await _send_page(update, context, session, code, int(page_str))


@restricted
async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


async def deliver_session(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str = None, password: str = None):
    if code is None:
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

    updated = db.increment_downloads(session["id"])
    if not updated:
        await _reply(update, "This share has hit its download limit.")
        return

    try:
        await _send_page(update, context, session, code, page=1)
    except Exception:
        await _reply(update, "⚠️ Delivery failed. Please try again later.")
        raise


def build_share_card(session: dict, bot_username: str):
    items = db.get_items(session["id"])
    code = session["code"]
    label = session["label"] or "Untitled"
    expires = session["expires_at"] or "never"
    limit = session["download_limit"]
    downloads_str = f"{session['downloads_used']}/{limit}" if limit else str(session["downloads_used"])

    text = (
        f"📦 *{md(label)}*\n"
        f"──────────\n"
        f"▸ Items — {len(items)}\n"
        f"▸ Expires — {expires}\n"
        f"▸ Downloads — {downloads_str}\n"
        f"▸ Code — {md(code)}\n"
        f"▸ 🔗 t.me/{md(bot_username)}?start={md(code)}\n"
        f"──────────\n"
        f"Open this share? ↓"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📧 Open", callback_data=f"open_share:{code}"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel_share"),
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
    query = update.callback_query
    await query.answer()
    code = query.data.split(":", 1)[1]
    await deliver_session(update, context, code=code)


async def cancel_share_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Cancelled.")


def build_management_card(session: dict, bot_username: str, saved: bool = False):
    items = db.get_items(session["id"])
    code = session["code"]
    label = session["label"] or "Untitled"
    header = f"✅ *{md(label)} saved*" if saved else f"📦 *{md(label)}*"

    text = (
        f"{header}\n"
        f"──────────\n"
        f"▸ Items — {len(items)}\n"
        f"▸ Code — {md(code)}\n"
        f"▸ 🔗 t.me/{md(bot_username)}?start={md(code)}\n"
        f"──────────\n"
        f"Share the link, or manage it below ↓"
    )
    share_url = f"https://t.me/share/url?url={quote(f'https://t.me/{bot_username}?start={code}')}"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📁 Open", callback_data=f"open_share:{code}"),
         InlineKeyboardButton("ℹ️ Info", callback_data=f"card_info:{code}")],
        [InlineKeyboardButton("↗️ Share link", url=share_url)],
        [InlineKeyboardButton("✏️ Edit", callback_data=f"card_edit:{code}"),
         InlineKeyboardButton("🗑 Delete", callback_data=f"card_delete:{code}")],
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
    query = update.callback_query
    await query.answer()
    code = query.data.split(":", 1)[1]
    await send_management_card(update, context, code, saved=False)


async def card_info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        f"ℹ️ *Session Info*\n\n"
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
    query = update.callback_query
    await query.answer()
    code = query.data.split(":", 1)[1]
    owner_id = update.effective_user.id
    session = db.get_session_by_code(code)
    if not session or session["owner_id"] != owner_id:
        await query.message.reply_text("Session not found.")
        return
    db.delete_session(owner_id, session["id"])
    await query.edit_message_text(f"🗑 Deleted `{code}`.", parse_mode="Markdown")


async def cloud_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner_id = update.effective_user.id
    sessions = db.list_sessions(owner_id, limit=50)

    rows = []
    for s in sessions:
        marker = "🟢" if s["status"] == "open" else "⚪"
        label = s["label"] or "Untitled"
        rows.append([InlineKeyboardButton(f"{marker} {label} ({s['code']})", callback_data=f"card_view:{s['code']}")])
    rows.append([InlineKeyboardButton("◀ Menu", callback_data="menu:root")])
    keyboard = InlineKeyboardMarkup(rows)

    if not sessions:
        text = (
            "📁 *My Cloud*\n"
            "──────────\n\n"
            "Nothing here yet — tap 📥 New Upload!"
        )
    else:
        text = (
            "📁 *My Cloud*\n"
            "──────────\n\n"
            "Tap a session to manage it:"
        )
    await update.effective_message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


@restricted
async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message.reply_to_message:
        await message.reply_text("Reply to a delivered item with /remove to delete it.")
        return

    forward_origin = message.reply_to_message.forward_origin
    if not forward_origin or not hasattr(forward_origin, "message_id"):
        await message.reply_text("Couldn't identify that item. /remove only works on delivered files.")
        return

    item = db.get_item_by_vault_message(forward_origin.message_id)
    if not item:
        await message.reply_text("Couldn't find that item in any session.")
        return

    session = db.get_session_by_id(item["session_id"])
    if not session or session["owner_id"] != update.effective_user.id:
        await message.reply_text("Not authorized to remove this item.")
        return

    db.delete_item(item["id"])
    await message.reply_text("🗑 Removed.")


@restricted
async def zip_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reply(update, "📦 ZIP download is not implemented yet.")


@restricted
async def clean_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reply(update, "🗑 Bulk item deletion is not implemented yet.")


async def filter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Filters are not implemented yet.")


async def clean_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Bulk delete is not implemented yet.")


async def clean_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Bulk delete is not implemented yet.")
