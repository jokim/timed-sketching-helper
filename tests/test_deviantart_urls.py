import pytest

from timed_sketching_helper.sources.base import UnknownSourceError, resolve
from timed_sketching_helper.sources.deviantart import DeviantArtProvider


@pytest.fixture
def provider() -> DeviantArtProvider:
    return DeviantArtProvider()


@pytest.mark.parametrize(
    ("url", "kind", "username", "folder_id"),
    [
        (
            "https://www.deviantart.com/someuser/gallery/12345/folder-name",
            "gallery",
            "someuser",
            "12345",
        ),
        ("https://www.deviantart.com/someuser/gallery/all", "gallery", "someuser", None),
        ("https://www.deviantart.com/someuser/gallery", "gallery", "someuser", None),
        ("https://www.deviantart.com/someuser/gallery/", "gallery", "someuser", None),
        (
            "https://www.deviantart.com/someuser/favourites/67890/fav-folder",
            "favourites",
            "someuser",
            "67890",
        ),
        (
            "https://www.deviantart.com/someuser/favourites/all",
            "favourites",
            "someuser",
            None,
        ),
        (
            "https://www.deviantart.com/someuser/favourites",
            "favourites",
            "someuser",
            None,
        ),
        ("https://www.deviantart.com/someuser", "gallery", "someuser", None),
        ("http://deviantart.com/someuser", "gallery", "someuser", None),
        (
            "https://someuser.deviantart.com/gallery/12345/folder-name",
            "gallery",
            "someuser",
            "12345",
        ),
    ],
)
def test_parses_supported_url_shapes(provider, url, kind, username, folder_id):
    ref = provider.parse(url)
    assert ref.provider == "deviantart"
    assert ref.kind == kind
    assert ref.username == username
    assert ref.folder_id == folder_id
    assert ref.raw_url == url


def test_parses_tag_url(provider):
    ref = provider.parse("https://www.deviantart.com/tag/hamster")
    assert ref.provider == "deviantart"
    assert ref.kind == "tag"
    assert ref.tag == "hamster"
    assert ref.username == ""
    assert ref.folder_id is None
    assert ref.raw_url == "https://www.deviantart.com/tag/hamster"


def test_parses_tag_url_normalizes_case_and_leading_hash(provider):
    assert provider.parse("https://deviantart.com/tag/HamsterArt").tag == "hamsterart"
    assert provider.parse("https://www.deviantart.com/tag/%23hamster").tag == "hamster"


def test_parse_rejects_tag_url_without_a_tag(provider):
    with pytest.raises(ValueError):
        provider.parse("https://www.deviantart.com/tag/")


def test_matches_only_deviantart_hosts(provider):
    assert provider.matches("https://www.deviantart.com/someuser/gallery/all")
    assert provider.matches("https://someuser.deviantart.com/gallery")
    assert not provider.matches("https://www.pinterest.com/someuser/board")
    assert not provider.matches("not a url at all")


def test_parse_rejects_non_deviantart_url(provider):
    with pytest.raises(ValueError):
        provider.parse("https://www.pinterest.com/someuser/board")


def test_parse_rejects_deviantart_url_without_username(provider):
    with pytest.raises(ValueError):
        provider.parse("https://www.deviantart.com/")


def test_registry_resolves_deviantart_url_to_provider():
    provider = resolve("https://www.deviantart.com/someuser/gallery/all")
    assert isinstance(provider, DeviantArtProvider)


def test_registry_raises_for_unknown_source():
    with pytest.raises(UnknownSourceError):
        resolve("https://example.com/whatever")
