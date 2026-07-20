from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ContextTypes

import db
from utils import restricted, parse_duration

MAIN_MENU_TEXT = (
    "\U0001F47B *GHOST'S STORAGE*\n\n"
    "Your private Telegram cloud drive.\n\n"
    "/create \u2013 start a new storage session\n"
    "/stop \u2013 finish the active session\n"
    "/open <code> \u2013 retrieve a collection\n"
    "/list \u2013 your sessions\n"
    "/help \u2013 all commands"
)

# Persistent button menu shown under the chat input, matching the
# reference bot's layout (2 buttons per row).
MAIN_MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["\U0001F4E5 New Upload", "\U0001F4C1 My Cloud"],
        ["\U0001F464 Profile", "\U0001F511 Open Code"],
        ["\u2699\ufe0f Settings", "\u2753 Help"],
    ],
    resize_keyboard=True,
)


@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Deep link support: tapping a t.me/yourbot?start=CODE link (e.g. from
    # a /share card) arrives here with the code as context.args[0].
    if context.args:
        from items import send_share_card
        await send_share_card(update, context, context.args[0])
        return

    await update.message.reply_text(
        MAIN_MENU_TEXT, parse_mode="Markdown", reply_markup=MAIN_MENU_KEYBOARD
    )


@restricted
async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner_id = update.effective_user.id
    sessions = db.list_sessions(owner_id, limit=1000)
    open_count = sum(1 for s in sessions if s["status"] == "open")
    total_items = sum(len(db.get_items(s["id"])) for s in sessions)
    user = update.effective_user
    await update.message.reply_text(
        f"\U0001F464 *Profile*\n\n"
        f"Name: {user.full_name}\n"
        f"User ID: `{owner_id}`\n"
        f"Sessions: {len(sessions)} ({open_count} open)\n"
        f"Total items stored: {total_items}",
        parse_mode="Markdown",
    )


@restricted
async def create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner_id = update.effective_user.id
    label = " ".join(context.args) if context.args else None

    existing = db.get_active_session(owner_id)
    if existing:
        await update.message.reply_text(
            f"You already have an open session (`{existing['code']}`). "
            f"Send files now, or /stop to finish it first.",
            parse_mode="Markdown",
        )
        return

    session = db.create_session(owner_id, label)
    await update.message.reply_text(
        f"\U0001F4E5 New session started \u2014 code `{session['code']}`.\n"
        f"Send me any photos, videos, or files now. /stop when you're done.",
        parse_mode="Markdown",
    )


@restricted
async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner_id = update.effective_user.id
    session = db.close_active_session(owner_id)
    if not session:
        await update.message.reply_text("No open session to stop. /create to start one.")
        return
    items = db.get_items(session["id"])
    await update.message.reply_text(
        f"\u2705 Session closed \u2014 {len(items)} item(s) saved.\n"
        f"Share code: `{session['code']}`",
        parse_mode="Markdown",
    )


@restricted
async def open_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/open <code> [password] -- delivers this command's actual file-sending
    logic lives in handlers/items.py:deliver_session, imported here to avoid
    a circular import."""
    from items import deliver_session
    await deliver_session(update, context)


@restricted
async def list_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner_id = update.effective_user.id
    sessions = db.list_sessions(owner_id)
    if not sessions:
        await update.message.reply_text("No sessions yet. /create to start one.")
        return
    lines = []
    for s in sessions:
        marker = "\U0001F7E2" if s["status"] == "open" else "\u26AA"
        label = f" \u2013 {s['label']}" if s["label"] else ""
        lines.append(f"{marker} `{s['code']}`{label}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@restricted
async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /search <term>")
        return
    term = " ".join(context.args)
    owner_id = update.effective_user.id
    results = db.search_sessions(owner_id, term)
    if not results:
        await update.message.reply_text(f"No sessions matching '{term}'.")
        return
    lines = [f"`{s['code']}` \u2013 {s['label'] or '(untitled)'}" for s in results]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@restricted
async def share_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = await _resolve_target_session(update, context)
    if not session:
        return
    from items import send_share_card
    await send_share_card(update, context, session["code"])


@restricted
async def edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/edit <code> -- reopen a closed session so more files can be added."""
    if not context.args:
        await update.message.reply_text("Usage: /edit <code>")
        return
    owner_id = update.effective_user.id
    session = db.get_session_by_code(context.args[0])
    if not session or session["owner_id"] != owner_id:
        await update.message.reply_text("Session not found.")
        return
    db.reopen_session(owner_id, session["id"])
    await update.message.reply_text(f"Reopened `{session['code']}`. Send more files, then /stop.", parse_mode="Markdown")


@restricted
async def label(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/label <code> <new label>"""
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /label <code> <new label>")
        return
    owner_id = update.effective_user.id
    code, new_label = context.args[0], " ".join(context.args[1:])
    session = db.get_session_by_code(code)
    if not session or session["owner_id"] != owner_id:
        await update.message.reply_text("Session not found.")
        return
    db.rename_session(owner_id, session["id"], new_label)
    await update.message.reply_text(f"Renamed `{code}` to \u201c{new_label}\u201d.", parse_mode="Markdown")


@restricted
async def duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/duration <code> <24h|7d|30d|90d|off>"""
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /duration <code> <24h|7d|30d|90d|off>")
        return
    owner_id = update.effective_user.id
    session = db.get_session_by_code(context.args[0])
    if not session or session["owner_id"] != owner_id:
        await update.message.reply_text("Session not found.")
        return
    try:
        expires_at = parse_duration(context.args[1])
    except ValueError as e:
        await update.message.reply_text(str(e))
        return
    db.set_expiry(owner_id, session["id"], expires_at)
    await update.message.reply_text(
        f"Expiry for `{session['code']}` set to {expires_at or 'never'}.", parse_mode="Markdown"
    )


@restricted
async def delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/delete <code>"""
    if not context.args:
        await update.message.reply_text("Usage: /delete <code>")
        return
    owner_id = update.effective_user.id
    session = db.get_session_by_code(context.args[0])
    if not session or session["owner_id"] != owner_id:
        await update.message.reply_text("Session not found.")
        return
    db.delete_session(owner_id, session["id"])
    await update.message.reply_text(f"Deleted `{session['code']}`.", parse_mode="Markdown")


@restricted
async def tag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tag <code> <tag1> <tag2> ..."""
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /tag <code> <tag1> [tag2 ...]")
        return
    owner_id = update.effective_user.id
    session = db.get_session_by_code(context.args[0])
    if not session or session["owner_id"] != owner_id:
        await update.message.reply_text("Session not found.")
        return
    db.add_tags(session["id"], context.args[1:])
    await update.message.reply_text(f"Tagged `{session['code']}`.", parse_mode="Markdown")


@restricted
async def untag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/untag <code>"""
    if not context.args:
        await update.message.reply_text("Usage: /untag <code>")
        return
    owner_id = update.effective_user.id
    session = db.get_session_by_code(context.args[0])
    if not session or session["owner_id"] != owner_id:
        await update.message.reply_text("Session not found.")
        return
    db.clear_tags(session["id"])
    await update.message.reply_text(f"Cleared tags on `{session['code']}`.", parse_mode="Markdown")


async def _resolve_target_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shared helper: /command <code> -> session, with ownership + existence checks."""
    owner_id = update.effective_user.id
    if context.args:
        session = db.get_session_by_code(context.args[0])
    else:
        session = db.get_active_session(owner_id)
    if not session or session["owner_id"] != owner_id:
        await update.message.reply_text("Session not found. Usage: /command <code>")
        return None
    return session
