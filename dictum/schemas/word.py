import datetime as dt
import typing as t

import pydantic
import sqlmodel

from dictum.enums import SearchHitKind
from dictum.enums import WordStatus
from dictum.models.word import PART_OF_SPEECH_MAX_LENGTH
from dictum.models.word import SPELLING_MAX_LENGTH
from dictum.models.word import TEXT_MAX_LENGTH
from dictum.models.word import DefinitionBase
from dictum.models.word import ExampleBase
from dictum.models.word import LexemeBase
from dictum.models.word import SpellingBase
from dictum.models.word import WordBase
from dictum.schemas.factory import partial_model
from dictum.services import spelling as spelling_service
from dictum.services.text import normalize_part_of_speech

if t.TYPE_CHECKING:
    from dictum.models.word import Lexeme
    from dictum.models.word import Word

STRICT: t.Final = pydantic.ConfigDict(extra="forbid")
"""Input schemas reject unknown keys, so a mislabelled payload fails loudly instead of losing fields."""


class SpellingIn(SpellingBase):
    """One spelling or phrase; whitespace is normalised, the vault separator ` / ` is rejected."""

    model_config = STRICT

    text: str = sqlmodel.Field(min_length=1, max_length=SPELLING_MAX_LENGTH)

    @pydantic.field_validator("text")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return spelling_service.validate(text=value)


class SpellingOut(SpellingBase):
    id: int


class ExampleIn(ExampleBase):
    """Example payload; order in the list is the stored position."""

    model_config = STRICT

    text: str = sqlmodel.Field(min_length=1, max_length=TEXT_MAX_LENGTH)


class ExampleOut(ExampleBase):
    id: int
    position: int


class DefinitionIn(DefinitionBase):
    """Definition payload with the examples that illustrate it."""

    model_config = STRICT

    text: str = sqlmodel.Field(min_length=1, max_length=TEXT_MAX_LENGTH)
    examples: list[ExampleIn] = sqlmodel.Field(default_factory=list)


class DefinitionOut(DefinitionBase):
    id: int
    position: int
    examples: list[ExampleOut]


class LexemeIn(LexemeBase):
    """One part of speech with its definitions; the label is case folded so `Noun` equals `noun`."""

    model_config = STRICT

    part_of_speech: str = sqlmodel.Field(min_length=1, max_length=PART_OF_SPEECH_MAX_LENGTH)
    definitions: list[DefinitionIn] = sqlmodel.Field(default_factory=list)

    @pydantic.field_validator("part_of_speech")
    @classmethod
    def _normalize(cls, value: str) -> str:
        normalized = normalize_part_of_speech(text=value)
        if not normalized:
            msg = "part of speech must contain visible characters"
            raise ValueError(msg)
        return normalized


class LexemeOut(LexemeBase):
    id: int
    position: int
    definitions: list[DefinitionOut]


class WordCreate(WordBase):
    """Full word payload used by POST and PUT (children fully replaced).

    `examples` at word level are loose sentences not tied to one definition.
    """

    model_config = STRICT

    spellings: list[SpellingIn] = sqlmodel.Field(min_length=1)
    lexemes: list[LexemeIn] = sqlmodel.Field(default_factory=list)
    examples: list[ExampleIn] = sqlmodel.Field(default_factory=list)

    @pydantic.field_validator("spellings")
    @classmethod
    def _unique_spellings(cls, value: list[SpellingIn]) -> list[SpellingIn]:
        seen: set[str] = set()
        for s in value:
            k = spelling_service.key(text=s.text)
            if k in seen:
                msg = f"duplicate spelling: {s.text}"
                raise ValueError(msg)
            seen.add(k)
        return value

    @pydantic.field_validator("lexemes")
    @classmethod
    def _unique_parts_of_speech(cls, value: list[LexemeIn]) -> list[LexemeIn]:
        seen: set[str] = set()
        for lexeme in value:
            if lexeme.part_of_speech in seen:
                msg = f"duplicate part of speech: {lexeme.part_of_speech}"
                raise ValueError(msg)
            seen.add(lexeme.part_of_speech)
        return value


WordUpdate = partial_model(base=WordBase, name="WordUpdate")


class WordResponse(WordBase):
    """Full word as stored; `examples` holds only the loose sentences, the rest sit under their definition."""

    id: int
    created_at: dt.datetime
    updated_at: dt.datetime
    spellings: list[SpellingOut]
    lexemes: list[LexemeOut]
    examples: list[ExampleOut]

    @classmethod
    def from_word(cls, *, word: Word) -> t.Self:
        """Build the response from a fully loaded ORM word."""
        return cls(
            id=word.id,
            created_at=word.created_at,
            updated_at=word.updated_at,
            notes=word.notes,
            status=word.status,
            spellings=[SpellingOut.model_validate(s) for s in word.spellings],
            lexemes=[LexemeOut.model_validate(lexeme) for lexeme in word.lexemes],
            examples=[ExampleOut.model_validate(e) for e in word.examples if e.definition_id is None],
        )


class WordSummary(pydantic.BaseModel):
    """Compact row for lists and search results."""

    id: int
    spellings: list[SpellingOut]
    parts_of_speech: list[str]
    status: WordStatus
    definition_count: int = 0

    @classmethod
    def from_word(cls, *, word: Word) -> t.Self:
        """Build the summary from a fully loaded ORM word."""
        lexemes: list[Lexeme] = word.lexemes
        return cls(
            id=word.id,
            spellings=[SpellingOut.model_validate(s) for s in word.spellings],
            parts_of_speech=[lexeme.part_of_speech for lexeme in lexemes],
            status=word.status,
            definition_count=sum(len(lexeme.definitions) for lexeme in lexemes),
        )


class WordPage(pydantic.BaseModel):
    items: list[WordSummary]
    total: int
    offset: int
    limit: int


class SearchHit(pydantic.BaseModel):
    """One search result; `matched` is the spelling or the definition text that scored."""

    word: WordSummary
    matched: str
    score: float
    kind: SearchHitKind


class SearchResponse(pydantic.BaseModel):
    query: str
    hits: list[SearchHit]


class Stats(pydantic.BaseModel):
    words: int
    inbox: int
    definitions: int
    examples: int
