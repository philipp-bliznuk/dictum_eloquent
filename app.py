import contextlib
import dataclasses
import logging
import typing as t

import fastapi
import fastapi.staticfiles
import httpx

from dictum.database import Database
from dictum.exceptions import NotFoundError
from dictum.middleware import AllowedHostsMiddleware
from dictum.routers import registry
from dictum.services.ai import build_ai_service
from dictum.services.audio import AudioCache
from dictum.services.lookup import build_lookup_service
from dictum.services.search import SearchIndex
from dictum.settings import get_settings

if t.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib

    from dictum.services.ai import BedrockClient
    from dictum.settings import Settings

API_PREFIX: t.Final = "/api"
USER_AGENT: t.Final = "dictum-eloquent/0.1"
ACCESS_LOGGER: t.Final = "uvicorn.access"
_PATH_ARG: t.Final = 2
"""Position of the request path in uvicorn access records: client, method, path, http version, status."""
log = logging.getLogger("dictum")

type Lifespan = cabc.Callable[[fastapi.FastAPI], contextlib.AbstractAsyncContextManager[None]]


class _HealthcheckFilter(logging.Filter):
    """Drops access-log lines for the container healthcheck, which polls `/api/health` every 30 s."""

    @t.override
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) <= _PATH_ARG:
            return True
        return not str(args[_PATH_ARG]).startswith(f"{API_PREFIX}/health")


quiet_healthcheck = _HealthcheckFilter()
"""Single instance so repeated `create_app()` calls attach it once (`addFilter` ignores duplicates)."""


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Backends:
    """Outbound dependencies a test can swap for fakes."""

    transport: httpx.AsyncBaseTransport | None = None
    """Replaces the HTTP transport used by dictionary providers and the audio cache."""
    bedrock: BedrockClient | None = None
    """Replaces the boto3 Bedrock client (credentials still gate the feature)."""


def _lifespan(*, settings: Settings, backends: Backends) -> Lifespan:
    @contextlib.asynccontextmanager
    async def lifespan(app: fastapi.FastAPI) -> cabc.AsyncGenerator[None]:
        settings.audio_dir.mkdir(parents=True, exist_ok=True)
        app.state.db = Database(settings=settings)
        app.state.search = SearchIndex()
        app.state.http = httpx.AsyncClient(
            timeout=settings.http_timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
            transport=backends.transport,
        )
        app.state.audio = AudioCache(client=app.state.http, directory=settings.audio_dir)
        app.state.lookup = build_lookup_service(settings=settings, client=app.state.http, audio_cache=app.state.audio)
        app.state.ai = build_ai_service(settings=settings, client=backends.bedrock)
        log.info(
            "started env=%s db=%s providers=%s ai=%s allowed_hosts=%s",
            settings.environment,
            settings.database_url,
            [p.name for p in app.state.lookup.providers],
            app.state.ai is not None,
            ",".join(settings.allowed_hosts),
        )
        try:
            yield
        finally:
            await app.state.http.aclose()
            await app.state.db.dispose()

    return lifespan


def _set_middleware(*, app: fastapi.FastAPI, settings: Settings) -> None:
    app.add_middleware(AllowedHostsMiddleware, allowed_hosts=settings.allowed_hosts)


def _set_routers(*, app: fastapi.FastAPI) -> None:
    for router in registry:
        app.include_router(router, prefix=API_PREFIX)


def _resolve_asset(*, dist_root: pathlib.Path, full_path: str) -> pathlib.Path | None:
    """Real file under `dist_root` for `full_path`, or None (traversal, odd bytes, missing)."""
    try:
        candidate = (dist_root / full_path).resolve()
        if candidate.is_relative_to(dist_root) and candidate.is_file():
            return candidate
    except ValueError, OSError:
        pass
    return None


def _spa_handler(*, dist_root: pathlib.Path) -> cabc.Callable[..., fastapi.responses.FileResponse]:
    index_file = dist_root / "index.html"
    api_root = API_PREFIX.lstrip("/")

    def spa(*, full_path: str) -> fastapi.responses.FileResponse:
        if full_path == api_root or full_path.startswith(f"{api_root}/"):
            raise NotFoundError
        asset = _resolve_asset(dist_root=dist_root, full_path=full_path)
        return fastapi.responses.FileResponse(asset or index_file)

    return spa


def _set_static(*, app: fastapi.FastAPI, settings: Settings) -> None:
    app.mount(
        "/audio",
        fastapi.staticfiles.StaticFiles(directory=settings.audio_dir, check_dir=False),
        name="audio",
    )
    dist_root = settings.ui_dist.resolve()
    if not (dist_root / "index.html").is_file():
        log.warning("ui bundle missing at %s; api only", settings.ui_dist)
        return
    app.mount("/assets", fastapi.staticfiles.StaticFiles(directory=dist_root / "assets"), name="assets")
    app.get(
        "/{full_path:path}",
        include_in_schema=False,
        response_model=None,
        status_code=fastapi.status.HTTP_200_OK,
    )(_spa_handler(dist_root=dist_root))


def _set_logging(*, settings: Settings) -> None:
    logging.basicConfig(level=settings.log_level)
    logging.getLogger(ACCESS_LOGGER).addFilter(quiet_healthcheck)


def create_app(*, settings: Settings | None = None, backends: Backends | None = None) -> fastapi.FastAPI:
    """Build the FastAPI application.

    Args:
        settings: Overrides the environment-derived settings (tests).
        backends: Fake outbound clients (tests); production builds real ones from `settings`.
    """
    settings = settings or get_settings()
    _set_logging(settings=settings)
    app = fastapi.FastAPI(
        title="Dictum Eloquent",
        version="0.1.0",
        debug=settings.is_local,
        lifespan=_lifespan(settings=settings, backends=backends or Backends()),
        docs_url="/api/docs" if not settings.is_test else None,
        openapi_url="/api/openapi.json",
    )
    _set_middleware(app=app, settings=settings)
    _set_routers(app=app)
    _set_static(app=app, settings=settings)
    return app
