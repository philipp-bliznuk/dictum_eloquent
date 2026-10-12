import fastapi
import pydantic
import sqlalchemy as sa

from dictum.dependencies.db import AsyncDBSession

router = fastapi.APIRouter(prefix="/health", tags=["service"])


class Health(pydantic.BaseModel):
    """Healthcheck response plus the optional capabilities the UI should offer."""

    status: str
    database: str
    providers: list[str]
    """Dictionary providers in lookup order."""
    ai: bool
    """True when the `/generate/*` endpoints have Bedrock credentials."""


@router.get("", response_model=Health, status_code=fastapi.status.HTTP_200_OK)
async def healthcheck(*, request: fastapi.Request, session: AsyncDBSession) -> Health:
    """Liveness + database round trip."""
    await session.execute(sa.text("SELECT 1"))
    return Health(
        status="ok",
        database="ok",
        providers=[p.name for p in request.app.state.lookup.providers],
        ai=request.app.state.ai is not None,
    )
