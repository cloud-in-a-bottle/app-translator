"""Publish generated repos to a Forgejo/Gitea instance."""

import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote

import attr
import httpx

from app_translator.build import GeneratedRepo
from app_translator.hosting.base import PublishedRepo
from app_translator.hosting.git_repo import write_worktree


class ForgejoError(Exception):
    pass


@attr.s(auto_attribs=True, frozen=True)
class ForgejoHost:
    base_url: str
    token: str

    @property
    def kind(self) -> str:
        return "forgejo"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"token {self.token}", "Accept": "application/json"}

    def _whoami(self, client: httpx.Client) -> str:
        response = client.get(f"{self.base_url}/api/v1/user", headers=self._headers())
        if response.status_code != 200:
            raise ForgejoError(f"could not identify the Forgejo user (HTTP {response.status_code}): {response.text[:200]}")
        username = response.json().get("login")
        if not username:
            raise ForgejoError("Forgejo did not return a login for this token")
        return str(username)

    def _ensure_repo(self, client: httpx.Client, slug: str, description: str) -> None:
        response = client.post(
            f"{self.base_url}/api/v1/user/repos",
            headers=self._headers(),
            json={"name": slug, "description": description[:255], "private": False, "auto_init": False},
        )
        if response.status_code in (200, 201):
            return
        # 409 means it already exists, which is fine — we force-push over it.
        if response.status_code == 409:
            return
        raise ForgejoError(f"creating the Forgejo repo failed (HTTP {response.status_code}): {response.text[:200]}")

    def publish(self, repo: GeneratedRepo, slug: str) -> PublishedRepo:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            username = self._whoami(client)
            self._ensure_repo(client, slug, repo.service.description)

        host = self.base_url.split("://", 1)[-1].rstrip("/")
        scheme = self.base_url.split("://", 1)[0]
        push_url = f"{scheme}://{quote(username)}:{quote(self.token)}@{host}/{username}/{slug}.git"
        with tempfile.TemporaryDirectory() as scratch:
            worktree = Path(scratch) / slug
            write_worktree(repo, worktree)
            result = subprocess.run(
                ["git", "push", "--force", push_url, "HEAD:refs/heads/main"],
                cwd=worktree,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                # Never leak the token in an error surfaced to the browser.
                raise ForgejoError(f"git push to Forgejo failed: {result.stderr.replace(self.token, '***')[:300]}")

        browse_url = f"{self.base_url.rstrip('/')}/{username}/{slug}"
        return PublishedRepo(clone_url=f"{browse_url}.git", browse_url=browse_url, host_kind=self.kind)
