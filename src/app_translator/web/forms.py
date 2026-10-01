"""Rebuilding a ServiceSpec from the review form the user submits."""

from typing import Mapping
from typing import Sequence

from app_translator.ir import EnvVar
from app_translator.ir import ImageSource
from app_translator.ir import MountSpec
from app_translator.ir import PortSpec
from app_translator.ir import ResourceSpec
from app_translator.ir import ServiceSpec
from app_translator.naming import is_valid_app_name


class FormError(Exception):
    pass


def _single(form: Mapping[str, object], key: str, default: str = "") -> str:
    value = form.get(key, default)
    if isinstance(value, list):
        return str(value[0]) if value else default
    return str(value) if value is not None else default


def _int(form: Mapping[str, object], key: str, *, field_label: str) -> int:
    raw = _single(form, key).strip()
    if not raw:
        raise FormError(f"{field_label} is required")
    try:
        return int(raw)
    except ValueError:
        raise FormError(f"{field_label} must be a whole number (got {raw!r})") from None


def _float(form: Mapping[str, object], key: str, *, field_label: str) -> float:
    raw = _single(form, key).strip()
    if not raw:
        raise FormError(f"{field_label} is required")
    try:
        return float(raw)
    except ValueError:
        raise FormError(f"{field_label} must be a number (got {raw!r})") from None


def _csv(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def service_from_form(
    form: Mapping[str, object],
    *,
    env_keys: Sequence[str],
    env_values: Sequence[str],
    secret_keys: Sequence[str],
    data_relative_keys: Sequence[str],
    mount_paths: Sequence[str],
    symlinked_paths: Sequence[str],
    extra_ports: tuple[PortSpec, ...],
) -> ServiceSpec:
    name = _single(form, "name").strip()
    if not is_valid_app_name(name):
        raise FormError(
            f"{name!r} is not a usable app name: lowercase letters, digits, hyphen and underscore only, "
            "starting with a letter, 32 characters max."
        )

    image_ref = _single(form, "image_ref").strip()
    if not image_ref:
        raise FormError("the container image is required")

    port = _int(form, "http_port", field_label="the HTTP port")
    if not 1 <= port <= 65535:
        raise FormError(f"the HTTP port must be between 1 and 65535 (got {port})")

    secrets = set(secret_keys)
    data_relative = set(data_relative_keys)
    env: list[EnvVar] = []
    seen: set[str] = set()
    for key, value in zip(env_keys, env_values):
        clean_key = key.strip()
        if not clean_key:
            continue
        if clean_key in seen:
            raise FormError(f"environment variable {clean_key!r} is listed twice")
        seen.add(clean_key)
        is_secret = clean_key in secrets
        env.append(
            EnvVar(
                key=clean_key,
                value=None if is_secret else value,
                is_secret=is_secret,
                data_relative=clean_key in data_relative and not is_secret,
            )
        )

    symlinked = set(symlinked_paths)
    mounts = tuple(
        MountSpec(container_path=path.strip(), tier="persistent", symlink_at_startup=path.strip() in symlinked)
        for path in mount_paths
        if path.strip()
    )

    health_path = _single(form, "health_path").strip() or None
    if health_path and not health_path.startswith("/"):
        raise FormError(f"the health check path must start with '/' (got {health_path!r})")

    public_paths = _csv(_single(form, "public_paths"))
    for path in public_paths:
        if not path.startswith("/"):
            raise FormError(f"public path {path!r} must start with '/'")

    return ServiceSpec(
        name=name,
        image=ImageSource(kind="registry", ref=image_ref),
        http_port=port,
        command=_single(form, "command").strip() or None,
        env=tuple(env),
        extra_ports=extra_ports,
        mounts=mounts,
        resources=ResourceSpec(
            memory_mb=_int(form, "memory_mb", field_label="memory"),
            cpu_cores=_float(form, "cpu_cores", field_label="CPU cores"),
        ),
        health_path=health_path,
        public_paths=public_paths,
        gpu=_single(form, "gpu") == "on",
        description=_single(form, "description").strip(),
        image_user=_single(form, "image_user").strip(),
        run_as_root=_single(form, "run_as_root") == "on",
    )
