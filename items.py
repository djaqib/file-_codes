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
    """Fires on any incoming photo/video/document/etc. Auto-creates a
    session if you forgot to /create one first."""
    owner_id = update.effective_user.id
    message = update.message

    file_type, file_id, file_unique_id = file_type_and_id(message)
    if not file_type:
        return  # not a file we handle -- ignore silently

    settings = db.get_settings(owner_id)

    if file_type == "photo" and not settings["accept_photos_enabled"]:
        await message.reply_text("\U0001F6AB Photo uploads are currently disabled in /settings.")
        return
    if file_type == "document" and not settings["accept_documents_enabled"]:
        await message.reply_text("\U0001F6AB Document uploads are currently disabled in /settings.")
        return

    session = db.get_active_session(owner_id)
    if not session:
        session = db.create_session(owner_id)
        await message.reply_text(
            f"\U0001F4E5 No open session \u2014 started one automatically "
            f"(code `{session['code']}`). Send /stop when done.",
            parse_mode="Markdown",
        )

    # Dedup only ever looks within THIS active session.
    if settings["dedup_enabled"] and db.is_duplicate_in_session(session["id"], file_unique_id):
        await message.reply_text("\u26A0\ufe0f Duplicate \u2014 already in this session, skipped.")
        return

    caption = message.caption if settings["captions_enabled"] else None

    # Forward the actual bytes into the private vault channel so the file
    # survives even if this bot / this chat is later deleted or banned.
    # Retry up to 2 times if the upload times out under load, with delays.
    send = getattr(context.bot, SEND_METHOD[file_type])
    vault_message = None
    for attempt in range(3):  # try up to 3 times (initial + 2 retries)
        try:
            vault_message = await send(chat_id=VAULT_CHANNEL_ID, **{file_type: file_id}, caption=caption)
            break  # success, stop retrying
        except TimedOut:
            if attempt == 2:  # final attempt
                await message.reply_text("\u26A0\ufe0f Upload timed out — file too large or connection too slow. Try again later.")
                return
            # retry with delay on attempt 0 and 1
            await message.reply_text(f"\u23F3 Retry {attempt + 1}/2 — upload timed out, waiting before retry...")
            await asyncio.sleep(5)  # wait 5 seconds before retrying
        except RetryAfter as e:
            # Telegram flood control -- back off and retry
            wait_time = e.retry_after + 1
            await message.reply_text(f"\u23F3 Rate limited — waiting {wait_time}s before retry...")
            await asyncio.sleep(wait_time)
            vault_message = await send(chat_id=VAULT_CHANNEL_ID, **{file_type: file_id}, caption=caption)
            break

    db.add_item(session["id"], VAULT_CHANNEL_ID, vault_message.message_id, file_type, caption, file_unique_id)
    await message.reply_text("\u2705 Saved to current session.")


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


def build_pagination_keyboard(code: str, page: int, total_pages: int) -> InlineKeyboardMarkup | None:
    if total_pages <= 1:
        return None

    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("\u25C0", callback_data=f"page:{code}:{page-1}"))

    window = 1  # show current page +/- this many neighbors
    start = max(1, page - window)
    end = min(total_pages, page + window)

    if start > 1:
        nav_row.append(InlineKeyboardButton("1", callback_data=f"page:{code}:1"))
        if start > 2:
            nav_row.append(InlineKeyboardButton("\u2026", callback_data="noop"))
    for p in range(start, end + 1):
        label = f"\u00B7{p}\u00B7" if p == page else str(p)
        nav_row.append(InlineKeyboardButton(label, callback_data=f"page:{code}:{p}"))
    if end < total_pages:
        if end < total_pages - 1:
            nav_row.append(InlineKeyboardButton("\u2026", callback_data="noop"))
        nav_row.append(InlineKeyboardButton(str(total_pages), callback_data=f"page:{code}:{total_pages}"))

    if page < total_pages:
        nav_row.append(InlineKeyboardButton("\u25B6", callback_data=f"page:{code}:{page+1}"))

    rows = [nav_row]

    # Only worth showing +/-30 jump buttons once there are enough pages
    # that a single tap of the neighbor buttons wouldn't get you far.
    if total_pages > 50:
        jump_row = []
        if page > JUMP_SIZE:
            jump_row.append(InlineKeyboardButton(f"\u00AB{JUMP_SIZE}", callback_data=f"page:{code}:{max(1, page - JUMP_SIZE)}"))
        if page + JUMP_SIZE <= total_pages:
            jump_row.append(InlineKeyboardButton(f"{JUMP_SIZE}\u00BB", callback_data=f"page:{code}:{min(total_pages, page + JUMP_SIZE)}"))
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

    chat_id = update.effective_chat.id
    for idx, item in enumerate(chunk):
        # Add delay between items to avoid flood control, but not before first item
        if idx > 0:
            await asyncio.sleep(FLOOD_DELAY)
        
        try:
            # copy_message (not forward_message) sends a fresh copy with no
            # "Forwarded from ..." attribution, and caption="" strips any
            # caption -- so delivered files never reveal the vault channel or
            # carry captions along with them.
            await context.bot.copy_message(
                chat_id=chat_id,
                from_chat_id=item["vault_chat_id"],
                message_id=item["vault_message_id"],
                caption="",
            )
        except RetryAfter as e:
            # Telegram flood control during delivery
            wait_time = e.retry_after + 1
            await _reply(update, f"\u23F3 Rate limited — waiting {wait_time}s before continuing delivery...")
            await asyncio.sleep(wait_time)
            await context.bot.copy_message(
                chat_id=chat_id,
                from_chat_id=item["vault_chat_id"],
                message_id=item["vault_message_id"],
                caption="",
            )

    keyboard = build_pagination_keyboard(code, page, total_pages)
    text = f"Page {page}/{total_pages} \u2014 items {start + 1}\u2013{min(start + PAGE_SIZE, total)} of {total}"
    await _reply(update, text, reply_markup=keyboard)


@restricted
async def page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fires when a page-number or +/-30 jump button is tapped."""
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
    # forwards. This works for items delivered via /open, but won't match if you
    # forward the same file again manually.
    if not forward_origin or not hasattr(forward_origin, "message_id"):
        await message.reply_text("Couldn't identify that item. /remove only works on delivered files.")
        return

    item = db.get_item_by_vault_message(forward_origin.message_id)
    if not item:
        await message.reply_text("Couldn't find that item in any session.")
        return

    db.delete_item(item["id"])
    await message.reply_text("\U0001F5D1\ufe0f Removed.")
