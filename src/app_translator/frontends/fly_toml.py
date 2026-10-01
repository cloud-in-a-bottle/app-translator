"""fly.toml -> ImportedStack.

Field names follow the Fly.io configuration reference. Anything we cannot carry
over becomes a TranslationNote rather than a silent drop.
"""

import re
import tomllib
from typing import Any

from app_translator.ir import EnvVar
from app_translator.ir import ImageSource
from app_translator.ir import ImportedStack
from app_translator.ir import MountSpec
from app_translator.ir import PortSpec
from app_translator.ir import ResourceSpec
from app_translator.ir import ServiceSpec
from app_translator.ir import TranslationNote
from app_translator.naming import sanitize_app_name

# Fly's documented default when internal_port is omitted: "The default is 8080. We
# recommend applications use the default." It applies to [http_service] and [[services]].
FLY_DEFAULT_INTERNAL_PORT = 8080

# Caddy owns these on the host, so an app can never bind them itself.
_RESERVED_HOST_PORTS = frozenset({80, 443})
_UNPRIVILEGED_PORT_FLOOR = 25

# Fly VM presets -> (memory_mb, shared vCPUs). Only the common shared sizes;
# anything else becomes a note telling the user to set the numbers by hand.
_VM_PRESETS: dict[str, tuple[int, int]] = {
    "shared-cpu-1x": (256, 1),
    "shared-cpu-2x": (512, 2),
    "shared-cpu-4x": (1024, 4),
    "shared-cpu-8x": (2048, 8),
    "performance-1x": (2048, 1),
    "performance-2x": (4096, 2),
    "performance-4x": (8192, 4),
    "performance-8x": (16384, 8),
}

# A key matching one of these almost certainly holds a credential, even though
# fly.toml's [env] is documented as non-secret. We flag, we do not decide.
_SECRET_HINT = re.compile(r"(password|passwd|secret|token|api_?key|private_?key|credential)", re.IGNORECASE)


class UnsupportedConfigError(Exception):
    """The input is valid but cannot become a working single-container app."""


def parse_memory_mb(raw: object) -> int | None:
    """Parse fly's memory forms: 1024 (MB), "512mb", "1gb"."""
    if isinstance(raw, int):
        return raw
    if not isinstance(raw, str):
        return None
    text = raw.strip().lower().replace(" ", "")
    match = re.fullmatch(r"(\d+)(mb|gb|m|g)?", text)
    if match is None:
        return None
    value = int(match.group(1))
    unit = match.group(2) or "mb"
    return value * 1024 if unit in ("gb", "g") else value


def _as_table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _as_array(data: dict[str, Any], key: str) -> list[Any]:
    value = data.get(key)
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


def _parse_image(data: dict[str, Any], notes: list[TranslationNote]) -> ImageSource:
    build = _as_table(data, "build")
    if build.get("builder") or build.get("buildpacks"):
        raise UnsupportedConfigError(
            "[build].builder / [build].buildpacks describe a Cloud Native Buildpack build, "
            "which produces no Dockerfile. Build the image yourself and re-import with "
            "[build].image, or point the importer at a repo with a Dockerfile."
        )
    if build.get("image"):
        ref = str(build["image"])
        notes.append(
            TranslationNote(
                field="[build].image",
                severity="info",
                message=f"emitting a one-line Dockerfile (FROM {ref}) because openhost builds every app from a Dockerfile.",
            )
        )
        return ImageSource(kind="registry", ref=ref)
    if build.get("dockerfile"):
        raw_path = str(build["dockerfile"])
        path = raw_path.lstrip("/")
        if path != raw_path:
            notes.append(
                TranslationNote(
                    field="[build].dockerfile",
                    severity="assumed",
                    message=(
                        f"rewrote {raw_path!r} as {path!r}: fly reads it relative to the repo, but openhost joins it "
                        "onto the repo path, so a leading slash would point outside the repo."
                    ),
                )
            )
        notes.append(
            TranslationNote(
                field="[build].dockerfile",
                severity="needs_action",
                message=(
                    f"the source config builds {path} from its own repo. The generated repo cannot "
                    "contain that build context, so either import a registry image instead or copy "
                    "the app source into the generated repo by hand."
                ),
            )
        )
        return ImageSource(kind="dockerfile", ref=path)
    if build.get("build-target"):
        notes.append(
            TranslationNote(
                field="[build].build-target",
                severity="dropped",
                message="multi-stage --target is not expressible; the emitted Dockerfile is single-stage.",
            )
        )
    notes.append(
        TranslationNote(
            field="[build]",
            severity="needs_action",
            message=(
                "the config names no image and no Dockerfile, so fly builds whatever Dockerfile it finds in the "
                "repo. Assuming a Dockerfile at the repo root — the app builds from its own source."
            ),
        )
    )
    return ImageSource(kind="dockerfile", ref="Dockerfile")


def _parse_port(data: dict[str, Any], notes: list[TranslationNote]) -> tuple[int | None, tuple[PortSpec, ...]]:
    extra: list[PortSpec] = []
    http_service = _as_table(data, "http_service")
    main_port: int | None = None
    if "internal_port" in http_service:
        main_port = int(http_service["internal_port"])

    for index, service in enumerate(_as_array(data, "services")):
        if not isinstance(service, dict):
            continue
        internal = service.get("internal_port")
        protocol = str(service.get("protocol", "tcp")).lower()
        if main_port is None and internal is not None and protocol == "tcp":
            main_port = int(internal)
            continue
        if internal is None:
            continue
        for port_entry in _as_array(service, "ports"):
            if not isinstance(port_entry, dict):
                continue
            host_port = port_entry.get("port")
            if host_port in _RESERVED_HOST_PORTS:
                notes.append(
                    TranslationNote(
                        field=f"[[services]][{index}].ports",
                        severity="info",
                        message=f"host port {host_port} is served by the built-in front door; routed through it instead.",
                    )
                )
                continue
            if not isinstance(host_port, int):
                continue
            if host_port < _UNPRIVILEGED_PORT_FLOOR:
                notes.append(
                    TranslationNote(
                        field=f"[[services]][{index}].ports",
                        severity="dropped",
                        message=f"host port {host_port} is below the rootless-podman floor of {_UNPRIVILEGED_PORT_FLOOR}.",
                    )
                )
                continue
            extra.append(PortSpec(label=f"svc{index}-{host_port}", container_port=int(internal), host_port=host_port))

    metrics = _as_table(data, "metrics")
    metrics_port = metrics.get("port")
    if isinstance(metrics_port, int) and metrics_port == main_port:
        notes.append(
            TranslationNote(
                field="[metrics]",
                severity="info",
                message=f"metrics are served on the app's main port ({main_port}), so no extra mapping is needed.",
            )
        )
    elif metrics_port:
        extra.append(PortSpec(label="metrics", container_port=int(metrics_port), host_port=0))
        notes.append(
            TranslationNote(
                field="[metrics]",
                severity="info",
                message="exposed as a [[ports]] mapping; nothing on the host scrapes it automatically.",
            )
        )
    return main_port, tuple(extra)


def _http_check_ports(data: dict[str, Any]) -> list[int]:
    """Ports that HTTP checks target, in declaration order."""
    ports: list[int] = []
    for check in _as_table(data, "checks").values():
        if not isinstance(check, dict):
            continue
        if str(check.get("type", "")).lower() != "http":
            continue
        port = check.get("port")
        if isinstance(port, int) and port not in ports:
            ports.append(port)
    return ports


def _declares_no_service(data: dict[str, Any]) -> bool:
    """True when the config exposes nothing — no [http_service] and no [[services]]."""
    if _as_table(data, "http_service"):
        return False
    services = data.get("services")
    if isinstance(services, list) and any(isinstance(entry, dict) and entry for entry in services):
        return False
    return True


def _parse_health_path(data: dict[str, Any], main_port: int | None, notes: list[TranslationNote]) -> str | None:
    for check in _as_array(_as_table(data, "http_service"), "checks"):
        if isinstance(check, dict) and check.get("path"):
            return str(check["path"])
    for name, check in _as_table(data, "checks").items():
        if not isinstance(check, dict):
            continue
        if str(check.get("type", "")).lower() != "http":
            notes.append(
                TranslationNote(
                    field=f"[checks.{name}]",
                    severity="dropped",
                    message="only HTTP checks have an equivalent; openhost stores a bare path.",
                )
            )
            continue
        port = check.get("port")
        if port is not None and main_port is not None and int(port) != main_port:
            notes.append(
                TranslationNote(
                    field=f"[checks.{name}]",
                    severity="dropped",
                    message=f"check targets port {port}, not the app's main port {main_port}.",
                )
            )
            continue
        if check.get("path"):
            return str(check["path"])
    return None


def _parse_resources(data: dict[str, Any], notes: list[TranslationNote]) -> tuple[ResourceSpec, bool]:
    vms = _as_array(data, "vm")
    vm = vms[0] if vms and isinstance(vms[0], dict) else {}
    memory_mb: int | None = None
    cpu_cores: float | None = None

    preset = vm.get("size")
    if isinstance(preset, str):
        if preset in _VM_PRESETS:
            memory_mb, vcpus = _VM_PRESETS[preset]
            cpu_cores = float(vcpus)
            notes.append(
                TranslationNote(
                    field="[[vm]].size",
                    severity="assumed",
                    message=f"expanded preset {preset} to {memory_mb} MB / {cpu_cores} cores — confirm or override.",
                )
            )
        else:
            notes.append(
                TranslationNote(
                    field="[[vm]].size",
                    severity="needs_action",
                    message=f"unknown VM preset {preset!r}; set memory and cpu by hand.",
                )
            )

    explicit_memory = parse_memory_mb(vm.get("memory_mb", vm.get("memory")))
    if explicit_memory is not None:
        memory_mb = explicit_memory
    if vm.get("cpus") is not None:
        cpu_cores = float(vm["cpus"])
        notes.append(
            TranslationNote(
                field="[[vm]].cpus",
                severity="assumed",
                message=(
                    f"mapped {vm['cpus']} fly vCPU(s) to {cpu_cores} cpu_cores. A fly shared vCPU is a "
                    "fraction of a core, so this is probably generous — lower it if the host is busy."
                ),
            )
        )

    gpu = bool(vm.get("gpu_kind") or vm.get("gpus"))
    if gpu:
        notes.append(
            TranslationNote(
                field="[[vm]].gpu_kind",
                severity="needs_action",
                message="openhost parses [resources].gpu but never passes it to podman — the app gets no GPU.",
            )
        )

    resources = ResourceSpec(
        memory_mb=memory_mb if memory_mb is not None else 128,
        cpu_cores=cpu_cores if cpu_cores is not None else 0.1,
    )
    return resources, gpu


def _parse_command(data: dict[str, Any], notes: list[TranslationNote]) -> tuple[str | None, str | None]:
    processes = _as_table(data, "processes")
    if len(processes) > 1:
        names = ", ".join(sorted(processes))
        raise UnsupportedConfigError(
            f"[processes] declares {len(processes)} process groups ({names}). openhost runs one container "
            "per app, so importing this would silently drop every group but one. Split it into one app per "
            "process group, or build an image that supervises them together."
        )
    command: str | None = None
    if processes:
        command = str(next(iter(processes.values())))

    experimental = _as_table(data, "experimental")
    raw_cmd = experimental.get("cmd")
    if isinstance(raw_cmd, list) and raw_cmd:
        command = " ".join(str(part) for part in raw_cmd)
    elif isinstance(raw_cmd, str):
        command = raw_cmd

    entrypoint: str | None = None
    raw_entrypoint = experimental.get("entrypoint")
    if isinstance(raw_entrypoint, list) and raw_entrypoint:
        entrypoint = " ".join(str(part) for part in raw_entrypoint)
    elif isinstance(raw_entrypoint, str):
        entrypoint = raw_entrypoint

    if command and ("&&" in command or "|" in command or ";" in command):
        notes.append(
            TranslationNote(
                field="command",
                severity="needs_action",
                message=(
                    "the command needs a shell, but openhost splits it on whitespace with no shell. "
                    "It has been moved into the generated startup script instead."
                ),
            )
        )
    return command, entrypoint


def _parse_env(data: dict[str, Any], notes: list[TranslationNote]) -> tuple[EnvVar, ...]:
    env: list[EnvVar] = []
    for key, value in _as_table(data, "env").items():
        looks_secret = bool(_SECRET_HINT.search(key))
        env.append(
            EnvVar(
                key=str(key),
                value=str(value),
                is_secret=looks_secret,
                description="flagged as a likely credential by name" if looks_secret else "",
            )
        )
        if looks_secret:
            notes.append(
                TranslationNote(
                    field=f"[env].{key}",
                    severity="needs_action",
                    message=(
                        "the name looks like a credential. It has been marked secret, so the value will be "
                        "fetched from the secrets service at startup instead of baked into the image."
                    ),
                )
            )
    notes.append(
        TranslationNote(
            field="fly secrets",
            severity="needs_action",
            message=(
                "fly keeps secrets outside fly.toml, so this file cannot tell us which ones exist. "
                "Add any the app needs on the review form."
            ),
        )
    )
    return tuple(env)


def _parse_mounts(data: dict[str, Any], notes: list[TranslationNote]) -> tuple[MountSpec, ...]:
    mounts: list[MountSpec] = []
    for mount in _as_array(data, "mounts"):
        if not isinstance(mount, dict):
            continue
        destination = mount.get("destination")
        if not destination:
            continue
        mounts.append(MountSpec(container_path=str(destination), tier="persistent"))
        notes.append(
            TranslationNote(
                field="[[mounts]]",
                severity="info",
                message=f"{destination} will be persisted in the app's permanent data directory.",
            )
        )
    return tuple(mounts)


def _note_dropped(data: dict[str, Any], notes: list[TranslationNote]) -> None:
    """Record the fields we knowingly discard, so the user sees them."""
    simple_drops = {
        "primary_region": "one host, one region.",
        "swap_size_mb": "no per-container swap control.",
        "kill_signal": "podman's default SIGTERM applies.",
        "kill_timeout": "podman's default 10s stop timeout applies.",
        "console_command": "no remote-console equivalent.",
    }
    for key, reason in simple_drops.items():
        if key in data:
            notes.append(TranslationNote(field=key, severity="dropped", message=reason))

    http_service = _as_table(data, "http_service")
    for key in ("auto_stop_machines", "auto_start_machines", "min_machines_running"):
        if key in http_service:
            notes.append(
                TranslationNote(
                    field=f"[http_service].{key}",
                    severity="dropped",
                    message="openhost runs exactly one container that is always on.",
                )
            )
    if "concurrency" in http_service:
        notes.append(
            TranslationNote(
                field="[http_service.concurrency]",
                severity="dropped",
                message="no load balancer in front of the app, so concurrency limits have nowhere to apply.",
            )
        )
    if _as_array(data, "restart"):
        notes.append(
            TranslationNote(
                field="[[restart]]",
                severity="dropped",
                message="the restart policy is fixed at unless-stopped.",
            )
        )
    if _as_array(data, "statics"):
        notes.append(
            TranslationNote(
                field="[[statics]]",
                severity="needs_action",
                message="fly serves these from its edge, bypassing the app. The app must serve them itself here.",
            )
        )
    if _as_array(data, "files"):
        notes.append(
            TranslationNote(
                field="[[files]]",
                severity="needs_action",
                message="file provisioning is not translated yet; add the files to the generated repo by hand.",
            )
        )
    deploy = _as_table(data, "deploy")
    if deploy.get("release_command"):
        notes.append(
            TranslationNote(
                field="[deploy].release_command",
                severity="needs_action",
                message=(
                    "moved into the startup script, so it runs on every start rather than once per release. "
                    "It must be idempotent."
                ),
            )
        )


def parse_fly_toml(raw_text: str) -> ImportedStack:
    """Parse a fly.toml into the IR. Raises UnsupportedConfigError for configs that cannot work."""
    try:
        data = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as exc:
        raise UnsupportedConfigError(f"not valid TOML: {exc}") from exc

    if not isinstance(data.get("app"), str) or not data["app"].strip():
        raise UnsupportedConfigError("fly.toml has no top-level `app` name, so this is not a fly config.")

    notes: list[TranslationNote] = []
    name = sanitize_app_name(data["app"])
    if name != data["app"]:
        notes.append(
            TranslationNote(
                field="app",
                severity="assumed",
                message=f"renamed {data['app']!r} to {name!r} to satisfy openhost's app-name rules.",
            )
        )

    image = _parse_image(data, notes)
    main_port, extra_ports = _parse_port(data, notes)
    if main_port is None and _declares_no_service(data):
        notes.append(
            TranslationNote(
                field="[[services]]",
                severity="needs_action",
                message=(
                    "this config exposes no HTTP service at all, so there is no port to route. OpenHost serves "
                    "every app over HTTP and marks an app that does not answer on its main port as failed, so "
                    "a non-HTTP service (a database, a queue) cannot be hosted as an app here."
                ),
            )
        )

    if main_port is None:
        for candidate in _http_check_ports(data):
            main_port = candidate
            notes.append(
                TranslationNote(
                    field="[checks]",
                    severity="assumed",
                    message=(
                        f"no service declares a port, so took {candidate} from an HTTP check. Verify it — a check "
                        "port is often an internal health endpoint rather than the app's own port."
                    ),
                )
            )
            break

    health_path = _parse_health_path(data, main_port, notes)
    resources, gpu = _parse_resources(data, notes)
    command, entrypoint = _parse_command(data, notes)
    env = _parse_env(data, notes)
    mounts = _parse_mounts(data, notes)
    _note_dropped(data, notes)

    notes.append(
        TranslationNote(
            field="[routing].public_paths",
            severity="needs_action",
            message=(
                "fly serves this app publicly; every openhost app sits behind owner login. "
                "List any paths that must stay public (webhooks, OAuth callbacks) on the review form."
            ),
        )
    )

    service = ServiceSpec(
        name=name,
        image=image,
        http_port=main_port,
        command=command,
        entrypoint=entrypoint,
        env=env,
        extra_ports=extra_ports,
        mounts=mounts,
        resources=resources,
        health_path=health_path,
        gpu=gpu,
        description=f"imported from fly.toml ({data['app']})",
    )
    return ImportedStack(source_format="fly.toml", services=(service,), notes=tuple(notes))
