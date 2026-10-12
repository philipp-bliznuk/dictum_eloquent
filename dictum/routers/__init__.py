import typing as t

from dictum.routers import generate
from dictum.routers import lookup
from dictum.routers import search
from dictum.routers import transfer
from dictum.routers import words
from dictum.routers.service import healthcheck

if t.TYPE_CHECKING:
    import fastapi

registry: list[fastapi.APIRouter] = [
    healthcheck.router,
    words.router,
    search.router,
    lookup.router,
    generate.router,
    transfer.router,
]

__all__ = ["registry"]
