import typing as t

import fastapi

from dictum.exceptions import ProviderNotConfiguredError
from dictum.services.ai import AIService


def get_ai(request: fastapi.Request) -> AIService:
    """Bedrock service built at startup; 503 while credentials are missing."""
    service: AIService | None = request.app.state.ai
    if service is None:
        raise ProviderNotConfiguredError(
            detail="AI generation is disabled: set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY"
        )
    return service


AIDep = t.Annotated[AIService, fastapi.Depends(get_ai)]
