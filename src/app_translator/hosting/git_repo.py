"""Writing a GeneratedRepo out as a real git repository on disk."""

import os
import subprocess
from pathlib import Path

from app_translator.build import GeneratedRepo

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "app-translator",
    "GIT_AUTHOR_EMAIL": "app-translator@localhost",
    "GIT_COMMITTER_NAME": "app-translator",
    "GIT_COMMITTER_EMAIL": "app-translator@localhost",
}


def _git(args: list[str], cwd: Path) -> None:
    env = {**os.environ, **_GIT_ENV}
    result = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")


def write_worktree(repo: GeneratedRepo, destination: Path) -> None:
    """Write the generated files into a directory and commit them on `main`."""
    destination.mkdir(parents=True, exist_ok=True)
    for generated in repo.files:
        target = destination / generated.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(generated.content)
        if generated.executable:
            target.chmod(0o755)
    _git(["init", "-q", "-b", "main"], destination)
    _git(["add", "-A"], destination)
    _git(["commit", "-q", "-m", f"Generated {repo.service.name} from an imported config"], destination)


def make_bare_clone(worktree: Path, bare_path: Path) -> None:
    """Create a bare repo that can be served over plain HTTP."""
    bare_path.parent.mkdir(parents=True, exist_ok=True)
    _git(["clone", "--bare", "-q", str(worktree), str(bare_path)], bare_path.parent)
    # Dumb HTTP clients need these generated indexes to find refs and packs.
    _git(["update-server-info"], bare_path)
