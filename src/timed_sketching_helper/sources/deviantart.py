"""DeviantArt source provider: URL parsing + the official API client."""

from __future__ import annotations

import time
from urllib.parse import urlparse

import httpx

from timed_sketching_helper.models import ImageMeta, SourceRef

API_BASE = "https://www.deviantart.com/api/v1/oauth2"
TOKEN_URL = "https://www.deviantart.com/api/v1/oauth2/token"
PAGE_LIMIT = 24
MAX_ITEMS = 2000
USER_AGENT = "timed-sketching-helper/0.1 (personal drawing practice tool)"


class DeviantArtAuthError(RuntimeError):
    """The DeviantArt API rejected our credentials."""


class DeviantArtProvider:
    name = "deviantart"

    def __init__(self, client_id: str = "", client_secret: str = "") -> None:
        self._client_id = client_id
        self._client_secret = client_secret
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
        async with httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": USER_AGENT}
        ) as client:
            folder_id = await self._resolve_folder_id(client, ref)
            endpoint = "gallery" if ref.kind == "gallery" else "collections"
            path = f"/{endpoint}/{folder_id or 'all'}"

            images: list[ImageMeta] = []
            offset = 0
            while len(images) < MAX_ITEMS:
                payload = await self._get(
                    client,
                    path,
                    params={
                        "username": ref.username,
                        "offset": offset,
                        "limit": PAGE_LIMIT,
                    },
                )
                for deviation in payload.get("results", []):
                    meta = _deviation_to_meta(deviation)
                    if meta is not None:
                        images.append(meta)
                if not payload.get("has_more"):
                    break
                next_offset = payload.get("next_offset")
                if next_offset is None:
                    break
                offset = next_offset
            return images

    async def _resolve_folder_id(
        self, client: httpx.AsyncClient, ref: SourceRef
    ) -> str | None:
        if ref.folder_id is None or ref.folder_id.isdigit():
            return ref.folder_id

        endpoint = "gallery" if ref.kind == "gallery" else "collections"
        payload = await self._get(
            client,
            f"/{endpoint}/folders",
            params={"username": ref.username, "limit": 50},
        )
        wanted = ref.folder_id.replace("-", " ").lower()
        for folder in payload.get("results", []):
            if folder.get("name", "").lower() == wanted:
                return str(folder["folderid"])
        raise ValueError(
            f"DeviantArt {endpoint} folder not found: {ref.folder_id!r}"
        )

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
            token = await self._get_token(client)
            response = await client.get(
                f"{API_BASE}{path}",
                params=request_params,
                headers={"Authorization": f"Bearer {token}"},
            )
        response.raise_for_status()
        return response.json()

    async def _get_token(self, client: httpx.AsyncClient) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
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
        if response.status_code != 200:
            raise DeviantArtAuthError(
                f"DeviantArt token request failed ({response.status_code})."
            )
        data = response.json()
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
