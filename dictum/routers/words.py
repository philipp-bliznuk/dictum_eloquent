import typing as t

import fastapi
import sqlalchemy as sa

from dictum.controllers.word import headword
from dictum.controllers.word import word_controller
from dictum.dependencies.db import AsyncDBSession
from dictum.dependencies.db import mark_search_dirty
from dictum.dependencies.lookup import LookupDep
from dictum.enums import WordStatus
from dictum.exceptions import AudioNotFoundError
from dictum.models.word import SPELLING_MAX_LENGTH
from dictum.models.word import Word
from dictum.schemas.word import Stats
from dictum.schemas.word import WordCreate
from dictum.schemas.word import WordPage
from dictum.schemas.word import WordResponse
from dictum.schemas.word import WordSummary
from dictum.schemas.word import WordUpdate
from dictum.services.text import normalize_part_of_speech

if t.TYPE_CHECKING:
    from dictum.schemas.lookup import DraftEntry

router = fastapi.APIRouter(prefix="/words", tags=["words"])


@router.get("", response_model=WordPage, status_code=fastapi.status.HTTP_200_OK)
async def list_words(
    *,
    session: AsyncDBSession,
    status: WordStatus | None = None,
    offset: t.Annotated[int, fastapi.Query(ge=0)] = 0,
    limit: t.Annotated[int, fastapi.Query(ge=1, le=200)] = 50,
) -> WordPage:
    """Paged words, newest first by id."""
    where: list[sa.ColumnElement[bool]] = []
    if status is not None:
        where.append(Word.status == status)
    rows, total = await word_controller.list(session=session, offset=offset, limit=limit, where=where)
    return WordPage(items=[WordSummary.from_word(word=w) for w in rows], total=total, offset=offset, limit=limit)


@router.get("/stats", response_model=Stats, status_code=fastapi.status.HTTP_200_OK)
async def stats(*, session: AsyncDBSession) -> Stats:
    """Dashboard counts."""
    return await word_controller.stats(session=session)


@router.get("/by-spelling", response_model=WordResponse, status_code=fastapi.status.HTTP_200_OK)
async def get_by_spelling(
    *,
    session: AsyncDBSession,
    text: t.Annotated[str, fastapi.Query(min_length=1, max_length=SPELLING_MAX_LENGTH)],
) -> WordResponse:
    """Resolve any spelling or phrase (case-, accent-composition- and whitespace-insensitive) to its word."""
    word = await word_controller.find_by_spelling(session=session, text=text)
    if word is None:
        raise word_controller.not_found
    return WordResponse.from_word(word=word)


@router.get("/{word_id}", response_model=WordResponse, status_code=fastapi.status.HTTP_200_OK)
async def get_word(*, session: AsyncDBSession, word_id: int) -> WordResponse:
    """Full word by id."""
    word = await word_controller.get(session=session, object_id=word_id)
    return WordResponse.from_word(word=word)


@router.post("", response_model=WordResponse, status_code=fastapi.status.HTTP_201_CREATED)
async def create_word(*, session: AsyncDBSession, payload: WordCreate) -> WordResponse:
    """Create a word with spellings, lexemes (part of speech, definitions, examples) and loose examples."""
    word = await word_controller.create_from_payload(session=session, payload=payload)
    mark_search_dirty(session=session)
    return WordResponse.from_word(word=word)


@router.put("/{word_id}", response_model=WordResponse, status_code=fastapi.status.HTTP_200_OK)
async def replace_word(*, session: AsyncDBSession, word_id: int, payload: WordCreate) -> WordResponse:
    """Replace scalars and all children."""
    word = await word_controller.get(session=session, object_id=word_id)
    word = await word_controller.replace_from_payload(session=session, word=word, payload=payload)
    mark_search_dirty(session=session)
    return WordResponse.from_word(word=word)


@router.patch("/{word_id}", response_model=WordResponse, status_code=fastapi.status.HTTP_200_OK)
async def patch_word(*, session: AsyncDBSession, word_id: int, payload: WordUpdate) -> WordResponse:  # type: ignore[valid-type]
    """Partial update of the word's own fields (status, notes)."""
    word = await word_controller.get(session=session, object_id=word_id)
    word = await word_controller.update(session=session, obj=word, values=payload.model_dump(exclude_unset=True))
    return WordResponse.from_word(word=word)


def _audio_by_part_of_speech(*, entries: list[DraftEntry]) -> dict[str, DraftEntry]:
    """Entries carrying audio keyed by normalised part of speech; `""` maps to the first one as fallback."""
    found: dict[str, DraftEntry] = {}
    for entry in entries:
        if entry.audio_path:
            found.setdefault("", entry)
            found.setdefault(normalize_part_of_speech(text=entry.part_of_speech), entry)
    return found


def _attach_audio(*, word: Word, entries: list[DraftEntry]) -> None:
    """Give every lexeme without audio the entry of its own part of speech, or any entry with audio."""
    audio = _audio_by_part_of_speech(entries=entries)
    if not audio:
        raise AudioNotFoundError
    for lexeme in word.lexemes:
        if lexeme.audio_path:
            continue
        entry = audio.get(lexeme.part_of_speech) or audio[""]
        lexeme.audio_path = entry.audio_path
        lexeme.pronunciation = lexeme.pronunciation or entry.pronunciation


@router.post("/{word_id}/audio", response_model=WordResponse, status_code=fastapi.status.HTTP_200_OK)
async def fetch_audio(*, session: AsyncDBSession, word_id: int, lookup: LookupDep) -> WordResponse:
    """Look the headword up again and attach cached pronunciation audio (and respelling, if missing).

    Only lexemes without audio are touched; a word whose lexemes all have audio is returned unchanged.
    """
    word = await word_controller.get(session=session, object_id=word_id)
    if any(not lexeme.audio_path for lexeme in word.lexemes):
        outcome = await lookup.lookup(text=headword(word=word))
        _attach_audio(word=word, entries=outcome.entries)
        await session.flush()
    return WordResponse.from_word(word=word)


@router.delete("/{word_id}", status_code=fastapi.status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_word(*, session: AsyncDBSession, word_id: int) -> None:
    """Delete a word and its children."""
    word = await word_controller.get(session=session, object_id=word_id)
    await word_controller.delete(session=session, obj=word)
    mark_search_dirty(session=session)
