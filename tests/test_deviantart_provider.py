import httpx
import pytest
import respx

from timed_sketching_helper.models import SourceRef
from timed_sketching_helper.sources.deviantart import (
    API_BASE,
    TOKEN_URL,
    DeviantArtAuthError,
    DeviantArtProvider,
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


async def test_list_images_without_credentials_raises_auth_error():
    with pytest.raises(DeviantArtAuthError):
        await DeviantArtProvider().list_images(gallery_ref())
