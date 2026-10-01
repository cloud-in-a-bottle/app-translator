from app_translator.frontends.fly_toml import parse_fly_toml
from app_translator.plan import plan_persistence
from app_translator.registry import ImageConfig

CONFIG = """
app = "grafana"

[build]
  image = "grafana/grafana:11.1.0"

[http_service]
  internal_port = 3000

[[mounts]]
  source = "grafana_data"
  destination = "/var/lib/grafana"
"""

GRAFANA_IMAGE = ImageConfig(
    entrypoint=("/run.sh",),
    exposed_ports=(3000,),
    user="472",
    env=("GF_PATHS_DATA=/var/lib/grafana", "GF_PATHS_HOME=/usr/share/grafana"),
)


def test_env_override_is_preferred_over_symlinking() -> None:
    service = parse_fly_toml(CONFIG).service
    planned, notes = plan_persistence(service, GRAFANA_IMAGE)

    overrides = {entry.key: entry.value for entry in planned.data_relative_env}
    assert overrides == {"GF_PATHS_DATA": "persisted-0"}
    assert planned.mounts[0].symlink_at_startup is False
    assert any("points GF_PATHS_DATA" in (entry.description or "") for entry in planned.env)
    assert any(note.severity == "info" for note in notes)


def test_non_root_image_without_a_setting_warns_loudly() -> None:
    service = parse_fly_toml(CONFIG).service
    planned, notes = plan_persistence(service, ImageConfig(entrypoint=("/app",), user="1000"))

    assert planned.data_relative_env == ()
    assert planned.mounts[0].symlink_at_startup is True
    assert any(note.severity == "needs_action" and "read-only parent" in note.message for note in notes)


def test_root_image_just_symlinks_quietly() -> None:
    service = parse_fly_toml(CONFIG).service
    planned, notes = plan_persistence(service, ImageConfig(entrypoint=("/app",), user=""))

    assert planned.data_relative_env == ()
    assert planned.mounts[0].symlink_at_startup is True
    assert notes == ()


def test_non_root_image_runs_as_root_so_it_can_write_its_data() -> None:
    service = parse_fly_toml(CONFIG).service
    planned, notes = plan_persistence(service, GRAFANA_IMAGE)

    assert planned.image_user == "472"
    assert planned.run_as_root is True
    assert any(note.field == "USER" and note.severity == "assumed" for note in notes)


def test_root_image_is_left_alone() -> None:
    service = parse_fly_toml(CONFIG).service
    planned, _ = plan_persistence(service, ImageConfig(entrypoint=("/app",), user="root"))

    assert planned.run_as_root is False
