"""SQLite persistence: schema, connection helper, and query functions.

Every query takes an ``account_id``. v1 always passes ``DEFAULT_ACCOUNT_ID``
(see ``current_account``); that function is the single seam for real
multi-user support later.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from timed_sketching_helper.models import ImageList, ImageMeta, ListItem, SourceRef

DEFAULT_ACCOUNT_ID = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS image_lists (
    id         INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    provider   TEXT NOT NULL,
    kind       TEXT NOT NULL,
    source_url TEXT NOT NULL,
    title      TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    UNIQUE (account_id, source_url)
);

CREATE TABLE IF NOT EXISTS list_items (
    id         INTEGER PRIMARY KEY,
    list_id    INTEGER NOT NULL REFERENCES image_lists(id) ON DELETE CASCADE,
    source_id  TEXT NOT NULL,
    title      TEXT NOT NULL,
    author     TEXT NOT NULL,
    image_url  TEXT NOT NULL,
    page_url   TEXT NOT NULL,
    width      INTEGER,
    height     INTEGER,
    position   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS image_cache (
    source_id    TEXT PRIMARY KEY,
    content_type TEXT NOT NULL,
    cached_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS preferences (
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    PRIMARY KEY (account_id, key)
);
"""

DEFAULT_PREFERENCES = {"default_count": "20", "default_duration": "90"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_account() -> int:
    return DEFAULT_ACCOUNT_ID


def connect(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO accounts (id, name, created_at) VALUES (?, ?, ?)",
        (DEFAULT_ACCOUNT_ID, "default", _now()),
    )
    for key, value in DEFAULT_PREFERENCES.items():
        conn.execute(
            "INSERT OR IGNORE INTO preferences (account_id, key, value) VALUES (?, ?, ?)",
            (DEFAULT_ACCOUNT_ID, key, value),
        )
    conn.commit()


# -- image lists ----------------------------------------------------------------


def get_list_meta(
    conn: sqlite3.Connection, account_id: int, source_url: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM image_lists WHERE account_id = ? AND source_url = ?",
        (account_id, source_url),
    ).fetchone()


def save_list(
    conn: sqlite3.Connection,
    account_id: int,
    ref: SourceRef,
    title: str,
    images: list[ImageMeta],
) -> int:
    existing = get_list_meta(conn, account_id, ref.raw_url)
    if existing is not None:
        list_id = existing["id"]
        conn.execute("DELETE FROM list_items WHERE list_id = ?", (list_id,))
        conn.execute(
            "UPDATE image_lists SET provider=?, kind=?, title=?, fetched_at=? WHERE id=?",
            (ref.provider, ref.kind, title, _now(), list_id),
        )
    else:
        cursor = conn.execute(
            "INSERT INTO image_lists (account_id, provider, kind, source_url, title, fetched_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (account_id, ref.provider, ref.kind, ref.raw_url, title, _now()),
        )
        list_id = int(cursor.lastrowid)

    conn.executemany(
        "INSERT INTO list_items"
        " (list_id, source_id, title, author, image_url, page_url, width, height, position)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                list_id,
                img.source_id,
                img.title,
                img.author,
                img.image_url,
                img.page_url,
                img.width,
                img.height,
                position,
            )
            for position, img in enumerate(images)
        ],
    )
    conn.commit()
    return list_id


def _row_to_item(row: sqlite3.Row) -> ListItem:
    return ListItem(
        source_id=row["source_id"],
        title=row["title"],
        author=row["author"],
        image_url=row["image_url"],
        page_url=row["page_url"],
        position=row["position"],
        width=row["width"],
        height=row["height"],
    )


def load_list(conn: sqlite3.Connection, list_id: int) -> ImageList | None:
    meta = conn.execute(
        "SELECT * FROM image_lists WHERE id = ?", (list_id,)
    ).fetchone()
    if meta is None:
        return None
    rows = conn.execute(
        "SELECT * FROM list_items WHERE list_id = ? ORDER BY position", (list_id,)
    ).fetchall()
    return ImageList(
        id=meta["id"],
        account_id=meta["account_id"],
        provider=meta["provider"],
        kind=meta["kind"],
        source_url=meta["source_url"],
        title=meta["title"],
        fetched_at=meta["fetched_at"],
        items=[_row_to_item(r) for r in rows],
    )


def find_image_url(conn: sqlite3.Connection, source_id: str) -> str | None:
    row = conn.execute(
        "SELECT image_url FROM list_items WHERE source_id = ? LIMIT 1", (source_id,)
    ).fetchone()
    return row["image_url"] if row else None


def recent_lists(
    conn: sqlite3.Connection, account_id: int, limit: int = 10
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, source_url, title, kind, fetched_at FROM image_lists"
        " WHERE account_id = ? ORDER BY fetched_at DESC LIMIT ?",
        (account_id, limit),
    ).fetchall()


# -- image cache index --------------------------------------------------------


def get_cache_entry(
    conn: sqlite3.Connection, source_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM image_cache WHERE source_id = ?", (source_id,)
    ).fetchone()


def record_cache_entry(
    conn: sqlite3.Connection, source_id: str, content_type: str
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO image_cache (source_id, content_type, cached_at)"
        " VALUES (?, ?, ?)",
        (source_id, content_type, _now()),
    )
    conn.commit()


def clear_cache_entry(conn: sqlite3.Connection, source_id: str) -> None:
    conn.execute("DELETE FROM image_cache WHERE source_id = ?", (source_id,))
    conn.commit()


# -- preferences -------------------------------------------------------------


def get_preferences(conn: sqlite3.Connection, account_id: int) -> dict[str, str]:
    rows = conn.execute(
        "SELECT key, value FROM preferences WHERE account_id = ?", (account_id,)
    ).fetchall()
    prefs = dict(DEFAULT_PREFERENCES)
    prefs.update({r["key"]: r["value"] for r in rows})
    return prefs


def set_preferences(
    conn: sqlite3.Connection, account_id: int, values: dict[str, str]
) -> None:
    for key, value in values.items():
        conn.execute(
            "INSERT OR REPLACE INTO preferences (account_id, key, value) VALUES (?, ?, ?)",
            (account_id, key, str(value)),
        )
    conn.commit()
