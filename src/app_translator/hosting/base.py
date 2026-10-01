from typing import Protocol

import attr

from app_translator.build import GeneratedRepo


@attr.s(auto_attribs=True, frozen=True)
class PublishedRepo:
    clone_url: str
    browse_url: str
    host_kind: str


class RepoHost(Protocol):
    """Somewhere a generated repo can live so the compute space can clone it."""

    @property
    def kind(self) -> str: ...

    def publish(self, repo: GeneratedRepo, slug: str) -> PublishedRepo: ...
