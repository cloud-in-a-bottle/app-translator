"""Serve generated repos from this app's own data directory over plain HTTP.

The zero-credential default: no git forge required. The repos are served under a
public path so the compute space's router can clone them without authentication.
"""

import shutil
import tempfile
from pathlib import Path

import attr

from app_translator.build import GeneratedRepo
from app_translator.hosting.base import PublishedRepo
from app_translator.hosting.git_repo import make_bare_clone
from app_translator.hosting.git_repo import write_worktree


@attr.s(auto_attribs=True, frozen=True)
class SelfServedGitHost:
    repos_dir: Path
    public_base_url: str

    @property
    def kind(self) -> str:
        return "self-served"

    def publish(self, repo: GeneratedRepo, slug: str) -> PublishedRepo:
        bare_path = self.repos_dir / f"{slug}.git"
        if bare_path.exists():
            shutil.rmtree(bare_path)
        with tempfile.TemporaryDirectory() as scratch:
            worktree = Path(scratch) / slug
            write_worktree(repo, worktree)
            make_bare_clone(worktree, bare_path)
        clone_url = f"{self.public_base_url.rstrip('/')}/git/{slug}.git"
        return PublishedRepo(clone_url=clone_url, browse_url=clone_url, host_kind=self.kind)
