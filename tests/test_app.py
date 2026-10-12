import contextlib
import logging
import typing as t

import httpx
import pytest

from app import ACCESS_LOGGER
from app import Backends
from app import create_app
from app import quiet_healthcheck
from dictum.database import Database
from dictum.services.ai import AIService
from dictum.services.audio import AudioCache
from dictum.services.lookup import LookupService
from dictum.services.search import SearchIndex
from tests.conftest import AWS_SECRET_ACCESS_KEY
from tests.conftest import MW_API_KEY
from tests.conftest import FakeBedrock
from tests.conftest import make_ai_settings
from tests.conftest import make_settings
from tests.conftest import tool_use_response

if t.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib

    import fastapi
    from pytest_mock import MockerFixture

    from dictum.settings import Settings


def _build_dist(root: pathlib.Path) -> None:
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<html>spa</html>")
    (root / "assets" / "app.js").write_text("console.log(1)")
    (root / "favicon.ico").write_bytes(b"icon")


@contextlib.asynccontextmanager
async def _client(*, settings: Settings) -> cabc.AsyncGenerator[tuple[fastapi.FastAPI, httpx.AsyncClient]]:
    app = create_app(settings=settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield app, client


@pytest.fixture
async def spa(tmp_path: pathlib.Path) -> cabc.AsyncIterator[tuple[fastapi.FastAPI, httpx.AsyncClient]]:
    settings = make_settings(tmp_path=tmp_path)
    _build_dist(settings.ui_dist)
    async with _client(settings=settings) as pair:
        yield pair


ACCESS_FORMAT: t.Final = '%s - "%s %s HTTP/%s" %d'
"""Message uvicorn logs per request; args are client, method, path with query string, http version, status."""


def _access_record(*, args: object) -> logging.LogRecord:
    return logging.LogRecord(
        name=ACCESS_LOGGER, level=logging.INFO, pathname=__file__, lineno=0, msg=ACCESS_FORMAT, args=args, exc_info=None
    )


def _access(path: str) -> tuple[str, str, str, str, int]:
    return ("127.0.0.1:50000", "GET", path, "1.1", 200)


class TestHealthcheckFilter:
    @pytest.mark.parametrize(
        ("args", "kept"),
        [
            (_access("/api/health"), False),
            (_access("/api/health?x=1"), False),
            (_access("/api/health/"), False),
            (_access("/api/words"), True),
            (_access("/api/words?q=health"), True),
            (_access("/health"), True),
            (_access("/"), True),
            (None, True),
            (("127.0.0.1:50000", "GET"), True),
            (({"path": "/api/health"},), True),
        ],
        ids=[
            "health",
            "health-with-query",
            "health-trailing-slash",
            "other-path",
            "query-mentions-health",
            "unprefixed-health",
            "root",
            "no-args",
            "short-tuple",
            "mapping-args",
        ],
    )
    def test_filter(self, *, args: object, kept: bool) -> None:
        assert quiet_healthcheck.filter(_access_record(args=args)) is kept

    def test_create_app_attaches_filter_once(self, tmp_path: pathlib.Path) -> None:
        create_app(settings=make_settings(tmp_path=tmp_path))
        create_app(settings=make_settings(tmp_path=tmp_path))
        assert logging.getLogger(ACCESS_LOGGER).filters.count(quiet_healthcheck) == 1

    def test_access_logger_drops_healthcheck_lines(self, settings: Settings, caplog: pytest.LogCaptureFixture) -> None:
        create_app(settings=settings)
        access = logging.getLogger(ACCESS_LOGGER)
        with caplog.at_level(logging.INFO, logger=ACCESS_LOGGER):
            access.info(ACCESS_FORMAT, *_access("/api/health"))
            access.info(ACCESS_FORMAT, *_access("/api/health?probe=1"))
            access.info(ACCESS_FORMAT, *_access("/api/words"))
        lines = [r.getMessage() for r in caplog.records if r.name == ACCESS_LOGGER]
        assert lines == ['127.0.0.1:50000 - "GET /api/words HTTP/1.1" 200']

    def test_logging_level_from_settings(self, tmp_path: pathlib.Path, mocker: MockerFixture) -> None:
        basic_config = mocker.patch("app.logging.basicConfig")
        create_app(settings=make_settings(tmp_path=tmp_path, log_level="debug"))
        basic_config.assert_called_once_with(level="DEBUG")


class TestCreateApp:
    def test_metadata_and_state(self, settings: Settings) -> None:
        app = create_app(settings=settings)
        assert app.title == "Dictum Eloquent"
        assert app.debug is False
        assert app.docs_url is None
        assert app.openapi_url == "/api/openapi.json"
        assert not hasattr(app.state, "db")

    def test_local_enables_debug_and_docs(self, tmp_path: pathlib.Path) -> None:
        app = create_app(settings=make_settings(tmp_path=tmp_path, environment="local"))
        assert app.debug is True
        assert app.docs_url == "/api/docs"

    def test_falls_back_to_get_settings(self, mocker: MockerFixture, settings: Settings) -> None:
        mocker.patch("app.get_settings", return_value=settings)
        app = create_app()
        assert app.docs_url is None

    async def test_lifespan_populates_state_and_disposes(self, settings: Settings, mocker: MockerFixture) -> None:
        app = create_app(settings=settings)
        dispose = mocker.spy(Database, "dispose")
        async with app.router.lifespan_context(app):
            assert isinstance(app.state.db, Database)
            assert isinstance(app.state.search, SearchIndex)
            assert isinstance(app.state.http, httpx.AsyncClient)
            assert isinstance(app.state.audio, AudioCache)
            assert isinstance(app.state.lookup, LookupService)
            assert settings.audio_dir.is_dir()
            assert app.state.http.is_closed is False
        assert dispose.call_count == 1
        assert app.state.http.is_closed is True

    async def test_http_client_configured_from_settings(self, tmp_path: pathlib.Path) -> None:
        settings = make_settings(tmp_path=tmp_path, http_timeout=2.5, mw_api_key=MW_API_KEY)
        app = create_app(
            settings=settings, backends=Backends(transport=httpx.MockTransport(lambda _r: httpx.Response(500)))
        )
        async with app.router.lifespan_context(app):
            http: httpx.AsyncClient = app.state.http
            assert http.timeout == httpx.Timeout(2.5)
            assert http.follow_redirects is True
            assert http.headers["user-agent"] == "dictum-eloquent/0.1"
            assert [p.name for p in app.state.lookup.providers] == ["mw", "free_dict"]

    async def test_transport_replaces_outbound_http(self, settings: Settings) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json=[])

        app = create_app(settings=settings, backends=Backends(transport=httpx.MockTransport(handler)))
        async with app.router.lifespan_context(app):
            response = await app.state.http.get("https://free.test/en/abandon")
        assert response.status_code == 200
        assert seen == ["https://free.test/en/abandon"]

    async def test_startup_logs_providers(self, settings: Settings, caplog: pytest.LogCaptureFixture) -> None:
        app = create_app(settings=settings)
        with caplog.at_level("INFO", logger="dictum"):
            async with app.router.lifespan_context(app):
                pass
        assert "providers=['free_dict']" in caplog.text

    async def test_ai_disabled_without_credentials(self, settings: Settings, caplog: pytest.LogCaptureFixture) -> None:
        app = create_app(settings=settings, backends=Backends(bedrock=FakeBedrock()))
        with caplog.at_level("INFO", logger="dictum"):
            async with app.router.lifespan_context(app):
                assert app.state.ai is None
        assert " ai=False" in caplog.text

    async def test_ai_enabled_with_credentials_and_injected_client(
        self, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        bedrock = FakeBedrock()
        app = create_app(settings=make_ai_settings(tmp_path=tmp_path), backends=Backends(bedrock=bedrock))
        with caplog.at_level("INFO", logger="dictum"):
            async with app.router.lifespan_context(app):
                assert isinstance(app.state.ai, AIService)
                bedrock.queue(tool_use_response(name="record_entries", payload={"known": False, "entries": []}))
                assert await app.state.ai.generate_entries(text="abandon") == []
        assert " ai=True" in caplog.text
        assert len(bedrock.calls) == 1
        assert AWS_SECRET_ACCESS_KEY not in caplog.text

    async def test_routes_registered(self, client: httpx.AsyncClient) -> None:
        schema = (await client.get("/api/openapi.json")).json()
        assert set(schema["paths"]) >= {
            "/api/health",
            "/api/words",
            "/api/words/stats",
            "/api/words/by-spelling",
            "/api/words/{word_id}",
            "/api/words/{word_id}/audio",
            "/api/lookup",
            "/api/search",
            "/api/import",
            "/api/export.json",
            "/api/export.md",
            "/api/generate/examples",
            "/api/generate/definitions",
        }

    async def test_lookup_operations_documented(self, client: httpx.AsyncClient) -> None:
        paths = (await client.get("/api/openapi.json")).json()["paths"]
        lookup = paths["/api/lookup"]["post"]
        assert lookup["tags"] == ["lookup"]
        assert set(lookup["responses"]) == {"200", "422"}
        audio = paths["/api/words/{word_id}/audio"]["post"]
        assert audio["tags"] == ["words"]
        assert set(audio["responses"]) == {"200", "422"}

    @pytest.mark.parametrize("path", ["/api/generate/examples", "/api/generate/definitions"])
    async def test_generate_operations_documented(self, client: httpx.AsyncClient, path: str) -> None:
        paths = (await client.get("/api/openapi.json")).json()["paths"]
        assert set(paths[path]) == {"post"}
        operation = paths[path]["post"]
        assert operation["tags"] == ["generate"]
        assert set(operation["responses"]) == {"200", "422"}

    async def test_docs_disabled_in_test(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/api/docs")).status_code == 404

    async def test_docs_enabled_local(self, tmp_path: pathlib.Path) -> None:
        async with _client(settings=make_settings(tmp_path=tmp_path, environment="local")) as (_, client):
            assert (await client.get("/api/docs")).status_code == 200


class TestAllowedHosts:
    async def test_wildcard_accepts_any_host(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/health", headers={"Host": "anything.example"})
        assert response.status_code == 200

    async def test_allow_list_enforced(self, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture) -> None:
        settings = make_settings(tmp_path=tmp_path, allowed_hosts="dictum.local, 192.168.50.199:3000")
        async with _client(settings=settings) as (_, client):
            assert (await client.get("/api/health", headers={"Host": "dictum.local"})).status_code == 200
            assert (await client.get("/api/health", headers={"Host": "DICTUM.local:3000"})).status_code == 200
            assert (await client.get("/api/health", headers={"Host": "192.168.50.199:3000"})).status_code == 200
            with caplog.at_level(logging.WARNING, logger="dictum.hosts"):
                response = await client.get("/api/health", headers={"Host": "evil.example"})
        assert response.status_code == 400
        assert response.json() == {"detail": "Host 'evil.example' is not in ALLOWED_HOSTS"}
        assert "rejected Host 'evil.example'; ALLOWED_HOSTS=dictum.local,192.168.50.199" in caplog.text

    @pytest.mark.parametrize("host", ["localhost", "localhost:3000", "127.0.0.1:3000", "[::1]:3000"])
    async def test_loopback_always_allowed(self, tmp_path: pathlib.Path, host: str) -> None:
        settings = make_settings(tmp_path=tmp_path, allowed_hosts="dictum.local")
        async with _client(settings=settings) as (_, client):
            assert (await client.get("/api/health", headers={"Host": host})).status_code == 200

    async def test_startup_logs_allow_list(self, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture) -> None:
        settings = make_settings(tmp_path=tmp_path, allowed_hosts="dictum.local;127.0.0.1")
        with caplog.at_level(logging.INFO, logger="dictum"):
            async with _client(settings=settings):
                pass
        assert "allowed_hosts=dictum.local,127.0.0.1" in caplog.text


class TestStaticWithoutBundle:
    async def test_root_is_404(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/")).status_code == 404
        assert (await client.get("/some/route")).status_code == 404

    async def test_audio_served(self, client: httpx.AsyncClient, settings: Settings) -> None:
        (settings.audio_dir / "a.mp3").write_bytes(b"\x00\x01")
        response = await client.get("/audio/a.mp3")
        assert response.status_code == 200
        assert response.content == b"\x00\x01"
        assert (await client.get("/audio/missing.mp3")).status_code == 404


class TestSpa:
    @pytest.mark.parametrize("path", ["/", "/some/route", "/words/12", "/index.html"])
    async def test_index_served(self, spa: tuple[fastapi.FastAPI, httpx.AsyncClient], path: str) -> None:
        _, client = spa
        response = await client.get(path)
        assert response.status_code == 200
        assert response.text == "<html>spa</html>"
        assert response.headers["content-type"].startswith("text/html")

    async def test_existing_file_served(self, spa: tuple[fastapi.FastAPI, httpx.AsyncClient]) -> None:
        _, client = spa
        response = await client.get("/favicon.ico")
        assert response.status_code == 200
        assert response.content == b"icon"

    async def test_assets_mount(self, spa: tuple[fastapi.FastAPI, httpx.AsyncClient]) -> None:
        _, client = spa
        response = await client.get("/assets/app.js")
        assert response.status_code == 200
        assert response.text == "console.log(1)"
        assert (await client.get("/assets/nope.js")).status_code == 404

    async def test_api_still_wins(self, spa: tuple[fastapi.FastAPI, httpx.AsyncClient]) -> None:
        _, client = spa
        response = await client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert (await client.get("/api/nope")).status_code == 404
        assert (await client.get("/api")).status_code == 404
        assert (await client.get("/api/")).status_code == 404

    async def test_api_prefix_lookalike_is_spa(self, spa: tuple[fastapi.FastAPI, httpx.AsyncClient]) -> None:
        _, client = spa
        response = await client.get("/apiary")
        assert response.status_code == 200
        assert response.text == "<html>spa</html>"

    @pytest.mark.parametrize("path", ["/foo%00bar", "/%00", "/assets%00/x.js"])
    async def test_null_byte_falls_back_to_index(
        self, spa: tuple[fastapi.FastAPI, httpx.AsyncClient], path: str
    ) -> None:
        _, client = spa
        response = await client.get(path)
        assert response.status_code == 200
        assert response.text == "<html>spa</html>"

    async def test_directory_falls_back_to_index(self, spa: tuple[fastapi.FastAPI, httpx.AsyncClient]) -> None:
        _, client = spa
        response = await client.get("/assets")
        assert response.status_code == 200
        assert response.text == "<html>spa</html>"

    async def test_traversal_falls_back_to_index(
        self, spa: tuple[fastapi.FastAPI, httpx.AsyncClient], tmp_path: pathlib.Path
    ) -> None:
        (tmp_path / "secret.txt").write_text("nope")
        _, client = spa
        response = await client.get("/%2e%2e/secret.txt")
        assert response.status_code == 200
        assert response.text == "<html>spa</html>"
