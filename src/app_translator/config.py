"""Runtime settings, read from the environment the platform injects.

Required values fail loudly at startup rather than defaulting to something plausible.
"""

import os
from pathlib import Path

import attr


class MissingSettingError(Exception):
    pass


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise MissingSettingError(f"{name} is not set; this app expects to run as an OpenHost app")
    return value


@attr.s(auto_attribs=True, frozen=True)
class Settings:
    app_name: str
    zone_domain: str
    data_dir: Path
    router_url: str
    app_token: str
    port: int
    forgejo_base_url: str | None = None
    forgejo_token: str | None = None

    @property
    def public_base_url(self) -> str:
        return f"https://{self.app_name}.{self.zone_domain}"

    @property
    def repos_dir(self) -> Path:
        return self.data_dir / "repos"

    @property
    def install_base_url(self) -> str:
        return f"https://{self.zone_domain}/add_app"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            app_name=_required("OPENHOST_APP_NAME"),
            zone_domain=_required("OPENHOST_ZONE_DOMAIN"),
            data_dir=Path(_required("OPENHOST_APP_DATA_DIR")),
            router_url=_required("OPENHOST_ROUTER_URL"),
            app_token=_required("OPENHOST_APP_TOKEN"),
            port=int(os.environ.get("PORT", "8080")),
            forgejo_base_url=os.environ.get("FORGEJO_BASE_URL") or None,
            forgejo_token=os.environ.get("FORGEJO_TOKEN") or None,
        )
