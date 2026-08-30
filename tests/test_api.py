import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from timed_sketching_helper import db as db_module
from timed_sketching_helper.config import Config
from timed_sketching_helper.imagecache import ImageCache
from timed_sketching_helper.main import create_app
from timed_sketching_helper.models import ImageMeta, SourceRef
from timed_sketching_helper.sources.base import UnknownSourceError

GALLERY_URL = "https://www.deviantart.com/artist/gallery/all"
REDIRECT_URI = "http://127.0.0.1:8765/auth/deviantart/callback"
TOKEN_URL = "https://www.deviantart.com/oauth2/token"


class FakeProvider:
    name = "deviantart"

    def __init__(self, images):
        self.images = images

    def matches(self, url):
        return "deviantart.com" in url

    def parse(self, url):
        return SourceRef("deviantart", "gallery", "artist", None, url)

    async def list_images(self, ref, *, on_progress=None):
        for n, _ in enumerate(self.images, start=1):
            if on_progress:
                on_progress(n, n)
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


@pytest.fixture
def auth_client(conn, tmp_path):
    cfg = Config(
        deviantart_client_id="cid",
        deviantart_client_secret="csecret",
        deviantart_redirect_uri=REDIRECT_URI,
        mature_content=True,
        data_dir=tmp_path,
        list_ttl_hours=24,
    )
    app = create_app(
        conn=conn, cache=ImageCache(conn, tmp_path / "cache"), cfg=cfg
    )
    return TestClient(app)


def test_auth_status_starts_disconnected(auth_client):
    assert auth_client.get("/auth/deviantart/status").json() == {
        "connected": False,
        "username": None,
    }


def test_login_redirects_to_authorize_with_matching_state_cookie(auth_client):
    res = auth_client.get("/auth/deviantart/login", follow_redirects=False)

    assert res.status_code == 302
    location = res.headers["location"]
    assert location.startswith("https://www.deviantart.com/oauth2/authorize?")
    state = auth_client.cookies.get("da_oauth_state")
    assert state and f"state={state}" in location
    assert auth_client.cookies.get("da_oauth_verifier")
    assert "code_challenge=" in location
    assert "code_challenge_method=S256" in location


def test_callback_rejects_state_mismatch(auth_client):
    auth_client.cookies.set("da_oauth_state", "expected")

    res = auth_client.get(
        "/auth/deviantart/callback?code=x&state=wrong", follow_redirects=False
    )

    assert res.status_code == 400
    assert auth_client.get("/auth/deviantart/status").json()["connected"] is False


def test_callback_rejects_missing_pkce_verifier(auth_client):
    auth_client.cookies.set("da_oauth_state", "s123")

    res = auth_client.get(
        "/auth/deviantart/callback?code=x&state=s123", follow_redirects=False
    )

    assert res.status_code == 400


@respx.mock
def test_callback_exchanges_code_and_connects(auth_client):
    respx.mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "acc",
                "refresh_token": "ref",
                "expires_in": 3600,
                "scope": "user browse",
            },
        )
    )
    respx.mock.get(url__startswith="https://www.deviantart.com/api/v1/oauth2/user/whoami").mock(
        return_value=httpx.Response(200, json={"username": "ninjatron"})
    )
    auth_client.cookies.set("da_oauth_state", "s123")
    auth_client.cookies.set("da_oauth_verifier", "v123")

    res = auth_client.get(
        "/auth/deviantart/callback?code=the-code&state=s123",
        follow_redirects=False,
    )

    assert res.status_code == 302
    assert res.headers["location"] == "/?da_auth=connected"
    assert auth_client.get("/auth/deviantart/status").json() == {
        "connected": True,
        "username": "ninjatron",
    }


def test_logout_disconnects(auth_client, conn):
    db_module.save_oauth(
        conn,
        1,
        access_token="a",
        refresh_token="r",
        expires_at="2999-01-01T00:00:00+00:00",
        scope="",
        username="ninjatron",
    )

    res = auth_client.post("/auth/deviantart/logout")

    assert res.status_code == 204
    assert auth_client.get("/auth/deviantart/status").json()["connected"] is False


@respx.mock
def test_create_list_then_read_and_recent(client):
    created = client.post("/api/lists", json={"url": GALLERY_URL}).json()
    assert created["count"] == 3

    listing = client.get(f"/api/lists/{created['list_id']}").json()
    assert [i["source_id"] for i in listing["items"]] == ["a", "b", "c"]

    recent = client.get("/api/recent").json()
    assert recent[0]["url"] == GALLERY_URL


def _stream_lines(client, url):
    with client.stream(
        "POST",
        "/api/lists",
        json={"url": url},
        headers={"Accept": "application/x-ndjson"},
    ) as res:
        assert res.status_code == 200
        return [json.loads(line) for line in res.iter_lines() if line.strip()]


def test_create_list_streams_progress_then_result(client):
    messages = _stream_lines(client, GALLERY_URL)

    types = [m["type"] for m in messages]
    assert "progress" in types
    assert types[-1] == "result"

    result = messages[-1]
    assert result["count"] == 3
    assert "list_id" in result
    assert result["thumb"] == "a"  # first image, for the saved-list icon

    requests = [m["requests"] for m in messages if m["type"] == "progress"]
    assert requests == sorted(requests)  # monotonic, never resets


def test_create_list_stream_emits_error_line_for_unknown_url(client):
    messages = _stream_lines(client, "https://example.com/whatever")

    assert messages[-1]["type"] == "error"
    assert messages[-1]["error"]


def test_create_list_without_stream_accept_still_returns_json(client):
    body = client.post("/api/lists", json={"url": GALLERY_URL}).json()
    assert body["count"] == 3
    assert "list_id" in body
    assert body["thumb"] == "a"


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


@respx.mock
def test_create_list_does_not_download_image_bytes(client, conn):
    route = respx.mock.get(url__startswith="https://img.example/").mock(
        return_value=httpx.Response(200, content=b"x", headers={"Content-Type": "image/png"})
    )

    created = client.post("/api/lists", json={"url": GALLERY_URL}).json()

    assert created["count"] == 3
    assert route.call_count == 0
    assert db_module.get_cache_entry(conn, "a") is None


@respx.mock
def test_image_endpoint_downloads_on_demand(client, conn):
    respx.mock.get(url__startswith="https://img.example/").mock(
        return_value=httpx.Response(200, content=b"BYTES", headers={"Content-Type": "image/png"})
    )
    client.post("/api/lists", json={"url": GALLERY_URL})
    assert db_module.get_cache_entry(conn, "b") is None

    res = client.get("/api/images/b")

    assert res.status_code == 200
    assert res.content == b"BYTES"
    assert db_module.get_cache_entry(conn, "b") is not None


@respx.mock
def test_create_session_precaches_only_the_shown_images(client, conn):
    respx.mock.get(url__startswith="https://img.example/").mock(
        return_value=httpx.Response(200, content=b"x", headers={"Content-Type": "image/png"})
    )
    list_id = client.post("/api/lists", json={"url": GALLERY_URL}).json()["list_id"]

    session = client.post(
        "/api/sessions", json={"list_id": list_id, "count": 2, "duration": 30}
    ).json()

    shown = {i["source_id"] for i in session["items"]}
    pooled = {i["source_id"] for i in session["reroll_pool"]}
    cached = {sid for sid in ("a", "b", "c") if db_module.get_cache_entry(conn, sid)}
    assert cached == shown
    for sid in pooled:
        assert db_module.get_cache_entry(conn, sid) is None


class RotatingProvider:
    """Hands out a fresh signed URL for each image on every re-fetch."""

    name = "deviantart"

    def __init__(self):
        self.calls = 0

    def matches(self, url):
        return "deviantart.com" in url

    def parse(self, url):
        return SourceRef("deviantart", "gallery", "artist", None, url)

    async def list_images(self, ref, *, on_progress=None):
        self.calls += 1
        return [
            ImageMeta(
                source_id="a",
                title="Ta",
                author="artist",
                image_url=f"https://img.example/a.png?sig=v{self.calls}",
                page_url="https://www.deviantart.com/artist/art/a",
            )
        ]


@respx.mock
def test_image_endpoint_refreshes_expired_url_and_retries(conn, tmp_path):
    provider = RotatingProvider()

    def resolver(url):
        if provider.matches(url):
            return provider
        raise UnknownSourceError(url)

    app = create_app(
        conn=conn, cache=ImageCache(conn, tmp_path / "cache"), resolver=resolver
    )
    client = TestClient(app)

    respx.mock.get("https://img.example/a.png", params={"sig": "v1"}).mock(
        return_value=httpx.Response(403)
    )
    respx.mock.get("https://img.example/a.png", params={"sig": "v2"}).mock(
        return_value=httpx.Response(200, content=b"FRESH", headers={"Content-Type": "image/png"})
    )

    client.post("/api/lists", json={"url": GALLERY_URL})
    res = client.get("/api/images/a")

    assert res.status_code == 200
    assert res.content == b"FRESH"
    assert provider.calls == 2


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
