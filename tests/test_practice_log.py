from timed_sketching_helper import db as db_module
from timed_sketching_helper.models import ListItem

import pytest
from fastapi.testclient import TestClient

from timed_sketching_helper.imagecache import ImageCache
from timed_sketching_helper.main import create_app
from timed_sketching_helper.models import ImageMeta, SourceRef
from timed_sketching_helper.sources.base import UnknownSourceError

GALLERY_URL = "https://www.deviantart.com/artist/gallery/all"
SESSION_A = "session-a"
SESSION_B = "session-b"


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


def _session_id(client):
    """Trigger the session-cookie middleware and return the id it minted."""
    client.get("/api/practice-log")
    return client.cookies.get("tsh_session")


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
        SESSION_A,
        source_url="https://www.deviantart.com/artist/gallery/all",
        list_title="artist · gallery",
        list_kind="gallery",
        count=2,
        duration=30,
        items=[_item("a"), _item("b")],
    )

    row = db_module.get_practice_log(conn, practice_id, SESSION_A)
    assert row["source_url"] == "https://www.deviantart.com/artist/gallery/all"
    assert row["count"] == 2
    assert row["status"] == "in_progress"
    assert row["ended_at"] is None

    items = db_module.practice_log_items(conn, practice_id)
    assert [i["source_id"] for i in items] == ["a", "b"]


def test_finish_practice_log_sets_status_and_shown_count(conn):
    practice_id = db_module.create_practice_log(
        conn, SESSION_A,
        source_url="https://x", list_title="x", list_kind="gallery",
        count=1, duration=10, items=[_item("a")],
    )

    updated = db_module.finish_practice_log(
        conn, practice_id, SESSION_A,
        status="completed", shown_count=1, elapsed_seconds=42,
    )

    assert updated is True
    row = db_module.get_practice_log(conn, practice_id, SESSION_A)
    assert row["status"] == "completed"
    assert row["shown_count"] == 1
    assert row["elapsed_seconds"] == 42
    assert row["ended_at"] is not None


def test_finish_practice_log_returns_false_for_unknown_id(conn):
    assert db_module.finish_practice_log(
        conn, 999, SESSION_A,
        status="completed", shown_count=0, elapsed_seconds=0,
    ) is False


def test_init_db_adds_elapsed_seconds_column_to_existing_practice_log_table(conn):
    conn.execute("ALTER TABLE practice_log DROP COLUMN elapsed_seconds")
    conn.commit()
    columns_before = {
        row["name"] for row in conn.execute("PRAGMA table_info(practice_log)").fetchall()
    }
    assert "elapsed_seconds" not in columns_before

    db_module.init_db(conn)

    columns_after = {
        row["name"] for row in conn.execute("PRAGMA table_info(practice_log)").fetchall()
    }
    assert "elapsed_seconds" in columns_after


def test_init_db_migrates_account_keyed_practice_log_table(conn):
    """An old-schema table (keyed by the shared account id) gets dropped so
    every browser doesn't start out sharing whatever history was recorded
    under it."""
    conn.execute("DROP TABLE practice_log_items")
    conn.execute("DROP TABLE practice_log")
    conn.execute(
        "CREATE TABLE practice_log ("
        " id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL,"
        " source_url TEXT NOT NULL, list_title TEXT NOT NULL,"
        " list_kind TEXT NOT NULL, count INTEGER NOT NULL,"
        " duration INTEGER NOT NULL, started_at TEXT NOT NULL,"
        " ended_at TEXT, status TEXT NOT NULL DEFAULT 'in_progress',"
        " shown_count INTEGER, elapsed_seconds INTEGER)"
    )
    conn.execute(
        "INSERT INTO practice_log"
        " (account_id, source_url, list_title, list_kind, count, duration, started_at, status)"
        " VALUES (1, 'https://x', 'x', 'gallery', 1, 10, '2020-01-01T00:00:00+00:00', 'in_progress')"
    )
    conn.commit()

    db_module.init_db(conn)

    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(practice_log)").fetchall()
    }
    assert "session_id" in columns
    assert "account_id" not in columns
    assert db_module.list_practice_log(conn, SESSION_A) == []


def test_list_practice_log_orders_newest_first_with_thumb(conn):
    first = db_module.create_practice_log(
        conn, SESSION_A,
        source_url="https://x", list_title="x", list_kind="gallery",
        count=1, duration=10, items=[_item("a")],
    )
    second = db_module.create_practice_log(
        conn, SESSION_A,
        source_url="https://y", list_title="y", list_kind="gallery",
        count=1, duration=10, items=[_item("b")],
    )

    rows = db_module.list_practice_log(conn, SESSION_A)

    assert [r["id"] for r in rows] == [second, first]
    assert rows[0]["thumb"] == "b"


def test_list_practice_log_does_not_include_other_sessions_entries(conn):
    db_module.create_practice_log(
        conn, SESSION_A,
        source_url="https://x", list_title="x", list_kind="gallery",
        count=1, duration=10, items=[_item("a")],
    )
    db_module.create_practice_log(
        conn, SESSION_B,
        source_url="https://y", list_title="y", list_kind="gallery",
        count=1, duration=10, items=[_item("b")],
    )

    assert [r["source_url"] for r in db_module.list_practice_log(conn, SESSION_A)] == ["https://x"]
    assert [r["source_url"] for r in db_module.list_practice_log(conn, SESSION_B)] == ["https://y"]


def test_delete_practice_log_cascades_items(conn):
    practice_id = db_module.create_practice_log(
        conn, SESSION_A,
        source_url="https://x", list_title="x", list_kind="gallery",
        count=1, duration=10, items=[_item("a")],
    )

    assert db_module.delete_practice_log(conn, practice_id, SESSION_A) is True
    assert db_module.get_practice_log(conn, practice_id, SESSION_A) is None
    assert db_module.practice_log_items(conn, practice_id) == []


def test_delete_practice_log_returns_false_for_unknown_id(conn):
    assert db_module.delete_practice_log(conn, 999, SESSION_A) is False


def test_delete_practice_log_does_not_remove_other_sessions_entry(conn):
    practice_id = db_module.create_practice_log(
        conn, SESSION_A,
        source_url="https://x", list_title="x", list_kind="gallery",
        count=1, duration=10, items=[_item("a")],
    )

    assert db_module.delete_practice_log(conn, practice_id, SESSION_B) is False
    assert db_module.get_practice_log(conn, practice_id, SESSION_A) is not None


def test_clear_practice_log_removes_all_entries_for_session(conn):
    db_module.create_practice_log(
        conn, SESSION_A,
        source_url="https://x", list_title="x", list_kind="gallery",
        count=1, duration=10, items=[_item("a")],
    )
    db_module.create_practice_log(
        conn, SESSION_A,
        source_url="https://y", list_title="y", list_kind="gallery",
        count=1, duration=10, items=[_item("b")],
    )

    db_module.clear_practice_log(conn, SESSION_A)

    assert db_module.list_practice_log(conn, SESSION_A) == []


def test_clear_practice_log_does_not_remove_other_sessions_entries(conn):
    db_module.create_practice_log(
        conn, SESSION_A,
        source_url="https://x", list_title="x", list_kind="gallery",
        count=1, duration=10, items=[_item("a")],
    )
    db_module.create_practice_log(
        conn, SESSION_B,
        source_url="https://y", list_title="y", list_kind="gallery",
        count=1, duration=10, items=[_item("b")],
    )

    db_module.clear_practice_log(conn, SESSION_A)

    assert db_module.list_practice_log(conn, SESSION_A) == []
    assert len(db_module.list_practice_log(conn, SESSION_B)) == 1


def test_create_session_records_practice_log_entry(client, conn):
    list_id = client.post("/api/lists", json={"url": GALLERY_URL}).json()["list_id"]

    session = client.post(
        "/api/sessions", json={"list_id": list_id, "count": 2, "duration": 30}
    ).json()

    assert session["practice_id"] is not None
    row = db_module.get_practice_log(
        conn, session["practice_id"], client.cookies.get("tsh_session")
    )
    assert row["source_url"] == GALLERY_URL
    assert row["count"] == 2
    assert row["duration"] == 30
    assert row["status"] == "in_progress"
    items = db_module.practice_log_items(conn, session["practice_id"])
    assert {i["source_id"] for i in items} == {i["source_id"] for i in session["items"]}


def test_finish_practice_endpoint_updates_status(client):
    list_id = client.post("/api/lists", json={"url": GALLERY_URL}).json()["list_id"]
    practice_id = client.post(
        "/api/sessions", json={"list_id": list_id, "count": 2, "duration": 30}
    ).json()["practice_id"]

    res = client.patch(
        f"/api/practice-log/{practice_id}",
        json={"status": "completed", "shown_count": 2, "elapsed_seconds": 65},
    )

    assert res.status_code == 200

    entry = client.get(f"/api/practice-log/{practice_id}").json()
    assert entry["elapsed_seconds"] == 65


def test_finish_practice_endpoint_404_for_unknown_id(client):
    res = client.patch(
        "/api/practice-log/999",
        json={"status": "completed", "shown_count": 0, "elapsed_seconds": 0},
    )
    assert res.status_code == 404


def test_finish_practice_endpoint_rejects_invalid_status(client):
    list_id = client.post("/api/lists", json={"url": GALLERY_URL}).json()["list_id"]
    practice_id = client.post(
        "/api/sessions", json={"list_id": list_id, "count": 1, "duration": 10}
    ).json()["practice_id"]

    res = client.patch(
        f"/api/practice-log/{practice_id}",
        json={"status": "bogus", "shown_count": 1, "elapsed_seconds": 1},
    )

    assert res.status_code == 422


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
    assert entries[0]["elapsed_seconds"] is None


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


def test_practice_log_is_isolated_per_browser(conn, tmp_path):
    """Two browsers hitting the same server must never see each other's
    practice history — this is the privacy bug the session-id keying fixes."""
    provider = FakeProvider([meta("a"), meta("b"), meta("c")])

    def resolver(url):
        if provider.matches(url):
            return provider
        raise UnknownSourceError(url)

    app = create_app(conn=conn, cache=ImageCache(conn, tmp_path / "cache"), resolver=resolver)
    browser_a = TestClient(app, base_url="http://localhost")
    browser_b = TestClient(app, base_url="http://localhost")

    list_id = browser_a.post("/api/lists", json={"url": GALLERY_URL}).json()["list_id"]
    browser_a.post("/api/sessions", json={"list_id": list_id, "count": 1, "duration": 10})

    assert len(browser_a.get("/api/practice-log").json()) == 1
    assert browser_b.get("/api/practice-log").json() == []

    list_id_b = browser_b.post("/api/lists", json={"url": GALLERY_URL}).json()["list_id"]
    practice_id_b = browser_b.post(
        "/api/sessions", json={"list_id": list_id_b, "count": 1, "duration": 10}
    ).json()["practice_id"]

    # Browser A can't read, finish, or delete browser B's entry.
    assert browser_a.get(f"/api/practice-log/{practice_id_b}").status_code == 404
    assert browser_a.patch(
        f"/api/practice-log/{practice_id_b}",
        json={"status": "completed", "shown_count": 1, "elapsed_seconds": 1},
    ).status_code == 404
    assert browser_a.delete(f"/api/practice-log/{practice_id_b}").status_code == 404

    # Clearing browser A's log leaves browser B's entry untouched.
    browser_a.delete("/api/practice-log")
    assert browser_a.get("/api/practice-log").json() == []
    assert len(browser_b.get("/api/practice-log").json()) == 1
