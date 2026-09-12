from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from timed_sketching_helper import db as db_module
from timed_sketching_helper.imagecache import CacheFetchError, ImageCache
from timed_sketching_helper.models import ListItem


def item(source_id, url):
    return ListItem(
        source_id=source_id,
        title="t",
        author="a",
        image_url=url,
        page_url="p",
        position=0,
    )


@pytest.fixture
def cache(conn, tmp_path):
    return ImageCache(conn, tmp_path / "cache")


@respx.mock
async def test_ensure_downloads_and_records_entry(cache, conn):
    route = respx.mock.get("https://img.example/a.png").mock(
        return_value=httpx.Response(
            200, content=b"PNGDATA", headers={"Content-Type": "image/png"}
        )
    )

    path, content_type = await cache.ensure("a", "https://img.example/a.png")

    assert path.read_bytes() == b"PNGDATA"
    assert content_type == "image/png"
    assert route.call_count == 1
    assert db_module.get_cache_entry(conn, "a")["content_type"] == "image/png"


@respx.mock
async def test_ensure_is_idempotent(cache):
    route = respx.mock.get("https://img.example/a.png").mock(
        return_value=httpx.Response(200, content=b"X", headers={"Content-Type": "image/png"})
    )

    await cache.ensure("a", "https://img.example/a.png")
    await cache.ensure("a", "https://img.example/a.png")

    assert route.call_count == 1


@respx.mock
async def test_open_cached_returns_none_until_downloaded(cache):
    assert cache.open_cached("a") is None
    respx.mock.get("https://img.example/a.png").mock(
        return_value=httpx.Response(200, content=b"X", headers={"Content-Type": "image/jpeg"})
    )

    await cache.ensure("a", "https://img.example/a.png")

    result = cache.open_cached("a")
    assert result is not None
    path, content_type = result
    assert path.exists()
    assert content_type == "image/jpeg"


@respx.mock
async def test_ensure_raises_on_expired_url(cache):
    respx.mock.get("https://img.example/gone.png").mock(
        return_value=httpx.Response(403)
    )

    with pytest.raises(CacheFetchError):
        await cache.ensure("g", "https://img.example/gone.png")


@respx.mock
async def test_ensure_many_downloads_all_and_tolerates_failures(cache, conn):
    respx.mock.get("https://img.example/a.png").mock(
        return_value=httpx.Response(200, content=b"A", headers={"Content-Type": "image/png"})
    )
    respx.mock.get("https://img.example/b.png").mock(
        return_value=httpx.Response(404)
    )
    respx.mock.get("https://img.example/c.png").mock(
        return_value=httpx.Response(200, content=b"C", headers={"Content-Type": "image/png"})
    )

    await cache.ensure_many(
        [
            item("a", "https://img.example/a.png"),
            item("b", "https://img.example/b.png"),
            item("c", "https://img.example/c.png"),
        ]
    )

    assert cache.open_cached("a") is not None
    assert cache.open_cached("b") is None
    assert cache.open_cached("c") is not None


def _backdate(conn, source_id, hours):
    stale = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    conn.execute(
        "UPDATE image_cache SET cached_at = ? WHERE source_id = ?",
        (stale, source_id),
    )
    conn.commit()


@respx.mock
async def test_open_cached_expires_stale_entry(conn, tmp_path):
    cache = ImageCache(conn, tmp_path / "cache", ttl_hours=1)
    respx.mock.get("https://img.example/a.png").mock(
        return_value=httpx.Response(200, content=b"X", headers={"Content-Type": "image/png"})
    )
    path, _ = await cache.ensure("a", "https://img.example/a.png")
    assert path.exists()

    _backdate(conn, "a", hours=2)

    assert cache.open_cached("a") is None
    assert db_module.get_cache_entry(conn, "a") is None
    assert not path.exists()


@respx.mock
async def test_ensure_redownloads_after_ttl_expiry(conn, tmp_path):
    cache = ImageCache(conn, tmp_path / "cache", ttl_hours=1)
    route = respx.mock.get("https://img.example/a.png").mock(
        return_value=httpx.Response(200, content=b"X", headers={"Content-Type": "image/png"})
    )
    await cache.ensure("a", "https://img.example/a.png")
    _backdate(conn, "a", hours=2)

    await cache.ensure("a", "https://img.example/a.png")

    assert route.call_count == 2


@respx.mock
async def test_open_cached_returns_entry_within_ttl(conn, tmp_path):
    cache = ImageCache(conn, tmp_path / "cache", ttl_hours=720)
    respx.mock.get("https://img.example/a.png").mock(
        return_value=httpx.Response(200, content=b"X", headers={"Content-Type": "image/png"})
    )
    await cache.ensure("a", "https://img.example/a.png")
    _backdate(conn, "a", hours=1)

    assert cache.open_cached("a") is not None
