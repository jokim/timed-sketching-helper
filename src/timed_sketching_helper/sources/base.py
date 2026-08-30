"""The source-provider seam.

A source provider knows how to recognise a URL for one site (DeviantArt today,
Pinterest or others later), turn it into a normalized ``SourceRef``, and list
the images behind it. Adding a site means adding one module that implements
``SourceProvider`` and appending it to ``REGISTRY``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from timed_sketching_helper.models import ImageMeta, SourceRef


class UnknownSourceError(ValueError):
    """No registered provider recognises the given URL."""


@runtime_checkable
class SourceProvider(Protocol):
    name: str

    def matches(self, url: str) -> bool: ...

    def parse(self, url: str) -> SourceRef: ...

    async def list_images(self, ref: SourceRef) -> list[ImageMeta]: ...


def _build_registry() -> list[SourceProvider]:
    from timed_sketching_helper.sources.deviantart import DeviantArtProvider

    return [DeviantArtProvider()]


REGISTRY: list[SourceProvider] = _build_registry()


def resolve(url: str) -> SourceProvider:
    for provider in REGISTRY:
        if provider.matches(url):
            return provider
    raise UnknownSourceError(f"No source provider handles this URL: {url!r}")
