from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler

import db
from utils import restricted, parse_duration, back_to_menu_keyboard

MAIN_MENU_CARD_TEXT = (
    "\U0001F47B *GHOST'S STORAGE*\n"
    "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
    "Hey *{name}* \u2014 your files, stored forever, even if this bot disappears.\n\n"
    "Pick an option below \u2193"
)

# Conversation states for the /create name+description flow.
NAME, DESC = range(2)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001F4E5 New Upload", callback_data="menu:upload"),
         InlineKeyboardButton("\U0001F4C1 My Cloud", callback_data="menu:cloud")],
        [InlineKeyboardButton("\U0001F464 Profile", callback_data="menu:profile"),
         InlineKeyboardButton("\U0001F511 Open Code", callback_data="menu:opencode")],
        [InlineKeyboardButton("\u2699\ufe0f Settings", callback_data="menu:settings"),
         InlineKeyboardButton("\u2753 Help", callback_data="menu:help")],
    ])


@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Deep link support: tapping a t.me/yourbot?start=CODE link (e.g. from
    # a share card) arrives here with the code as context.args[0].
    if context.args:
        from items import send_share_card
        await send_share_card(update, context, context.args[0])
        return

    name = update.effective_user.first_name or "there"
    await update.effective_message.reply_text(
        MAIN_MENU_CARD_TEXT.format(name=name),
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


@restricted
async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner_id = update.effective_user.id
    sessions = db.list_sessions(owner_id, limit=1000)
    open_count = sum(1 for s in sessions if s["status"] == "open")
    total_items = sum(len(db.get_items(s["id"])) for s in sessions)
    user = update.effective_user
    await update.effective_message.reply_text(
        f"\U0001F464 *Profile*\n\n"
        f"Name: {user.full_name}\n"
        f"User ID: `{owner_id}`\n"
        f"Sessions: {len(sessions)} ({open_count} open)\n"
        f"Total items stored: {total_items}",
        parse_mode="Markdown",
        reply_markup=back_to_menu_keyboard(),
    )


# ---------- /create as a name -> description -> confirm conversation ----------

@restricted
async def create_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner_id = update.effective_user.id
    existing = db.get_active_session(owner_id)
    if existing:
        await update.effective_message.reply_text(
            f"You already have an open session (`{existing['code']}`). "
            f"Send files now, or /stop to finish it first.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    # Old-style fast path: "/create My Label" skips the conversation entirely.
    if context.args:
        label = " ".join(context.args)
        session_row = db.create_session(owner_id, label)
        from items import send_management_card
        await send_management_card(update, context, session_row["code"], saved=True)
        return ConversationHandler.END

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("\u23ED\ufe0f Skip", callback_data="create:skipname"),
        InlineKeyboardButton("\u274C Cancel", callback_data="create:cancel"),
    ]])
    await update.effective_message.reply_text(
        "\U0001F4E5 *New storage session*\n\nSend a *name* for it (e.g. _Vacation Photos_), or Skip:",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return NAME


async def _ask_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("\u23ED\ufe0f Skip", callback_data="create:skipdesc"),
        InlineKeyboardButton("\u274C Cancel", callback_data="create:cancel"),
    ]])
    await update.effective_message.reply_text(
        "\U0001F4DD Add a short *description* for this storage (shown when someone opens the code), or Skip:",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def create_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["pending_label"] = update.message.text.strip()
    await _ask_description(update, context)
    return DESC


async def create_skip_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    context.user_data["pending_label"] = None
    await _ask_description(update, context)
    return DESC


async def _finalize_create(update: Update, context: ContextTypes.DEFAULT_TYPE, description: str | None):
    owner_id = update.effective_user.id
    label = context.user_data.pop("pending_label", None)
    session_row = db.create_session(owner_id, label)
    if description:
        db.set_description(owner_id, session_row["id"], description)
    from items import send_management_card
    await send_management_card(update, context, session_row["code"], saved=True)


async def create_receive_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _finalize_create(update, context, update.message.text.strip())
    return ConversationHandler.END


async def create_skip_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _finalize_create(update, context, None)
    return ConversationHandler.END


async def create_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    context.user_data.pop("pending_label", None)
    await update.effective_message.reply_text("Cancelled.")
    return ConversationHandler.END


@restricted
async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner_id = update.effective_user.id
    session_row = db.close_active_session(owner_id)
    if not session_row:
        await update.effective_message.reply_text("No open session to stop. /create to start one.")
        return
    from items import send_management_card
    await send_management_card(update, context, session_row["code"], saved=True)


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
    from items import send_management_card
    await send_management_card(update, context, session["code"], saved=False)


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
