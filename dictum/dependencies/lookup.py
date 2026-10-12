import typing as t

import fastapi

from dictum.services.lookup import LookupService


def get_lookup(request: fastapi.Request) -> LookupService:
    """Provider chain built at startup."""
    return request.app.state.lookup


LookupDep = t.Annotated[LookupService, fastapi.Depends(get_lookup)]
