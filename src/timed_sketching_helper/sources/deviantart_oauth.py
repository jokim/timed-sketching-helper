"""DeviantArt Authorization Code grant: user login so mature/"sensitive"
deviations come back un-blurred.

The client-credentials grant the browse client uses is treated as anonymous by
DeviantArt, and anonymous ``content.src`` URLs carry a ``blur`` claim for mature
content. A token minted for a real user whose account has mature content enabled
does not. This module runs the login dance and keeps the resulting tokens
(access + rotating refresh) in ``deviantart_oauth``.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from timed_sketching_helper import db
from timed_sketching_helper.sources.deviantart import (
    API_BASE,
    TOKEN_URL,
    USER_AGENT,
    DeviantArtAuthError,
    _error_detail,
)

AUTHORIZE_URL = "https://www.deviantart.com/oauth2/authorize"
# "browse" covers gallery/collections listing; "user" lets us read the linked
# username for display (and confirms *which* account is linked).
SCOPE = "user browse"
# Refresh this many seconds before the access token actually expires.
EXPIRY_MARGIN = timedelta(seconds=60)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def make_pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)``. DeviantArt's authorize
    endpoint now rejects requests without a PKCE challenge."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class DeviantArtOAuth:
    def __init__(
        self,
        conn: sqlite3.Connection,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> None:
        self._conn = conn
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri

    # -- login dance -------------------------------------------------------

    def authorize_url(self, state: str, code_challenge: str) -> str:
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._client_id,
                "redirect_uri": self._redirect_uri,
                "scope": SCOPE,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{AUTHORIZE_URL}?{query}"

    async def exchange(
        self, account_id: int, code: str, code_verifier: str
    ) -> None:
        async with self._http() as client:
            payload = await self._token_request(
                client,
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self._redirect_uri,
                    "code_verifier": code_verifier,
                },
            )
            username = await self._whoami(client, payload["access_token"])
        self._store(account_id, payload, username)

    # -- token access -----------------------------------------------------

    async def access_token(
        self, account_id: int, *, force: bool = False
    ) -> str | None:
        row = db.get_oauth(self._conn, account_id)
        if row is None:
            return None
        if not force and not _is_expired(row["expires_at"]):
            return row["access_token"]

        async with self._http() as client:
            try:
                payload = await self._token_request(
                    client,
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": row["refresh_token"],
                    },
                )
            except DeviantArtAuthError:
                # A dead refresh token is unrecoverable — drop it so the UI
                # prompts for a fresh login instead of failing every request.
                db.delete_oauth(self._conn, account_id)
                raise
        self._store(account_id, payload, row["username"])
        return payload["access_token"]

    # -- status ---------------------------------------------------------

    def status(self, account_id: int) -> dict:
        row = db.get_oauth(self._conn, account_id)
        if row is None:
            return {"connected": False, "username": None}
        return {"connected": True, "username": row["username"]}

    def logout(self, account_id: int) -> None:
        db.delete_oauth(self._conn, account_id)

    # -- internals ------------------------------------------------------

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": USER_AGENT}
        )

    async def _token_request(
        self, client: httpx.AsyncClient, grant: dict
    ) -> dict:
        response = await client.post(
            TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                **grant,
            },
        )
        data = response.json() if response.status_code == 200 else {}
        if response.status_code != 200 or "access_token" not in data:
            raise DeviantArtAuthError(
                f"DeviantArt token request failed ({_error_detail(response)})."
            )
        return data

    async def _whoami(
        self, client: httpx.AsyncClient, access_token: str
    ) -> str | None:
        response = await client.get(
            f"{API_BASE}/user/whoami",
            params={"mature_content": "true"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if response.status_code != 200:
            return None
        return response.json().get("username")

    def _store(self, account_id: int, payload: dict, username: str | None) -> None:
        expires_in = int(payload.get("expires_in", 3600))
        db.save_oauth(
            self._conn,
            account_id,
            access_token=payload["access_token"],
            refresh_token=payload["refresh_token"],
            expires_at=(_now() + timedelta(seconds=expires_in)).isoformat(),
            scope=payload.get("scope", ""),
            username=username,
        )


def _is_expired(expires_at: str) -> bool:
    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError:
        return True
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return _now() >= expiry - EXPIRY_MARGIN
