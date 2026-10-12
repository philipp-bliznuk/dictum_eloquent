"""Request-scoped database session: the transaction must commit before the response is sent."""

import collections.abc as cabc
import typing as t

import httpx
import pytest
import sqlalchemy as sa
import sqlalchemy.orm as sa_orm

from dictum.dependencies import AsyncDBSession
from tests.conftest import word_payload

if t.TYPE_CHECKING:
    import fastapi

Message = dict[str, t.Any]
Send = cabc.Callable[[Message], cabc.Awaitable[None]]
Receive = cabc.Callable[[], cabc.Awaitable[Message]]
Asgi = cabc.Callable[[Message, Receive, Send], cabc.Awaitable[None]]


class _CommitSpy:
    """Counts ORM commits and records the count seen when each response starts."""

    def __init__(self) -> None:
        self.commits = 0
        self.at_response_start: list[int] = []

    def after_commit(self, _session: sa_orm.Session) -> None:
        self.commits += 1

    def wrap(self, app: fastapi.FastAPI) -> Asgi:
        async def asgi(scope: Message, receive: Receive, send: Send) -> None:
            async def spying_send(message: Message) -> None:
                if message["type"] == "http.response.start":
                    self.at_response_start.append(self.commits)
                await send(message)

            await app(scope, receive, spying_send)

        return asgi


@pytest.fixture
def commit_spy() -> cabc.Iterator[_CommitSpy]:
    spy = _CommitSpy()
    sa.event.listen(sa_orm.Session, "after_commit", spy.after_commit)
    yield spy
    sa.event.remove(sa_orm.Session, "after_commit", spy.after_commit)


def test_session_dependency_is_function_scoped() -> None:
    """FastAPI's default `request` scope would run the commit after the response was sent."""
    dependency = AsyncDBSession.__metadata__[0]
    assert dependency.scope == "function"


async def test_write_commits_before_response_starts(app: fastapi.FastAPI, commit_spy: _CommitSpy) -> None:
    transport = httpx.ASGITransport(app=commit_spy.wrap(app))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/words", json=word_payload())
        assert response.status_code == 201
        assert commit_spy.at_response_start == [1]
        follow_up = await client.get("/api/words/by-spelling", params={"text": "abandon"})
    assert follow_up.status_code == 200
    assert follow_up.json()["id"] == response.json()["id"]
    assert commit_spy.commits == 2  # the read-only request still ends its transaction
