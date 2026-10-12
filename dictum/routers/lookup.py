import typing as t

import fastapi

from dictum.controllers.word import word_controller
from dictum.dependencies.db import AsyncDBSession
from dictum.dependencies.lookup import LookupDep
from dictum.schemas.lookup import LookupRequest
from dictum.schemas.lookup import LookupResponse
from dictum.services import spelling as spelling_service

if t.TYPE_CHECKING:
    import collections.abc as cabc

    from sqlalchemy.ext.asyncio import AsyncSession

router = fastapi.APIRouter(prefix="/lookup", tags=["lookup"])


async def _existing_word_id(*, session: AsyncSession, texts: cabc.Iterable[str]) -> int | None:
    unique = {spelling_service.key(text=text): text for text in texts}
    for text in unique.values():
        word = await word_controller.find_by_spelling(session=session, text=text)
        if word is not None:
            return word.id
    return None


@router.post("", response_model=LookupResponse, status_code=fastapi.status.HTTP_200_OK)
async def lookup(*, session: AsyncDBSession, service: LookupDep, payload: LookupRequest) -> LookupResponse:
    """Draft entries for a word or phrase from the dictionary providers; nothing is persisted."""
    outcome = await service.lookup(text=payload.text)
    texts = [payload.text, *(s.text for e in outcome.entries for s in e.spellings)]
    return LookupResponse(
        query=payload.text,
        found=outcome.found,
        entries=outcome.entries,
        suggestions=outcome.suggestions,
        attempts=outcome.attempts,
        existing_word_id=await _existing_word_id(session=session, texts=texts),
    )
