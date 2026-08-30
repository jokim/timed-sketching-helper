import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from timed_sketching_helper import db as db_module
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

    async def list_images(self, ref):
        return list(self.images)


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

    app = create_app(
        conn=conn,
        cache=ImageCache(conn, tmp_path / "cache"),
        resolver=resolver,
    )
    return TestClient(app)


@respx.mock
def test_create_list_then_read_and_recent(client):
    respx.mock.get(url__startswith="https://img.example/").mock(
        return_value=httpx.Response(200, content=b"x", headers={"Content-Type": "image/png"})
    )

    created = client.post("/api/lists", json={"url": GALLERY_URL}).json()
    assert created["count"] == 3

    listing = client.get(f"/api/lists/{created['list_id']}").json()
    assert [i["source_id"] for i in listing["items"]] == ["a", "b", "c"]

    recent = client.get("/api/recent").json()
    assert recent[0]["url"] == GALLERY_URL


@respx.mock
def test_create_session_partitions_items(client):
    respx.mock.get(url__startswith="https://img.example/").mock(
        return_value=httpx.Response(200, content=b"x", headers={"Content-Type": "image/png"})
    )
    list_id = client.post("/api/lists", json={"url": GALLERY_URL}).json()["list_id"]

    session = client.post(
        "/api/sessions", json={"list_id": list_id, "count": 2, "duration": 30}
    ).json()

    assert session["duration"] == 30
    assert len(session["items"]) == 2
    assert len(session["reroll_pool"]) == 1
    shown = {i["source_id"] for i in session["items"]}
    pooled = {i["source_id"] for i in session["reroll_pool"]}
    assert shown | pooled == {"a", "b", "c"}
    assert shown.isdisjoint(pooled)


def test_unknown_source_url_returns_400(client):
    res = client.post("/api/lists", json={"url": "https://example.com/whatever"})
    assert res.status_code == 400
    assert "error" in res.json()


@respx.mock
def test_image_endpoint_serves_cached_bytes(client):
    respx.mock.get("https://img.example/a.png").mock(
        return_value=httpx.Response(
            200, content=b"PNGBYTES", headers={"Content-Type": "image/png"}
        )
    )
    respx.mock.get(url__startswith="https://img.example/").mock(
        return_value=httpx.Response(200, content=b"other", headers={"Content-Type": "image/png"})
    )
    client.post("/api/lists", json={"url": GALLERY_URL})

    res = client.get("/api/images/a")

    assert res.status_code == 200
    assert res.content == b"PNGBYTES"
    assert res.headers["content-type"] == "image/png"


def test_image_endpoint_404_for_unknown_id(client):
    assert client.get("/api/images/nope").status_code == 404


def test_prefs_round_trip(client):
    assert client.get("/api/prefs").json() == {
        "default_count": 20,
        "default_duration": 90,
    }

    updated = client.put(
        "/api/prefs", json={"default_count": 12, "default_duration": 60}
    ).json()
    assert updated == {"default_count": 12, "default_duration": 60}
    assert client.get("/api/prefs").json()["default_count"] == 12
