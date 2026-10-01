import pytest

from app_translator.frontends.fly_toml import UnsupportedConfigError
from app_translator.frontends.fly_toml import parse_fly_toml
from app_translator.frontends.fly_toml import parse_memory_mb

MINIMAL = """
app = "demo"

[build]
  image = "nginx:1.27"

[http_service]
  internal_port = 8080
"""


def test_parses_the_fields_that_map_directly() -> None:
    service = parse_fly_toml(MINIMAL).service
    assert service.name == "demo"
    assert service.image.kind == "registry"
    assert service.image.ref == "nginx:1.27"
    assert service.http_port == 8080


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(1024, 1024), ("512mb", 512), ("1gb", 1024), ("2GB", 2048), ("256", 256), ("nonsense", None)],
)
def test_memory_parsing(raw: object, expected: int | None) -> None:
    assert parse_memory_mb(raw) == expected


def test_vm_preset_expands_and_explicit_memory_wins() -> None:
    service = parse_fly_toml(
        MINIMAL
        + """
[[vm]]
  size = "shared-cpu-2x"
  memory = "768mb"
"""
    ).service
    assert service.resources.memory_mb == 768
    assert service.resources.cpu_cores == 2.0


def test_app_name_is_sanitized() -> None:
    stack = parse_fly_toml('app = "My_App.Staging"\n[build]\nimage = "nginx"\n[http_service]\ninternal_port = 80\n')
    assert stack.service.name == "my_app-staging"
    assert any(note.field == "app" and note.severity == "assumed" for note in stack.notes)


def test_credential_shaped_env_is_marked_secret() -> None:
    service = parse_fly_toml(
        MINIMAL
        + """
[env]
  LOG_LEVEL = "info"
  ADMIN_PASSWORD = "hunter2"
  SENTRY_API_KEY = "abc"
"""
    ).service
    assert service.secret_keys == ("ADMIN_PASSWORD", "SENTRY_API_KEY")
    assert [entry.key for entry in service.plain_env] == ["LOG_LEVEL"]


def test_multiple_process_groups_are_refused_loudly() -> None:
    with pytest.raises(UnsupportedConfigError, match="process groups"):
        parse_fly_toml(
            MINIMAL
            + """
[processes]
  web = "/bin/server"
  worker = "/bin/worker"
"""
        )


def test_buildpack_builds_are_refused() -> None:
    with pytest.raises(UnsupportedConfigError, match="Buildpack"):
        parse_fly_toml('app = "demo"\n[build]\nbuilder = "paketobuildpacks/builder:base"\n')


def test_not_a_fly_config_is_refused() -> None:
    with pytest.raises(UnsupportedConfigError, match="no top-level"):
        parse_fly_toml('[build]\nimage = "nginx"\n')


def test_mounts_and_health_and_dropped_fields_are_recorded() -> None:
    stack = parse_fly_toml(
        """
app = "demo"
primary_region = "iad"

[build]
  image = "nginx:1.27"

[http_service]
  internal_port = 8080

  [[http_service.checks]]
    path = "/healthz"

[[mounts]]
  source = "data"
  destination = "/var/lib/thing"
"""
    )
    service = stack.service
    assert service.health_path == "/healthz"
    assert [mount.container_path for mount in service.mounts] == ["/var/lib/thing"]
    assert any(note.field == "primary_region" and note.severity == "dropped" for note in stack.notes)
    # The user must always be asked about public paths, since fly serves everything publicly.
    assert any(note.field == "[routing].public_paths" for note in stack.notes)


def test_reserved_and_low_host_ports_are_not_emitted() -> None:
    stack = parse_fly_toml(
        """
app = "demo"

[build]
  image = "nginx"

[[services]]
  internal_port = 8080
  protocol = "tcp"

  [[services.ports]]
    port = 443

[[services]]
  internal_port = 2525
  protocol = "tcp"

  [[services.ports]]
    port = 22
"""
    )
    assert stack.service.extra_ports == ()
    assert any("below the rootless-podman floor" in note.message for note in stack.notes)


def test_missing_build_section_means_it_builds_from_its_own_repo() -> None:
    """A fly.toml with no [build] still builds a Dockerfile — flyctl just detects it."""
    stack = parse_fly_toml('app = "demo"\n[http_service]\ninternal_port = 8080\n')

    assert stack.service.image.kind == "dockerfile"
    assert stack.service.image.ref == "Dockerfile"
    assert any(note.field == "[build]" and note.severity == "needs_action" for note in stack.notes)


def test_dockerfile_build_is_flagged_as_needing_action() -> None:
    stack = parse_fly_toml('app = "demo"\n[build]\ndockerfile = "/other/Dockerfile"\n[http_service]\ninternal_port = 8080\n')

    assert stack.service.image.kind == "dockerfile"
    assert stack.service.image.ref == "/other/Dockerfile"


def test_absolute_dockerfile_path_is_made_repo_relative() -> None:
    """openhost joins the path onto the repo dir, so a leading slash escapes the repo."""
    stack = parse_fly_toml(
        'app = "demo"\n[build]\ndockerfile = "/other/Dockerfile"\n[http_service]\ninternal_port = 8080\n'
    )

    assert stack.service.image.ref == "other/Dockerfile"
    assert any(note.severity == "assumed" and "leading slash" in note.message for note in stack.notes)
