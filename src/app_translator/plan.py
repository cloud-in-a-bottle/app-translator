"""Decisions that need the image's metadata, not just the source config.

The important one: how to persist a path. Replacing a directory with a symlink needs
write access to its parent, which an image running as a non-root user does not have.
When the image exposes its own setting for that path (as `grafana/grafana` does with
`GF_PATHS_DATA`), overriding that setting is both safer and simpler.
"""

import re

import attr

from app_translator.emit.startup import DATA_PLACEHOLDER
from app_translator.ir import EnvVar
from app_translator.ir import ServiceSpec
from app_translator.ir import TranslationNote
from app_translator.registry import ImageConfig


def rewrite_path_in_command(command: str, path: str, replacement: str) -> str | None:
    """Replace `path` in a command only where it is a whole path, not part of a longer one.

    `/prometheus` must match in `--storage.tsdb.path=/prometheus` but not in
    `/bin/prometheus` or `/etc/prometheus/prometheus.yml`.
    """
    pattern = re.compile(r"(?:^|(?<=[\s=:,]))" + re.escape(path) + r"(?=[\s=:,/]|$)")
    rewritten, count = pattern.subn(replacement, command)
    return rewritten if count else None


def _image_env(image_config: ImageConfig) -> dict[str, str]:
    env: dict[str, str] = {}
    for entry in image_config.env:
        key, _, value = entry.partition("=")
        env[key] = value
    return env


def _runs_as_root(image_config: ImageConfig) -> bool:
    user = image_config.user.split(":", 1)[0]
    return user in ("", "0", "root")


def plan_persistence(
    service: ServiceSpec, image_config: ImageConfig | None
) -> tuple[ServiceSpec, tuple[TranslationNote, ...]]:
    """Choose, per mount, between an env-var override and a startup symlink."""
    if image_config is None:
        return service, ()
    service = attr.evolve(service, image_user=image_config.user)
    if not service.mounts:
        return service, ()

    image_env = _image_env(image_config)
    runs_as_root = _runs_as_root(image_config)
    notes: list[TranslationNote] = []
    env: list[EnvVar] = list(service.env)
    mounts = list(service.mounts)

    command = service.command
    for index, mount in enumerate(service.mounts):
        matching_keys = sorted(key for key, value in image_env.items() if value == mount.container_path)
        store = f"persisted-{index}"

        # Second choice: the path appears in the command, so point the flag at the data
        # directory. Needed for images that declare a VOLUME on it, where no symlink can win.
        rewritten = (
            rewrite_path_in_command(command, mount.container_path, f"{DATA_PLACEHOLDER}/{store}")
            if not matching_keys and command
            else None
        )
        if rewritten is not None:
            command = rewritten
            mounts[index] = attr.evolve(mount, symlink_at_startup=False)
            notes.append(
                TranslationNote(
                    field="[[mounts]]",
                    severity="info",
                    message=(
                        f"the command points at {mount.container_path}, so that argument now points at the app's "
                        "data directory instead. The path is resolved at startup, so renaming the app keeps working."
                    ),
                )
            )
            continue

        if matching_keys:
            for key in matching_keys:
                env.append(
                    EnvVar(
                        key=key,
                        value=store,
                        data_relative=True,
                        description=f"points {key} at the app's data directory instead of {mount.container_path}",
                    )
                )
            mounts[index] = attr.evolve(mount, symlink_at_startup=False)
            notes.append(
                TranslationNote(
                    field="[[mounts]]",
                    severity="info",
                    message=(
                        f"the image configures {mount.container_path} through {', '.join(matching_keys)}, so that "
                        "setting is pointed at the app's data directory instead of symlinking the path. Safer, and "
                        f"it works even though the image runs as user {image_config.user or 'root'}."
                    ),
                )
            )
            continue

        if mount.container_path in image_config.volumes:
            notes.append(
                TranslationNote(
                    field="[[mounts]]",
                    severity="needs_action",
                    message=(
                        f"the image declares a VOLUME at {mount.container_path}, so the container runtime mounts "
                        "something there and the path cannot be replaced with a symlink (it fails with 'Device or "
                        "resource busy'). Nothing in the image's command or settings references that path either, so "
                        "set the app's own data-directory option by hand."
                    ),
                )
            )
            continue

        if not runs_as_root:
            notes.append(
                TranslationNote(
                    field="[[mounts]]",
                    severity="needs_action",
                    message=(
                        f"{mount.container_path} has to be persisted, but the image runs as user "
                        f"{image_config.user} and exposes no setting for that path. The startup symlink will "
                        "probably fail on a read-only parent directory — check the app's docs for a data-directory "
                        "setting and add it as an environment variable."
                    ),
                )
            )

    # The data mount is idmapped, so inside the container it belongs to root. An image
    # that drops to another uid cannot write there, which defeats the whole point of
    # persisting it. Running as root inside a rootless user namespace is an unprivileged
    # host uid, so this is the pragmatic default — but it is surfaced, not hidden.
    run_as_root = not runs_as_root
    if run_as_root:
        notes.append(
            TranslationNote(
                field="USER",
                severity="assumed",
                message=(
                    f"the image runs as user {image_config.user}, which cannot write to the app's data "
                    "directory (it is mapped to root inside the container). The generated image therefore runs "
                    "as root. Under rootless podman that is still an unprivileged host user, but untick it on "
                    "the form if the app must keep its own uid."
                ),
            )
        )

    return (
        attr.evolve(service, env=tuple(env), mounts=tuple(mounts), command=command, run_as_root=run_as_root),
        tuple(notes),
    )
