from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from timed_sketching_helper import db as db_module
from timed_sketching_helper.sources.deviantart import API_BASE, TOKEN_URL
from timed_sketching_helper.sources.deviantart import DeviantArtAuthError
from timed_sketching_helper.sources.deviantart_oauth import (
    DeviantArtOAuth,
    make_pkce_pair,
)

REDIRECT = "http://127.0.0.1:8765/auth/deviantart/callback"


def _oauth(conn):
    return DeviantArtOAuth(conn, "cid", "csecret", REDIRECT)


def _iso(dt):
    return dt.isoformat()


def test_make_pkce_pair_challenge_is_s256_of_verifier():
    import base64
    import hashlib

    verifier, challenge = make_pkce_pair()

    assert 43 <= len(verifier) <= 128
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert challenge == expected


def test_authorize_url_contains_required_params(conn):
    url = _oauth(conn).authorize_url("xyz-state", "the-challenge")

    assert url.startswith("https://www.deviantart.com/oauth2/authorize?")
    q = parse_qs(urlparse(url).query)
    assert q["response_type"] == ["code"]
    assert q["client_id"] == ["cid"]
    assert q["redirect_uri"] == [REDIRECT]
    assert q["state"] == ["xyz-state"]
    assert q["code_challenge"] == ["the-challenge"]
    assert q["code_challenge_method"] == ["S256"]
    assert "browse" in q["scope"][0]


@respx.mock
async def test_exchange_persists_tokens_and_username(conn):
    token = respx.mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "acc-1",
                "refresh_token": "ref-1",
                "expires_in": 3600,
                "scope": "user browse",
            },
        )
    )
    whoami = respx.mock.get(url__startswith=f"{API_BASE}/user/whoami").mock(
        return_value=httpx.Response(200, json={"username": "ninjatron"})
    )

    await _oauth(conn).exchange(1, "the-code", "the-verifier")

    assert token.called and whoami.called
    body = token.calls[0].request.content
    assert b"grant_type=authorization_code" in body
    assert b"code=the-code" in body
    assert b"code_verifier=the-verifier" in body
    row = db_module.get_oauth(conn, 1)
    assert row["access_token"] == "acc-1"
    assert row["refresh_token"] == "ref-1"
    assert row["username"] == "ninjatron"


@respx.mock
async def test_exchange_failure_raises_auth_error(conn):
    respx.mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "bad code"}
        )
    )

    with pytest.raises(DeviantArtAuthError, match="invalid_grant"):
        await _oauth(conn).exchange(1, "the-code", "the-verifier")
    assert db_module.get_oauth(conn, 1) is None


async def test_access_token_returns_none_when_not_connected(conn):
    assert await _oauth(conn).access_token(1) is None


async def test_access_token_returns_stored_token_when_fresh(conn):
    db_module.save_oauth(
        conn,
        1,
        access_token="acc-fresh",
        refresh_token="ref-1",
        expires_at=_iso(datetime.now(timezone.utc) + timedelta(hours=1)),
        scope="browse",
        username="ninjatron",
    )

    assert await _oauth(conn).access_token(1) == "acc-fresh"


@respx.mock
async def test_access_token_refreshes_when_expired_and_rotates_refresh_token(conn):
    db_module.save_oauth(
        conn,
        1,
        access_token="acc-old",
        refresh_token="ref-old",
        expires_at=_iso(datetime.now(timezone.utc) - timedelta(minutes=5)),
        scope="browse",
        username="ninjatron",
    )
    token = respx.mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "acc-new",
                "refresh_token": "ref-new",
                "expires_in": 3600,
            },
        )
    )

    result = await _oauth(conn).access_token(1)

    assert result == "acc-new"
    assert b"grant_type=refresh_token" in token.calls[0].request.content
    assert b"refresh_token=ref-old" in token.calls[0].request.content
    row = db_module.get_oauth(conn, 1)
    assert row["access_token"] == "acc-new"
    assert row["refresh_token"] == "ref-new"


@respx.mock
async def test_access_token_force_refreshes_even_when_fresh(conn):
    db_module.save_oauth(
        conn,
        1,
        access_token="acc-old",
        refresh_token="ref-old",
        expires_at=_iso(datetime.now(timezone.utc) + timedelta(hours=1)),
        scope="browse",
        username="ninjatron",
    )
    respx.mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "acc-new", "refresh_token": "ref-new", "expires_in": 3600}
        )
    )

    assert await _oauth(conn).access_token(1, force=True) == "acc-new"


@respx.mock
async def test_refresh_failure_disconnects_and_raises(conn):
    db_module.save_oauth(
        conn,
        1,
        access_token="acc-old",
        refresh_token="ref-old",
        expires_at=_iso(datetime.now(timezone.utc) - timedelta(minutes=5)),
        scope="browse",
        username="ninjatron",
    )
    respx.mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            400, json={"error": "invalid_request", "error_description": "expired refresh"}
        )
    )

    with pytest.raises(DeviantArtAuthError):
        await _oauth(conn).access_token(1)
    assert db_module.get_oauth(conn, 1) is None


def test_status_and_logout(conn):
    o = _oauth(conn)
    assert o.status(1) == {"connected": False, "username": None}

    db_module.save_oauth(
        conn,
        1,
        access_token="acc",
        refresh_token="ref",
        expires_at=_iso(datetime.now(timezone.utc) + timedelta(hours=1)),
        scope="browse",
        username="ninjatron",
    )
    assert o.status(1) == {"connected": True, "username": "ninjatron"}

    o.logout(1)
    assert o.status(1) == {"connected": False, "username": None}
