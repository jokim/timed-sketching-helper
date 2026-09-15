# Practice Log Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the user a persistent, browsable log of every drawing practice they've run (source, images shown, outcome), with per-entry delete, one-click restart, and a full-log clear in Settings.

**Architecture:** Two new SQLite tables (`practice_log`, `practice_log_items`) written best-effort from the existing `POST /api/sessions` handler and read/deleted through five small new `/api/practice-log*` endpoints; the frontend gets a new `#view-practice-log` page reached from a button on the start screen, plus a small app-wide Settings modal (separate from the existing in-session one) holding "Clear practice log".

**Tech Stack:** FastAPI + sqlite3 (backend, unchanged deps), vanilla JS/HTML/CSS (frontend, no build step, no new deps).

**Spec:** `docs/superpowers/specs/2026-09-14-practice-log-design.md`

## Global Constraints

- Named "practice" throughout (tables, endpoints, JS identifiers, UI copy for the new feature) — never "session" — to avoid clashing with the existing `tsh_session` browser-identity cookie and the `/api/sessions` drawing-session machinery, which are **not** renamed or otherwise touched.
- Practice-log writes (creation on `POST /api/sessions`, and the finish `PATCH`) are best-effort: any DB error is caught and logged, never raised, and never blocks the actual practice/timer flow.
- No statistics UI in this pass — data model only.
- No pagination beyond a flat `limit` query param (default 200, hard-capped at 1000) on `GET /api/practice-log`.
- No new runtime dependencies, frontend build step, or JS tooling — plain browser JS matching the existing `static/app.js` style (no comments unless the WHY is non-obvious, `"use strict"`, `$()` helper, `api()` helper).
- Tests need no network/credentials — mock with `respx` where a request would otherwise hit DeviantArt, matching `tests/test_api.py`.
- No browser-automation tooling (no Playwright/etc.) — frontend tasks are verified by starting the dev server and checking with `curl`/reading the served HTML; a human verifies the interactive behavior manually.

---

## Task 1: `practice_log` / `practice_log_items` tables + CRUD in `db.py`

**Files:**
- Modify: `src/timed_sketching_helper/db.py`
- Test: `tests/test_practice_log.py` (new file)

**Interfaces:**
- Produces (used by Task 2+):
  - `db.create_practice_log(conn, account_id, *, source_url: str, list_title: str, list_kind: str, count: int, duration: int, items: list) -> int` — `items` elements need `.source_id`/`.title`/`.author`/`.page_url` attributes (a `ListItem` satisfies this). Returns the new `practice_log.id`.
  - `db.finish_practice_log(conn, practice_id: int, account_id: int, *, status: str, shown_count: int) -> bool` — `True` if a row was updated.
  - `db.list_practice_log(conn, account_id: int, limit: int = 200) -> list[sqlite3.Row]` — newest-first, each row has a `thumb` column (the `source_id` of the item at `position = 0`, or `None`).
  - `db.get_practice_log(conn, practice_id: int, account_id: int) -> sqlite3.Row | None`
  - `db.practice_log_items(conn, practice_id: int) -> list[sqlite3.Row]` — each with `source_id`, `title`, `author`, `page_url`, ordered by `position`.
  - `db.delete_practice_log(conn, practice_id: int, account_id: int) -> bool` — `True` if a row was deleted.
  - `db.clear_practice_log(conn, account_id: int) -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_practice_log.py`:

```python
from timed_sketching_helper import db as db_module
from timed_sketching_helper.models import ListItem


def _item(source_id):
    return ListItem(
        source_id=source_id,
        title=f"T{source_id}",
        author="artist",
        image_url=f"https://img.example/{source_id}.png",
        page_url=f"https://www.deviantart.com/artist/art/{source_id}",
        position=0,
    )


def test_create_practice_log_stores_entry_and_items(conn):
    practice_id = db_module.create_practice_log(
        conn,
        db_module.current_account(),
        source_url="https://www.deviantart.com/artist/gallery/all",
        list_title="artist · gallery",
        list_kind="gallery",
        count=2,
        duration=30,
        items=[_item("a"), _item("b")],
    )

    row = db_module.get_practice_log(conn, practice_id, db_module.current_account())
    assert row["source_url"] == "https://www.deviantart.com/artist/gallery/all"
    assert row["count"] == 2
    assert row["status"] == "in_progress"
    assert row["ended_at"] is None

    items = db_module.practice_log_items(conn, practice_id)
    assert [i["source_id"] for i in items] == ["a", "b"]


def test_finish_practice_log_sets_status_and_shown_count(conn):
    practice_id = db_module.create_practice_log(
        conn, db_module.current_account(),
        source_url="https://x", list_title="x", list_kind="gallery",
        count=1, duration=10, items=[_item("a")],
    )

    updated = db_module.finish_practice_log(
        conn, practice_id, db_module.current_account(),
        status="completed", shown_count=1,
    )

    assert updated is True
    row = db_module.get_practice_log(conn, practice_id, db_module.current_account())
    assert row["status"] == "completed"
    assert row["shown_count"] == 1
    assert row["ended_at"] is not None


def test_finish_practice_log_returns_false_for_unknown_id(conn):
    assert db_module.finish_practice_log(
        conn, 999, db_module.current_account(), status="completed", shown_count=0
    ) is False


def test_list_practice_log_orders_newest_first_with_thumb(conn):
    first = db_module.create_practice_log(
        conn, db_module.current_account(),
        source_url="https://x", list_title="x", list_kind="gallery",
        count=1, duration=10, items=[_item("a")],
    )
    second = db_module.create_practice_log(
        conn, db_module.current_account(),
        source_url="https://y", list_title="y", list_kind="gallery",
        count=1, duration=10, items=[_item("b")],
    )

    rows = db_module.list_practice_log(conn, db_module.current_account())

    assert [r["id"] for r in rows] == [second, first]
    assert rows[0]["thumb"] == "b"


def test_delete_practice_log_cascades_items(conn):
    practice_id = db_module.create_practice_log(
        conn, db_module.current_account(),
        source_url="https://x", list_title="x", list_kind="gallery",
        count=1, duration=10, items=[_item("a")],
    )

    assert db_module.delete_practice_log(conn, practice_id, db_module.current_account()) is True
    assert db_module.get_practice_log(conn, practice_id, db_module.current_account()) is None
    assert db_module.practice_log_items(conn, practice_id) == []


def test_delete_practice_log_returns_false_for_unknown_id(conn):
    assert db_module.delete_practice_log(conn, 999, db_module.current_account()) is False


def test_clear_practice_log_removes_all_entries_for_account(conn):
    db_module.create_practice_log(
        conn, db_module.current_account(),
        source_url="https://x", list_title="x", list_kind="gallery",
        count=1, duration=10, items=[_item("a")],
    )
    db_module.create_practice_log(
        conn, db_module.current_account(),
        source_url="https://y", list_title="y", list_kind="gallery",
        count=1, duration=10, items=[_item("b")],
    )

    db_module.clear_practice_log(conn, db_module.current_account())

    assert db_module.list_practice_log(conn, db_module.current_account()) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_practice_log.py -v`
Expected: FAIL — `AttributeError: module 'timed_sketching_helper.db' has no attribute 'create_practice_log'` (and similar).

- [ ] **Step 3: Add the tables to `SCHEMA` in `db.py`**

In `src/timed_sketching_helper/db.py`, insert into the `SCHEMA` string, after the `preferences` table's closing `);` and before the `deviantart_oauth` table:

```sql
CREATE TABLE IF NOT EXISTS practice_log (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    source_url  TEXT NOT NULL,
    list_title  TEXT NOT NULL,
    list_kind   TEXT NOT NULL,
    count       INTEGER NOT NULL,
    duration    INTEGER NOT NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    status      TEXT NOT NULL DEFAULT 'in_progress',
    shown_count INTEGER
);

CREATE TABLE IF NOT EXISTS practice_log_items (
    id              INTEGER PRIMARY KEY,
    practice_log_id INTEGER NOT NULL REFERENCES practice_log(id) ON DELETE CASCADE,
    source_id       TEXT NOT NULL,
    title           TEXT NOT NULL,
    author          TEXT NOT NULL,
    page_url        TEXT NOT NULL,
    position        INTEGER NOT NULL
);
```

- [ ] **Step 4: Add the CRUD functions**

At the end of `src/timed_sketching_helper/db.py`, after the `-- preferences --` section, add:

```python
# -- practice log --------------------------------------------------------


def create_practice_log(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    source_url: str,
    list_title: str,
    list_kind: str,
    count: int,
    duration: int,
    items: list,
) -> int:
    cursor = conn.execute(
        "INSERT INTO practice_log"
        " (account_id, source_url, list_title, list_kind, count, duration, started_at, status)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'in_progress')",
        (account_id, source_url, list_title, list_kind, count, duration, _now()),
    )
    practice_id = int(cursor.lastrowid)
    conn.executemany(
        "INSERT INTO practice_log_items"
        " (practice_log_id, source_id, title, author, page_url, position)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (practice_id, item.source_id, item.title, item.author, item.page_url, position)
            for position, item in enumerate(items)
        ],
    )
    conn.commit()
    return practice_id


def finish_practice_log(
    conn: sqlite3.Connection,
    practice_id: int,
    account_id: int,
    *,
    status: str,
    shown_count: int,
) -> bool:
    cursor = conn.execute(
        "UPDATE practice_log SET status = ?, shown_count = ?, ended_at = ?"
        " WHERE id = ? AND account_id = ?",
        (status, shown_count, _now(), practice_id, account_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def list_practice_log(
    conn: sqlite3.Connection, account_id: int, limit: int = 200
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT pl.*, ("
        "  SELECT source_id FROM practice_log_items"
        "  WHERE practice_log_id = pl.id AND position = 0"
        ") AS thumb"
        " FROM practice_log pl WHERE account_id = ? ORDER BY started_at DESC LIMIT ?",
        (account_id, limit),
    ).fetchall()


def get_practice_log(
    conn: sqlite3.Connection, practice_id: int, account_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM practice_log WHERE id = ? AND account_id = ?",
        (practice_id, account_id),
    ).fetchone()


def practice_log_items(conn: sqlite3.Connection, practice_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT source_id, title, author, page_url FROM practice_log_items"
        " WHERE practice_log_id = ? ORDER BY position",
        (practice_id,),
    ).fetchall()


def delete_practice_log(
    conn: sqlite3.Connection, practice_id: int, account_id: int
) -> bool:
    cursor = conn.execute(
        "DELETE FROM practice_log WHERE id = ? AND account_id = ?",
        (practice_id, account_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def clear_practice_log(conn: sqlite3.Connection, account_id: int) -> None:
    conn.execute("DELETE FROM practice_log WHERE account_id = ?", (account_id,))
    conn.commit()
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_practice_log.py -v`
Expected: PASS (7 tests)

- [ ] **Step 6: Commit**

```bash
git add src/timed_sketching_helper/db.py tests/test_practice_log.py
git commit -m "Add practice_log/practice_log_items tables and CRUD functions"
```

---

## Task 2: Write a `practice_log` entry from `POST /api/sessions`

**Files:**
- Modify: `src/timed_sketching_helper/main.py`
- Test: `tests/test_practice_log.py`

**Interfaces:**
- Consumes: `db.create_practice_log` (Task 1).
- Produces: `POST /api/sessions` response gains `"practice_id": int | None`, consumed by the frontend in Task 6 and by Tasks 3-5's tests.

- [ ] **Step 1: Add the shared test fixtures + failing test**

Append to the top of `tests/test_practice_log.py` (after the existing imports) and add the new test at the end of the file:

```python
import pytest
from fastapi.testclient import TestClient

from timed_sketching_helper.imagecache import ImageCache
from timed_sketching_helper.main import create_app
from timed_sketching_helper.models import ImageMeta, SourceRef
from timed_sketching_helper.sources.base import UnknownSourceError

GALLERY_URL = "https://www.deviantart.com/artist/gallery/all"


class FakeProvider:
    name = "deviantart"

    def __init__(self, images):
        self.images = images

    def matches(self, url):
        return "deviantart.com" in url

    def parse(self, url):
        return SourceRef("deviantart", "gallery", "artist", None, url)

    async def list_images(self, ref, *, on_progress=None, max_images=None, max_requests=None):
        images = list(self.images)
        if max_images is not None:
            images = images[:max_images]
        for n, _ in enumerate(images, start=1):
            if on_progress:
                on_progress(n, n)
        return images


def meta(source_id):
    return ImageMeta(
        source_id=source_id,
        title=f"T{source_id}",
        author="artist",
        image_url=f"https://img.example/{source_id}.png",
        page_url=f"https://www.deviantart.com/artist/art/{source_id}",
    )


@pytest.fixture
def client(conn, tmp_path):
    provider = FakeProvider([meta("a"), meta("b"), meta("c")])

    def resolver(url):
        if provider.matches(url):
            return provider
        raise UnknownSourceError(url)

    app = create_app(conn=conn, cache=ImageCache(conn, tmp_path / "cache"), resolver=resolver)
    return TestClient(app, base_url="http://localhost")


def test_create_session_records_practice_log_entry(client, conn):
    list_id = client.post("/api/lists", json={"url": GALLERY_URL}).json()["list_id"]

    session = client.post(
        "/api/sessions", json={"list_id": list_id, "count": 2, "duration": 30}
    ).json()

    assert session["practice_id"] is not None
    row = db_module.get_practice_log(
        conn, session["practice_id"], db_module.current_account()
    )
    assert row["source_url"] == GALLERY_URL
    assert row["count"] == 2
    assert row["duration"] == 30
    assert row["status"] == "in_progress"
    items = db_module.practice_log_items(conn, session["practice_id"])
    assert {i["source_id"] for i in items} == {i["source_id"] for i in session["items"]}
```

(`db_module` is already imported at the top of the file from Task 1.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_practice_log.py::test_create_session_records_practice_log_entry -v`
Expected: FAIL — `KeyError: 'practice_id'` (the response doesn't have that key yet).

- [ ] **Step 3: Wire practice-log creation into `create_session`**

In `src/timed_sketching_helper/main.py`, find the `create_session` handler (currently):

```python
    @app.post("/api/sessions")
    async def create_session(
        body: SessionRequest, background_tasks: BackgroundTasks
    ) -> dict:
        image_list = db.load_list(conn, body.list_id)
        if image_list is None:
            raise HTTPException(404, "List not found.")
        by_id = {i.source_id: i for i in image_list.items}
        selected, pool = build_session(list(by_id), body.count)
        backup = pool[:BACKUP_POOL_SIZE]
        background_tasks.add_task(precache, [by_id[s] for s in selected + backup])
        return {
            "duration": body.duration,
            "items": [_item_dto(by_id[s]) for s in selected],
            "reroll_pool": [_item_dto(by_id[s]) for s in pool],
        }
```

Replace it with:

```python
    @app.post("/api/sessions")
    async def create_session(
        body: SessionRequest, background_tasks: BackgroundTasks
    ) -> dict:
        image_list = db.load_list(conn, body.list_id)
        if image_list is None:
            raise HTTPException(404, "List not found.")
        by_id = {i.source_id: i for i in image_list.items}
        selected, pool = build_session(list(by_id), body.count)
        backup = pool[:BACKUP_POOL_SIZE]
        background_tasks.add_task(precache, [by_id[s] for s in selected + backup])
        practice_id = None
        try:
            practice_id = db.create_practice_log(
                conn,
                db.current_account(),
                source_url=image_list.source_url,
                list_title=image_list.title,
                list_kind=image_list.kind,
                count=body.count,
                duration=body.duration,
                items=[by_id[s] for s in selected],
            )
        except Exception:  # noqa: BLE001 - logging a practice must never block starting it
            logger.exception("Failed to record practice log entry")
        return {
            "duration": body.duration,
            "practice_id": practice_id,
            "items": [_item_dto(by_id[s]) for s in selected],
            "reroll_pool": [_item_dto(by_id[s]) for s in pool],
        }
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_practice_log.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Run the full suite to check for regressions**

Run: `uv run pytest -q`
Expected: PASS — in particular `test_create_session_partitions_items` and
`test_create_session_precaches_shown_and_backup_pool_images` in
`tests/test_api.py` still pass (they don't assert on the full response dict
shape, only specific keys, so the new `practice_id` key is additive).

- [ ] **Step 6: Commit**

```bash
git add src/timed_sketching_helper/main.py tests/test_practice_log.py
git commit -m "Record a practice_log entry when a practice session starts"
```

---

## Task 3: `PATCH /api/practice-log/{id}` — record how a practice ended

**Files:**
- Modify: `src/timed_sketching_helper/main.py`
- Test: `tests/test_practice_log.py`

**Interfaces:**
- Consumes: `db.finish_practice_log` (Task 1).
- Produces: `PATCH /api/practice-log/{practice_id}` with body `{"status": "completed"|"ended_early", "shown_count": int}`; consumed by the frontend in Task 6.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_practice_log.py`:

```python
def test_finish_practice_endpoint_updates_status(client):
    list_id = client.post("/api/lists", json={"url": GALLERY_URL}).json()["list_id"]
    practice_id = client.post(
        "/api/sessions", json={"list_id": list_id, "count": 2, "duration": 30}
    ).json()["practice_id"]

    res = client.patch(
        f"/api/practice-log/{practice_id}",
        json={"status": "completed", "shown_count": 2},
    )

    assert res.status_code == 200


def test_finish_practice_endpoint_404_for_unknown_id(client):
    res = client.patch(
        "/api/practice-log/999", json={"status": "completed", "shown_count": 0}
    )
    assert res.status_code == 404


def test_finish_practice_endpoint_rejects_invalid_status(client):
    list_id = client.post("/api/lists", json={"url": GALLERY_URL}).json()["list_id"]
    practice_id = client.post(
        "/api/sessions", json={"list_id": list_id, "count": 1, "duration": 10}
    ).json()["practice_id"]

    res = client.patch(
        f"/api/practice-log/{practice_id}",
        json={"status": "bogus", "shown_count": 1},
    )

    assert res.status_code == 422
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_practice_log.py -k finish_practice_endpoint -v`
Expected: FAIL with 404 "Not Found" for all three (the route doesn't exist yet).

- [ ] **Step 3: Add the `Literal` import and `PracticeFinishRequest` model**

In `src/timed_sketching_helper/main.py`, change:

```python
from pathlib import Path
from urllib.parse import urlparse
```

to:

```python
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
```

Then, near the other `BaseModel` subclasses (after `PrefsRequest`), add:

```python
class PracticeFinishRequest(BaseModel):
    status: Literal["completed", "ended_early"]
    shown_count: int = Field(ge=0)
```

- [ ] **Step 4: Add the endpoint**

In `src/timed_sketching_helper/main.py`, directly after the `create_session` endpoint (before `@app.post("/api/precache")`), add:

```python
    @app.patch("/api/practice-log/{practice_id}")
    async def finish_practice_log_entry(
        practice_id: int, body: PracticeFinishRequest
    ) -> dict:
        updated = db.finish_practice_log(
            conn,
            practice_id,
            db.current_account(),
            status=body.status,
            shown_count=body.shown_count,
        )
        if not updated:
            raise HTTPException(404, "Practice log entry not found.")
        return {"status": "ok"}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_practice_log.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/timed_sketching_helper/main.py tests/test_practice_log.py
git commit -m "Add PATCH /api/practice-log/{id} to record how a practice ended"
```

---

## Task 4: `GET /api/practice-log` and `GET /api/practice-log/{id}`

**Files:**
- Modify: `src/timed_sketching_helper/main.py`
- Test: `tests/test_practice_log.py`

**Interfaces:**
- Consumes: `db.list_practice_log`, `db.get_practice_log`, `db.practice_log_items` (Task 1).
- Produces:
  - `GET /api/practice-log?limit=200` -> `list[dict]`, each dict:
    `{id, source_url, list_title, list_kind, count, duration, started_at, ended_at, status, shown_count, thumb}`.
  - `GET /api/practice-log/{id}` -> the same dict plus
    `"items": [{source_id, title, author, page_url}, ...]`.
  Both consumed by the frontend in Task 8.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_practice_log.py`:

```python
def test_list_practice_log_endpoint_returns_newest_first(client):
    list_id = client.post("/api/lists", json={"url": GALLERY_URL}).json()["list_id"]
    first = client.post(
        "/api/sessions", json={"list_id": list_id, "count": 1, "duration": 10}
    ).json()["practice_id"]
    second = client.post(
        "/api/sessions", json={"list_id": list_id, "count": 1, "duration": 10}
    ).json()["practice_id"]

    entries = client.get("/api/practice-log").json()

    assert [e["id"] for e in entries] == [second, first]
    assert entries[0]["source_url"] == GALLERY_URL
    assert entries[0]["thumb"] is not None


def test_read_practice_log_endpoint_returns_items(client):
    list_id = client.post("/api/lists", json={"url": GALLERY_URL}).json()["list_id"]
    session = client.post(
        "/api/sessions", json={"list_id": list_id, "count": 2, "duration": 10}
    ).json()

    entry = client.get(f"/api/practice-log/{session['practice_id']}").json()

    assert {i["source_id"] for i in entry["items"]} == {
        i["source_id"] for i in session["items"]
    }


def test_read_practice_log_endpoint_404_for_unknown_id(client):
    assert client.get("/api/practice-log/999").status_code == 404
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_practice_log.py -k "list_practice_log_endpoint or read_practice_log_endpoint" -v`
Expected: FAIL (404 Not Found — routes don't exist yet)

- [ ] **Step 3: Add a `_practice_log_dto` helper**

In `src/timed_sketching_helper/main.py`, next to the existing `_item_dto` helper, add:

```python
def _practice_log_dto(row, thumb: str | None = None) -> dict:
    return {
        "id": row["id"],
        "source_url": row["source_url"],
        "list_title": row["list_title"],
        "list_kind": row["list_kind"],
        "count": row["count"],
        "duration": row["duration"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "status": row["status"],
        "shown_count": row["shown_count"],
        "thumb": thumb,
    }
```

- [ ] **Step 4: Add the two endpoints**

In `src/timed_sketching_helper/main.py`, directly after the `finish_practice_log_entry` endpoint added in Task 3, add:

```python
    @app.get("/api/practice-log")
    async def list_practice_log_entries(limit: int = 200) -> list[dict]:
        rows = db.list_practice_log(
            conn, db.current_account(), limit=min(max(limit, 1), 1000)
        )
        return [_practice_log_dto(r, thumb=r["thumb"]) for r in rows]

    @app.get("/api/practice-log/{practice_id}")
    async def read_practice_log_entry(practice_id: int) -> dict:
        row = db.get_practice_log(conn, practice_id, db.current_account())
        if row is None:
            raise HTTPException(404, "Practice log entry not found.")
        items = db.practice_log_items(conn, practice_id)
        dto = _practice_log_dto(
            row, thumb=items[0]["source_id"] if items else None
        )
        dto["items"] = [
            {
                "source_id": i["source_id"],
                "title": i["title"],
                "author": i["author"],
                "page_url": i["page_url"],
            }
            for i in items
        ]
        return dto
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_practice_log.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/timed_sketching_helper/main.py tests/test_practice_log.py
git commit -m "Add GET /api/practice-log and GET /api/practice-log/{id}"
```

---

## Task 5: `DELETE /api/practice-log/{id}` and `DELETE /api/practice-log`

**Files:**
- Modify: `src/timed_sketching_helper/main.py`
- Test: `tests/test_practice_log.py`

**Interfaces:**
- Consumes: `db.delete_practice_log`, `db.clear_practice_log` (Task 1).
- Produces: `DELETE /api/practice-log/{id}` (204, or 404 if unknown), `DELETE /api/practice-log` (204, always). Consumed by the frontend in Tasks 8-9.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_practice_log.py`:

```python
def test_delete_practice_log_endpoint_removes_entry(client):
    list_id = client.post("/api/lists", json={"url": GALLERY_URL}).json()["list_id"]
    practice_id = client.post(
        "/api/sessions", json={"list_id": list_id, "count": 1, "duration": 10}
    ).json()["practice_id"]

    res = client.delete(f"/api/practice-log/{practice_id}")

    assert res.status_code == 204
    assert client.get(f"/api/practice-log/{practice_id}").status_code == 404


def test_delete_practice_log_endpoint_404_for_unknown_id(client):
    assert client.delete("/api/practice-log/999").status_code == 404


def test_clear_practice_log_endpoint_removes_all_entries(client):
    list_id = client.post("/api/lists", json={"url": GALLERY_URL}).json()["list_id"]
    client.post("/api/sessions", json={"list_id": list_id, "count": 1, "duration": 10})
    client.post("/api/sessions", json={"list_id": list_id, "count": 1, "duration": 10})

    res = client.delete("/api/practice-log")

    assert res.status_code == 204
    assert client.get("/api/practice-log").json() == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_practice_log.py -k "delete_practice_log_endpoint or clear_practice_log_endpoint" -v`
Expected: FAIL (404 Not Found — routes don't exist yet)

- [ ] **Step 3: Add the two endpoints**

In `src/timed_sketching_helper/main.py`, directly after the `read_practice_log_entry` endpoint added in Task 4, add:

```python
    @app.delete("/api/practice-log/{practice_id}")
    async def delete_practice_log_entry(practice_id: int) -> Response:
        deleted = db.delete_practice_log(conn, practice_id, db.current_account())
        if not deleted:
            raise HTTPException(404, "Practice log entry not found.")
        return Response(status_code=204)

    @app.delete("/api/practice-log")
    async def clear_practice_log_entries() -> Response:
        db.clear_practice_log(conn, db.current_account())
        return Response(status_code=204)
```

Note: FastAPI resolves `DELETE /api/practice-log` (no path param) against the more specific `/api/practice-log/{practice_id}` route correctly regardless of declaration order because the segment counts differ — no route-ordering concern here.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_practice_log.py -v`
Expected: PASS (all tests in the file — this is also the full backend surface for the feature)

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS, no regressions.

- [ ] **Step 6: Commit**

```bash
git add src/timed_sketching_helper/main.py tests/test_practice_log.py
git commit -m "Add DELETE /api/practice-log/{id} and DELETE /api/practice-log"
```

---

## Task 6: Frontend — track `practiceId` through a running session and report its outcome

**Files:**
- Modify: `src/timed_sketching_helper/static/app.js`

**Interfaces:**
- Consumes: `POST /api/sessions` response `practice_id` field (Task 2), `PATCH /api/practice-log/{id}` (Task 3).
- Produces: `state.practiceId` (used by nothing else yet — Task 8's restart flow creates a fresh one via `beginFromUrl`/`startSession`, it doesn't read this field).

No backend/JS test harness exists for this file (per Global Constraints); verification is a full-suite regression run (this task touches no Python) plus a manual dev-server check.

- [ ] **Step 1: Add `practiceId` to `state`**

In `src/timed_sketching_helper/static/app.js`, find:

```js
const state = {
  listId: null,
  listUrl: null,
  listTitle: null,
  listKind: null,
  listThumb: null,
  listCount: null,
  duration: 90,
  count: 20,
};
```

Change to:

```js
const state = {
  listId: null,
  listUrl: null,
  listTitle: null,
  listKind: null,
  listThumb: null,
  listCount: null,
  duration: 90,
  count: 20,
  practiceId: null,
};
```

- [ ] **Step 2: Capture `practice_id` in `startSession()`**

Find:

```js
async function startSession() {
  const data = await api("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      list_id: state.listId,
      count: state.count,
      duration: state.duration,
    }),
  });
  session.items = data.items;
  session.pool = data.reroll_pool;
  session.index = 0;
  state.duration = data.duration;
  preloaded.clear();
  setStartStatus("");
  show("session");
  resetPauseUI();
  beginCountdown({ start: true });
}
```

Change to:

```js
async function startSession() {
  const data = await api("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      list_id: state.listId,
      count: state.count,
      duration: state.duration,
    }),
  });
  session.items = data.items;
  session.pool = data.reroll_pool;
  session.index = 0;
  state.duration = data.duration;
  state.practiceId = data.practice_id ?? null;
  preloaded.clear();
  setStartStatus("");
  show("session");
  resetPauseUI();
  beginCountdown({ start: true });
}
```

- [ ] **Step 3: Add a `reportPracticeOutcome` helper and call it from `finishSession`/`endSession`**

Find:

```js
function finishSession() {
  clearInterval(session.ticker);
  cancelCountdown();
  $("#done-summary").textContent = `You practiced ${session.items.length} images at ${state.duration}s each.`;
  renderFavButton();
  show("done");
}

function endSession() {
  clearInterval(session.ticker);
  cancelCountdown();
  renderSaved();
  loadAuthStatus();
  show("start");
}
```

Change to:

```js
// Fire-and-forget: a failure here must never block leaving the practice.
// Clears state.practiceId so a later "New images from the same reference"
// click (which calls startSession() directly, bypassing this function)
// can't double-report the just-finished practice.
function reportPracticeOutcome(status, shownCount) {
  if (!state.practiceId) return;
  fetch(`/api/practice-log/${state.practiceId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status, shown_count: shownCount }),
  }).catch(() => {});
  state.practiceId = null;
}

function finishSession() {
  clearInterval(session.ticker);
  cancelCountdown();
  $("#done-summary").textContent = `You practiced ${session.items.length} images at ${state.duration}s each.`;
  renderFavButton();
  reportPracticeOutcome("completed", session.items.length);
  show("done");
}

function endSession() {
  clearInterval(session.ticker);
  cancelCountdown();
  reportPracticeOutcome("ended_early", session.index);
  renderSaved();
  loadAuthStatus();
  show("start");
}
```

- [ ] **Step 4: Run the full backend test suite (regression check)**

Run: `uv run pytest -q`
Expected: PASS (this task touches no Python).

- [ ] **Step 5: Manual smoke check**

Run: `uv run timed-sketching-helper &` then `curl -s http://127.0.0.1:8765/static/app.js | grep -c reportPracticeOutcome` — expect `2` (the definition + at least... actually expect at least 2, one per call site plus the definition, i.e. 3). Stop the server (`kill %1`). Interactive verification (starting a practice, ending it early, confirming no console errors) is done manually by the user in a browser — do not attempt automated browser testing.

- [ ] **Step 6: Commit**

```bash
git add src/timed_sketching_helper/static/app.js
git commit -m "Track practice_id through a running session and report its outcome"
```

---

## Task 7: Frontend — extract `beginFromUrl()` from the start-form submit handler

**Files:**
- Modify: `src/timed_sketching_helper/static/app.js`

**Interfaces:**
- Produces: `beginFromUrl(url, { count, duration, forceRefresh = false, maxImages = null, maxRequests = null }) -> Promise<void>` — fetches the list, remembers it, saves prefs, and starts a session; used by the submit handler here and by the restart button in Task 8.

Pure refactor — behavior must be identical to before. No new test coverage exists for this file; verify via the full backend suite (unaffected) and a manual check that starting a practice from the form still works.

- [ ] **Step 1: Replace the submit handler with `beginFromUrl` + a thin handler**

Find:

```js
$("#start-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const url = $("#url").value.trim();
  const forceRefresh = $("#force-refresh").checked;
  const maxImagesRaw = $("#max-images").value.trim();
  const maxImages = maxImagesRaw ? Math.max(1, Math.floor(Number(maxImagesRaw))) : null;
  const maxRequestsRaw = $("#max-requests").value.trim();
  const maxRequests = maxRequestsRaw ? Math.max(1, Math.floor(Number(maxRequestsRaw))) : null;
  state.count = Number($("#count").value);
  state.duration = Number($("#duration").value);
  const btn = $("#start-btn");
  btn.disabled = true;
  setStartStatus("");
  setFetchProgress({ requests: 0, images: 0 });
  try {
    const list = await fetchListStreaming(
      url,
      forceRefresh,
      maxImages,
      maxRequests,
      setFetchProgress,
    );
    setFetchProgress("done");
    state.listId = list.list_id;
    state.listUrl = url;
    state.listTitle = list.title || url;
    state.listKind = list.kind || "";
    state.listThumb = list.thumb || "";
    state.listCount = list.count ?? null;
    rememberRecent({
      url,
      title: state.listTitle,
      kind: state.listKind,
      thumb: state.listThumb,
      count: state.listCount,
    });
    setStartStatus(`Fetched ${list.count} images. Preparing session…`);
    await api("/api/prefs", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ default_count: state.count, default_duration: state.duration }),
    }).catch(() => {});
    await startSession();
  } catch (err) {
    setStartStatus(err.message, true);
  } finally {
    setFetchProgress(null);
    btn.disabled = false;
  }
});
```

Replace with:

```js
async function beginFromUrl(
  url,
  { count, duration, forceRefresh = false, maxImages = null, maxRequests = null },
) {
  state.count = count;
  state.duration = duration;
  const btn = $("#start-btn");
  btn.disabled = true;
  setStartStatus("");
  setFetchProgress({ requests: 0, images: 0 });
  try {
    const list = await fetchListStreaming(
      url,
      forceRefresh,
      maxImages,
      maxRequests,
      setFetchProgress,
    );
    setFetchProgress("done");
    state.listId = list.list_id;
    state.listUrl = url;
    state.listTitle = list.title || url;
    state.listKind = list.kind || "";
    state.listThumb = list.thumb || "";
    state.listCount = list.count ?? null;
    rememberRecent({
      url,
      title: state.listTitle,
      kind: state.listKind,
      thumb: state.listThumb,
      count: state.listCount,
    });
    setStartStatus(`Fetched ${list.count} images. Preparing session…`);
    await api("/api/prefs", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ default_count: state.count, default_duration: state.duration }),
    }).catch(() => {});
    await startSession();
  } catch (err) {
    setStartStatus(err.message, true);
  } finally {
    setFetchProgress(null);
    btn.disabled = false;
  }
}

$("#start-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const url = $("#url").value.trim();
  const forceRefresh = $("#force-refresh").checked;
  const maxImagesRaw = $("#max-images").value.trim();
  const maxImages = maxImagesRaw ? Math.max(1, Math.floor(Number(maxImagesRaw))) : null;
  const maxRequestsRaw = $("#max-requests").value.trim();
  const maxRequests = maxRequestsRaw ? Math.max(1, Math.floor(Number(maxRequestsRaw))) : null;
  await beginFromUrl(url, {
    count: Number($("#count").value),
    duration: Number($("#duration").value),
    forceRefresh,
    maxImages,
    maxRequests,
  });
});
```

- [ ] **Step 2: Run the full backend test suite (regression check)**

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Step 3: Manual smoke check**

Start the dev server (`uv run timed-sketching-helper`) and in a browser confirm pasting a DeviantArt URL and clicking "Start practice" still fetches and starts a session exactly as before (this is a pure refactor — behavior must be unchanged). Per the no-browser-automation constraint, this check is done by the user, not by launching a headless browser.

- [ ] **Step 4: Commit**

```bash
git add src/timed_sketching_helper/static/app.js
git commit -m "Extract beginFromUrl() from the start-form submit handler"
```

---

## Task 8: Frontend — Practice log view (markup, styles, and behavior)

**Files:**
- Modify: `src/timed_sketching_helper/static/index.html`
- Modify: `src/timed_sketching_helper/static/styles.css`
- Modify: `src/timed_sketching_helper/static/app.js`

**Interfaces:**
- Consumes: `GET /api/practice-log`, `GET /api/practice-log/{id}`, `DELETE /api/practice-log/{id}` (Tasks 4-5), `beginFromUrl()` (Task 7), `savedThumb()`/`displayTitle()`-style helpers already in `app.js`, `externalHref()` already in `app.js`.
- Produces: `#view-practice-log`, reachable via a new `#practice-log-btn` on the start screen; `openPracticeLog()` for later reference.

- [ ] **Step 1: Add the "Practice log" button and the new view's markup to `index.html`**

In `src/timed_sketching_helper/static/index.html`, find the brand header:

```html
            <header class="brand">
              <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor"
                stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M6 2h12M6 22h12M8 2v4l8 12v4M16 2v4L8 18v4" />
              </svg>
              <span>Timed Sketching Helper</span>
            </header>
```

Replace with:

```html
            <header class="brand">
              <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor"
                stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M6 2h12M6 22h12M8 2v4l8 12v4M16 2v4L8 18v4" />
              </svg>
              <span>Timed Sketching Helper</span>
              <span class="brand-actions">
                <button type="button" id="practice-log-btn" title="Practice log" aria-label="Practice log">
                  <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor"
                    stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                    <path d="M8 6h13M8 12h13M8 18h13" /><path d="M3 6h.01M3 12h.01M3 18h.01" />
                  </svg>
                </button>
                <button type="button" id="app-settings-btn" title="Settings" aria-label="Settings">
                  <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor"
                    stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                    <circle cx="12" cy="12" r="3" />
                    <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
                  </svg>
                </button>
              </span>
            </header>
```

Then, find the closing of the "Done view" section:

```html
      </section>
    </main>
    <script src="/static/app.js"></script>
  </body>
</html>
```

Replace with (inserting the new view before `</main>`):

```html
      </section>

      <!-- Practice log view -->
      <section id="view-practice-log" class="view" hidden>
        <div class="log-page">
          <header class="log-header">
            <button type="button" id="log-back-btn" class="log-back">
              <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor"
                stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M19 12H5" /><path d="M11 18 5 12l6-6" />
              </svg>
              <span>Back</span>
            </button>
            <h1>Practice log</h1>
          </header>
          <p id="log-empty" class="lede" hidden>No practices logged yet — start one from the main page.</p>
          <ul id="log-list" class="log-list"></ul>
        </div>
      </section>
    </main>
    <script src="/static/app.js"></script>
  </body>
</html>
```

- [ ] **Step 2: Add CSS for the header buttons and the log view**

In `src/timed_sketching_helper/static/styles.css`, find:

```css
.brand {
  display: flex;
  align-items: center;
  gap: 9px;
  margin-bottom: 24px;
  color: var(--fg-dim);
  font-family: Georgia, "Iowan Old Style", serif;
  letter-spacing: 0.01em;
}
```

Replace with:

```css
.brand {
  display: flex;
  align-items: center;
  gap: 9px;
  margin-bottom: 24px;
  color: var(--fg-dim);
  font-family: Georgia, "Iowan Old Style", serif;
  letter-spacing: 0.01em;
}

.brand-actions {
  margin-left: auto;
  display: flex;
  gap: 6px;
  font-family: system-ui, sans-serif;
}
.brand-actions button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  padding: 0;
  color: var(--muted);
  background: var(--panel-2);
  border: 1px solid var(--border);
  border-radius: 8px;
  cursor: pointer;
  transition: border-color 0.12s, color 0.12s;
}
.brand-actions button:hover { border-color: var(--accent); color: var(--accent); }
```

Then, at the end of the file, add:

```css
/* ---- Practice log view ------------------------------------------------ */

#view-practice-log { display: block; }

.log-page {
  width: 100%;
  max-width: 720px;
  margin: 0 auto;
}

.log-header {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 20px;
}
.log-header h1 {
  margin: 0;
  font-size: 20px;
}

.log-back {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  font: 500 13px system-ui, sans-serif;
  color: var(--fg-dim);
  background: var(--panel-2);
  border: 1px solid var(--border);
  border-radius: 9px;
  cursor: pointer;
}
.log-back:hover { border-color: var(--accent); color: var(--fg); }

.log-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.log-row {
  background: var(--panel);
  border: 1px solid var(--border-soft);
  border-radius: 12px;
  padding: 12px 14px;
}
.log-row-main {
  display: flex;
  align-items: center;
  gap: 12px;
}
.log-row-main .saved-thumb { width: 44px; }

.log-info { flex: 1; min-width: 0; }
.log-title {
  font-size: 14px;
  color: var(--fg);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.log-meta {
  margin-top: 2px;
  font-size: 12px;
  color: var(--muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.log-status {
  flex: none;
  font: 600 11px/1 system-ui, sans-serif;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  padding: 4px 8px;
  border-radius: 6px;
  color: var(--muted);
  background: var(--panel-2);
  border: 1px solid var(--border);
}
.log-status[data-status="completed"] { color: var(--accent-hi); }
.log-status[data-status="ended_early"] { color: var(--danger); }

.log-row-actions {
  flex: none;
  display: flex;
  gap: 6px;
}
.log-row-actions button {
  padding: 7px 10px;
  font: 500 12px system-ui, sans-serif;
  color: var(--fg-dim);
  background: var(--panel-2);
  border: 1px solid var(--border);
  border-radius: 8px;
  cursor: pointer;
}
.log-row-actions button:hover { border-color: var(--accent); color: var(--fg); }
.log-row-actions .log-del:hover { border-color: var(--danger); color: var(--danger); }

.log-expand-btn {
  margin-top: 8px;
  padding: 6px 10px;
  font: 500 12px system-ui, sans-serif;
  color: var(--muted);
  background: transparent;
  border: 1px dashed var(--border);
  border-radius: 8px;
  cursor: pointer;
}
.log-expand-btn:hover { color: var(--fg-dim); border-color: var(--accent); }

.log-thumbs {
  margin-top: 10px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.log-thumbs a {
  display: block;
  width: 52px;
  height: 52px;
  border-radius: 8px;
  overflow: hidden;
  background: var(--panel-2);
  border: 1px solid var(--border);
}
.log-thumbs img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
```

- [ ] **Step 3: Add the view-switching entry and log rendering logic to `app.js`**

In `src/timed_sketching_helper/static/app.js`, find:

```js
const views = {
  start: $("#view-start"),
  session: $("#view-session"),
  done: $("#view-done"),
};
```

Replace with:

```js
const views = {
  start: $("#view-start"),
  session: $("#view-session"),
  done: $("#view-done"),
  practiceLog: $("#view-practice-log"),
};
```

Then, near the end of the file, find:

```js
$("#again-btn").addEventListener("click", startSession);
$("#new-btn").addEventListener("click", () => {
  renderSaved();
  loadAuthStatus();
  show("start");
});
```

and add, directly after it:

```js
// ---- Practice log -----------------------------------------------------

function formatLogTimestamp(iso) {
  try {
    return new Date(iso).toLocaleString(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
    });
  } catch {
    return iso;
  }
}

function logStatusLabel(status) {
  if (status === "completed") return "Completed";
  if (status === "ended_early") return "Ended early";
  return "In progress";
}

function logRow(entry) {
  const li = document.createElement("li");
  li.className = "log-row";

  const main = document.createElement("div");
  main.className = "log-row-main";

  const info = document.createElement("div");
  info.className = "log-info";
  const title = document.createElement("div");
  title.className = "log-title";
  title.textContent = entry.list_title || entry.source_url;
  const meta = document.createElement("div");
  meta.className = "log-meta";
  const shown =
    entry.shown_count != null
      ? `${entry.shown_count}/${entry.count} shown`
      : `${entry.count} images`;
  meta.textContent = `${entry.list_kind || "?"} · ${shown} · ${entry.duration}s each · ${formatLogTimestamp(entry.started_at)}`;
  info.append(title, meta);

  const status = document.createElement("span");
  status.className = "log-status";
  status.dataset.status = entry.status;
  status.textContent = logStatusLabel(entry.status);

  const actions = document.createElement("div");
  actions.className = "log-row-actions";
  const restart = document.createElement("button");
  restart.type = "button";
  restart.textContent = "Restart";
  restart.addEventListener("click", () => restartPractice(entry));
  const del = document.createElement("button");
  del.type = "button";
  del.className = "log-del";
  del.textContent = "Delete";
  del.addEventListener("click", () => deleteLogEntry(entry.id, li));
  actions.append(restart, del);

  main.append(
    savedThumb({ kind: entry.list_kind, title: entry.list_title, thumb: entry.thumb }),
    info,
    status,
    actions,
  );

  const expandBtn = document.createElement("button");
  expandBtn.type = "button";
  expandBtn.className = "log-expand-btn";
  expandBtn.textContent = "Show images";
  const thumbs = document.createElement("div");
  thumbs.className = "log-thumbs";
  thumbs.hidden = true;
  expandBtn.addEventListener("click", async () => {
    if (!thumbs.hidden) {
      thumbs.hidden = true;
      expandBtn.textContent = "Show images";
      return;
    }
    expandBtn.textContent = "Loading…";
    try {
      const detail = await api(`/api/practice-log/${entry.id}`);
      thumbs.innerHTML = "";
      for (const item of detail.items) {
        const link = document.createElement("a");
        const href = externalHref(item.page_url);
        if (href) {
          link.href = href;
          link.target = "_blank";
          link.rel = "noreferrer";
        } else {
          link.href = "#";
        }
        link.title = item.title || "";
        const img = document.createElement("img");
        img.loading = "lazy";
        img.alt = item.title || "";
        img.src = `/api/images/${encodeURIComponent(item.source_id)}`;
        img.addEventListener("error", () => img.remove());
        link.appendChild(img);
        thumbs.appendChild(link);
      }
      thumbs.hidden = false;
      expandBtn.textContent = "Hide images";
    } catch {
      expandBtn.textContent = "Show images";
    }
  });

  li.append(main, expandBtn, thumbs);
  return li;
}

async function loadPracticeLog() {
  const list = $("#log-list");
  list.innerHTML = "";
  let entries = [];
  try {
    entries = await api("/api/practice-log");
  } catch {
    /* leave the list empty on failure */
  }
  $("#log-empty").hidden = entries.length > 0;
  for (const entry of entries) list.appendChild(logRow(entry));
}

async function deleteLogEntry(id, rowEl) {
  try {
    await fetch(`/api/practice-log/${id}`, { method: "DELETE" });
  } catch {
    /* best effort */
  }
  rowEl.remove();
  if (!$("#log-list").children.length) $("#log-empty").hidden = false;
}

function openPracticeLog() {
  show("practiceLog");
  loadPracticeLog();
}

async function restartPractice(entry) {
  show("start");
  await beginFromUrl(entry.source_url, {
    count: entry.count,
    duration: entry.duration,
  });
}

$("#practice-log-btn").addEventListener("click", openPracticeLog);
$("#log-back-btn").addEventListener("click", () => show("start"));
```

- [ ] **Step 4: Run the full backend test suite (regression check)**

Run: `uv run pytest -q`
Expected: PASS (this task touches no Python).

- [ ] **Step 5: Manual smoke check**

Run: `uv run timed-sketching-helper &`, then:
```bash
curl -s http://127.0.0.1:8765/ | grep -c 'id="practice-log-btn"'
curl -s http://127.0.0.1:8765/ | grep -c 'id="view-practice-log"'
curl -s http://127.0.0.1:8765/static/app.js | grep -c 'function openPracticeLog'
```
Expect `1` for each. Stop the server (`kill %1`). Interactive verification (opening the log, expanding a row's thumbnails, deleting an entry, restarting a practice) is done manually by the user in a browser.

- [ ] **Step 6: Commit**

```bash
git add src/timed_sketching_helper/static/index.html src/timed_sketching_helper/static/styles.css src/timed_sketching_helper/static/app.js
git commit -m "Add the practice log view: list, expand-to-view-images, delete, restart"
```

---

## Task 9: Frontend — app-wide Settings modal with "Clear practice log"

**Files:**
- Modify: `src/timed_sketching_helper/static/index.html`
- Modify: `src/timed_sketching_helper/static/styles.css`
- Modify: `src/timed_sketching_helper/static/app.js`

**Interfaces:**
- Consumes: `DELETE /api/practice-log` (Task 5), `loadPracticeLog()` and `views.practiceLog` (Task 8).
- Produces: `#app-settings-modal`, opened by the `#app-settings-btn` added in Task 8.

- [ ] **Step 1: Add the modal markup**

In `src/timed_sketching_helper/static/index.html`, find the closing of the new practice-log view added in Task 8:

```html
          <p id="log-empty" class="lede" hidden>No practices logged yet — start one from the main page.</p>
          <ul id="log-list" class="log-list"></ul>
        </div>
      </section>
    </main>
```

Replace with (inserting the modal before `</main>`):

```html
          <p id="log-empty" class="lede" hidden>No practices logged yet — start one from the main page.</p>
          <ul id="log-list" class="log-list"></ul>
        </div>
      </section>

      <div id="app-settings-modal" hidden>
        <div id="app-settings-backdrop"></div>
        <div id="app-settings-panel" role="dialog" aria-modal="true" aria-labelledby="app-settings-title">
          <div id="app-settings-header">
            <h2 id="app-settings-title">Settings</h2>
            <button type="button" id="app-settings-close" title="Close" aria-label="Close">
              <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor"
                stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M6 6l12 12M18 6 6 18" />
              </svg>
            </button>
          </div>
          <div class="settings-section">
            <h3>Practice log</h3>
            <p class="field-hint">Every practice you run is saved locally so you can revisit or restart it.</p>
            <button type="button" id="clear-practice-log-btn" class="danger-btn">Clear practice log</button>
          </div>
        </div>
      </div>
    </main>
```

- [ ] **Step 2: Add CSS for the modal**

At the end of `src/timed_sketching_helper/static/styles.css`, add:

```css
/* ---- App-wide settings modal ------------------------------------------ */
/* Opened from the start screen's gear button (#app-settings-btn). Separate
   from #settings-modal (the in-session dock/beep modal, which auto-pauses
   the running practice) since this one is only ever reachable when no
   practice is running. */

#app-settings-modal {
  position: fixed;
  inset: 0;
  z-index: 20;
  display: flex;
  align-items: center;
  justify-content: center;
}
#app-settings-backdrop {
  position: absolute;
  inset: 0;
  background: rgba(8, 9, 10, 0.6);
}
#app-settings-panel {
  position: relative;
  width: 100%;
  max-width: 360px;
  margin: 16px;
  background: var(--panel);
  border: 1px solid var(--border-soft);
  border-radius: 16px;
  padding: 20px 22px 24px;
  box-shadow: 0 30px 70px -30px rgba(0, 0, 0, 0.7);
}
#app-settings-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
#app-settings-header h2 {
  margin: 0;
  font: 600 17px/1 system-ui, sans-serif;
  color: var(--fg);
}
#app-settings-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 6px;
  color: var(--muted);
  background: transparent;
  border: 1px solid transparent;
  border-radius: 8px;
  cursor: pointer;
}
#app-settings-close:hover { color: var(--fg); border-color: var(--border); }

.danger-btn {
  margin-top: 10px;
  padding: 9px 14px;
  font: 500 13px system-ui, sans-serif;
  color: var(--danger);
  background: var(--panel-2);
  border: 1px solid var(--border);
  border-radius: 9px;
  cursor: pointer;
}
.danger-btn:hover { border-color: var(--danger); background: rgba(214, 139, 129, 0.12); }
```

- [ ] **Step 3: Add the JS behavior**

In `src/timed_sketching_helper/static/app.js`, find the end of the Practice log section added in Task 8:

```js
$("#practice-log-btn").addEventListener("click", openPracticeLog);
$("#log-back-btn").addEventListener("click", () => show("start"));
```

Replace with (adding the settings modal logic after it):

```js
$("#practice-log-btn").addEventListener("click", openPracticeLog);
$("#log-back-btn").addEventListener("click", () => show("start"));

// ---- App-wide settings modal -------------------------------------------

function openAppSettings() {
  $("#app-settings-modal").hidden = false;
}

function closeAppSettings() {
  $("#app-settings-modal").hidden = true;
}

$("#app-settings-btn").addEventListener("click", openAppSettings);
$("#app-settings-backdrop").addEventListener("click", closeAppSettings);
$("#app-settings-close").addEventListener("click", closeAppSettings);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("#app-settings-modal").hidden) closeAppSettings();
});

$("#clear-practice-log-btn").addEventListener("click", async () => {
  if (!window.confirm("Clear your entire practice log? This can't be undone.")) return;
  try {
    await fetch("/api/practice-log", { method: "DELETE" });
  } catch {
    /* best effort */
  }
  closeAppSettings();
  if (!views.practiceLog.hidden) loadPracticeLog();
});
```

- [ ] **Step 4: Run the full backend test suite (regression check)**

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Step 5: Manual smoke check**

Run: `uv run timed-sketching-helper &`, then:
```bash
curl -s http://127.0.0.1:8765/ | grep -c 'id="app-settings-modal"'
curl -s http://127.0.0.1:8765/ | grep -c 'id="clear-practice-log-btn"'
```
Expect `1` for each. Stop the server (`kill %1`). Interactive verification (opening Settings, clearing the log, confirming the log view updates) is done manually by the user.

- [ ] **Step 6: Commit**

```bash
git add src/timed_sketching_helper/static/index.html src/timed_sketching_helper/static/styles.css src/timed_sketching_helper/static/app.js
git commit -m "Add app-wide Settings modal with Clear practice log"
```

---

## Task 10: Document the practice log subsystem in `CLAUDE.md`

**Files:**
- Modify: `CLAUDE.md`

**Interfaces:** None — documentation only.

- [ ] **Step 1: Add an architecture bullet**

In `CLAUDE.md`, in the "Architecture" section, after the bullet point describing "Reroll swaps in the pre-downloaded backup pool..." (search for `**Countdown beep.**` and insert before it), add:

```markdown
- **Practice log.** `POST /api/sessions` best-effort writes a `practice_log`
  row (+ a `practice_log_items` snapshot of the selected images) via
  `db.create_practice_log`, named "practice" throughout to avoid colliding
  with the unrelated `tsh_session` browser-identity cookie and the
  `/api/sessions` drawing-session machinery above — neither is renamed.
  `finishSession()`/`endSession()` in `app.js` fire-and-forget a
  `PATCH /api/practice-log/{id}` with the outcome (`completed` /
  `ended_early`) and how many images were actually shown
  (`session.items.length` vs `session.index`). The log is entirely
  independent of `image_lists`/`list_items` — its items are a copy taken at
  practice start, since `list_items` gets overwritten on every re-fetch of a
  list (`db.save_list`). The `#view-practice-log` screen (opened from the
  start screen's `#practice-log-btn`) lists entries via
  `GET /api/practice-log`, lazily loads a row's image thumbnails via
  `GET /api/practice-log/{id}` on expand, and its Restart button calls the
  shared `beginFromUrl()` (also used by the start form's submit handler)
  with that entry's `source_url`/`count`/`duration` to begin a **new**
  practice with a fresh random selection — no special-casing, since it's
  the same code path as any other practice start. `DELETE
  /api/practice-log/{id}` removes one entry; `DELETE /api/practice-log`
  (wired to "Clear practice log" in the new app-wide `#app-settings-modal`,
  distinct from the in-session dock/beep `#settings-modal`) clears all of
  them for the account.
```

- [ ] **Step 2: Run the full test suite one last time**

Run: `uv run pytest -q`
Expected: PASS, full suite green.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "Document the practice log subsystem in CLAUDE.md"
```

---

## Spec coverage check (self-review)

- Data model (`practice_log`/`practice_log_items`) — Task 1.
- `POST /api/sessions` writes the log entry — Task 2.
- Outcome tracking (`PATCH .../finish`) — Task 3.
- List/read endpoints — Task 4.
- Delete-one / clear-all endpoints — Task 5.
- Frontend `practiceId` wiring + outcome reporting — Task 6.
- `beginFromUrl` extraction (needed for restart) — Task 7.
- Practice log view: button on main page, per-entry delete, per-entry
  restart with fresh random images, image thumbnails — Task 8.
- Settings-based full-log deletion, reachable via a main-page button — Task 9.
- Statistics: explicitly out of scope for this pass (Non-goals in the spec)
  — no task needed; the data model supports it later.
- Architecture documentation kept current — Task 10.
