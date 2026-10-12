import typing as t

import fastapi
import sqlalchemy.ext.asyncio as sa_async

from dictum.database import Database

if t.TYPE_CHECKING:
    import collections.abc as cabc

SEARCH_DIRTY: t.Final = "search_dirty"
"""`session.info` flag; the search index is invalidated only after the request's transaction committed."""


def get_database(*, request: fastapi.Request) -> Database:
    """Database owned by the app (set in lifespan)."""
    return request.app.state.db


def mark_search_dirty(*, session: sa_async.AsyncSession) -> None:
    """Ask `get_db` to invalidate the search index once this session commits."""
    session.info[SEARCH_DIRTY] = True


async def get_db(*, request: fastapi.Request) -> cabc.AsyncIterator[sa_async.AsyncSession]:
    """Per-request transactional session: commit on success, rollback on error.

    Registered with ``scope="function"`` (see `AsyncDBSession`) so the commit runs
    right after the endpoint returns and before the response is sent. FastAPI's default
    ``scope="request"`` would commit after the response, so a client could read stale
    data on its next request and a commit failure would arrive after a 2xx was sent.

    The search index is invalidated only after a successful commit, so concurrent
    searches never cache a snapshot taken from an uncommitted write.

    Yields:
        Open session for the current request.
    """
    db: Database = request.app.state.db
    session = db.session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
    if session.info.get(SEARCH_DIRTY):
        request.app.state.search.invalidate()


AsyncDBSession = t.Annotated[sa_async.AsyncSession, fastapi.Depends(get_db, scope="function")]
DatabaseDep = t.Annotated[Database, fastapi.Depends(get_database)]
