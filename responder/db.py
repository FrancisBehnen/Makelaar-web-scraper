"""SQLite access for the responder.

The scrapers own the ``houses`` table; this service owns ``responses`` and
``kv``. Connections are per-thread (sqlite3 objects are not thread-safe) and
WAL mode keeps the file shareable between all three containers.
"""

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
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    c.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
    c.commit()


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
