"""
Postgres access layer.

Uses a small psycopg2 connection pool. All functions are synchronous —
handlers call them via `context.application.run_in_executor` or plain
sync calls wrapped with `asyncio.to_thread` (see handlers/*.py).
"""
import secrets
import hashlib
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from psycopg2.pool import SimpleConnectionPool

from config import DATABASE_URL, SHARE_CODE_LENGTH, SHARE_CODE_ALPHABET, FILE_CODE_PREFIX

_pool = SimpleConnectionPool(1, 10, dsn=DATABASE_URL)


@contextmanager
def get_cursor(commit=False):
    conn = _pool.getconn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        yield cur
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        _pool.putconn(conn)


def init_db():
    with get_cursor(commit=True) as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id              SERIAL PRIMARY KEY,
            owner_id        BIGINT NOT NULL,
            code            TEXT UNIQUE NOT NULL,
            label           TEXT,
            status          TEXT NOT NULL DEFAULT 'open',  -- open | closed
            password_hash   TEXT,
            download_limit  INTEGER,                       -- NULL = unlimited
            downloads_used  INTEGER NOT NULL DEFAULT 0,
            expires_at      TIMESTAMPTZ,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id               SERIAL PRIMARY KEY,
            session_id       INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            vault_chat_id    BIGINT NOT NULL,
            vault_message_id BIGINT NOT NULL,
            file_type        TEXT NOT NULL,   -- photo | video | document | animation | audio
            file_unique_id   TEXT,            -- Telegram's stable per-file identifier, used for dedup
            caption          TEXT,
            added_at         TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS tags (
            id          SERIAL PRIMARY KEY,
            session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            tag         TEXT NOT NULL
        );
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id                    BIGINT PRIMARY KEY,
            captions_enabled           BOOLEAN NOT NULL DEFAULT TRUE,
            album_grouping             BOOLEAN NOT NULL DEFAULT TRUE,
            dedup_enabled              BOOLEAN NOT NULL DEFAULT TRUE,
            accept_photos_enabled      BOOLEAN NOT NULL DEFAULT TRUE,
            accept_text_enabled        BOOLEAN NOT NULL DEFAULT TRUE,
            accept_documents_enabled   BOOLEAN NOT NULL DEFAULT TRUE
        );
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS active_session (
            user_id     BIGINT PRIMARY KEY,
            session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE
        );
        """)

        # Migration-safe: these columns were added after the tables above
        # were first created in production, so CREATE TABLE IF NOT EXISTS
        # alone won't add them to an already-existing table.
        cur.execute("ALTER TABLE items ADD COLUMN IF NOT EXISTS file_unique_id TEXT;")
        cur.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS description TEXT;")
        cur.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS dedup_enabled BOOLEAN NOT NULL DEFAULT TRUE;")
        cur.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS accept_photos_enabled BOOLEAN NOT NULL DEFAULT TRUE;")
        cur.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS accept_text_enabled BOOLEAN NOT NULL DEFAULT TRUE;")
        cur.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS accept_documents_enabled BOOLEAN NOT NULL DEFAULT TRUE;")
        # Superseded by the single dedup_enabled toggle above.
        cur.execute("ALTER TABLE user_settings DROP COLUMN IF EXISTS dedup_photos_enabled;")
        cur.execute("ALTER TABLE user_settings DROP COLUMN IF EXISTS dedup_documents_enabled;")


# ---------- codes / passwords ----------

def generate_unique_code():
    with get_cursor() as cur:
        while True:
            code = FILE_CODE_PREFIX + "".join(secrets.choice(SHARE_CODE_ALPHABET) for _ in range(SHARE_CODE_LENGTH))
            cur.execute("SELECT 1 FROM sessions WHERE code = %s", (code,))
            if cur.fetchone() is None:
                return code


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


# ---------- sessions ----------

def create_session(owner_id: int, label: str | None = None) -> dict:
    code = generate_unique_code()
    with get_cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO sessions (owner_id, code, label) VALUES (%s, %s, %s) RETURNING *",
            (owner_id, code, label),
        )
        session = cur.fetchone()
        cur.execute(
            """INSERT INTO active_session (user_id, session_id) VALUES (%s, %s)
               ON CONFLICT (user_id) DO UPDATE SET session_id = EXCLUDED.session_id""",
            (owner_id, session["id"]),
        )
        return session


def get_active_session(owner_id: int) -> dict | None:
    with get_cursor() as cur:
        cur.execute("""
            SELECT s.* FROM sessions s
            JOIN active_session a ON a.session_id = s.id
            WHERE a.user_id = %s AND s.status = 'open'
        """, (owner_id,))
        return cur.fetchone()


def close_active_session(owner_id: int) -> dict | None:
    session = get_active_session(owner_id)
    if not session:
        return None
    with get_cursor(commit=True) as cur:
        cur.execute("UPDATE sessions SET status = 'closed' WHERE id = %s", (session["id"],))
        cur.execute("DELETE FROM active_session WHERE user_id = %s", (owner_id,))
    return session


def reopen_session(owner_id: int, session_id: int) -> bool:
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE sessions SET status = 'open' WHERE id = %s AND owner_id = %s",
            (session_id, owner_id),
        )
        if cur.rowcount == 0:
            return False
        cur.execute(
            """INSERT INTO active_session (user_id, session_id) VALUES (%s, %s)
               ON CONFLICT (user_id) DO UPDATE SET session_id = EXCLUDED.session_id""",
            (owner_id, session_id),
        )
        return True


def get_session_by_code(code: str) -> dict | None:
    with get_cursor() as cur:
        cur.execute("SELECT * FROM sessions WHERE code = %s", (code.strip().upper(),))
        return cur.fetchone()


def list_sessions(owner_id: int, limit: int = 30) -> list:
    with get_cursor() as cur:
        cur.execute(
            "SELECT * FROM sessions WHERE owner_id = %s ORDER BY created_at DESC LIMIT %s",
            (owner_id, limit),
        )
        return cur.fetchall()


def search_sessions(owner_id: int, term: str) -> list:
    with get_cursor() as cur:
        cur.execute("""
            SELECT DISTINCT s.* FROM sessions s
            LEFT JOIN tags t ON t.session_id = s.id
            WHERE s.owner_id = %s AND (s.label ILIKE %s OR t.tag ILIKE %s)
            ORDER BY s.created_at DESC
        """, (owner_id, f"%{term}%", f"%{term}%"))
        return cur.fetchall()


def delete_session(owner_id: int, session_id: int) -> bool:
    with get_cursor(commit=True) as cur:
        cur.execute("DELETE FROM sessions WHERE id = %s AND owner_id = %s", (session_id, owner_id))
        return cur.rowcount > 0


def rename_session(owner_id: int, session_id: int, label: str) -> bool:
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE sessions SET label = %s WHERE id = %s AND owner_id = %s",
            (label, session_id, owner_id),
        )
        return cur.rowcount > 0


def set_description(owner_id: int, session_id: int, description: str | None) -> bool:
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE sessions SET description = %s WHERE id = %s AND owner_id = %s",
            (description, session_id, owner_id),
        )
        return cur.rowcount > 0


def set_password(owner_id: int, session_id: int, password: str | None) -> bool:
    pw_hash = hash_password(password) if password else None
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE sessions SET password_hash = %s WHERE id = %s AND owner_id = %s",
            (pw_hash, session_id, owner_id),
        )
        return cur.rowcount > 0


def set_download_limit(owner_id: int, session_id: int, limit: int | None) -> bool:
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE sessions SET download_limit = %s WHERE id = %s AND owner_id = %s",
            (limit, session_id, owner_id),
        )
        return cur.rowcount > 0


def set_expiry(owner_id: int, session_id: int, expires_at) -> bool:
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE sessions SET expires_at = %s WHERE id = %s AND owner_id = %s",
            (expires_at, session_id, owner_id),
        )
        return cur.rowcount > 0


def increment_downloads(session_id: int):
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE sessions SET downloads_used = downloads_used + 1 WHERE id = %s",
            (session_id,),
        )


# ---------- items ----------

def add_item(session_id: int, vault_chat_id: int, vault_message_id: int, file_type: str, caption: str | None, file_unique_id: str | None = None):
    with get_cursor(commit=True) as cur:
        cur.execute(
            """INSERT INTO items (session_id, vault_chat_id, vault_message_id, file_type, caption, file_unique_id)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
            (session_id, vault_chat_id, vault_message_id, file_type, caption, file_unique_id),
        )
        return cur.fetchone()


def get_items(session_id: int) -> list:
    with get_cursor() as cur:
        cur.execute("SELECT * FROM items WHERE session_id = %s ORDER BY added_at", (session_id,))
        return cur.fetchall()


def delete_item(item_id: int) -> dict | None:
    with get_cursor(commit=True) as cur:
        cur.execute("DELETE FROM items WHERE id = %s RETURNING *", (item_id,))
        return cur.fetchone()


def get_item_by_vault_message(vault_message_id: int) -> dict | None:
    with get_cursor() as cur:
        cur.execute("SELECT * FROM items WHERE vault_message_id = %s", (vault_message_id,))
        return cur.fetchone()


def is_duplicate_in_session(session_id: int, file_unique_id: str) -> bool:
    """Checks for this exact file within THIS session only -- dedup never
    looks across sessions, past or present."""
    if not file_unique_id:
        return False
    with get_cursor() as cur:
        cur.execute(
            "SELECT 1 FROM items WHERE session_id = %s AND file_unique_id = %s LIMIT 1",
            (session_id, file_unique_id),
        )
        return cur.fetchone() is not None


# ---------- tags ----------

def add_tags(session_id: int, tags: list[str]):
    with get_cursor(commit=True) as cur:
        for tag in tags:
            cur.execute("INSERT INTO tags (session_id, tag) VALUES (%s, %s)", (session_id, tag.strip().lower()))


def clear_tags(session_id: int):
    with get_cursor(commit=True) as cur:
        cur.execute("DELETE FROM tags WHERE session_id = %s", (session_id,))


def get_tags(session_id: int) -> list:
    with get_cursor() as cur:
        cur.execute("SELECT tag FROM tags WHERE session_id = %s", (session_id,))
        return [r["tag"] for r in cur.fetchall()]


# ---------- user settings ----------

def get_settings(user_id: int) -> dict:
    with get_cursor(commit=True) as cur:
        cur.execute("SELECT * FROM user_settings WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        if row:
            return row
        cur.execute(
            "INSERT INTO user_settings (user_id) VALUES (%s) RETURNING *",
            (user_id,),
        )
        return cur.fetchone()


def toggle_captions(user_id: int) -> bool:
    settings = get_settings(user_id)
    new_val = not settings["captions_enabled"]
    with get_cursor(commit=True) as cur:
        cur.execute("UPDATE user_settings SET captions_enabled = %s WHERE user_id = %s", (new_val, user_id))
    return new_val


def toggle_album(user_id: int) -> bool:
    settings = get_settings(user_id)
    new_val = not settings["album_grouping"]
    with get_cursor(commit=True) as cur:
        cur.execute("UPDATE user_settings SET album_grouping = %s WHERE user_id = %s", (new_val, user_id))
    return new_val


def toggle_dedup(user_id: int) -> bool:
    settings = get_settings(user_id)
    new_val = not settings["dedup_enabled"]
    with get_cursor(commit=True) as cur:
        cur.execute("UPDATE user_settings SET dedup_enabled = %s WHERE user_id = %s", (new_val, user_id))
    return new_val


def toggle_accept_photos(user_id: int) -> bool:
    settings = get_settings(user_id)
    new_val = not settings["accept_photos_enabled"]
    with get_cursor(commit=True) as cur:
        cur.execute("UPDATE user_settings SET accept_photos_enabled = %s WHERE user_id = %s", (new_val, user_id))
    return new_val


def toggle_accept_text(user_id: int) -> bool:
    settings = get_settings(user_id)
    new_val = not settings["accept_text_enabled"]
    with get_cursor(commit=True) as cur:
        cur.execute("UPDATE user_settings SET accept_text_enabled = %s WHERE user_id = %s", (new_val, user_id))
    return new_val


def toggle_accept_documents(user_id: int) -> bool:
    settings = get_settings(user_id)
    new_val = not settings["accept_documents_enabled"]
    with get_cursor(commit=True) as cur:
        cur.execute("UPDATE user_settings SET accept_documents_enabled = %s WHERE user_id = %s", (new_val, user_id))
    return new_val
