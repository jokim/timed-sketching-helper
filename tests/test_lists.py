from datetime import datetime, timedelta, timezone

import pytest

from timed_sketching_helper import db as db_module
from timed_sketching_helper.lists import _title_for, get_list
from timed_sketching_helper.models import ImageMeta, SourceRef

ACCOUNT = 1
URL = "https://www.deviantart.com/artist/gallery/all"


class FakeProvider:
    name = "deviantart"

    def __init__(self, images):
        self.images = images
        self.calls = 0

    def parse(self, url):
        return SourceRef("deviantart", "gallery", "artist", None, url)

    async def list_images(self, ref):
        self.calls += 1
        return list(self.images)


def meta(source_id):
    return ImageMeta(
        source_id=source_id,
        title=f"T{source_id}",
        author="artist",
        image_url=f"https://img/{source_id}.jpg",
        page_url=f"https://page/{source_id}",
    )


def resolver_for(provider):
    return lambda url: provider


def test_title_for_tag():
    ref = SourceRef(
        "deviantart", "tag", "", None,
        "https://www.deviantart.com/tag/hamster", tag="hamster",
    )
    assert _title_for(ref) == "#hamster"


async def test_first_fetch_persists_and_returns_items(conn):
    provider = FakeProvider([meta("a"), meta("b")])

    result = await get_list(conn, ACCOUNT, URL, resolver=resolver_for(provider))

    assert [i.source_id for i in result.items] == ["a", "b"]
    assert provider.calls == 1
    assert result.id is not None


async def test_second_fetch_within_ttl_uses_cache(conn):
    provider = FakeProvider([meta("a")])
    await get_list(conn, ACCOUNT, URL, resolver=resolver_for(provider))

    await get_list(conn, ACCOUNT, URL, resolver=resolver_for(provider))

    assert provider.calls == 1


async def test_force_refresh_refetches(conn):
    provider = FakeProvider([meta("a")])
    await get_list(conn, ACCOUNT, URL, resolver=resolver_for(provider))

    provider.images = [meta("a"), meta("c")]
    result = await get_list(
        conn, ACCOUNT, URL, resolver=resolver_for(provider), force_refresh=True
    )

    assert provider.calls == 2
    assert [i.source_id for i in result.items] == ["a", "c"]


async def test_clear_image_cache_drops_entries_so_they_redownload(conn):
    provider = FakeProvider([meta("a")])
    await get_list(conn, ACCOUNT, URL, resolver=resolver_for(provider))
    db_module.record_cache_entry(conn, "a", "image/jpeg")

    await get_list(
        conn,
        ACCOUNT,
        URL,
        resolver=resolver_for(provider),
        force_refresh=True,
        clear_image_cache=True,
    )

    assert db_module.get_cache_entry(conn, "a") is None


async def test_force_refresh_alone_keeps_cached_image_bytes(conn):
    provider = FakeProvider([meta("a")])
    await get_list(conn, ACCOUNT, URL, resolver=resolver_for(provider))
    db_module.record_cache_entry(conn, "a", "image/jpeg")

    await get_list(
        conn, ACCOUNT, URL, resolver=resolver_for(provider), force_refresh=True
    )

    assert db_module.get_cache_entry(conn, "a") is not None


async def test_stale_list_is_refetched(conn):
    provider = FakeProvider([meta("a")])
    await get_list(conn, ACCOUNT, URL, resolver=resolver_for(provider))
    old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    conn.execute("UPDATE image_lists SET fetched_at = ?", (old,))
    conn.commit()

    await get_list(
        conn, ACCOUNT, URL, resolver=resolver_for(provider), ttl_hours=24
    )

    assert provider.calls == 2


async def test_duplicate_source_ids_are_removed(conn):
    provider = FakeProvider([meta("a"), meta("b"), meta("a")])

    result = await get_list(conn, ACCOUNT, URL, resolver=resolver_for(provider))

    assert [i.source_id for i in result.items] == ["a", "b"]


async def test_get_list_does_not_download_image_bytes(conn):
    provider = FakeProvider([meta("a"), meta("b")])

    await get_list(conn, ACCOUNT, URL, resolver=resolver_for(provider))

    assert db_module.get_cache_entry(conn, "a") is None
    assert db_module.get_cache_entry(conn, "b") is None


async def test_empty_list_raises(conn):
    provider = FakeProvider([])

    with pytest.raises(ValueError):
        await get_list(conn, ACCOUNT, URL, resolver=resolver_for(provider))
