import collections
import json
import os
import pathlib
import typing as t
import urllib.parse

import botocore.exceptions
import httpx
import pytest

from app import Backends
from app import create_app
from dictum.models import metadata
from dictum.services.audio import AudioCache
from dictum.settings import Settings

if t.TYPE_CHECKING:
    import collections.abc as cabc

    import fastapi
    import sqlalchemy.ext.asyncio as sa_async

    from dictum.database import Database
    from dictum.services.ai import BedrockClient

FIXTURES: t.Final = pathlib.Path(__file__).parent / "fixtures"
MW_API_KEY: t.Final = "test-key"
MW_BASE_URL: t.Final = "https://mw.test/json/"
MW_AUDIO_BASE_URL: t.Final = "https://audio.test/mp3/"
FREE_DICTIONARY_BASE_URL: t.Final = "https://free.test/en/"
MP3_BYTES: t.Final = b"ID3\x03\x00\x00\x00\x00\x00\x00"
AWS_ACCESS_KEY_ID: t.Final = "AKIATEST"
AWS_SECRET_ACCESS_KEY: t.Final = "hunter2-xyz"
"""Fake Bedrock credentials; distinctive so tests can prove they never leak into reprs or responses."""
AWS_ENV_VARS: t.Final = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION", "BEDROCK_MODEL_ID")

type Responder = cabc.Callable[[httpx.Request], httpx.Response]
type Json = dict[str, t.Any]


def make_settings(*, tmp_path: pathlib.Path, **overrides: t.Any) -> Settings:
    """Settings bound to a throwaway file DB under `tmp_path` and fake upstream hosts; never reads `.env`."""
    values: dict[str, t.Any] = {
        "environment": "test",
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        "data_dir": tmp_path / "data",
        "ui_dist": tmp_path / "dist",
        "mw_base_url": MW_BASE_URL,
        "mw_audio_base_url": MW_AUDIO_BASE_URL,
        "free_dictionary_base_url": FREE_DICTIONARY_BASE_URL,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def make_ai_settings(*, tmp_path: pathlib.Path, **overrides: t.Any) -> Settings:
    """`make_settings` plus fake Bedrock credentials, so `Settings.ai_enabled` is True."""
    return make_settings(
        tmp_path=tmp_path,
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
        **overrides,
    )


NESTED_EXAMPLE: t.Final = "They *abandon* the plan."
"""Example sentence nested under the default definition of `word_payload`."""
LOOSE_EXAMPLE: t.Final = "with reckless *abandon*"
"""Word-level (loose) example of `word_payload`; not tied to one definition."""


def lexeme_payload(**overrides: t.Any) -> dict[str, t.Any]:
    """Valid `LexemeIn` body: the verb `abandon` with one definition and one nested example; override any key."""
    payload: dict[str, t.Any] = {
        "part_of_speech": "verb",
        "pronunciation": "uh-BAN-dun",
        "audio_path": None,
        "definitions": [
            {"text": "to give up", "source": "user", "examples": [{"text": NESTED_EXAMPLE, "source": "user"}]},
        ],
    }
    payload.update(overrides)
    return payload


def word_payload(**overrides: t.Any) -> dict[str, t.Any]:
    """Valid `WordCreate` body (one lexeme, one nested and one loose example); override any top-level key."""
    payload: dict[str, t.Any] = {
        "spellings": [{"text": "Abandon", "kind": "primary"}],
        "notes": None,
        "status": "learned",
        "lexemes": [lexeme_payload()],
        "examples": [{"text": LOOSE_EXAMPLE, "source": "vault"}],
    }
    payload.update(overrides)
    return payload


def legacy_word_payload(**overrides: t.Any) -> dict[str, t.Any]:
    """Valid version 1 word (`LegacyWord`): grammar on the word, examples linked by `definition_index`."""
    payload: dict[str, t.Any] = {
        "spellings": [{"text": "Abandon", "kind": "primary"}],
        "part_of_speech": "verb",
        "pronunciation": "uh-BAN-dun",
        "audio_path": None,
        "notes": None,
        "status": "learned",
        "definitions": [{"text": "to give up", "source": "user"}],
        "examples": [
            {"text": NESTED_EXAMPLE, "source": "user", "definition_index": 0},
            {"text": LOOSE_EXAMPLE, "source": "vault", "definition_index": None},
        ],
    }
    payload.update(overrides)
    return payload


def mw_fixture(*, name: str) -> list[t.Any]:
    """Recorded Merriam-Webster response from `tests/fixtures/mw_<name>.json`."""
    return json.loads((FIXTURES / f"mw_{name}.json").read_text(encoding="utf-8"))


def mw_url(*, query: str) -> str:
    """Collegiate endpoint for `query` as `MWProvider` builds it (query string excluded)."""
    return f"{MW_BASE_URL}{urllib.parse.quote(query, safe='')}"


def mw_audio_url(*, ref: str, subdir: str | None = None) -> str:
    """Media URL for an audio reference; the subdirectory defaults to the first letter."""
    return f"{MW_AUDIO_BASE_URL}{subdir or ref[0]}/{ref}.mp3"


def free_dictionary_url(*, query: str) -> str:
    """dictionaryapi.dev endpoint for `query` as `FreeDictionaryProvider` builds it."""
    return f"{FREE_DICTIONARY_BASE_URL}{urllib.parse.quote(query, safe='')}"


def bare_url(url: httpx.URL | str) -> str:
    """`url` without its query string; MW appends `?key=` so routes are matched on the path only."""
    return str(httpx.URL(url).copy_with(query=None))


class FakeUpstream:
    """In-memory stand-in for every outbound host; URLs without a route fail like an unreachable network."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._routes: dict[str, Responder] = {}

    def route(self, *, url: str, responder: Responder) -> None:
        """Call `responder` for each request to `url` (query string ignored)."""
        self._routes[bare_url(url)] = responder

    def json(self, *, url: str, payload: object, status_code: int = 200) -> None:
        """Serve `payload` as JSON."""
        self.route(url=url, responder=lambda _request: httpx.Response(status_code, json=payload))

    def text(self, *, url: str, body: str = "", status_code: int = 200) -> None:
        """Serve a plain body (invalid JSON, HTML error pages, empty 500s)."""
        self.route(url=url, responder=lambda _request: httpx.Response(status_code, text=body))

    def audio(self, *, url: str, content: bytes = MP3_BYTES, content_type: str = "audio/mpeg") -> None:
        """Serve binary audio."""
        headers = {"content-type": content_type}
        self.route(url=url, responder=lambda _request: httpx.Response(200, content=content, headers=headers))

    def fail(self, *, url: str, exc: Exception) -> None:
        """Raise `exc` from the transport (timeouts, connection resets)."""

        def responder(_request: httpx.Request) -> httpx.Response:
            raise exc

        self.route(url=url, responder=responder)

    def requested(self, *, url: str) -> list[httpx.Request]:
        """Requests made to `url` so far (query string ignored)."""
        return [r for r in self.requests if bare_url(r.url) == bare_url(url)]

    @property
    def urls(self) -> list[str]:
        """Bare URLs in request order."""
        return [bare_url(r.url) for r in self.requests]

    def handler(self, request: httpx.Request) -> httpx.Response:
        """`httpx.MockTransport` entry point."""
        self.requests.append(request)
        responder = self._routes.get(bare_url(request.url))
        if responder is None:
            msg = f"no fake route for {request.url}"
            raise httpx.ConnectError(msg, request=request)
        return responder(request)


def tool_use_response(*, name: str, payload: object, stop_reason: str = "tool_use") -> Json:
    """Converse API answer whose single content block is the model calling tool `name` with `payload`."""
    return {
        "stopReason": stop_reason,
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"toolUse": {"toolUseId": "t1", "name": name, "input": payload}}],
            }
        },
        "usage": {"inputTokens": 1, "outputTokens": 1},
    }


def client_error(*, code: str, message: str = "boom", operation: str = "Converse") -> botocore.exceptions.ClientError:
    """AWS-side failure as botocore raises it; `message` is where the account ARN usually appears."""
    return botocore.exceptions.ClientError({"Error": {"Code": code, "Message": message}}, operation)


class FakeBedrock:
    """In-memory stand-in for the boto3 `bedrock-runtime` client; `converse` answers from a FIFO queue."""

    def __init__(self) -> None:
        self.calls: list[Json] = []
        self._queue: collections.deque[Json | Exception] = collections.deque()

    def queue(self, *items: Json | Exception) -> None:
        """Script the next `converse` calls: a dict is returned, an exception is raised."""
        self._queue.extend(items)

    @property
    def pending(self) -> int:
        """Scripted answers not consumed yet."""
        return len(self._queue)

    def converse(self, **kwargs: t.Any) -> Json:
        """Record the request and pop the next scripted answer; an empty queue is a test bug."""
        self.calls.append(kwargs)
        if not self._queue:
            msg = f"FakeBedrock: no response queued for call #{len(self.calls)}"
            raise AssertionError(msg)
        item = self._queue.popleft()
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def prompts(self) -> list[str]:
        """User prompt text of every call, in order."""
        return [call["messages"][0]["content"][0]["text"] for call in self.calls]

    @property
    def tools(self) -> list[str]:
        """Forced tool name of every call, in order."""
        return [call["toolConfig"]["toolChoice"]["tool"]["name"] for call in self.calls]

    @property
    def tool_specs(self) -> list[Json]:
        """Tool schema (`toolSpec`) offered in every call, in order; the same object when the service reuses it."""
        return [call["toolConfig"]["tools"][0]["toolSpec"] for call in self.calls]


def part_of_speech_enum(*, tool: Json) -> list[str]:
    """Allowed `part_of_speech` values in a `record_entries` tool schema."""
    return tool["inputSchema"]["json"]["properties"]["entries"]["items"]["properties"]["part_of_speech"]["enum"]


def definitions_schema(*, tool: Json) -> Json:
    """JSON schema of one entry's `definitions` array in a `record_entries` tool schema."""
    return tool["inputSchema"]["json"]["properties"]["entries"]["items"]["properties"]["definitions"]


def pytest_configure(config: pytest.Config) -> None:
    """Developer shells often export AWS credentials; they must not flip `Settings.ai_enabled` under tests."""
    for name in AWS_ENV_VARS:
        os.environ.pop(name, None)


@pytest.fixture
def settings(tmp_path: pathlib.Path) -> Settings:
    return make_settings(tmp_path=tmp_path)


@pytest.fixture
def upstream() -> FakeUpstream:
    return FakeUpstream()


@pytest.fixture
def transport(upstream: FakeUpstream) -> httpx.MockTransport:
    return httpx.MockTransport(upstream.handler)


@pytest.fixture
async def http(transport: httpx.MockTransport) -> cabc.AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=transport, timeout=1.0) as c:
        yield c


@pytest.fixture
def audio_cache(http: httpx.AsyncClient, tmp_path: pathlib.Path) -> AudioCache:
    return AudioCache(client=http, directory=tmp_path / "audio")


@pytest.fixture
def bedrock() -> BedrockClient | None:
    """Fake Bedrock client; `None` keeps AI disabled. Override per module together with AWS settings."""
    return None


@pytest.fixture
async def app(
    settings: Settings, transport: httpx.MockTransport, bedrock: BedrockClient | None
) -> cabc.AsyncIterator[fastapi.FastAPI]:
    application = create_app(settings=settings, backends=Backends(transport=transport, bedrock=bedrock))
    async with application.router.lifespan_context(application):
        async with application.state.db.engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
        yield application


@pytest.fixture
def db(app: fastapi.FastAPI) -> Database:
    return app.state.db


@pytest.fixture
async def session(db: Database) -> cabc.AsyncIterator[sa_async.AsyncSession]:
    async with db.session() as sess:
        yield sess


@pytest.fixture
async def client(app: fastapi.FastAPI) -> cabc.AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def word_id(client: httpx.AsyncClient) -> int:
    response = await client.post("/api/words", json=word_payload())
    assert response.status_code == 201
    return response.json()["id"]
