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
