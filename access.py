from telegram import Update
from telegram.ext import ContextTypes

import db
from utils import restricted


@restricted
async def lock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/lock <code> <password>"""
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /lock <code> <password>")
        return
    owner_id = update.effective_user.id
    code, password = context.args[0], context.args[1]
    session = db.get_session_by_code(code)
    if not session or session["owner_id"] != owner_id:
        await update.message.reply_text("Session not found.")
        return
    db.set_password(owner_id, session["id"], password)
    await update.message.reply_text(f"\U0001F512 `{code}` is now password protected.", parse_mode="Markdown")


@restricted
async def unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/unlock <code>"""
    if not context.args:
        await update.message.reply_text("Usage: /unlock <code>")
        return
    owner_id = update.effective_user.id
    session = db.get_session_by_code(context.args[0])
    if not session or session["owner_id"] != owner_id:
        await update.message.reply_text("Session not found.")
        return
    db.set_password(owner_id, session["id"], None)
    await update.message.reply_text(f"\U0001F513 Password removed from `{session['code']}`.", parse_mode="Markdown")


@restricted
async def limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/limit <code> <number|off>"""
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /limit <code> <number|off>")
        return
    owner_id = update.effective_user.id
    session = db.get_session_by_code(context.args[0])
    if not session or session["owner_id"] != owner_id:
        await update.message.reply_text("Session not found.")
        return
    token = context.args[1].lower()
    value = None if token == "off" else int(token)
    db.set_download_limit(owner_id, session["id"], value)
    await update.message.reply_text(
        f"Download limit for `{session['code']}` set to {value or 'unlimited'}.", parse_mode="Markdown"
    )
