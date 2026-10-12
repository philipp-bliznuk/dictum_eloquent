import typing as t

import pydantic

from dictum.models.word import SPELLING_MAX_LENGTH
from dictum.models.word import TEXT_MAX_LENGTH
from dictum.schemas.lookup import DraftEntry
from dictum.schemas.lookup import DraftExample
from dictum.schemas.lookup import LookupRequest
from dictum.services import spelling as spelling_service

MAX_DEFINITIONS: t.Final = 20
"""Definitions one examples request may carry; keeps a single model call well inside its output budget."""

MAX_EXAMPLES_PER_DEFINITION: t.Final = 5

MAX_SENSES_PER_POS: t.Final = 8
"""Safety ceiling for AI-drafted definitions per part of speech; the prompt asks for every distinct sense."""

MAX_PARTS_OF_SPEECH: t.Final = 10

DefinitionText = t.Annotated[
    str, pydantic.StringConstraints(strip_whitespace=True, min_length=1, max_length=TEXT_MAX_LENGTH)
]

PartOfSpeech = t.Annotated[str, pydantic.StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


class ExamplesRequest(pydantic.BaseModel):
    """Definitions (in display order) that need example sentences."""

    word: str = pydantic.Field(min_length=1, max_length=SPELLING_MAX_LENGTH)
    part_of_speech: str = pydantic.Field(min_length=1, max_length=64)
    definitions: list[DefinitionText] = pydantic.Field(min_length=1, max_length=MAX_DEFINITIONS)
    per_definition: int = pydantic.Field(default=2, ge=1, le=MAX_EXAMPLES_PER_DEFINITION)

    @pydantic.field_validator("word")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return spelling_service.validate(text=value)


class ExamplesResponse(pydantic.BaseModel):
    examples: list[list[DraftExample]]
    """Aligned with the request's `definitions`; a definition the model skipped gets an empty list."""


class DefinitionsRequest(LookupRequest):
    """Word or phrase to draft from scratch, optionally steered by what the reader already noted."""

    hints: list[DefinitionText] = pydantic.Field(default_factory=list, max_length=MAX_DEFINITIONS)
    """Senses noted earlier (any wording); the model covers them first, rewritten cleanly, then adds the rest."""
    parts_of_speech: list[PartOfSpeech] = pydantic.Field(default_factory=list, max_length=MAX_PARTS_OF_SPEECH)
    """Restrict drafting to these parts of speech, e.g. `["noun"]` for a run-on the dictionary left undefined."""
    per_definition: int = pydantic.Field(default=2, ge=1, le=MAX_EXAMPLES_PER_DEFINITION)


class DefinitionsResponse(pydantic.BaseModel):
    query: str
    found: bool
    """False when the model does not recognise the text (likely a misspelling); nothing is drafted then."""
    entries: list[DraftEntry]
