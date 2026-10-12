import functools
import typing as t

import fastapi

from dictum.controllers.word import word_controller
from dictum.dependencies.db import AsyncDBSession
from dictum.models.word import SPELLING_MAX_LENGTH
from dictum.schemas.word import SearchHit
from dictum.schemas.word import SearchResponse
from dictum.schemas.word import WordSummary

if t.TYPE_CHECKING:
    from dictum.services.search import SearchIndex

router = fastapi.APIRouter(prefix="/search", tags=["search"])


@router.get("", response_model=SearchResponse, status_code=fastapi.status.HTTP_200_OK)
async def search(
    *,
    request: fastapi.Request,
    session: AsyncDBSession,
    q: t.Annotated[str, fastapi.Query(min_length=1, max_length=SPELLING_MAX_LENGTH)],
    limit: t.Annotated[int, fastapi.Query(ge=1, le=50)] = 20,
) -> SearchResponse:
    """Fuzzy search over every spelling, then over definition texts (ranked below)."""
    index: SearchIndex = request.app.state.search
    await index.ensure_fresh(
        spellings=functools.partial(word_controller.spelling_index_rows, session=session),
        definitions=functools.partial(word_controller.definition_index_rows, session=session),
    )
    hits = index.query(text=q, limit=limit)
    words = await word_controller.get_many(session=session, object_ids=[h.word_id for h in hits])
    return SearchResponse(
        query=q,
        hits=[
            SearchHit(
                word=WordSummary.from_word(word=words[h.word_id]),
                matched=h.matched,
                score=h.score,
                kind=h.kind,
            )
            for h in hits
            if h.word_id in words
        ],
    )
