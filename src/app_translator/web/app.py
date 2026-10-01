import asyncio
import logging
import mimetypes
from pathlib import Path
from typing import Annotated
from typing import Any

import anyio
import attr
from litestar import Litestar
from litestar import Request
from litestar import Response
from litestar import get
from litestar import post
from litestar.plugins.jinja import JinjaTemplateEngine
from litestar.exceptions import HTTPException
from litestar.params import Parameter
from litestar.response import Redirect
from litestar.response import Template
from litestar.static_files import create_static_files_router
from litestar.template.config import TemplateConfig

from app_translator.build import MissingCommandError
from app_translator.build import build_repo
from app_translator.config import Settings
from app_translator.emit.manifest import render_manifest
from app_translator.fetch import FetchError
from app_translator.fetch import fetch_config_text
from app_translator.fetch import github_repo_url
from app_translator.frontends.fly_toml import FLY_DEFAULT_INTERNAL_PORT
from app_translator.frontends.fly_toml import UnsupportedConfigError
from app_translator.frontends.fly_toml import parse_fly_toml
from app_translator.hosting.base import RepoHost
from app_translator.hosting.forgejo import ForgejoError
from app_translator.hosting.forgejo import ForgejoHost
from app_translator.hosting.self_served import SelfServedGitHost
from app_translator.ir import ServiceSpec
from app_translator.ir import TranslationNote
from app_translator.jobs import Job
from app_translator.jobs import JobStore
from app_translator.netprobe import probe
from app_translator.plan import plan_persistence
from app_translator.registry import ImageConfig
from app_translator.registry import inspect_image
from app_translator.web.forms import FormError
from app_translator.web.forms import service_from_form

logger = logging.getLogger("app_translator")

TEMPLATES_DIR = Path(__file__).parent / "templates"
# Examples ship inside the package so they are present wherever it is installed.
EXAMPLES_DIR = Path(__file__).parent.parent / "examples" / "fly"


def _settings(request: Request[Any, Any, Any]) -> Settings:
    settings = request.app.state.settings
    assert isinstance(settings, Settings)
    return settings


def _repo_host(request: Request[Any, Any, Any]) -> RepoHost:
    host = request.app.state.repo_host
    assert isinstance(host, (SelfServedGitHost, ForgejoHost))
    return host


def _all(form: object, key: str) -> list[str]:
    """Every value submitted for a repeated form field, or [] if the field is absent."""
    getall = getattr(form, "getall", None)
    if getall is None:
        value = form.get(key) if isinstance(form, dict) else None  # pragma: no cover - defensive
        return [] if value is None else [str(value)]
    try:
        return [str(value) for value in getall(key)]
    except KeyError:
        return []


def _error(message: str, *, detail: str = "", status_code: int = 400) -> Template:
    return Template(
        template_name="error.html",
        context={"message": message, "detail": detail},
        status_code=status_code,
    )


@get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "ok"}


@get("/")
async def index(request: Request[Any, Any, Any]) -> Template:
    examples = sorted(path.name for path in EXAMPLES_DIR.glob("*.toml")) if EXAMPLES_DIR.is_dir() else []
    return Template(
        template_name="index.html",
        context={"examples": examples, "host_kind": _repo_host(request).kind},
    )


def _review_context(
    source_text: str,
    service: ServiceSpec,
    notes: tuple[Any, ...],
    image_config: ImageConfig | None,
    *,
    source_label: str,
    settings: Settings,
    source_repo_url: str | None = None,
) -> dict[str, Any]:
    command = service.command or (" ".join(image_config.argv) if image_config else "")
    # An app that builds from its own repo cannot be rehosted in a generated repo — we
    # have no copy of its source. The right move is a manifest committed to that repo.
    builds_from_source = service.image.kind == "dockerfile"
    own_repo_manifest = render_manifest(service) if builds_from_source and service.http_port else ""
    own_repo_install_url = (
        f"{settings.install_base_url}?repo={source_repo_url}" if builds_from_source and source_repo_url else ""
    )
    return {
        "builds_from_source": builds_from_source,
        "own_repo_manifest": own_repo_manifest,
        "own_repo_install_url": own_repo_install_url,
        "source_repo_url": source_repo_url or "",
        "source_text": source_text,
        "source_label": source_label,
        "service": service,
        "command": command,
        "notes": notes,
        "image_user": image_config.user if image_config else "",
        "image_ports": image_config.exposed_ports if image_config else (),
        "inspected": image_config is not None,
    }


def _translate(
    *,
    source_text: str,
    source_label: str,
    source_repo_url: str | None,
    settings: Settings,
) -> dict[str, Any]:
    """Blocking: parses, then talks to a registry. Run this in a worker thread."""
    stack = parse_fly_toml(source_text)
    service = stack.service
    image_config = inspect_image(service.image.ref) if service.image.kind == "registry" else None
    notes = list(stack.notes)

    # Where the port came from, so the form can say rather than leave the user guessing.
    port_source = stack.port_source

    # The image's own metadata fills holes the config left: the port it listens on and
    # the command to run.
    if service.http_port is None and image_config and image_config.exposed_ports:
        service = attr.evolve(service, http_port=image_config.exposed_ports[0])
        port_source = "the image's EXPOSE"

    if service.http_port is None:
        service = attr.evolve(service, http_port=FLY_DEFAULT_INTERNAL_PORT)
        port_source = f"fly's default of {FLY_DEFAULT_INTERNAL_PORT}"
        notes.append(
            TranslationNote(
                field="internal_port",
                severity="assumed",
                message=(
                    f"nothing declared a port, so this is fly's documented default of {FLY_DEFAULT_INTERNAL_PORT}. "
                    "Check it against the app — if nothing listens there, the app will install and then fail its "
                    "readiness check."
                ),
            )
        )

    service, persistence_notes = plan_persistence(service, image_config)
    context = _review_context(
        source_text,
        service,
        tuple(notes) + persistence_notes,
        image_config,
        source_label=source_label,
        settings=settings,
        source_repo_url=source_repo_url,
    )
    context["port_source"] = port_source
    return context


async def _run_job(app_state: Any, job_id: str, **translate_kwargs: Any) -> None:  # noqa: ANN401
    """Own one job from queued to finished. One translation runs at a time."""
    store: JobStore = app_state.jobs
    async with app_state.translate_semaphore:
        store.mark_running(job_id)
        try:
            context = await anyio.to_thread.run_sync(lambda: _translate(**translate_kwargs))
        except (UnsupportedConfigError, ValueError) as exc:
            store.mark_failed(job_id, "That config cannot be translated as-is.", str(exc))
        except Exception as exc:  # noqa: BLE001 - a job must never die silently
            logger.exception("translation job %s failed", job_id)
            store.mark_failed(job_id, "The translation failed unexpectedly.", f"{type(exc).__name__}: {exc}")
        else:
            store.mark_done(job_id, context)


@post("/review", status_code=303)
async def review(request: Request[Any, Any, Any]) -> Redirect:
    """Start a translation and hand the browser a job to watch."""
    form = await request.form()
    source_url = str(form.get("source_url", "")).strip()
    pasted = str(form.get("source_text", "")).strip()
    example = str(form.get("example", "")).strip()

    source_label = "pasted config"
    source_repo_url = github_repo_url(source_url) if source_url else None
    store: JobStore = request.app.state.jobs

    if example:
        candidate = EXAMPLES_DIR / example
        if not candidate.is_file() or candidate.parent != EXAMPLES_DIR:
            job = store.create(f"example: {example}")
            store.mark_failed(job.job_id, f"Unknown example {example!r}.")
            return Redirect(f"/jobs/{job.job_id}", status_code=303)
        pasted = candidate.read_text()
        source_label = f"example: {example}"

    job = store.create(source_label if not source_url else source_url)

    if source_url and not pasted:
        # Fetching is network work too, so it belongs in the job, not in this request.
        async def fetch_then_translate() -> None:
            async with request.app.state.translate_semaphore:
                store.mark_running(job.job_id)
                try:
                    text, resolved = await anyio.to_thread.run_sync(fetch_config_text, source_url)
                except FetchError as exc:
                    store.mark_failed(job.job_id, "Could not fetch that config.", str(exc))
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.exception("fetch for job %s failed", job.job_id)
                    store.mark_failed(job.job_id, "Could not fetch that config.", f"{type(exc).__name__}: {exc}")
                    return
                try:
                    context = await anyio.to_thread.run_sync(
                        lambda: _translate(
                            source_text=text,
                            source_label=resolved,
                            source_repo_url=source_repo_url,
                            settings=_settings(request),
                        )
                    )
                except (UnsupportedConfigError, ValueError) as exc:
                    store.mark_failed(job.job_id, "That config cannot be translated as-is.", str(exc))
                except Exception as exc:  # noqa: BLE001
                    logger.exception("translation job %s failed", job.job_id)
                    store.mark_failed(job.job_id, "The translation failed unexpectedly.", f"{type(exc).__name__}: {exc}")
                else:
                    store.mark_done(job.job_id, context)

        asyncio.create_task(fetch_then_translate())
        return Redirect(f"/jobs/{job.job_id}", status_code=303)

    if not pasted:
        store.mark_failed(job.job_id, "Give me a URL to fetch or paste a fly.toml.")
        return Redirect(f"/jobs/{job.job_id}", status_code=303)

    asyncio.create_task(
        _run_job(
            request.app.state,
            job.job_id,
            source_text=pasted,
            source_label=source_label,
            source_repo_url=source_repo_url,
            settings=_settings(request),
        )
    )
    return Redirect(f"/jobs/{job.job_id}", status_code=303)


@get("/jobs/{job_id:str}")
async def job_status(
    job_id: Annotated[str, Parameter(description="Translation job id")],
    request: Request[Any, Any, Any],
) -> Template:
    """The review page once the job is done; a waiting page until then."""
    store: JobStore = request.app.state.jobs
    job: Job | None = store.get(job_id)
    if job is None:
        return _error(
            "That translation is no longer around.",
            detail="Jobs are kept in memory, so they are lost when the app restarts.",
            status_code=404,
        )
    if job.state == "failed":
        return _error(job.error_message, detail=job.error_detail)
    if job.state == "done" and job.context is not None:
        return Template(template_name="review.html", context=job.context)
    return Template(
        template_name="loading.html",
        context={
            "job": job,
            "elapsed": int(job.elapsed_seconds),
            "ahead": max(0, store.queue_length() - 1),
        },
    )


@post("/generate")
async def generate(request: Request[Any, Any, Any]) -> Template:
    form = await request.form()
    source_text = str(form.get("source_text", ""))
    if not source_text.strip():
        return _error("The original config went missing from the form; start again.")

    try:
        stack = parse_fly_toml(source_text)
    except (UnsupportedConfigError, ValueError) as exc:
        return _error("That config cannot be translated as-is.", detail=str(exc))

    try:
        service = service_from_form(
            form,
            env_keys=_all(form, "env_key"),
            env_values=_all(form, "env_value"),
            secret_keys=_all(form, "env_secret"),
            data_relative_keys=_all(form, "env_data_relative"),
            mount_paths=_all(form, "mount_path"),
            symlinked_paths=_all(form, "mount_symlink"),
            extra_ports=stack.service.extra_ports,
        )
    except FormError as exc:
        return _error("That form needs a fix.", detail=str(exc))

    try:
        repo = build_repo(stack, service, image_config=None, source_text=source_text)
    except (MissingCommandError, ValueError) as exc:
        return _error("Cannot generate the repo yet.", detail=str(exc))

    host = _repo_host(request)
    try:
        # Writing and publishing a repo shells out to git; keep it off the event loop.
        published = await anyio.to_thread.run_sync(host.publish, repo, service.name)
    except (ForgejoError, RuntimeError) as exc:
        return _error("Publishing the generated repo failed.", detail=str(exc))

    settings = _settings(request)
    install_url = f"{settings.install_base_url}?repo={published.clone_url}"
    return Template(
        template_name="done.html",
        context={
            "service": service,
            "published": published,
            "install_url": install_url,
            "files": repo.files,
            "secret_keys": service.secret_keys,
            "zone_domain": settings.zone_domain,
        },
    )


@get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response[bytes]:
    """Browsers ask for this on every page; answer it rather than logging a 404."""
    icon = (Path(__file__).parent / "static" / "favicon.svg").read_bytes()
    return Response(content=icon, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})


@get("/debug/probe", include_in_schema=False)
async def debug_probe(
    url: Annotated[str, Parameter(query="url")] = "https://raw.githubusercontent.com/",
) -> dict[str, Any]:
    """Time DNS and a TCP connect to every address a host resolves to, from in here."""
    result = await anyio.to_thread.run_sync(probe, url)
    return {
        "host": result.host,
        "port": result.port,
        "dns_seconds": round(result.dns_seconds, 3),
        "dns_error": result.dns_error,
        "tls_seconds": round(result.tls_seconds, 3),
        "tls_error": result.tls_error,
        "addresses": [
            {
                "family": address.family,
                "address": address.address,
                "seconds": round(address.seconds, 3),
                "error": address.error,
            }
            for address in result.addresses
        ],
    }


@get("/git/{path:path}", include_in_schema=False)
async def serve_git(
    path: Annotated[str, Parameter(description="Path inside a generated bare repo")],
    request: Request[Any, Any, Any],
) -> Response[bytes]:
    """Serve generated bare repos over git's dumb HTTP protocol.

    Public (unauthenticated) so the compute space's router can clone from here.
    """
    settings = _settings(request)
    relative = path.lstrip("/")
    target = (settings.repos_dir / relative).resolve()
    repos_root = settings.repos_dir.resolve()
    if not str(target).startswith(str(repos_root)) or not target.is_file():
        return Response(content=b"not found", status_code=404, media_type="text/plain")
    media_type, _ = mimetypes.guess_type(target.name)
    return Response(
        content=target.read_bytes(),
        media_type=media_type or "application/octet-stream",
        headers={"Cache-Control": "no-cache"},
    )


def build_repo_host(settings: Settings) -> RepoHost:
    """Forgejo when it is configured, otherwise serve the repos ourselves."""
    if settings.forgejo_base_url and settings.forgejo_token:
        return ForgejoHost(base_url=settings.forgejo_base_url, token=settings.forgejo_token)
    return SelfServedGitHost(repos_dir=settings.repos_dir, public_base_url=settings.public_base_url)


def _handle_http_exception(request: Request[Any, Any, Any], exc: HTTPException) -> Response[bytes]:
    """Render Litestar's own HTTP errors (404s, mostly) without logging a traceback."""
    body = (
        f"<h1>{exc.status_code}</h1><p>{exc.detail}</p><p><a href=\"/\">Go to the start</a></p>"
    ).encode()
    return Response(content=body, media_type="text/html", status_code=exc.status_code)


def _handle_unexpected(request: Request[Any, Any, Any], exc: Exception) -> Response[bytes]:
    """Never fail silently: log the traceback and show the user what broke."""
    logger.exception("unhandled error serving %s %s", request.method, request.url.path)
    body = (
        "<h1>Something went wrong</h1>"
        f"<pre>{type(exc).__name__}: {exc}</pre>"
        '<p><a href="/">Start again</a></p>'
    ).encode()
    return Response(content=body, media_type="text/html", status_code=500)


def create_app(settings: Settings | None = None) -> Litestar:
    resolved = settings or Settings.from_env()
    resolved.repos_dir.mkdir(parents=True, exist_ok=True)
    app = Litestar(
        route_handlers=[
            index,
            review,
            job_status,
            generate,
            favicon,
            debug_probe,
            serve_git,
            health,
            create_static_files_router(path="/static", directories=[Path(__file__).parent / "static"]),
        ],
        template_config=TemplateConfig(directory=TEMPLATES_DIR, engine=JinjaTemplateEngine),
        exception_handlers={HTTPException: _handle_http_exception, Exception: _handle_unexpected},
    )
    app.state.settings = resolved
    app.state.repo_host = build_repo_host(resolved)
    app.state.jobs = JobStore()
    # One translation at a time: they are network-bound and the cost of queueing is
    # a second or two, while the cost of unbounded concurrency is a confusing app.
    app.state.translate_semaphore = asyncio.Semaphore(1)
    return app
