import datetime as dt
import typing as t

import sqlalchemy as sa
import sqlmodel


def utcnow() -> dt.datetime:
    """Timezone-aware UTC now."""
    return dt.datetime.now(tz=dt.UTC)


class UTCDateTime(sa.types.TypeDecorator[dt.datetime]):
    """Timezone-aware datetime that survives SQLite, which drops the offset on storage."""

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    @t.override
    def process_bind_param(self, value: dt.datetime | None, dialect: sa.Dialect) -> dt.datetime | None:
        if value is None or value.tzinfo is None:
            return value
        return value.astimezone(dt.UTC)

    @t.override
    def process_result_value(self, value: dt.datetime | None, dialect: sa.Dialect) -> dt.datetime | None:
        if value is None or value.tzinfo is not None:
            return value
        return value.replace(tzinfo=dt.UTC)


NAMING_CONVENTION: t.Final = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)


class BaseModel(sqlmodel.SQLModel):
    """Common columns for every table model."""

    metadata = metadata

    id: int | None = sqlmodel.Field(default=None, primary_key=True)
    created_at: dt.datetime = sqlmodel.Field(
        default_factory=utcnow,
        sa_type=UTCDateTime,
        nullable=False,
    )
    updated_at: dt.datetime = sqlmodel.Field(
        default_factory=utcnow,
        sa_type=UTCDateTime,
        nullable=False,
        sa_column_kwargs={"onupdate": utcnow},
    )
