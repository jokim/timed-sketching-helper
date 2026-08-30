"""DeviantArt source provider: URL parsing + the official API client."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qs, urlparse

import httpx

from timed_sketching_helper.models import ImageMeta, SourceRef

API_BASE = "https://www.deviantart.com/api/v1/oauth2"
# The OAuth2 token endpoint lives outside the /api/v1 tree (see
# https://www.deviantart.com/developers/authentication).
TOKEN_URL = "https://www.deviantart.com/oauth2/token"
PAGE_LIMIT = 24  # the API's maximum
FOLDER_PAGE_LIMIT = 50
MAX_ITEMS = 2000
USER_AGENT = "timed-sketching-helper/0.1 (personal drawing practice tool)"


class DeviantArtAuthError(RuntimeError):
    """The DeviantArt API rejected our credentials."""


class DeviantArtApiError(RuntimeError):
    """The DeviantArt API returned an error for a request."""


def _error_detail(response: httpx.Response) -> str:
    # DeviantArt reports token-endpoint failures as a 3xx redirect to a
    # /settings/applications/redirect_error page with the reason in the query.
    if response.is_redirect:
        query = parse_qs(urlparse(response.headers.get("location", "")).query)
        parts = [
            query[k][0] for k in ("error", "error_description") if query.get(k)
        ]
        return ": ".join(parts) if parts else f"HTTP {response.status_code} redirect"
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    parts = [str(body[k]) for k in ("error", "error_description") if body.get(k)]
    return ": ".join(parts) if parts else f"HTTP {response.status_code}"


UserTokenProvider = Callable[..., Awaitable[str | None]]


class DeviantArtProvider:
    name = "deviantart"

    def __init__(
        self,
        client_id: str = "",
        client_secret: str = "",
        *,
        user_token: UserTokenProvider | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        # When set, an awaitable returning a logged-in user's access token (or
        # None if nobody is logged in). Preferred over the client-credentials
        # grant because anonymous tokens get blurred mature content.
        self._user_token = user_token
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    # -- URL handling ---------------------------------------------------------

    def matches(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return host == "deviantart.com" or host.endswith(".deviantart.com")

    def parse(self, url: str) -> SourceRef:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if not (host == "deviantart.com" or host.endswith(".deviantart.com")):
            raise ValueError(f"Not a DeviantArt URL: {url!r}")

        segments = [s for s in parsed.path.split("/") if s]

        if host.endswith(".deviantart.com") and host not in {
            "deviantart.com",
            "www.deviantart.com",
        }:
            username = host.split(".", 1)[0]
        else:
            if not segments:
                raise ValueError(f"DeviantArt URL has no username: {url!r}")
            username = segments.pop(0)

        section = segments[0].lower() if segments else "gallery"
        kind = "favourites" if section == "favourites" else "gallery"

        folder_id: str | None = None
        if len(segments) >= 2 and segments[1].lower() != "all":
            folder_id = segments[1]

        return SourceRef(
            provider=self.name,
            kind=kind,
            username=username,
            folder_id=folder_id,
            raw_url=url,
        )

    # -- API access ---------------------------------------------------------

    async def list_images(self, ref: SourceRef) -> list[ImageMeta]:
        endpoint = "gallery" if ref.kind == "gallery" else "collections"
        async with httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": USER_AGENT}
        ) as client:
            folder_ids = await self._target_folder_ids(client, ref, endpoint)

            images: list[ImageMeta] = []
            seen: set[str] = set()
            for folder_id in folder_ids:
                async for deviation in self._iter_folder(
                    client, endpoint, folder_id, ref.username
                ):
                    meta = _deviation_to_meta(deviation)
                    if meta is not None and meta.source_id not in seen:
                        seen.add(meta.source_id)
                        images.append(meta)
                    if len(images) >= MAX_ITEMS:
                        return images
            return images

    async def _target_folder_ids(
        self, client: httpx.AsyncClient, ref: SourceRef, endpoint: str
    ) -> list[str]:
        if ref.folder_id and ref.folder_id.isdigit():
            return [ref.folder_id]
        if ref.folder_id:
            return [await self._folder_id_by_name(client, endpoint, ref)]
        # The URL targets the whole gallery / all favourites.
        if endpoint == "gallery":
            return ["all"]  # /gallery/all is a real endpoint
        # There is no /collections/all, so aggregate every collection folder.
        return await self._all_folder_ids(client, endpoint, ref.username)

    async def _iter_folder(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        folder_id: str,
        username: str,
    ):
        offset = 0
        while True:
            payload = await self._get(
                client,
                f"/{endpoint}/{folder_id}",
                params={
                    "username": username,
                    "offset": offset,
                    "limit": PAGE_LIMIT,
                },
            )
            for deviation in payload.get("results", []):
                yield deviation
            if not payload.get("has_more"):
                return
            next_offset = payload.get("next_offset")
            if next_offset is None:
                return
            offset = next_offset

    async def _all_folder_ids(
        self, client: httpx.AsyncClient, endpoint: str, username: str
    ) -> list[str]:
        return [f["folderid"] for f in await self._folders(client, endpoint, username)]

    async def _folder_id_by_name(
        self, client: httpx.AsyncClient, endpoint: str, ref: SourceRef
    ) -> str:
        wanted = ref.folder_id.replace("-", " ").lower()
        for folder in await self._folders(client, endpoint, ref.username):
            if folder.get("name", "").lower() == wanted:
                return str(folder["folderid"])
        raise DeviantArtApiError(
            f"DeviantArt {endpoint} folder not found: {ref.folder_id!r}"
        )

    async def _folders(
        self, client: httpx.AsyncClient, endpoint: str, username: str
    ) -> list[dict]:
        folders: list[dict] = []
        offset = 0
        while True:
            payload = await self._get(
                client,
                f"/{endpoint}/folders",
                params={
                    "username": username,
                    "offset": offset,
                    "limit": FOLDER_PAGE_LIMIT,
                },
            )
            folders.extend(payload.get("results", []))
            if not payload.get("has_more"):
                return folders
            next_offset = payload.get("next_offset")
            if next_offset is None:
                return folders
            offset = next_offset

    async def _get(
        self, client: httpx.AsyncClient, path: str, params: dict
    ) -> dict:
        token = await self._get_token(client)
        request_params = {**params, "mature_content": "true"}
        response = await client.get(
            f"{API_BASE}{path}",
            params=request_params,
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code == 401:
            self._token = None
            token = await self._get_token(client, force=True)
            response = await client.get(
                f"{API_BASE}{path}",
                params=request_params,
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code >= 400:
            raise DeviantArtApiError(
                f"DeviantArt API request to {path} failed ({_error_detail(response)})."
            )
        return response.json()

    async def _get_token(
        self, client: httpx.AsyncClient, *, force: bool = False
    ) -> str:
        if self._user_token is not None:
            user_token = await self._user_token(force=force)
            if user_token:
                return user_token
        if self._token and not force and time.monotonic() < self._token_expires_at:
            return self._token
        if not (self._client_id and self._client_secret):
            raise DeviantArtAuthError(
                "DeviantArt credentials are not configured."
            )
        response = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        data = response.json() if response.status_code == 200 else {}
        if response.status_code != 200 or "access_token" not in data:
            raise DeviantArtAuthError(
                f"DeviantArt token request failed ({_error_detail(response)}). "
                "Check DEVIANTART_CLIENT_ID / DEVIANTART_CLIENT_SECRET in .env."
            )
        self._token = data["access_token"]
        # Refresh a minute early to avoid races near expiry.
        self._token_expires_at = time.monotonic() + int(data.get("expires_in", 3600)) - 60
        return self._token


def _deviation_to_meta(deviation: dict) -> ImageMeta | None:
    content = deviation.get("content")
    if not content or not content.get("src"):
        return None
    author = deviation.get("author") or {}
    return ImageMeta(
        source_id=deviation["deviationid"],
        title=deviation.get("title", "") or "",
        author=author.get("username", "") or "",
        image_url=content["src"],
        page_url=deviation.get("url", "") or "",
        width=content.get("width"),
        height=content.get("height"),
    )
