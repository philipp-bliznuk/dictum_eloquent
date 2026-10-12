import contextlib
import typing as t

import sqlalchemy as sa
import sqlalchemy.ext.asyncio as sa_async

if t.TYPE_CHECKING:
    import collections.abc as cabc

    import sqlalchemy.engine.interfaces as sa_interfaces
    import sqlalchemy.pool as sa_pool

    from dictum.settings import Settings


def _configure_sqlite_connection(
    dbapi_connection: sa_interfaces.DBAPIConnection,
    _record: sa_pool.ConnectionPoolEntry,
) -> None:
    # pysqlite only emits BEGIN before DML; a SAVEPOINT issued first opens the transaction itself
    # and its RELEASE then acts as COMMIT. Take over transaction control so savepoints nest properly.
    dbapi_connection.isolation_level = None  # type: ignore[attr-defined]
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def _begin_transaction(conn: sa.Connection) -> None:
    conn.exec_driver_sql("BEGIN")


class Database:
    """Owns the async engine and hands out sessions."""

    def __init__(self, *, settings: Settings) -> None:
        self.engine = sa_async.create_async_engine(
            settings.database_url,
            echo=settings.is_local,
            connect_args={"check_same_thread": False},
        )
        sa.event.listen(self.engine.sync_engine, "connect", _configure_sqlite_connection)
        sa.event.listen(self.engine.sync_engine, "begin", _begin_transaction)
        self.session_factory = sa_async.async_sessionmaker(
            self.engine,
            expire_on_commit=False,
            autoflush=False,
        )

    @contextlib.asynccontextmanager
    async def session(self) -> cabc.AsyncGenerator[sa_async.AsyncSession]:
        """Transactional session: commit on success, rollback on error.

        Yields:
            Open session bound to this engine.
        """
        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def dispose(self) -> None:
        """Close engine connections."""
        await self.engine.dispose()
