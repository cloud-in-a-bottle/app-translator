"""Best-effort inspection of a registry image's config.

We need the image's own ENTRYPOINT/CMD because the generated startup shim replaces
ENTRYPOINT, and we must put the original argv back as CMD. Any failure here is
non-fatal: the review form asks the user for the command instead.
"""

import json
from typing import Any

import attr
import httpx

from app_translator.net import build_client

_DOCKER_HUB = "registry-1.docker.io"
_MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    )
)


@attr.s(auto_attribs=True, frozen=True)
class ImageConfig:
    entrypoint: tuple[str, ...] = ()
    command: tuple[str, ...] = ()
    exposed_ports: tuple[int, ...] = ()
    user: str = ""
    env: tuple[str, ...] = ()
    volumes: tuple[str, ...] = ()

    @property
    def argv(self) -> tuple[str, ...]:
        return self.entrypoint + self.command


@attr.s(auto_attribs=True, frozen=True)
class ParsedRef:
    registry: str
    repository: str
    reference: str


def parse_image_ref(ref: str) -> ParsedRef:
    """Split `[registry/]repo[:tag|@digest]` the way a container runtime would."""
    remainder = ref
    registry = _DOCKER_HUB
    head, slash, tail = remainder.partition("/")
    if slash and ("." in head or ":" in head or head == "localhost"):
        registry = head
        remainder = tail
    if "@" in remainder:
        repository, _, reference = remainder.partition("@")
    elif ":" in remainder.rsplit("/", 1)[-1]:
        repository, _, reference = remainder.rpartition(":")
    else:
        repository, reference = remainder, "latest"
    if registry == _DOCKER_HUB and "/" not in repository:
        repository = f"library/{repository}"
    return ParsedRef(registry=registry, repository=repository, reference=reference)


def _anonymous_token(client: httpx.Client, parsed: ParsedRef) -> str | None:
    """Fetch a pull token for registries that use the standard token dance."""
    probe = client.get(f"https://{parsed.registry}/v2/")
    if probe.status_code != 401:
        return None
    challenge = probe.headers.get("www-authenticate", "")
    if not challenge.lower().startswith("bearer "):
        return None
    params: dict[str, str] = {}
    for part in challenge[len("bearer ") :].split(","):
        key, _, value = part.strip().partition("=")
        params[key.strip()] = value.strip().strip('"')
    realm = params.get("realm")
    if not realm:
        return None
    response = client.get(
        realm,
        params={"service": params.get("service", parsed.registry), "scope": f"repository:{parsed.repository}:pull"},
    )
    response.raise_for_status()
    token = response.json().get("token") or response.json().get("access_token")
    return str(token) if token else None


def _select_manifest(body: dict[str, Any]) -> str | None:
    """From a manifest list, pick a linux/amd64 digest."""
    for entry in body.get("manifests", []):
        platform = entry.get("platform", {})
        if platform.get("os") == "linux" and platform.get("architecture") == "amd64":
            return str(entry["digest"])
    manifests = body.get("manifests", [])
    return str(manifests[0]["digest"]) if manifests else None


def inspect_image(ref: str, *, timeout: float = 20.0) -> ImageConfig | None:
    """Return the image's config, or None if it cannot be read."""
    parsed = parse_image_ref(ref)
    try:
        with build_client(read_timeout=timeout) as client:
            headers = {"Accept": _MANIFEST_ACCEPT}
            token = _anonymous_token(client, parsed)
            if token:
                headers["Authorization"] = f"Bearer {token}"

            base = f"https://{parsed.registry}/v2/{parsed.repository}"
            manifest_response = client.get(f"{base}/manifests/{parsed.reference}", headers=headers)
            manifest_response.raise_for_status()
            manifest = manifest_response.json()

            if "manifests" in manifest:
                digest = _select_manifest(manifest)
                if digest is None:
                    return None
                manifest_response = client.get(f"{base}/manifests/{digest}", headers=headers)
                manifest_response.raise_for_status()
                manifest = manifest_response.json()

            config_digest = manifest.get("config", {}).get("digest")
            if not config_digest:
                return None
            blob_response = client.get(f"{base}/blobs/{config_digest}", headers=headers)
            blob_response.raise_for_status()
            config = blob_response.json().get("config", {})
    except (httpx.HTTPError, json.JSONDecodeError, KeyError, ValueError):
        return None

    exposed: list[int] = []
    for raw_port in (config.get("ExposedPorts") or {}):
        port_text = str(raw_port).split("/", 1)[0]
        if port_text.isdigit():
            exposed.append(int(port_text))

    return ImageConfig(
        entrypoint=tuple(config.get("Entrypoint") or ()),
        command=tuple(config.get("Cmd") or ()),
        exposed_ports=tuple(sorted(exposed)),
        user=str(config.get("User") or ""),
        env=tuple(config.get("Env") or ()),
        volumes=tuple(sorted(config.get("Volumes") or ())),
    )
