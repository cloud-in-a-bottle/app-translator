import pytest

from app_translator.web.forms import FormError
from app_translator.web.forms import service_from_form

BASE = {
    "name": "demo",
    "http_port": "8080",
    "memory_mb": "256",
    "cpu_cores": "0.5",
    "health_path": "",
    "public_paths": "",
    "command": "",
    "description": "",
}


def _submit(**overrides: str) -> object:
    form = {**BASE, **overrides}
    return service_from_form(
        form,
        env_keys=[],
        env_values=[],
        secret_keys=[],
        data_relative_keys=[],
        mount_paths=[],
        symlinked_paths=[],
        extra_ports=(),
    )


def test_a_registry_reference_is_accepted() -> None:
    assert _submit(image_ref="grafana/grafana:11.1.0").image.ref == "grafana/grafana:11.1.0"


@pytest.mark.parametrize("bad", ["/other/Dockerfile", "Dockerfile", "deploy/dockerfile"])
def test_a_dockerfile_path_is_rejected(bad: str) -> None:
    with pytest.raises(FormError, match="looks like a path to a Dockerfile"):
        _submit(image_ref=bad)


def test_a_missing_image_explains_why_one_is_needed() -> None:
    with pytest.raises(FormError, match="published image reference is required"):
        _submit(image_ref="")


def test_a_bad_app_name_is_rejected() -> None:
    with pytest.raises(FormError, match="not a usable app name"):
        _submit(image_ref="nginx", name="Not Valid")


def test_a_public_path_must_be_absolute() -> None:
    with pytest.raises(FormError, match="must start with"):
        _submit(image_ref="nginx", public_paths="webhook")
