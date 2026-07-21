from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

import db
from utils import restricted


def _mark(value: bool) -> str:
    return "\u2705" if value else "\u274C"


def build_settings_card(s: dict):
    text = (
        "\u2699\ufe0f *Settings*\n"
        "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
        "Tap to toggle:"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{_mark(s['captions_enabled'])} Captions", callback_data="setting:captions")],
        [InlineKeyboardButton(f"{_mark(s['album_grouping'])} Album grouping", callback_data="setting:album")],
        [InlineKeyboardButton(f"{_mark(s['dedup_enabled'])} Dedup (active session, all files)", callback_data="setting:dedup")],
        [InlineKeyboardButton(f"{_mark(s['accept_photos_enabled'])} Accept photos", callback_data="setting:acceptphotos")],
        [InlineKeyboardButton(f"{_mark(s['accept_text_enabled'])} Accept text messages", callback_data="setting:accepttext")],
        [InlineKeyboardButton(f"{_mark(s['accept_documents_enabled'])} Accept documents", callback_data="setting:acceptdocs")],
        [InlineKeyboardButton("\u25C0 Menu", callback_data="menu:root")],
    ])
    return text, keyboard


@restricted
async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = db.get_settings(update.effective_user.id)
    text, keyboard = build_settings_card(s)
    await update.effective_message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


TOGGLE_FUNCS = {
    "captions": db.toggle_captions,
    "album": db.toggle_album,
    "dedup": db.toggle_dedup,
    "acceptphotos": db.toggle_accept_photos,
    "accepttext": db.toggle_accept_text,
    "acceptdocs": db.toggle_accept_documents,
}


@restricted
async def toggle_setting_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fires when any toggle row on the settings card is tapped -- flips
    that one setting and refreshes the card in place."""
    query = update.callback_query
    await query.answer()
    key = query.data.split(":", 1)[1]
    func = TOGGLE_FUNCS.get(key)
    if not func:
        return
    func(update.effective_user.id)
    s = db.get_settings(update.effective_user.id)
    text, keyboard = build_settings_card(s)
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
