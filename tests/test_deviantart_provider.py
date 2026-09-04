import httpx
import pytest
import respx

from timed_sketching_helper.models import SourceRef
from timed_sketching_helper.sources.deviantart import (
    API_BASE,
    HARD_MAX_REQUESTS,
    TOKEN_URL,
    DeviantArtApiError,
    DeviantArtAuthError,
    DeviantArtProvider,
    DeviantArtRateLimitError,
)


def favourites_ref(folder_id=None):
    return SourceRef(
        provider="deviantart",
        kind="favourites",
        username="artist",
        folder_id=folder_id,
        raw_url="https://www.deviantart.com/artist/favourites/all",
    )


def _token_route(respx_mock, expires_in=3600):
    return respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "tok-123", "expires_in": expires_in}
        )
    )


def _deviation(devid, *, with_content=True):
    d = {
        "deviationid": devid,
        "title": f"Piece {devid}",
        "author": {"username": "artist"},
        "url": f"https://www.deviantart.com/artist/art/{devid}",
    }
    if with_content:
        d["content"] = {
            "src": f"https://images.example/{devid}.jpg",
            "width": 800,
            "height": 600,
        }
    return d


def tag_ref(tag="hamster"):
    return SourceRef(
        provider="deviantart",
        kind="tag",
        username="",
        folder_id=None,
        raw_url=f"https://www.deviantart.com/tag/{tag}",
        tag=tag,
    )


def gallery_ref(folder_id=None):
    return SourceRef(
        provider="deviantart",
        kind="gallery",
        username="artist",
        folder_id=folder_id,
        raw_url="https://www.deviantart.com/artist/gallery/all",
    )


@respx.mock
async def test_list_images_follows_pagination():
    _token_route(respx.mock)
    route = respx.mock.get(url__startswith=f"{API_BASE}/gallery/all")
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "results": [_deviation("a"), _deviation("b")],
                "has_more": True,
                "next_offset": 2,
            },
        ),
        httpx.Response(
            200,
            json={
                "results": [_deviation("c")],
                "has_more": False,
                "next_offset": None,
            },
        ),
    ]

    images = await DeviantArtProvider("id", "secret").list_images(gallery_ref())

    assert [i.source_id for i in images] == ["a", "b", "c"]
    assert images[0].image_url == "https://images.example/a.jpg"
    assert images[0].author == "artist"
    assert route.call_count == 2


@respx.mock
async def test_list_images_reports_progress_per_request():
    _token_route(respx.mock)
    route = respx.mock.get(url__startswith=f"{API_BASE}/gallery/all")
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "results": [_deviation("a"), _deviation("b")],
                "has_more": True,
                "next_offset": 2,
            },
        ),
        httpx.Response(
            200,
            json={"results": [_deviation("c")], "has_more": False, "next_offset": None},
        ),
    ]

    events: list[tuple[int, int]] = []
    await DeviantArtProvider("id", "secret").list_images(
        gallery_ref(),
        on_progress=lambda requests, images: events.append((requests, images)),
    )

    assert events, "expected at least one progress report"
    assert events[-1] == (2, 3)  # two API page requests, three images collected
    assert [r for r, _ in events] == sorted(r for r, _ in events)  # never goes backwards


@respx.mock
async def test_list_images_fetches_tag_browse_with_pagination():
    _token_route(respx.mock)
    route = respx.mock.get(url__startswith=f"{API_BASE}/browse/tags")
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "results": [_deviation("a"), _deviation("b")],
                "has_more": True,
                "next_offset": 2,
            },
        ),
        httpx.Response(
            200,
            json={
                "results": [_deviation("c")],
                "has_more": False,
                "next_offset": None,
            },
        ),
    ]

    images = await DeviantArtProvider("id", "secret").list_images(tag_ref())

    assert [i.source_id for i in images] == ["a", "b", "c"]
    assert route.call_count == 2
    assert route.calls[0].request.url.params["tag"] == "hamster"


def _page_by_offset(prefix="d"):
    def handler(request):
        off = int(request.url.params.get("offset", 0))
        return httpx.Response(
            200,
            json={
                "results": [_deviation(f"{prefix}{off + i}") for i in range(24)],
                "has_more": True,
                "next_offset": off + 24,
            },
        )

    return handler


@respx.mock
async def test_list_images_stops_at_max_images_without_fetching_more_pages():
    _token_route(respx.mock)
    route = respx.mock.get(url__startswith=f"{API_BASE}/gallery/all")
    route.side_effect = _page_by_offset()

    images = await DeviantArtProvider("id", "secret", max_images=50).list_images(
        gallery_ref()
    )

    assert len(images) == 50
    assert route.call_count == 3  # 24 + 24 + 2, then stop; page 4 never requested


async def test_max_images_is_hard_capped_at_1000():
    assert DeviantArtProvider("id", "secret", max_images=99999)._max_images == 1000
    assert DeviantArtProvider("id", "secret")._max_images == 1000


@respx.mock
async def test_list_images_max_images_argument_lowers_the_fetch():
    _token_route(respx.mock)
    route = respx.mock.get(url__startswith=f"{API_BASE}/gallery/all")
    route.side_effect = _page_by_offset()

    images = await DeviantArtProvider("id", "secret").list_images(
        gallery_ref(), max_images=10
    )

    assert len(images) == 10
    assert route.call_count == 1  # first page of 24 already covers the limit


@respx.mock
async def test_list_images_max_images_argument_cannot_exceed_the_ceiling():
    _token_route(respx.mock)
    route = respx.mock.get(url__startswith=f"{API_BASE}/gallery/all")
    route.side_effect = _page_by_offset()

    images = await DeviantArtProvider("id", "secret", max_images=30).list_images(
        gallery_ref(), max_images=500
    )

    assert len(images) == 30


def _blurred_page_by_offset():
    """Every page is full of sensitive deviations the viewer can't see, so
    _collect drops them all — the image count never moves and only the request
    cap can stop the pagination."""

    def handler(request):
        off = int(request.url.params.get("offset", 0))
        return httpx.Response(
            200,
            json={
                "results": [
                    _blurred_deviation(f"b{off + i}") for i in range(24)
                ],
                "has_more": True,
                "next_offset": off + 24,
            },
        )

    return handler


async def test_max_requests_is_hard_capped_at_1000():
    assert (
        DeviantArtProvider("id", "secret", max_requests=99999)._max_requests
        == 1000
    )
    assert DeviantArtProvider("id", "secret")._max_requests == 1000


@respx.mock
async def test_list_images_stops_at_max_requests():
    _token_route(respx.mock)
    route = respx.mock.get(url__startswith=f"{API_BASE}/gallery/all")
    route.side_effect = _blurred_page_by_offset()

    images = await DeviantArtProvider(
        "id", "secret", max_requests=5
    ).list_images(gallery_ref())

    assert images == []
    assert route.call_count == 5  # pagination would never end on its own


@respx.mock
async def test_max_requests_argument_raises_the_configured_default():
    _token_route(respx.mock)
    route = respx.mock.get(url__startswith=f"{API_BASE}/gallery/all")
    route.side_effect = _blurred_page_by_offset()

    # The provider is configured with the default cap of 3; the per-fetch
    # argument lifts it (the opposite of how max_images clamps down).
    await DeviantArtProvider("id", "secret", max_requests=3).list_images(
        gallery_ref(), max_requests=9
    )

    assert route.call_count == 9


@respx.mock
async def test_max_requests_argument_cannot_exceed_the_hard_ceiling(monkeypatch):
    monkeypatch.setattr(
        "timed_sketching_helper.sources.deviantart.HARD_MAX_REQUESTS", 4
    )
    _token_route(respx.mock)
    route = respx.mock.get(url__startswith=f"{API_BASE}/gallery/all")
    route.side_effect = _blurred_page_by_offset()

    await DeviantArtProvider("id", "secret").list_images(
        gallery_ref(), max_requests=999
    )

    assert route.call_count == 4


@respx.mock
async def test_user_api_threshold_raises_rate_limit_error():
    _token_route(respx.mock)
    respx.mock.get(url__startswith=f"{API_BASE}/gallery/all").mock(
        return_value=httpx.Response(
            429,
            json={
                "error": "user_api_threshold",
                "error_description": "User request limit reached.",
            },
        )
    )

    with pytest.raises(DeviantArtRateLimitError):
        await DeviantArtProvider("id", "secret").list_images(gallery_ref())


@respx.mock
async def test_rate_limit_mid_fetch_returns_the_images_collected_so_far():
    _token_route(respx.mock)
    route = respx.mock.get(url__startswith=f"{API_BASE}/gallery/all")
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "results": [_deviation("a"), _deviation("b")],
                "has_more": True,
                "next_offset": 2,
            },
        ),
        httpx.Response(
            429,
            json={
                "error": "user_api_threshold",
                "error_description": "User request limit reached.",
            },
        ),
    ]

    images = await DeviantArtProvider("id", "secret").list_images(gallery_ref())

    assert [i.source_id for i in images] == ["a", "b"]


def search_ref(query="posing"):
    return SourceRef(
        provider="deviantart",
        kind="search",
        username="",
        folder_id=None,
        raw_url=f"https://www.deviantart.com/search?q={query}",
        query=query,
    )


@respx.mock
async def test_list_images_fetches_search_query_via_browse_home():
    _token_route(respx.mock)
    route = respx.mock.get(url__startswith=f"{API_BASE}/browse/home")
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "results": [_deviation("a"), _deviation("b")],
                "has_more": True,
                "next_offset": 2,
            },
        ),
        httpx.Response(
            200,
            json={"results": [_deviation("c")], "has_more": False, "next_offset": None},
        ),
    ]

    images = await DeviantArtProvider("id", "secret").list_images(search_ref("posing"))

    assert [i.source_id for i in images] == ["a", "b", "c"]
    assert route.call_count == 2
    assert route.calls[0].request.url.params["q"] == "posing"


def morelikethis_ref(seed="727534988", username="artist"):
    return SourceRef(
        provider="deviantart",
        kind="morelikethis",
        username=username,
        folder_id=None,
        raw_url=f"https://www.deviantart.com/morelikethis/{username}/{seed}",
        seed=seed,
    )


SEED_UUID = "F91963F8-6C67-3B8D-C837-3299168FEBA7"


def _seed_page_html(seed="727534988", uuid=SEED_UUID):
    # Mirrors the escaped Redux SSR state DeviantArt embeds in a deviation page.
    return (
        '<!doctype html><script>window.__REE__.emit("cacheReady","'
        '{\\"deviationExtended\\":{\\"' + seed + '\\":{\\"deviationUuid\\":\\"'
        + uuid + '\\",\\"isDaPro\\":false}}}");</script>'
    )


@respx.mock
async def test_list_images_morelikethis_resolves_seed_uuid_from_deviation_page():
    # The API rejects the URL's numeric id, so the provider reads the UUID
    # deviationid out of the deviation page's embedded state and seeds the
    # /browse/morelikethis/preview call with that.
    _token_route(respx.mock)
    page = respx.mock.get(
        "https://www.deviantart.com/artist/art/x-727534988"
    ).mock(return_value=httpx.Response(200, html=_seed_page_html()))
    preview = respx.mock.get(
        url__startswith=f"{API_BASE}/browse/morelikethis/preview"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "seed": SEED_UUID,
                "author": {"username": "artist"},
                "more_from_da": [_deviation("da1"), _deviation("da2")],
                "more_from_artist": [_deviation("art1"), _deviation("da1")],
            },
        )
    )

    images = await DeviantArtProvider("id", "secret").list_images(
        morelikethis_ref("727534988")
    )

    # more_from_da first, then more_from_artist, de-duplicated.
    assert [i.source_id for i in images] == ["da1", "da2", "art1"]
    assert page.called
    assert preview.calls[0].request.url.params["seed"] == SEED_UUID


@respx.mock
async def test_list_images_morelikethis_raises_when_seed_page_is_missing():
    _token_route(respx.mock)
    respx.mock.get("https://www.deviantart.com/artist/art/x-727534988").mock(
        return_value=httpx.Response(404, html="<title>DeviantArt: 404</title>")
    )

    with pytest.raises(DeviantArtApiError):
        await DeviantArtProvider("id", "secret").list_images(
            morelikethis_ref("727534988")
        )


@respx.mock
async def test_list_images_morelikethis_raises_when_uuid_not_in_page():
    _token_route(respx.mock)
    respx.mock.get("https://www.deviantart.com/artist/art/x-727534988").mock(
        return_value=httpx.Response(200, html="<!doctype html><body>no state here</body>")
    )

    with pytest.raises(DeviantArtApiError):
        await DeviantArtProvider("id", "secret").list_images(
            morelikethis_ref("727534988")
        )


@respx.mock
async def test_list_images_morelikethis_surfaces_api_errors():
    _token_route(respx.mock)
    respx.mock.get("https://www.deviantart.com/artist/art/x-727534988").mock(
        return_value=httpx.Response(200, html=_seed_page_html())
    )
    respx.mock.get(
        url__startswith=f"{API_BASE}/browse/morelikethis/preview"
    ).mock(return_value=httpx.Response(404, json={"error": "not_found"}))

    with pytest.raises(DeviantArtApiError):
        await DeviantArtProvider("id", "secret").list_images(
            morelikethis_ref("727534988")
        )


@respx.mock
async def test_list_images_skips_entries_without_content():
    _token_route(respx.mock)
    respx.mock.get(url__startswith=f"{API_BASE}/gallery/all").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    _deviation("a"),
                    _deviation("lit", with_content=False),
                    _deviation("b"),
                ],
                "has_more": False,
            },
        )
    )

    images = await DeviantArtProvider("id", "secret").list_images(gallery_ref())

    assert [i.source_id for i in images] == ["a", "b"]


@respx.mock
async def test_list_images_refreshes_token_on_401():
    respx.mock.post(TOKEN_URL).side_effect = [
        httpx.Response(200, json={"access_token": "stale", "expires_in": 3600}),
        httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600}),
    ]
    gallery = respx.mock.get(url__startswith=f"{API_BASE}/gallery/all")
    gallery.side_effect = [
        httpx.Response(401, json={"error": "invalid_token"}),
        httpx.Response(
            200, json={"results": [_deviation("a")], "has_more": False}
        ),
    ]

    images = await DeviantArtProvider("id", "secret").list_images(gallery_ref())

    assert [i.source_id for i in images] == ["a"]
    assert respx.mock.calls[-1].request.headers["Authorization"] == "Bearer fresh"


@respx.mock
async def test_list_images_resolves_favourites_folder_name_to_id():
    _token_route(respx.mock)
    folders = respx.mock.get(url__startswith=f"{API_BASE}/collections/folders").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"folderid": "999", "name": "Cool Refs"},
                    {"folderid": "111", "name": "Other"},
                ]
            },
        )
    )
    contents = respx.mock.get(url__startswith=f"{API_BASE}/collections/999").mock(
        return_value=httpx.Response(
            200, json={"results": [_deviation("a")], "has_more": False}
        )
    )

    ref = SourceRef(
        provider="deviantart",
        kind="favourites",
        username="artist",
        folder_id="cool-refs",
        raw_url="https://www.deviantart.com/artist/favourites/cool-refs",
    )
    images = await DeviantArtProvider("id", "secret").list_images(ref)

    assert folders.called
    assert contents.called
    assert [i.source_id for i in images] == ["a"]


@respx.mock
async def test_list_images_resolves_numeric_url_folder_id_via_name_slug():
    # deviantart.com/<user>/favourites/<legacy numeric id>/<name-slug>: the API
    # only accepts UUID folderids, so the numeric id must be resolved through
    # the folder listing by matching the URL's trailing name slug.
    _token_route(respx.mock)
    folders = respx.mock.get(url__startswith=f"{API_BASE}/collections/folders").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"folderid": "7BE985EE-FBDD-B030-80A8-27AC6C590CBD",
                     "name": "Model Stocks"},
                    {"folderid": "0D62C448-0422-34F1-78DC-89D94ABE6D5F",
                     "name": "Brushes"},
                ]
            },
        )
    )
    contents = respx.mock.get(
        url__startswith=f"{API_BASE}/collections/7BE985EE-FBDD-B030-80A8-27AC6C590CBD"
    ).mock(
        return_value=httpx.Response(
            200, json={"results": [_deviation("a")], "has_more": False}
        )
    )

    ref = SourceRef(
        provider="deviantart",
        kind="favourites",
        username="artist",
        folder_id="61706897",
        raw_url="https://www.deviantart.com/artist/favourites/61706897/model-stocks",
        folder_slug="model-stocks",
    )
    images = await DeviantArtProvider("id", "secret").list_images(ref)

    assert folders.called
    assert contents.called
    assert [i.source_id for i in images] == ["a"]


@respx.mock
async def test_list_images_matches_folder_slug_ignoring_punctuation():
    # deviantart.com/<user>/gallery/<numeric>/<slug>: the trailing slug drops
    # the punctuation the real folder name carries ("Confused, bi-product of a
    # misinformed culture" -> "confused-bi-product-of-a-misinformed-culture"),
    # so the resolver must compare slugified names, not a naive dash->space swap.
    _token_route(respx.mock)
    folders = respx.mock.get(url__startswith=f"{API_BASE}/gallery/folders").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "folderid": "7BE985EE-FBDD-B030-80A8-27AC6C590CBD",
                        "name": "Confused, bi-product of a misinformed culture",
                    },
                    {"folderid": "0D62C448-0422-34F1-78DC-89D94ABE6D5F",
                     "name": "Sketches"},
                ]
            },
        )
    )
    contents = respx.mock.get(
        url__startswith=f"{API_BASE}/gallery/7BE985EE-FBDD-B030-80A8-27AC6C590CBD"
    ).mock(
        return_value=httpx.Response(
            200, json={"results": [_deviation("a")], "has_more": False}
        )
    )

    ref = SourceRef(
        provider="deviantart",
        kind="gallery",
        username="matthieucolnat",
        folder_id="75383758",
        raw_url=(
            "https://www.deviantart.com/matthieucolnat/gallery/75383758/"
            "confused-bi-product-of-a-misinformed-culture"
        ),
        folder_slug="confused-bi-product-of-a-misinformed-culture",
    )
    images = await DeviantArtProvider("id", "secret").list_images(ref)

    assert folders.called
    assert contents.called
    assert [i.source_id for i in images] == ["a"]


async def test_list_images_without_credentials_raises_auth_error():
    with pytest.raises(DeviantArtAuthError):
        await DeviantArtProvider().list_images(gallery_ref())


@respx.mock
async def test_token_request_uses_documented_oauth_endpoint():
    token = respx.mock.post("https://www.deviantart.com/oauth2/token").mock(
        return_value=httpx.Response(
            200, json={"access_token": "tok", "expires_in": 3600}
        )
    )
    respx.mock.get(url__startswith=f"{API_BASE}/gallery/all").mock(
        return_value=httpx.Response(200, json={"results": [], "has_more": False})
    )
    respx.mock.get(url__startswith=f"{API_BASE}/gallery/folders").mock(
        return_value=httpx.Response(200, json={"results": [], "has_more": False})
    )

    await DeviantArtProvider("id", "secret").list_images(gallery_ref())

    assert token.called


@respx.mock
async def test_token_failure_surfaces_deviantart_error_detail():
    respx.mock.post("https://www.deviantart.com/oauth2/token").mock(
        return_value=httpx.Response(
            401,
            json={"error": "invalid_client", "error_description": "bad credentials"},
        )
    )

    with pytest.raises(DeviantArtAuthError, match="invalid_client"):
        await DeviantArtProvider("id", "secret").list_images(gallery_ref())


@respx.mock
async def test_token_redirect_error_is_reported_clearly():
    respx.mock.post("https://www.deviantart.com/oauth2/token").mock(
        return_value=httpx.Response(
            302,
            headers={
                "location": "https://www.deviantart.com/settings/applications/"
                "redirect_error?error=invalid_grant&error_description=Unknown+grant_type"
            },
        )
    )

    with pytest.raises(DeviantArtAuthError, match="Unknown grant_type"):
        await DeviantArtProvider("id", "secret").list_images(gallery_ref())


@respx.mock
async def test_favourites_all_aggregates_every_collection_folder():
    _token_route(respx.mock)
    respx.mock.get(url__startswith=f"{API_BASE}/collections/folders").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"folderid": "1", "name": "Featured"},
                    {"folderid": "2", "name": "Refs"},
                ],
                "has_more": False,
            },
        )
    )
    respx.mock.get(url__startswith=f"{API_BASE}/collections/1").mock(
        return_value=httpx.Response(
            200, json={"results": [_deviation("a")], "has_more": False}
        )
    )
    respx.mock.get(url__startswith=f"{API_BASE}/collections/2").mock(
        return_value=httpx.Response(
            200, json={"results": [_deviation("b")], "has_more": False}
        )
    )

    images = await DeviantArtProvider("id", "secret").list_images(favourites_ref())

    assert {i.source_id for i in images} == {"a", "b"}


@respx.mock
async def test_group_gallery_falls_back_to_folders_when_gallery_all_is_empty():
    # A DeviantArt group's /gallery/all returns nothing; its deviations are
    # only reachable through the group's gallery folders. The bare-gallery URL
    # must fall back to aggregating those folders.
    _token_route(respx.mock)
    empty_all = respx.mock.get(url__startswith=f"{API_BASE}/gallery/all").mock(
        return_value=httpx.Response(200, json={"results": [], "has_more": False})
    )
    folders = respx.mock.get(url__startswith=f"{API_BASE}/gallery/folders").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"folderid": "1", "name": "Featured"},
                    {"folderid": "2", "name": "Sketches"},
                ],
                "has_more": False,
            },
        )
    )
    respx.mock.get(url__startswith=f"{API_BASE}/gallery/1").mock(
        return_value=httpx.Response(
            200, json={"results": [_deviation("a")], "has_more": False}
        )
    )
    respx.mock.get(url__startswith=f"{API_BASE}/gallery/2").mock(
        return_value=httpx.Response(
            200, json={"results": [_deviation("b")], "has_more": False}
        )
    )

    images = await DeviantArtProvider("id", "secret").list_images(gallery_ref())

    assert {i.source_id for i in images} == {"a", "b"}
    assert empty_all.called
    assert folders.called


@respx.mock
async def test_get_prefers_user_token_over_client_credentials():
    async def user_token(*, force=False):
        return "user-tok"

    token = respx.mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "cc", "expires_in": 3600})
    )
    respx.mock.get(url__startswith=f"{API_BASE}/gallery/all").mock(
        return_value=httpx.Response(
            200, json={"results": [_deviation("a")], "has_more": False}
        )
    )

    images = await DeviantArtProvider(
        "id", "secret", user_token=user_token
    ).list_images(gallery_ref())

    assert [i.source_id for i in images] == ["a"]
    assert not token.called
    assert respx.mock.calls[-1].request.headers["Authorization"] == "Bearer user-tok"


@respx.mock
async def test_get_falls_back_to_client_credentials_when_no_user_logged_in():
    async def user_token(*, force=False):
        return None

    _token_route(respx.mock)
    respx.mock.get(url__startswith=f"{API_BASE}/gallery/all").mock(
        return_value=httpx.Response(200, json={"results": [], "has_more": False})
    )
    respx.mock.get(url__startswith=f"{API_BASE}/gallery/folders").mock(
        return_value=httpx.Response(200, json={"results": [], "has_more": False})
    )

    await DeviantArtProvider("id", "secret", user_token=user_token).list_images(
        gallery_ref()
    )

    assert respx.mock.calls[-1].request.headers["Authorization"] == "Bearer tok-123"


@respx.mock
async def test_user_token_is_force_refreshed_on_401():
    seen_force: list[bool] = []
    tokens = iter(["stale-user", "fresh-user"])

    async def user_token(*, force=False):
        seen_force.append(force)
        return next(tokens)

    gallery = respx.mock.get(url__startswith=f"{API_BASE}/gallery/all")
    gallery.side_effect = [
        httpx.Response(401, json={"error": "invalid_token"}),
        httpx.Response(
            200, json={"results": [_deviation("a")], "has_more": False}
        ),
    ]

    images = await DeviantArtProvider(
        "id", "secret", user_token=user_token
    ).list_images(gallery_ref())

    assert [i.source_id for i in images] == ["a"]
    assert seen_force == [False, True]
    assert respx.mock.calls[-1].request.headers["Authorization"] == "Bearer fresh-user"


@respx.mock
async def test_api_error_surfaces_deviantart_detail():
    _token_route(respx.mock)
    respx.mock.get(url__startswith=f"{API_BASE}/gallery/all").mock(
        return_value=httpx.Response(
            400,
            json={"error": "invalid_request", "error_description": "user not found"},
        )
    )

    with pytest.raises(DeviantArtApiError, match="user not found"):
        await DeviantArtProvider("id", "secret").list_images(gallery_ref())


_WIXMP = "https://images-wixmp-ed30a86b8c4ca887773594c2.wixmp.com"


@pytest.mark.parametrize(
    "src, blurred",
    [
        (
            f"{_WIXMP}/f/u/d.jpg/v1/fill/w_545,h_800,q_75,strp,blur_34/"
            "x_by_a_d-fullview.jpg?token=a.b.c",
            True,
        ),
        (
            f"{_WIXMP}/f/u/d.jpg/v1/fill/w_900,h_900,q_80,strp/"
            "x_by_a_d-fullview.jpg?token=a.b.c",
            False,
        ),
        (f"{_WIXMP}/f/u/d.png?token=a.b.c", False),
        # "blur" in the trailing pretty filename must not count as a transform
        (
            f"{_WIXMP}/f/u/d.jpg/v1/fill/w_900,h_900,q_80,strp/"
            "motion_blur_study_by_a_d-fullview.jpg?token=a.b.c",
            False,
        ),
    ],
)
def test_is_blurred_src(src, blurred):
    from timed_sketching_helper.sources.deviantart import _is_blurred_src

    assert _is_blurred_src(src) is blurred


def _blurred_deviation(devid):
    """A deviation whose content.src is a blurred rendition — what DeviantArt
    hands back for a sensitive deviation the current viewer may not see."""
    d = _deviation(devid)
    d["is_mature"] = True
    d["content"]["src"] = (
        f"{_WIXMP}/f/uuid/{devid}.jpg/v1/fill/w_545,h_800,q_75,strp,blur_34/"
        f"pretty_by_artist_{devid}-fullview.jpg?token=abc.def.ghi"
    )
    return d


def _clear_mature_deviation(devid):
    """A mature deviation the viewer *can* see — src carries no blur transform."""
    d = _deviation(devid)
    d["is_mature"] = True
    d["content"]["src"] = (
        f"{_WIXMP}/f/uuid/{devid}.jpg/v1/fill/w_900,h_900,q_80,strp/"
        f"pretty_by_artist_{devid}-fullview.jpg?token=abc.def.ghi"
    )
    return d


@respx.mock
async def test_list_images_drops_deviations_with_a_blurred_source():
    _token_route(respx.mock)
    route = respx.mock.get(url__startswith=f"{API_BASE}/gallery/all").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [_deviation("clear"), _blurred_deviation("hidden")],
                "has_more": False,
            },
        )
    )

    images = await DeviantArtProvider("id", "secret").list_images(gallery_ref())

    assert [i.source_id for i in images] == ["clear"]
    assert route.calls[0].request.url.params["mature_content"] == "false"


@respx.mock
async def test_list_images_keeps_blurred_deviations_out_even_when_logged_in():
    # A logged-in account with mature content disabled still gets blurred
    # renditions back from DeviantArt — they must not reach the session.
    async def user_token(*, force=False):
        return "user-tok"

    respx.mock.get(url__startswith=f"{API_BASE}/gallery/all").mock(
        return_value=httpx.Response(
            200,
            json={"results": [_blurred_deviation("hidden")], "has_more": False},
        )
    )
    # Everything filtered out looks the same as an empty gallery, so the group
    # fallback probes folders too; nothing there either.
    respx.mock.get(url__startswith=f"{API_BASE}/gallery/folders").mock(
        return_value=httpx.Response(200, json={"results": [], "has_more": False})
    )

    images = await DeviantArtProvider(
        "id", "secret", user_token=user_token
    ).list_images(gallery_ref())

    assert images == []


@respx.mock
async def test_list_images_keeps_viewable_mature_content_for_logged_in_user():
    async def user_token(*, force=False):
        return "user-tok"

    route = respx.mock.get(url__startswith=f"{API_BASE}/gallery/all").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [_clear_mature_deviation("visible")],
                "has_more": False,
            },
        )
    )

    images = await DeviantArtProvider(
        "id", "secret", user_token=user_token
    ).list_images(gallery_ref())

    assert [i.source_id for i in images] == ["visible"]
    assert route.calls[0].request.url.params["mature_content"] == "true"


@respx.mock
async def test_list_collections_returns_all_favourites_plus_named_folders():
    _token_route(respx.mock)
    folders = respx.mock.get(url__startswith=f"{API_BASE}/collections/folders").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"folderid": "999", "name": "Cool Refs", "size": 42},
                    {"folderid": "111", "name": "Confused, bi-product"},
                ]
            },
        )
    )
    respx.mock.get(url__startswith=f"{API_BASE}/collections/999").mock(
        return_value=httpx.Response(
            200, json={"results": [_deviation("a")], "has_more": False}
        )
    )
    respx.mock.get(url__startswith=f"{API_BASE}/collections/111").mock(
        return_value=httpx.Response(
            200, json={"results": [_deviation("b")], "has_more": False}
        )
    )

    collections = await DeviantArtProvider("id", "secret").list_collections("artist")

    assert folders.called
    assert collections == [
        {
            "name": "All favourites",
            "url": "https://www.deviantart.com/artist/favourites/all",
            "size": None,
            "thumb_url": None,
        },
        {
            "name": "Cool Refs",
            "url": "https://www.deviantart.com/artist/favourites/0/cool-refs",
            "size": 42,
            "thumb_url": "https://images.example/a.jpg",
        },
        {
            "name": "Confused, bi-product",
            "url": "https://www.deviantart.com/artist/favourites/0/confused-bi-product",
            "size": None,
            "thumb_url": "https://images.example/b.jpg",
        },
    ]


@respx.mock
async def test_list_collections_thumb_is_none_for_empty_folder():
    _token_route(respx.mock)
    respx.mock.get(url__startswith=f"{API_BASE}/collections/folders").mock(
        return_value=httpx.Response(
            200, json={"results": [{"folderid": "999", "name": "Empty"}]}
        )
    )
    respx.mock.get(url__startswith=f"{API_BASE}/collections/999").mock(
        return_value=httpx.Response(200, json={"results": [], "has_more": False})
    )

    collections = await DeviantArtProvider("id", "secret").list_collections("artist")

    assert collections[1]["thumb_url"] is None


@respx.mock
async def test_list_collections_skips_thumbs_once_request_budget_exhausted():
    _token_route(respx.mock)
    folders = respx.mock.get(url__startswith=f"{API_BASE}/collections/folders").mock(
        return_value=httpx.Response(
            200, json={"results": [{"folderid": "999", "name": "Cool Refs"}]}
        )
    )
    detail = respx.mock.get(url__startswith=f"{API_BASE}/collections/999")

    collections = await DeviantArtProvider(
        "id", "secret", max_requests=1
    ).list_collections("artist")

    assert folders.called
    assert not detail.called
    assert collections[1]["thumb_url"] is None
