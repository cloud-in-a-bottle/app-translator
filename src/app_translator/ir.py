"""The intermediate representation every front-end produces and the emitter consumes."""

from typing import Literal

import attr

MountTier = Literal["persistent", "temp", "archive"]
Severity = Literal["info", "assumed", "needs_action", "dropped"]
ImageKind = Literal["registry", "dockerfile"]


@attr.s(auto_attribs=True, frozen=True)
class TranslationNote:
    """One thing the importer assumed, changed, or could not carry over."""

    field: str
    severity: Severity
    message: str


@attr.s(auto_attribs=True, frozen=True)
class EnvVar:
    key: str
    value: str | None
    is_secret: bool = False
    description: str = ""
    # When set, `value` is a path relative to the app's data directory and is exported
    # by the startup script at runtime rather than baked into the image. That keeps it
    # correct if the app is later renamed.
    data_relative: bool = False


@attr.s(auto_attribs=True, frozen=True)
class PortSpec:
    label: str
    container_port: int
    host_port: int = 0


@attr.s(auto_attribs=True, frozen=True)
class MountSpec:
    """A path inside the container the source config wanted to persist."""

    container_path: str
    tier: MountTier = "persistent"
    symlink_at_startup: bool = True


@attr.s(auto_attribs=True, frozen=True)
class ImageSource:
    kind: ImageKind
    ref: str


@attr.s(auto_attribs=True, frozen=True)
class ResourceSpec:
    memory_mb: int = 128
    cpu_cores: float = 0.1


@attr.s(auto_attribs=True, frozen=True)
class ServiceSpec:
    name: str
    image: ImageSource
    http_port: int | None = None
    command: str | None = None
    entrypoint: str | None = None
    env: tuple[EnvVar, ...] = ()
    extra_ports: tuple[PortSpec, ...] = ()
    mounts: tuple[MountSpec, ...] = ()
    resources: ResourceSpec = ResourceSpec()
    health_path: str | None = None
    public_paths: tuple[str, ...] = ()
    gpu: bool = False
    description: str = ""
    # The user the base image runs as (from its metadata), and whether we override it.
    # A non-root image user cannot write to the idmapped data mount, so persisting data
    # usually means running as root — which inside a rootless user namespace is an
    # unprivileged host uid, not host root.
    image_user: str = ""
    run_as_root: bool = False

    @property
    def secret_keys(self) -> tuple[str, ...]:
        return tuple(e.key for e in self.env if e.is_secret)

    @property
    def plain_env(self) -> tuple[EnvVar, ...]:
        """Env that can be baked into the image: not secret, not data-directory-relative."""
        return tuple(e for e in self.env if not e.is_secret and not e.data_relative)

    @property
    def data_relative_env(self) -> tuple[EnvVar, ...]:
        return tuple(e for e in self.env if e.data_relative and not e.is_secret)


@attr.s(auto_attribs=True, frozen=True)
class ServiceEdge:
    """A dependency between two services in the source config.

    Unused while we only import single-service configs, but recorded rather than
    discarded so a multi-service front-end can use it later.
    """

    from_service: str
    to_service: str
    reason: str


@attr.s(auto_attribs=True, frozen=True)
class ImportedStack:
    source_format: str
    services: tuple[ServiceSpec, ...]
    notes: tuple[TranslationNote, ...] = ()
    edges: tuple[ServiceEdge, ...] = ()
    # Human-readable provenance for the main port, shown on the review form so a
    # guessed port is never mistaken for a declared one.
    port_source: str = ""

    @property
    def service(self) -> ServiceSpec:
        """The single service, for the single-service import path."""
        if len(self.services) != 1:
            raise ValueError(f"expected exactly one service, got {len(self.services)}")
        return self.services[0]
