from telegram import Update
from telegram.ext import ContextTypes

import db
from utils import restricted


@restricted
async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = db.get_settings(update.effective_user.id)
    await update.message.reply_text(
        f"Captions: {'on' if s['captions_enabled'] else 'off'}\n"
        f"Album grouping: {'on' if s['album_grouping'] else 'off'}\n\n"
        f"/togglecaptions or /togglealbum to change."
    )


@restricted
async def toggle_captions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_val = db.toggle_captions(update.effective_user.id)
    await update.message.reply_text(f"Captions turned {'on' if new_val else 'off'}.")


@restricted
async def toggle_album(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_val = db.toggle_album(update.effective_user.id)
    await update.message.reply_text(f"Album grouping turned {'on' if new_val else 'off'}.")
