"""Turning a pasted URL into the text of a config file."""

import re
from urllib.parse import urlparse

import httpx

_GITHUB_BLOB = re.compile(r"^https?://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$")
_GITHUB_REPO = re.compile(r"^https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$")

# Where a fly config normally lives in a repo.
_CANDIDATE_PATHS = ("fly.toml", "fly.prod.toml", "deploy/fly.toml")


class FetchError(Exception):
    pass


def _get(client: httpx.Client, url: str) -> str | None:
    try:
        response = client.get(url)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    return response.text


def candidate_urls(source_url: str) -> list[str]:
    """Expand a pasted URL into the raw-content URLs worth trying, in order."""
    url = source_url.strip()
    if blob := _GITHUB_BLOB.match(url):
        owner, repo, ref, path = blob.groups()
        return [f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"]
    if repo_match := _GITHUB_REPO.match(url):
        owner, repo = repo_match.groups()
        return [
            f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{path}" for path in _CANDIDATE_PATHS
        ]
    return [url]


def fetch_config_text(source_url: str, *, timeout: float = 20.0) -> tuple[str, str]:
    """Fetch a config file. Returns (text, resolved_url)."""
    parsed = urlparse(source_url.strip())
    if parsed.scheme not in ("http", "https"):
        raise FetchError("the URL must start with http:// or https://")

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        attempted = candidate_urls(source_url)
        for url in attempted:
            text = _get(client, url)
            if text is not None and text.strip():
                return text, url
    raise FetchError(
        "could not fetch a config from that URL. Tried: " + ", ".join(attempted) + ". "
        "Paste the file contents directly instead."
    )
