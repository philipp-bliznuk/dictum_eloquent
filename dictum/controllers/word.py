import typing as t

import sqlalchemy as sa
import sqlmodel

from dictum.controllers.base import Controller
from dictum.enums import SpellingKind
from dictum.enums import WordStatus
from dictum.exceptions import SpellingConflictError
from dictum.exceptions import WordNotFoundError
from dictum.models.word import Definition
from dictum.models.word import Example
from dictum.models.word import Lexeme
from dictum.models.word import Spelling
from dictum.models.word import Word
from dictum.schemas.word import Stats
from dictum.services import spelling as spelling_service

if t.TYPE_CHECKING:
    import sqlalchemy.ext.asyncio as sa_async

    from dictum.schemas.word import DefinitionIn
    from dictum.schemas.word import ExampleIn
    from dictum.schemas.word import LexemeIn
    from dictum.schemas.word import WordCreate

_CHILDREN: t.Final = frozenset({"spellings", "lexemes", "examples"})
_IMMUTABLE_ON_REPLACE: t.Final = _CHILDREN | {"created_at"}


def headword(*, word: Word) -> str:
    """Primary spelling, falling back to the first one (payloads guarantee at least one)."""
    primary = next((s for s in word.spellings if s.kind == SpellingKind.PRIMARY), None)
    return (primary or word.spellings[0]).text


class WordController(Controller[Word]):
    """CRUD for words including their spellings, lexemes, definitions and examples."""

    not_found = WordNotFoundError

    async def find_by_spelling(self, *, session: sa_async.AsyncSession, text: str) -> Word | None:
        """Exact match on any spelling, ignoring case, Unicode composition and surrounding whitespace."""
        stmt = sqlmodel.select(Word).join(Spelling).where(Spelling.text_key == spelling_service.key(text=text))
        return await session.scalar(stmt)

    async def ensure_spellings_free(
        self,
        *,
        session: sa_async.AsyncSession,
        texts: list[str],
        exclude_word_id: int | None = None,
    ) -> None:
        """Raise `SpellingConflictError` if any spelling belongs to another word."""
        keys = [spelling_service.key(text=x) for x in texts]
        stmt = sqlmodel.select(Spelling.text, Spelling.word_id).where(Spelling.text_key.in_(keys))
        if exclude_word_id is not None:
            stmt = stmt.where(Spelling.word_id != exclude_word_id)
        row = (await session.execute(stmt)).first()
        if row is not None:
            raise SpellingConflictError(spelling=row[0], existing_word_id=row[1])

    async def create_from_payload(self, *, session: sa_async.AsyncSession, payload: WordCreate) -> Word:
        """Create word + children from one request body."""
        await self.ensure_spellings_free(session=session, texts=[s.text for s in payload.spellings])
        word = Word(**_scalars(payload=payload))
        _attach_children(word=word, payload=payload)
        return await self.create(session=session, obj=word)

    async def replace_from_payload(self, *, session: sa_async.AsyncSession, word: Word, payload: WordCreate) -> Word:
        """Overwrite scalars and fully replace children; `created_at` is kept."""
        await self.ensure_spellings_free(
            session=session,
            texts=[s.text for s in payload.spellings],
            exclude_word_id=word.id,
        )
        for key, value in payload.model_dump(exclude=_IMMUTABLE_ON_REPLACE).items():
            setattr(word, key, value)
        word.spellings.clear()
        word.lexemes.clear()
        word.examples.clear()
        await session.flush()
        _attach_children(word=word, payload=payload)
        await session.flush()
        return word

    async def stats(self, *, session: sa_async.AsyncSession) -> Stats:
        """Counts for the dashboard."""
        words = await self.count(session=session)
        inbox = await session.scalar(
            sqlmodel.select(sa.func.count()).select_from(Word).where(Word.status == WordStatus.INBOX)
        )
        definitions = await session.scalar(sqlmodel.select(sa.func.count()).select_from(Definition))
        examples = await session.scalar(sqlmodel.select(sa.func.count()).select_from(Example))
        return Stats(words=words, inbox=inbox or 0, definitions=definitions or 0, examples=examples or 0)

    async def spelling_index_rows(self, *, session: sa_async.AsyncSession) -> list[tuple[str, int]]:
        """All `(spelling, word_id)` pairs for the search index."""
        rows = await session.execute(sqlmodel.select(Spelling.text, Spelling.word_id))
        return [(text, word_id) for text, word_id in rows]

    async def definition_index_rows(self, *, session: sa_async.AsyncSession) -> list[tuple[str, int]]:
        """All `(definition text, word_id)` pairs for the search index."""
        statement = sqlmodel.select(Definition.text, Lexeme.word_id).join(Lexeme, Definition.lexeme_id == Lexeme.id)
        rows = await session.execute(statement)
        return [(text, word_id) for text, word_id in rows]


def _scalars(*, payload: WordCreate) -> dict[str, t.Any]:
    """Column values of the word itself; an unset `created_at` (import payloads) falls back to the model default."""
    values = payload.model_dump(exclude=_CHILDREN)
    if values.get("created_at") is None:
        values.pop("created_at", None)
    return values


def _build_example(*, payload: ExampleIn, position: int) -> Example:
    return Example(text=payload.text, position=position, source=payload.source)


def _build_definition(*, payload: DefinitionIn, position: int, examples: list[Example]) -> Definition:
    definition = Definition(text=payload.text, position=position, source=payload.source)
    nested = [_build_example(payload=e, position=len(examples) + i) for i, e in enumerate(payload.examples)]
    # Assigning the list (even an empty one) initialises the collection, so serialising a fresh
    # definition never falls back to a lazy load outside the greenlet.
    definition.examples = nested
    examples.extend(nested)
    return definition


def _build_lexeme(*, payload: LexemeIn, position: int, examples: list[Example]) -> Lexeme:
    lexeme = Lexeme(
        part_of_speech=payload.part_of_speech,
        pronunciation=payload.pronunciation,
        audio_path=payload.audio_path,
        position=position,
    )
    lexeme.definitions = [
        _build_definition(payload=d, position=j, examples=examples) for j, d in enumerate(payload.definitions)
    ]
    return lexeme


def _attach_children(*, word: Word, payload: WordCreate) -> None:
    """Materialise the nested payload; one position counter numbers every example of the word.

    Examples nested under a definition are also appended to `word.examples` so the word owns
    them; the response splits them again by `definition_id`.
    """
    word.spellings = [Spelling(text=s.text, kind=s.kind) for s in payload.spellings]
    examples: list[Example] = []
    word.lexemes = [
        _build_lexeme(payload=lexeme, position=i, examples=examples) for i, lexeme in enumerate(payload.lexemes)
    ]
    for e in payload.examples:
        examples.append(_build_example(payload=e, position=len(examples)))
    word.examples = examples


word_controller = WordController(model=Word)
