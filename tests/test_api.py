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

    async def list_images(
        self, ref, *, on_progress=None, max_images=None, max_requests=None
    ):
        self.last_max_images = max_images
        self.last_max_requests = max_requests
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

    app = create_app(
        conn=conn,
        cache=ImageCache(conn, tmp_path / "cache"),
        resolver=resolver,
    )
    return TestClient(app, base_url="http://localhost")


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
    return TestClient(app, base_url="http://localhost")


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


def test_create_list_honours_max_images_from_the_body(client):
    body = client.post(
        "/api/lists", json={"url": GALLERY_URL, "max_images": 1}
    ).json()
    assert body["count"] == 1


def test_create_list_rejects_a_non_positive_max_images(client):
    res = client.post("/api/lists", json={"url": GALLERY_URL, "max_images": 0})
    assert res.status_code == 422


def test_create_list_forwards_max_requests_from_the_body(conn, tmp_path):
    provider = FakeProvider([meta("a"), meta("b")])

    def resolver(url):
        if provider.matches(url):
            return provider
        raise UnknownSourceError(url)

    app = create_app(
        conn=conn, cache=ImageCache(conn, tmp_path / "cache"), resolver=resolver
    )
    client = TestClient(app, base_url="http://localhost")

    client.post("/api/lists", json={"url": GALLERY_URL, "max_requests": 250})

    assert provider.last_max_requests == 250


def _rate_limited_client(conn, tmp_path):
    from timed_sketching_helper.sources.deviantart import DeviantArtRateLimitError

    class RateLimitedProvider(FakeProvider):
        async def list_images(self, ref, **kwargs):
            raise DeviantArtRateLimitError("Wait a few minutes and try again.")

    provider = RateLimitedProvider([])
    app = create_app(
        conn=conn,
        cache=ImageCache(conn, tmp_path / "cache"),
        resolver=lambda url: provider,
    )
    return TestClient(app, base_url="http://localhost")


def test_rate_limit_from_the_provider_returns_429(conn, tmp_path):
    client = _rate_limited_client(conn, tmp_path)

    res = client.post("/api/lists", json={"url": GALLERY_URL})

    assert res.status_code == 429
    assert "few minutes" in res.json()["error"]


def test_rate_limit_surfaces_as_an_error_line_on_the_stream(conn, tmp_path):
    client = _rate_limited_client(conn, tmp_path)

    messages = _stream_lines(client, GALLERY_URL)

    assert messages[-1]["type"] == "error"
    assert "few minutes" in messages[-1]["error"]


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
def test_create_session_precaches_shown_and_backup_pool_images(conn, tmp_path):
    respx.mock.get(url__startswith="https://img.example/").mock(
        return_value=httpx.Response(200, content=b"x", headers={"Content-Type": "image/png"})
    )
    ids = "abcdefg"  # 7 images, so the reroll pool (5) exceeds BACKUP_POOL_SIZE (3)
    provider = FakeProvider([meta(c) for c in ids])

    def resolver(url):
        if provider.matches(url):
            return provider
        raise UnknownSourceError(url)

    app = create_app(
        conn=conn, cache=ImageCache(conn, tmp_path / "cache"), resolver=resolver
    )
    client = TestClient(app, base_url="http://localhost")
    list_id = client.post("/api/lists", json={"url": GALLERY_URL}).json()["list_id"]

    session = client.post(
        "/api/sessions", json={"list_id": list_id, "count": 2, "duration": 30}
    ).json()

    shown = {i["source_id"] for i in session["items"]}
    pool_ids = [i["source_id"] for i in session["reroll_pool"]]
    assert len(pool_ids) == 5
    backup, rest = set(pool_ids[:3]), set(pool_ids[3:])

    cached = {sid for sid in ids if db_module.get_cache_entry(conn, sid)}
    assert cached == shown | backup
    assert rest.isdisjoint(cached)


@respx.mock
def test_precache_endpoint_downloads_named_ids(client, conn):
    respx.mock.get(url__startswith="https://img.example/").mock(
        return_value=httpx.Response(200, content=b"x", headers={"Content-Type": "image/png"})
    )
    client.post("/api/lists", json={"url": GALLERY_URL})
    assert db_module.get_cache_entry(conn, "c") is None

    res = client.post("/api/precache", json={"source_ids": ["c"]})

    assert res.status_code == 200
    assert db_module.get_cache_entry(conn, "c") is not None


def test_precache_endpoint_ignores_unknown_ids(client):
    res = client.post("/api/precache", json={"source_ids": ["nope"]})
    assert res.status_code == 200


class RotatingProvider:
    """Hands out a fresh signed URL for each image on every re-fetch."""

    name = "deviantart"

    def __init__(self):
        self.calls = 0

    def matches(self, url):
        return "deviantart.com" in url

    def parse(self, url):
        return SourceRef("deviantart", "gallery", "artist", None, url)

    async def list_images(
        self, ref, *, on_progress=None, max_images=None, max_requests=None
    ):
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
    client = TestClient(app, base_url="http://localhost")

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


def test_rejects_request_with_unknown_host_header(client):
    res = client.get("/api/prefs", headers={"host": "attacker.example"})
    assert res.status_code == 400


def test_accepts_loopback_host_headers(client):
    for host in ("localhost", "127.0.0.1", "127.0.0.1:8765"):
        assert client.get("/api/prefs", headers={"host": host}).status_code == 200


def test_trusted_hosts_wildcard_disables_the_check(conn, tmp_path):
    app = create_app(
        conn=conn,
        cache=ImageCache(conn, tmp_path / "cache"),
        resolver=lambda url: None,
        trusted_hosts=["*"],
    )
    open_client = TestClient(app, base_url="http://anything.example")
    assert open_client.get("/api/prefs").status_code == 200


def test_logout_blocked_from_cross_site_request(auth_client, conn):
    db_module.save_oauth(
        conn,
        1,
        access_token="a",
        refresh_token="r",
        expires_at="2999-01-01T00:00:00+00:00",
        scope="",
        username="ninjatron",
    )

    res = auth_client.post(
        "/auth/deviantart/logout", headers={"sec-fetch-site": "cross-site"}
    )

    assert res.status_code == 403
    assert auth_client.get("/auth/deviantart/status").json()["connected"] is True


def test_logout_blocked_when_origin_is_foreign(auth_client, conn):
    db_module.save_oauth(
        conn,
        1,
        access_token="a",
        refresh_token="r",
        expires_at="2999-01-01T00:00:00+00:00",
        scope="",
        username="ninjatron",
    )

    res = auth_client.post(
        "/auth/deviantart/logout", headers={"origin": "http://evil.example"}
    )

    assert res.status_code == 403


def test_logout_allowed_from_same_origin(auth_client, conn):
    db_module.save_oauth(
        conn,
        1,
        access_token="a",
        refresh_token="r",
        expires_at="2999-01-01T00:00:00+00:00",
        scope="",
        username="ninjatron",
    )

    res = auth_client.post(
        "/auth/deviantart/logout",
        headers={"sec-fetch-site": "same-origin", "origin": "http://localhost"},
    )

    assert res.status_code == 204
    assert auth_client.get("/auth/deviantart/status").json()["connected"] is False


def test_cross_site_header_does_not_block_safe_get(client):
    res = client.get("/api/prefs", headers={"sec-fetch-site": "cross-site"})
    assert res.status_code == 200


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
