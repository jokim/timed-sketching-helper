"""DeviantArt source provider: URL parsing + the official API client."""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from timed_sketching_helper.config import HARD_MAX_IMAGES
from timed_sketching_helper.models import ImageMeta, SourceRef
from timed_sketching_helper.sources.base import ProgressCallback

API_BASE = "https://www.deviantart.com/api/v1/oauth2"
# The OAuth2 token endpoint lives outside the /api/v1 tree (see
# https://www.deviantart.com/developers/authentication).
TOKEN_URL = "https://www.deviantart.com/oauth2/token"
PAGE_LIMIT = 24  # the API's maximum
FOLDER_PAGE_LIMIT = 50
USER_AGENT = "timed-sketching-helper/0.1 (personal drawing practice tool)"


# DeviantArt API folderids are UUIDs (e.g. 7BE985EE-FBDD-B030-80A8-27AC6C590CBD).
# The numeric ids that appear in deviantart.com folder URLs are a different,
# legacy scheme the API does not accept.
_UUID_RE = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)


def _is_api_folder_id(value: str) -> bool:
    return bool(_UUID_RE.match(value))


def _slugify(name: str) -> str:
    """Reduce a folder name to the slug DeviantArt puts in its folder URLs.

    A `deviantart.com/<user>/gallery/<numeric-id>/<slug>` URL's trailing slug is
    the folder name lowercased with every run of non-alphanumerics collapsed to a
    single hyphen ("Confused, bi-product of a misinformed culture" ->
    "confused-bi-product-of-a-misinformed-culture"). Comparing slugified names is
    punctuation-proof where a naive "-" -> " " swap is not.
    """
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# DeviantArt serves sensitive deviations the viewer may not see (logged out, or
# an account with mature content disabled) as a blurred rendition: the wixmp
# transform segment carries a `,blur_<n>` param, e.g.
#   .../v1/fill/w_545,h_800,q_75,strp,blur_34/pretty_by_artist_dxxx-fullview.jpg
# The `,` before `blur_` guarantees it is a transform param, not part of the
# trailing pretty filename.
_BLURRED_SRC_RE = re.compile(r"/v1/[a-z]+/[^/]*,blur_\d")


def _is_blurred_src(src: str) -> bool:
    return bool(_BLURRED_SRC_RE.search(src))


_UUID_CHARS = r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}"


def _seed_uuid_from_page(html: str, numeric_id: str) -> str | None:
    """Pull a deviation's API ``deviationid`` (a UUID) out of its web page.

    A DeviantArt deviation page embeds a Redux state blob whose
    ``deviationExtended`` map is keyed by the legacy numeric id and carries the
    UUID as ``deviationUuid`` (JSON-escaped inside a ``<script>`` string), e.g.
    ``\\"727534988\\":{\\"deviationUuid\\":\\"F91963F8-...\\"``.
    """
    match = re.search(
        rf'{re.escape(numeric_id)}\\?":\{{\\?"deviationUuid\\?":\\?"({_UUID_CHARS})',
        html,
    )
    return match.group(1) if match else None


class _Progress:
    """Tracks a single list fetch and reports it to an optional callback.

    ``request_done`` fires after every upstream API request (the signal that
    matters most — it means we are still making forward progress); ``flush``
    emits the final tally once collection is complete.
    """

    def __init__(self, callback: ProgressCallback | None) -> None:
        self._callback = callback
        self._requests = 0
        self._images = 0

    def request_done(self) -> None:
        self._requests += 1
        self._emit()

    def set_images(self, count: int) -> None:
        self._images = count

    def flush(self) -> None:
        self._emit()

    def _emit(self) -> None:
        if self._callback is not None:
            self._callback(self._requests, self._images)


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
        max_images: int = HARD_MAX_IMAGES,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        # Stop fetching once a list reaches this many images — a session only
        # ever shows a handful. Clamped to HARD_MAX_IMAGES whatever is passed.
        self._max_images = max(1, min(max_images, HARD_MAX_IMAGES))
        # When set, an awaitable returning a logged-in user's access token (or
        # None if nobody is logged in). Preferred over the client-credentials
        # grant because anonymous tokens get blurred mature content.
        self._user_token = user_token
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        # Set by _get_token: True while the active token belongs to a logged-in
        # DeviantArt user, False for the anonymous client-credentials grant.
        # Only used to decide the mature_content request param in _get; the
        # actual sensitive-content filter is the blur check in _collect.
        self._token_is_user = False

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

        # deviantart.com/tag/<name> — a site-wide tag feed, not user-scoped.
        if segments and segments[0].lower() == "tag" and host in {
            "deviantart.com",
            "www.deviantart.com",
        }:
            tag = unquote(segments[1]).lstrip("#").lower() if len(segments) >= 2 else ""
            if not tag:
                raise ValueError(f"DeviantArt tag URL has no tag: {url!r}")
            return SourceRef(
                provider=self.name,
                kind="tag",
                username="",
                folder_id=None,
                raw_url=url,
                tag=tag,
            )

        # deviantart.com/morelikethis/<username>/<seed> — the site's "more like
        # this" feed for one deviation. `seed` is the legacy numeric deviation
        # id from the URL; list_images resolves it to the UUID the API needs.
        if segments and segments[0].lower() == "morelikethis" and host in {
            "deviantart.com",
            "www.deviantart.com",
        }:
            if len(segments) < 3 or not segments[1] or not segments[2]:
                raise ValueError(
                    f"DeviantArt morelikethis URL needs a username and a seed id: {url!r}"
                )
            return SourceRef(
                provider=self.name,
                kind="morelikethis",
                username=segments[1],
                folder_id=None,
                raw_url=url,
                seed=segments[2],
            )

        # deviantart.com/search?q=<query> (also /search/deviations?q=…) — the
        # site-wide search feed, not user-scoped.
        if segments and segments[0].lower() == "search" and host in {
            "deviantart.com",
            "www.deviantart.com",
        }:
            query = parse_qs(parsed.query).get("q", [""])[0].strip()
            if not query:
                raise ValueError(f"DeviantArt search URL has no query: {url!r}")
            return SourceRef(
                provider=self.name,
                kind="search",
                username="",
                folder_id=None,
                raw_url=url,
                query=query,
            )

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
        folder_slug: str | None = None
        if len(segments) >= 2 and segments[1].lower() != "all":
            folder_id = segments[1]
            if len(segments) >= 3:
                folder_slug = unquote(segments[2]).lower()

        return SourceRef(
            provider=self.name,
            kind=kind,
            username=username,
            folder_id=folder_id,
            raw_url=url,
            folder_slug=folder_slug,
        )

    # -- API access ---------------------------------------------------------

    async def list_images(
        self, ref: SourceRef, *, on_progress: ProgressCallback | None = None
    ) -> list[ImageMeta]:
        progress = _Progress(on_progress)
        async with httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": USER_AGENT}
        ) as client:
            if ref.kind == "tag":
                images = await self._collect(
                    self._iter_pages(
                        client, "/browse/tags", {"tag": ref.tag}, progress
                    ),
                    progress,
                )
            elif ref.kind == "search":
                images = await self._collect(
                    self._iter_pages(
                        client, "/browse/home", {"q": ref.query}, progress
                    ),
                    progress,
                )
            elif ref.kind == "morelikethis":
                images = await self._collect(
                    self._iter_morelikethis(client, ref, progress), progress
                )
            else:
                endpoint = "gallery" if ref.kind == "gallery" else "collections"
                folder_ids = await self._target_folder_ids(
                    client, ref, endpoint, progress
                )
                images = await self._collect(
                    self._iter_folders(
                        client, endpoint, folder_ids, ref.username, progress
                    ),
                    progress,
                )
        progress.flush()
        return images

    async def _iter_folders(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        folder_ids: list[str],
        username: str,
        progress: _Progress,
    ):
        for folder_id in folder_ids:
            async for deviation in self._iter_pages(
                client, f"/{endpoint}/{folder_id}", {"username": username}, progress
            ):
                yield deviation

    async def _iter_morelikethis(
        self,
        client: httpx.AsyncClient,
        ref: SourceRef,
        progress: _Progress,
    ):
        # /browse/morelikethis/preview is a single, non-paginated request. The
        # older paginated /browse/morelikethis was removed from the API (like
        # /browse/newest and /browse/popular). Its `seed` must be the UUID
        # deviationid — the legacy numeric id in the URL 400s — so resolve the
        # UUID off the deviation's own web page first.
        seed = await self._resolve_seed_uuid(client, ref, progress)
        payload = await self._get(
            client, "/browse/morelikethis/preview", params={"seed": seed}
        )
        progress.request_done()
        # more_from_da is the "similar across DeviantArt" bucket; more_from_artist
        # is the seed author's other work. _collect de-dupes the overlap.
        for key in ("more_from_da", "more_from_artist"):
            for deviation in payload.get(key) or []:
                yield deviation

    async def _resolve_seed_uuid(
        self,
        client: httpx.AsyncClient,
        ref: SourceRef,
        progress: _Progress,
    ) -> str:
        """Resolve a morelikethis URL's numeric seed to its UUID deviationid.

        There is no API call that maps a legacy numeric id (or a deviation URL)
        to the UUID the API wants, but the deviation's own web page embeds it.
        Any slug works as long as the URL ends in ``-<numeric id>``.
        """
        page_url = f"https://www.deviantart.com/{ref.username}/art/x-{ref.seed}"
        response = await client.get(page_url, follow_redirects=True)
        progress.request_done()
        if response.status_code >= 400:
            raise DeviantArtApiError(
                f"Could not load the seed deviation page {page_url} "
                f"(HTTP {response.status_code}) to resolve its id."
            )
        seed_uuid = _seed_uuid_from_page(response.text, ref.seed)
        if seed_uuid is None:
            raise DeviantArtApiError(
                f"Could not find the deviation id for seed {ref.seed!r} on "
                f"{page_url}."
            )
        return seed_uuid

    async def _collect(self, deviations, progress: _Progress) -> list[ImageMeta]:
        images: list[ImageMeta] = []
        seen: set[str] = set()
        async for deviation in deviations:
            meta = _deviation_to_meta(deviation)
            if meta is None or meta.source_id in seen:
                continue
            # A blurred src means the viewer can't actually see this sensitive
            # deviation — useless as a drawing reference, so keep it out of the
            # list entirely.
            if _is_blurred_src(meta.image_url):
                continue
            seen.add(meta.source_id)
            images.append(meta)
            progress.set_images(len(images))
            if len(images) >= self._max_images:
                return images
        return images

    async def _target_folder_ids(
        self,
        client: httpx.AsyncClient,
        ref: SourceRef,
        endpoint: str,
        progress: _Progress,
    ) -> list[str]:
        if ref.folder_id and _is_api_folder_id(ref.folder_id):
            return [ref.folder_id]
        if ref.folder_id:
            # A legacy numeric id straight from a deviantart.com URL — the API's
            # /{endpoint}/{folderid} only accepts UUIDs (collections 400s, gallery
            # silently ignores it), so match the folder by name instead.
            return [await self._folder_id_by_name(client, endpoint, ref, progress)]
        # The URL targets the whole gallery / all favourites.
        if endpoint == "gallery":
            return ["all"]  # /gallery/all is a real endpoint
        # There is no /collections/all, so aggregate every collection folder.
        return await self._all_folder_ids(client, endpoint, ref.username, progress)

    async def _iter_pages(
        self,
        client: httpx.AsyncClient,
        path: str,
        params: dict,
        progress: _Progress,
    ):
        """Yield every deviation across an offset-paginated browse endpoint."""
        offset = 0
        while True:
            payload = await self._get(
                client,
                path,
                params={**params, "offset": offset, "limit": PAGE_LIMIT},
            )
            progress.request_done()
            for deviation in payload.get("results", []):
                yield deviation
            if not payload.get("has_more"):
                return
            next_offset = payload.get("next_offset")
            if next_offset is None:
                return
            offset = next_offset

    async def _all_folder_ids(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        username: str,
        progress: _Progress,
    ) -> list[str]:
        return [
            f["folderid"]
            for f in await self._folders(client, endpoint, username, progress)
        ]

    async def _folder_id_by_name(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        ref: SourceRef,
        progress: _Progress,
    ) -> str:
        name = ref.folder_slug or ref.folder_id
        wanted = _slugify(name)
        for folder in await self._folders(client, endpoint, ref.username, progress):
            if _slugify(folder.get("name", "")) == wanted:
                return str(folder["folderid"])
        raise DeviantArtApiError(
            f"DeviantArt {endpoint} folder not found: {name!r}"
        )

    async def _folders(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        username: str,
        progress: _Progress,
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
            progress.request_done()
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
        # Ask for mature content only when a real user is logged in. This param
        # is unreliable (DeviantArt under- and over-filters on it), so it is
        # just a hint — the blur check in _collect is what actually keeps
        # unviewable sensitive deviations out.
        mature = "true" if self._token_is_user else "false"
        request_params = {**params, "mature_content": mature}
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
                self._token_is_user = True
                return user_token
        self._token_is_user = False
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
