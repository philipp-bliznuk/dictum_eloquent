import dataclasses
import datetime as dt
import typing as t

import pydantic
import sqlalchemy.exc

from dictum.controllers.word import word_controller
from dictum.enums import ConflictPolicy
from dictum.enums import DefinitionSource
from dictum.enums import ExampleSource
from dictum.enums import WordStatus
from dictum.exceptions import SpellingConflictError
from dictum.exceptions import ValidationError
from dictum.models.word import NOTES_MAX_LENGTH
from dictum.models.word import PART_OF_SPEECH_MAX_LENGTH
from dictum.models.word import TEXT_MAX_LENGTH
from dictum.models.word import AudioPath
from dictum.schemas.word import STRICT
from dictum.schemas.word import SpellingIn
from dictum.schemas.word import WordCreate

if t.TYPE_CHECKING:
    import sqlalchemy.ext.asyncio as sa_async

    from dictum.models.word import Example
    from dictum.models.word import Lexeme
    from dictum.models.word import Word

IMPORT_VERSION: t.Final = 2
LEGACY_VERSION: t.Final = 1
"""Flat shape of the first release: one part of speech per word, examples linked by index."""
IMPORT_MAX_WORDS: t.Final = 20_000
"""Upper bound per import request; keeps a single oversized upload from exhausting the worker."""

type Json = dict[str, t.Any]


class WordImport(WordCreate):
    """`WordCreate` plus the creation time, so a round trip keeps the "Added" dates."""

    created_at: dt.datetime | None = None


class ImportFile(pydantic.BaseModel):
    """Request body of `POST /api/import` and response of `GET /api/export.json`."""

    model_config = STRICT

    version: t.Literal[2]
    words: list[WordImport] = pydantic.Field(max_length=IMPORT_MAX_WORDS)


class ExportFile(ImportFile):
    version: t.Literal[2] = IMPORT_VERSION


class LegacyDefinition(pydantic.BaseModel):
    model_config = STRICT

    text: str = pydantic.Field(min_length=1, max_length=TEXT_MAX_LENGTH)
    source: DefinitionSource = DefinitionSource.USER


class LegacyExample(pydantic.BaseModel):
    model_config = STRICT

    text: str = pydantic.Field(min_length=1, max_length=TEXT_MAX_LENGTH)
    source: ExampleSource = ExampleSource.USER
    definition_index: int | None = pydantic.Field(default=None, ge=0)


class LegacyWord(pydantic.BaseModel):
    """Version 1 word: grammatical fields on the word, examples linked to `definitions[i]`."""

    model_config = STRICT

    spellings: list[SpellingIn] = pydantic.Field(min_length=1)
    part_of_speech: str = pydantic.Field(min_length=1, max_length=PART_OF_SPEECH_MAX_LENGTH)
    pronunciation: str | None = pydantic.Field(default=None, max_length=255)
    audio_path: AudioPath | None = pydantic.Field(default=None, max_length=255)
    notes: str | None = pydantic.Field(default=None, max_length=NOTES_MAX_LENGTH)
    status: WordStatus = WordStatus.LEARNED
    definitions: list[LegacyDefinition] = pydantic.Field(default_factory=list)
    examples: list[LegacyExample] = pydantic.Field(default_factory=list)

    @pydantic.model_validator(mode="after")
    def _check_links(self) -> t.Self:
        n = len(self.definitions)
        bad = [e.definition_index for e in self.examples if e.definition_index is not None and e.definition_index >= n]
        if bad:
            msg = f"definition_index out of range: {bad}"
            raise ValueError(msg)
        return self


class LegacyImportFile(pydantic.BaseModel):
    """Version 1 export; accepted by `POST /api/import` and upgraded on the fly."""

    model_config = STRICT

    version: t.Literal[1]
    words: list[LegacyWord] = pydantic.Field(max_length=IMPORT_MAX_WORDS)


class ImportFailure(pydantic.BaseModel):
    """One word that could not be stored."""

    index: int
    spellings: list[str]
    error: str


class ImportReport(pydantic.BaseModel):
    """Outcome counters of an import run."""

    created: int = 0
    replaced: int = 0
    skipped: int = 0
    failures: list[ImportFailure] = pydantic.Field(default_factory=list)


def _upgrade_word(*, word: LegacyWord) -> Json:
    definitions: list[Json] = [{"text": d.text, "source": d.source, "examples": []} for d in word.definitions]
    loose: list[Json] = []
    for e in word.examples:
        item: Json = {"text": e.text, "source": e.source}
        if e.definition_index is None:
            loose.append(item)
        else:
            definitions[e.definition_index]["examples"].append(item)
    lexeme: Json = {
        "part_of_speech": word.part_of_speech,
        "pronunciation": word.pronunciation,
        "audio_path": word.audio_path,
        "definitions": definitions,
    }
    return {
        "spellings": [s.model_dump() for s in word.spellings],
        "notes": word.notes,
        "status": word.status,
        "lexemes": [lexeme],
        "examples": loose,
    }


def upgrade(*, legacy: LegacyImportFile) -> ImportFile:
    """Deterministic version 1 -> 2 conversion: the word's single part of speech becomes its only lexeme.

    Combined labels such as `noun / verb` stay one lexeme; splitting them needs a human or the AI.
    """
    words = [_upgrade_word(word=w) for w in legacy.words]
    try:
        return ImportFile.model_validate({"version": IMPORT_VERSION, "words": words})
    except pydantic.ValidationError as exc:
        details = "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors())
        raise ValidationError(detail=f"version 1 payload cannot be upgraded: {details}") from exc


def _example_payload(*, example: Example) -> Json:
    return {"text": example.text, "source": example.source}


def _lexeme_payload(*, lexeme: Lexeme) -> Json:
    return {
        "part_of_speech": lexeme.part_of_speech,
        "pronunciation": lexeme.pronunciation,
        "audio_path": lexeme.audio_path,
        "definitions": [
            {
                "text": d.text,
                "source": d.source,
                "examples": [_example_payload(example=e) for e in d.examples],
            }
            for d in lexeme.definitions
        ],
    }


def to_payload(*, word: Word) -> WordImport:
    """Convert a stored word into the import/export shape."""
    return WordImport(
        notes=word.notes,
        status=word.status,
        created_at=word.created_at,
        spellings=[{"text": s.text, "kind": s.kind} for s in word.spellings],
        lexemes=[_lexeme_payload(lexeme=lexeme) for lexeme in word.lexemes],
        examples=[_example_payload(example=e) for e in word.examples if e.definition_id is None],
    )


@dataclasses.dataclass(slots=True)
class Importer:
    """Imports words one by one; each in a savepoint so one failure does not poison the batch."""

    session: sa_async.AsyncSession
    policy: ConflictPolicy
    report: ImportReport = dataclasses.field(default_factory=ImportReport)

    async def run(self, *, data: ImportFile) -> ImportReport:
        """Import every word in `data` and return the report."""
        for index, payload in enumerate(data.words):
            await self._one(index=index, payload=payload)
        return self.report

    async def _one(self, *, index: int, payload: WordImport) -> None:
        try:
            async with self.session.begin_nested():
                await word_controller.create_from_payload(session=self.session, payload=payload)
            self.report.created += 1
        except SpellingConflictError as exc:
            await self._conflict(payload=payload, exc=exc)
        except sqlalchemy.exc.IntegrityError as exc:
            spellings = [s.text for s in payload.spellings]
            self.report.failures.append(ImportFailure(index=index, spellings=spellings, error=str(exc)))

    async def _conflict(self, *, payload: WordImport, exc: SpellingConflictError) -> None:
        if self.policy is ConflictPolicy.FAIL:
            raise ValidationError(detail=exc.detail)
        if self.policy is ConflictPolicy.SKIP:
            self.report.skipped += 1
            return
        existing = await word_controller.get(session=self.session, object_id=exc.existing_word_id)
        async with self.session.begin_nested():
            await word_controller.replace_from_payload(session=self.session, word=existing, payload=payload)
        self.report.replaced += 1
