# Personal Inventory Bot

A private, session-based Telegram file-storage bot. Upload a batch of
files, get a short share code back, retrieve them anytime with `/open`.
Files are mirrored into a private "vault" channel so they survive even
if the bot chat itself is lost.

## Setup

1. Create a bot via @BotFather, grab the token.
2. Create a **private** Telegram channel to act as the vault. Add your
   bot as an admin of that channel (needs "Post Messages" permission).
   Get the channel's numeric ID (forward a message from it to
   @userinfobot, or use @RawDataBot) — it'll look like `-100xxxxxxxxxx`.
3. Get your own numeric Telegram user ID (e.g. via @userinfobot).
4. Copy `.env.example` to `.env` and fill in:
   - `BOT_TOKEN`
   - `ALLOWED_USER_IDS` — comma-separated, just your ID for personal use
   - `VAULT_CHANNEL_ID`
   - `DATABASE_URL` — a Postgres connection string (Render gives you one
     for free with their managed Postgres add-on)
5. Install deps: `pip install -r requirements.txt`
6. Run: `python main.py`

On Render: set the same env vars in the dashboard, use `python main.py`
as the start command, and make sure it's deployed as a **worker**
(not a web service) since this bot uses polling rather than webhooks.

## Commands

See `/help` in the bot, or `main.py`'s `HELP_TEXT` for the full list.

## Notes / things you may want to adjust

- **`/remove`**: matching a replied-to message back to its vault entry
  relies on Telegram's `forward_origin.message_id`, which is only
  populated for channel forwards. This works for items delivered via
  `/open`, but won't match if you forward the same file again manually.
- **Expiry (`/duration`)** is stored but nothing currently *enforces*
  cleanup of expired sessions — `/open` checks and blocks delivery, but
  there's no background job deleting expired rows yet. Easy to add
  with a `JobQueue` repeating task calling a `db.delete_expired()`
  helper if you want real cleanup.
- **Album grouping** (`/togglealbum`) is stored in `user_settings` but
  not yet wired into `handle_upload` — right now every file is
  forwarded as a single message. If you want media-group batching
  (matching your video bot's `ALBUM_DELAY_SECONDS` pattern), that's a
  good next addition.
- Only one **active session per user** at a time (matches the original
  bot's flow: `/create` → send files → `/stop`).
