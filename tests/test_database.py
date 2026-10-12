import datetime as dt
import typing as t

import fastapi
import pytest
import sqlalchemy as sa
import sqlalchemy.ext.asyncio as sa_async
from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect

from dictum.database import Database
from dictum.dependencies.db import get_database
from dictum.models.base import UTCDateTime
from dictum.models.word import Word
from tests.conftest import make_settings

if t.TYPE_CHECKING:
    import pathlib

    import httpx
    from pytest_mock import MockerFixture

    from dictum.settings import Settings


class TestDatabase:
    async def test_session_commits_on_success(self, db: Database) -> None:
        async with db.session() as session:
            session.add(Word())
        async with db.session() as session:
            total = await session.scalar(sa.select(sa.func.count()).select_from(Word))
        assert total == 1

    async def test_session_rolls_back_on_error(self, db: Database, mocker: MockerFixture) -> None:
        rollback = mocker.spy(sa_async.AsyncSession, "rollback")

        async def add_then_fail() -> None:
            async with db.session() as session:
                session.add(Word())
                await session.flush()
                msg = "boom"
                raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="boom"):
            await add_then_fail()
        assert rollback.call_count == 1
        async with db.session() as session:
            total = await session.scalar(sa.select(sa.func.count()).select_from(Word))
        assert total == 0

    async def test_pragmas_applied(self, db: Database) -> None:
        async with db.engine.connect() as conn:
            foreign_keys = await conn.scalar(sa.text("PRAGMA foreign_keys"))
            journal = await conn.scalar(sa.text("PRAGMA journal_mode"))
        assert foreign_keys == 1
        assert journal == "wal"

    async def test_dispose(self, settings: Settings) -> None:
        database = Database(settings=settings)
        async with database.engine.connect() as conn:
            assert await conn.scalar(sa.text("SELECT 1")) == 1
        await database.dispose()
        assert database.engine.pool.checkedin() == 0

    async def test_echo_follows_is_local(self, tmp_path: pathlib.Path) -> None:
        database = Database(settings=make_settings(tmp_path=tmp_path, environment="local"))
        assert database.engine.echo is True
        await database.dispose()


class TestGetDb:
    async def test_get_database_returns_app_state(self, app: fastapi.FastAPI, db: Database) -> None:
        request = fastapi.Request({"type": "http", "app": app, "headers": []})
        assert get_database(request=request) is db

    async def test_rollback_on_handler_error(self, client: httpx.AsyncClient, mocker: MockerFixture) -> None:
        mocker.patch.object(sa_async.AsyncSession, "execute", side_effect=RuntimeError("db down"))
        rollback = mocker.spy(sa_async.AsyncSession, "rollback")
        close = mocker.spy(sa_async.AsyncSession, "close")
        with pytest.raises(RuntimeError, match="db down"):
            await client.get("/api/health")
        assert rollback.call_count == 1
        assert close.call_count == 1

    async def test_commit_on_success(self, client: httpx.AsyncClient, mocker: MockerFixture) -> None:
        commit = mocker.spy(sa_async.AsyncSession, "commit")
        response = await client.get("/api/health")
        assert response.status_code == 200
        assert commit.call_count == 1


class TestUTCDateTime:
    def test_bind_normalises_to_utc(self) -> None:
        plus_two = dt.timezone(dt.timedelta(hours=2))
        value = dt.datetime(2026, 10, 8, 12, 0, tzinfo=plus_two)
        stored = UTCDateTime().process_bind_param(value, sqlite_dialect())
        assert stored == dt.datetime(2026, 10, 8, 10, 0, tzinfo=dt.UTC)
        assert stored.tzinfo is dt.UTC

    def test_bind_passes_none_and_naive(self) -> None:
        naive = dt.datetime(2026, 10, 8, 12, 0)  # ruff: ignore[call-datetime-without-tzinfo]
        assert UTCDateTime().process_bind_param(None, sqlite_dialect()) is None
        assert UTCDateTime().process_bind_param(naive, sqlite_dialect()) is naive

    def test_result_attaches_utc_to_naive(self) -> None:
        naive = dt.datetime(2026, 10, 8, 12, 0)  # ruff: ignore[call-datetime-without-tzinfo]
        loaded = UTCDateTime().process_result_value(naive, sqlite_dialect())
        assert loaded == naive.replace(tzinfo=dt.UTC)

    def test_result_passes_none_and_aware(self) -> None:
        aware = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.UTC)
        assert UTCDateTime().process_result_value(None, sqlite_dialect()) is None
        assert UTCDateTime().process_result_value(aware, sqlite_dialect()) is aware


class TestSavepoints:
    async def test_nested_rollback_does_not_commit_outer(self, db: Database) -> None:
        session = db.session_factory()
        async with session.begin_nested():
            session.add(Word())
        await session.rollback()
        await session.close()
        async with db.session() as check:
            total = await check.scalar(sa.select(sa.func.count()).select_from(Word))
        assert total == 0
