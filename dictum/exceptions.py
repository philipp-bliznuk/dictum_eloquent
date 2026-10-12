import fastapi


class AppBaseError(fastapi.HTTPException):
    """Base for all domain errors; subclasses pin `status_code`."""

    status_code: int = fastapi.status.HTTP_500_INTERNAL_SERVER_ERROR
    default_detail: str = "Internal error"

    def __init__(self, *, detail: str | None = None, headers: dict[str, str] | None = None) -> None:
        super().__init__(status_code=self.status_code, detail=detail or self.default_detail, headers=headers)


class NotFoundError(AppBaseError):
    """Generic 404."""

    status_code = fastapi.status.HTTP_404_NOT_FOUND
    default_detail = "Not found"


class WordNotFoundError(NotFoundError):
    """404 for words."""

    default_detail = "Word not found"


class AudioNotFoundError(NotFoundError):
    """404 when no provider offers pronunciation audio for a word."""

    default_detail = "No pronunciation audio available"


class SpellingConflictError(AppBaseError):
    """Spelling already belongs to another word; `existing_word_id` points at it."""

    status_code = fastapi.status.HTTP_409_CONFLICT
    default_detail = "Spelling already exists"

    def __init__(self, *, spelling: str, existing_word_id: int) -> None:
        self.spelling = spelling
        self.existing_word_id = existing_word_id
        super().__init__(
            detail=f"Spelling '{spelling}' already belongs to word {existing_word_id}",
            headers={"Location": f"/api/words/{existing_word_id}"},
        )


class ValidationError(AppBaseError):
    """422 raised from domain logic (outside pydantic)."""

    status_code = fastapi.status.HTTP_422_UNPROCESSABLE_CONTENT
    default_detail = "Invalid input"


class UpstreamError(AppBaseError):
    """Dictionary / AI provider failed or is not configured."""

    status_code = fastapi.status.HTTP_502_BAD_GATEWAY
    default_detail = "Upstream provider failed"


class ProviderNotConfiguredError(AppBaseError):
    """503 when an optional provider has no credentials."""

    status_code = fastapi.status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "Provider not configured"
