"""The review flow must not block the app while it talks to the network."""

import html
import time
from pathlib import Path

import pytest
from litestar.testing import TestClient

from app_translator.config import Settings


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        app_name="app-translator",
        zone_domain="example.test",
        data_dir=tmp_path,
        router_url="http://127.0.0.1:1",
        app_token="test-token",
        port=8080,
    )
    from app_translator.web.app import create_app

    with TestClient(app=create_app(settings)) as test_client:
        yield test_client


def test_review_redirects_to_a_job_instead_of_blocking(client: TestClient) -> None:
    response = client.post("/review", data={"example": "grafana.toml"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/jobs/")


def test_the_job_page_reports_progress_then_the_review(client: TestClient) -> None:
    location = client.post("/review", data={"example": "grafana.toml"}, follow_redirects=False).headers["location"]

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        page = client.get(location)
        assert page.status_code == 200
        if "Review the translation" in page.text:
            assert "grafana" in page.text
            return
        # Until it finishes, the user gets a waiting page that refreshes itself.
        assert "Translating" in page.text
        assert 'http-equiv="refresh"' in page.text
        time.sleep(0.5)
    pytest.fail("the translation never finished")


def test_other_pages_still_serve_while_a_translation_runs(client: TestClient) -> None:
    client.post("/review", data={"example": "grafana.toml"}, follow_redirects=False)

    started = time.monotonic()
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/").status_code == 200
    # If the translation were blocking the loop these would queue behind it.
    assert time.monotonic() - started < 5


def test_an_unknown_job_says_so_without_a_500(client: TestClient) -> None:
    response = client.get("/jobs/doesnotexist")

    assert response.status_code == 404
    assert "no longer around" in response.text


def test_a_missing_page_is_a_404_not_an_internal_error(client: TestClient) -> None:
    response = client.get("/no/such/page")

    assert response.status_code == 404
    assert "500" not in response.text


def test_favicon_is_served(client: TestClient) -> None:
    response = client.get("/favicon.ico")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")


def test_the_port_falls_back_to_flys_default_and_says_so(client: TestClient) -> None:
    """A config that declares no port at all still produces a usable, labelled form."""
    config = 'app = "noport"\n[build]\nimage = "nginx:1.27-alpine"\n'

    location = client.post("/review", data={"source_text": config}, follow_redirects=False).headers["location"]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        page = client.get(location)
        if "Review the translation" in page.text:
            # nginx:alpine EXPOSEs 80, so the image wins over fly's default.
            rendered = html.unescape(page.text)
            assert 'id="http_port"' in rendered
            assert "from the image's EXPOSE" in rendered or "from fly's default of 8080" in rendered
            # No spinner on the port field: a stray scroll must not change it.
            assert 'id="http_port" name="http_port" type="text"' in page.text
            return
        time.sleep(0.5)
    pytest.fail("the translation never finished")
