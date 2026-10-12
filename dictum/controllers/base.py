import typing as t

import sqlalchemy as sa
import sqlmodel

from dictum.exceptions import NotFoundError
from dictum.models.base import BaseModel

if t.TYPE_CHECKING:
    import collections.abc as cabc

    import sqlalchemy.ext.asyncio as sa_async


class Controller[ModelType: BaseModel]:
    """Generic CRUD over one table model. One singleton per model."""

    model: type[ModelType]
    not_found: type[NotFoundError] = NotFoundError

    def __init__(self, *, model: type[ModelType]) -> None:
        self.model = model

    async def get(self, *, session: sa_async.AsyncSession, object_id: int) -> ModelType:
        """Fetch by primary key or raise `not_found`."""
        obj = await session.get(self.model, object_id)
        if obj is None:
            raise self.not_found
        return obj

    async def get_many(
        self,
        *,
        session: sa_async.AsyncSession,
        object_ids: cabc.Iterable[int],
    ) -> dict[int, ModelType]:
        """Rows for `object_ids` keyed by id in one query; ids that no longer exist are absent."""
        ids = list(object_ids)
        if not ids:
            return {}
        rows = await session.scalars(sqlmodel.select(self.model).where(self.model.id.in_(ids)))
        return {row.id: row for row in rows if row.id is not None}

    async def list(
        self,
        *,
        session: sa_async.AsyncSession,
        offset: int = 0,
        limit: int = 50,
        where: cabc.Sequence[sa.ColumnElement[bool]] = (),
    ) -> tuple[list[ModelType], int]:
        """Page of rows (newest first) plus total count for the same filter."""
        stmt = sqlmodel.select(self.model).where(*where)
        total = await session.scalar(sqlmodel.select(sa.func.count()).select_from(stmt.subquery()))
        rows = await session.scalars(stmt.order_by(self.model.id.desc()).offset(offset).limit(limit))
        return list(rows), total or 0

    async def create(self, *, session: sa_async.AsyncSession, obj: ModelType) -> ModelType:
        """Persist a new instance and flush to get its id."""
        session.add(obj)
        await session.flush()
        return obj

    async def update(self, *, session: sa_async.AsyncSession, obj: ModelType, values: dict[str, t.Any]) -> ModelType:
        """Apply `values` (already `exclude_unset`) to `obj`."""
        for key, value in values.items():
            setattr(obj, key, value)
        await session.flush()
        return obj

    async def delete(self, *, session: sa_async.AsyncSession, obj: ModelType) -> None:
        """Delete and flush."""
        await session.delete(obj)
        await session.flush()

    async def count(self, *, session: sa_async.AsyncSession) -> int:
        """Total rows."""
        return await session.scalar(sqlmodel.select(sa.func.count()).select_from(self.model)) or 0
