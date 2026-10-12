import asyncio
import logging.config
import typing as t

import sqlalchemy as sa
import sqlalchemy.ext.asyncio as sa_async
from alembic import context

from dictum import models
from dictum.models.base import UTCDateTime
from dictum.settings import get_settings

if t.TYPE_CHECKING:
    from alembic.autogenerate.api import AutogenContext

config = context.config
if config.config_file_name is not None:
    logging.config.fileConfig(config.config_file_name)

target_metadata = models.metadata


def _url() -> str:
    return (
        config.get_main_option("sqlalchemy.url")
        if context.get_x_argument(as_dictionary=True).get("ini")
        else get_settings().database_url
    )


def _render_item(type_: str, obj: object, _autogen_context: AutogenContext) -> str | t.Literal[False]:
    """Render app-level type decorators as their plain SQLAlchemy impl so migrations never import `dictum`."""
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
        render_item=_render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: sa.Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
        render_item=_render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = sa_async.create_async_engine(_url(), poolclass=sa.pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
