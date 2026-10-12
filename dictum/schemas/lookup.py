import pydantic

from dictum.enums import DefinitionSource
from dictum.enums import ExampleSource
from dictum.enums import ProviderOutcome
from dictum.models.word import SPELLING_MAX_LENGTH
from dictum.schemas.word import SpellingIn
from dictum.services import spelling as spelling_service


class LookupRequest(pydantic.BaseModel):
    """Word or phrase to look up; normalised like a spelling (whitespace collapsed, no ` / `)."""

    text: str = pydantic.Field(min_length=1, max_length=SPELLING_MAX_LENGTH)

    @pydantic.field_validator("text")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return spelling_service.validate(text=value)


class DraftExample(pydantic.BaseModel):
    text: str
    source: ExampleSource


class DraftDefinition(pydantic.BaseModel):
    text: str
    source: DefinitionSource
    examples: list[DraftExample] = []


class DraftEntry(pydantic.BaseModel):
    """One dictionary entry (headword + part of speech) for the user to review; nothing is persisted."""

    spellings: list[SpellingIn]
    part_of_speech: str
    pronunciation: str | None = None
    audio_path: str | None = None
    definitions: list[DraftDefinition]
    source: DefinitionSource


class ProviderAttempt(pydantic.BaseModel):
    provider: str
    outcome: ProviderOutcome
    detail: str | None = None


class LookupResponse(pydantic.BaseModel):
    query: str
    found: bool
    entries: list[DraftEntry]
    suggestions: list[str]
    attempts: list[ProviderAttempt]
    existing_word_id: int | None = None
    """Word already holding the query (or a drafted headword), so the UI can link instead of duplicating."""
