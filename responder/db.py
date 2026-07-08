"""SQLite access for the responder.

The scrapers own the ``houses`` table; this service owns ``responses`` and
``kv``. Connections are per-thread (sqlite3 objects are not thread-safe) and
WAL mode keeps the file shareable between all three containers.
"""

import json
import sqlite3
import threading

from config import DB_PATH

_local = threading.local()


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        _local.conn = c
    return c


def init_schema() -> None:
    c = conn()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            contact_method TEXT,
            contact_detail TEXT,
            contact_email TEXT,
            form_data TEXT,
            screenshot_path TEXT,
            tg_message_ids TEXT,
            error TEXT,
            listing_status TEXT NOT NULL DEFAULT 'available',
            last_checked_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    c.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            message_id INTEGER,
            sender_name TEXT,
            sender_username TEXT,
            ts TEXT NOT NULL,
            text TEXT NOT NULL
        )
        """
    )
    _migrate_responses(c)
    c.commit()


def _migrate_responses(c: sqlite3.Connection) -> None:
    """Add the delisting columns to pre-existing deployments (ALTER TABLE).

    ``listing_status`` tracks availability separately from the contact-flow
    ``status`` column: a listing can be status='sent' yet later go 'gone'.
    """
    cols = {row["name"] for row in c.execute("PRAGMA table_info(responses)")}
    if "listing_status" not in cols:
        c.execute(
            "ALTER TABLE responses "
            "ADD COLUMN listing_status TEXT NOT NULL DEFAULT 'available'"
        )
    if "last_checked_at" not in cols:
        c.execute("ALTER TABLE responses ADD COLUMN last_checked_at TEXT")


# ---------------------------------------------------------------------------
# chat_log — free-text group messages captured for the daily maintenance agent
# ---------------------------------------------------------------------------


def log_chat_message(
    *,
    chat_id: str,
    message_id: int | None,
    sender_name: str,
    sender_username: str,
    ts: str,
    text: str,
) -> None:
    """Append one group chat message (issue report / free text) to ``chat_log``.

    Called from the update loop for messages not consumed by any other flow; the
    caller wraps this defensively so a write failure never breaks polling.
    """
    c = conn()
    c.execute(
        "INSERT INTO chat_log "
        "(chat_id, message_id, sender_name, sender_username, ts, text) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (chat_id, message_id, sender_name, sender_username, ts, text),
    )
    c.commit()


def purge_old_chat_log(days: int = 14) -> int:
    """Delete chat_log rows older than ``days`` (keeps the table lean).

    ``ts`` is stored as an ISO-8601 string; ``datetime(ts)`` normalises it (incl.
    the ``T`` separator and timezone offset) so it compares against SQLite's UTC
    ``datetime('now')``. Returns the number of rows removed.
    """
    c = conn()
    cur = c.execute(
        "DELETE FROM chat_log WHERE datetime(ts) < datetime('now', ?)",
        (f"-{int(days)} days",),
    )
    c.commit()
    return cur.rowcount


def kv_get(key: str) -> str | None:
    row = conn().execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def kv_set(key: str, value: str) -> None:
    c = conn()
    c.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))
    c.commit()


def kv_delete(key: str) -> None:
    c = conn()
    c.execute("DELETE FROM kv WHERE key = ?", (key,))
    c.commit()


def _houses_table_exists() -> bool:
    # On a fresh volume the responder can boot before any scraper has created
    # the houses table.
    row = conn().execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'houses'"
    ).fetchone()
    return row is not None


def houses_count() -> int:
    if not _houses_table_exists():
        return 0
    return conn().execute("SELECT COUNT(*) AS n FROM houses").fetchone()["n"]


def responses_count() -> int:
    return conn().execute("SELECT COUNT(*) AS n FROM responses").fetchone()["n"]


def seed_existing() -> int:
    """Mark every house already in the DB as handled without notifying.

    Run once on the first deploy so the whole scrape history doesn't get
    announced as 'new'.
    """
    c = conn()
    cur = c.execute(
        "INSERT OR IGNORE INTO responses (url, status) "
        "SELECT url, 'seeded' FROM houses"
    )
    c.commit()
    return cur.rowcount


def new_house_urls() -> list[str]:
    if not _houses_table_exists():
        return []
    rows = conn().execute(
        """
        SELECT h.url FROM houses h
        LEFT JOIN responses r ON r.url = h.url
        WHERE r.url IS NULL
        """
    ).fetchall()
    return [row["url"] for row in rows]


def get_house(url: str) -> sqlite3.Row | None:
    if not _houses_table_exists():
        return None
    return conn().execute("SELECT * FROM houses WHERE url = ?", (url,)).fetchone()


def create_response(url: str, status: str) -> int:
    c = conn()
    c.execute(
        "INSERT OR IGNORE INTO responses (url, status) VALUES (?, ?)", (url, status)
    )
    c.commit()
    return c.execute(
        "SELECT id FROM responses WHERE url = ?", (url,)
    ).fetchone()["id"]


def get_response(response_id: int) -> sqlite3.Row | None:
    return conn().execute(
        "SELECT * FROM responses WHERE id = ?", (response_id,)
    ).fetchone()


def update_response(response_id: int, **fields) -> None:
    assignments = ", ".join(f"{name} = ?" for name in fields)
    c = conn()
    c.execute(
        f"UPDATE responses SET {assignments}, updated_at = datetime('now') "
        "WHERE id = ?",
        (*fields.values(), response_id),
    )
    c.commit()


def find_prior_response(url: str) -> sqlite3.Row | None:
    """Return the most recent notified response for the same address+city, different URL.

    Normalises addresses by stripping spaces so '2 F11' and '2F11' match.
    City comparison uses substring containment to handle postal-code prefixes
    like '2624 NM Delft' vs 'Delft'.
    """
    if not _houses_table_exists():
        return None
    return conn().execute(
        """
        SELECT r.* FROM responses r
        JOIN houses h      ON h.url      = r.url
        JOIN houses new_h  ON new_h.url  = ?
        WHERE r.url != ?
          AND r.status NOT IN ('seeded', 'cancelled', 'duplicate')
          AND lower(replace(h.straatnaamHuisnummer, ' ', ''))
              = lower(replace(new_h.straatnaamHuisnummer, ' ', ''))
          AND (lower(h.plaats) LIKE '%' || lower(new_h.plaats) || '%'
               OR lower(new_h.plaats) LIKE '%' || lower(h.plaats) || '%')
        ORDER BY r.created_at DESC
        LIMIT 1
        """,
        (url, url),
    ).fetchone()


def recent_responses(limit: int = 15) -> list[sqlite3.Row]:
    return conn().execute(
        """
        SELECT r.*, h.straatnaamHuisnummer, h.plaats
        FROM responses r
        LEFT JOIN houses h ON h.url = r.url
        WHERE r.status != 'seeded'
        ORDER BY r.updated_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def available_listings(limit: int) -> list[sqlite3.Row]:
    """Batch of still-available, previously-notified listings to re-check.

    Only rows that were actually announced (``tg_message_ids`` set) and are not
    seeded/duplicate/cancelled are eligible. Ordered by ``last_checked_at`` so
    the least-recently-checked listings (NULLs, i.e. never checked, come first
    in SQLite ASC) are picked, giving a round-robin over the whole table.
    """
    if not _houses_table_exists():
        return []
    return conn().execute(
        """
        SELECT r.id, r.url, r.tg_message_ids, h.straatnaamHuisnummer
        FROM responses r
        LEFT JOIN houses h ON h.url = r.url
        WHERE r.listing_status = 'available'
          AND r.tg_message_ids IS NOT NULL
          AND r.tg_message_ids NOT IN ('', '{}')
          AND r.status NOT IN ('seeded', 'duplicate', 'cancelled')
        ORDER BY r.last_checked_at ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def touch_listing_checked(response_id: int) -> None:
    c = conn()
    c.execute(
        "UPDATE responses SET last_checked_at = datetime('now') WHERE id = ?",
        (response_id,),
    )
    c.commit()


def mark_listing_gone(response_id: int) -> None:
    c = conn()
    c.execute(
        "UPDATE responses SET listing_status = 'gone', "
        "last_checked_at = datetime('now'), updated_at = datetime('now') "
        "WHERE id = ?",
        (response_id,),
    )
    c.commit()


def mark_dismissed_by_message(chat_id: str, message_id: int) -> bool:
    """Mark the listing whose Telegram notification matches as ``dismissed``.

    Looked up statelessly by (chat_id, message_id) — the pair a 🗑 callback
    carries. ``dismissed`` is excluded from ``available_listings`` so the
    delisting recheck never touches (or re-deletes) it. Returns True when a
    rental listing matched; False for koop messages (no responder-owned row).
    """
    c = conn()
    rows = c.execute(
        "SELECT id, tg_message_ids FROM responses "
        "WHERE tg_message_ids IS NOT NULL AND tg_message_ids NOT IN ('', '{}')"
    ).fetchall()
    for row in rows:
        ids = json.loads(row["tg_message_ids"] or "{}")
        if str(ids.get(str(chat_id))) == str(message_id):
            c.execute(
                "UPDATE responses SET listing_status = 'dismissed', "
                "updated_at = datetime('now') WHERE id = ?",
                (row["id"],),
            )
            c.commit()
            return True
    return False
