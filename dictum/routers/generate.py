import typing as t

import fastapi

from dictum.dependencies.ai import AIDep
from dictum.exceptions import UpstreamError
from dictum.providers.base import ProviderError
from dictum.schemas.generate import DefinitionsRequest
from dictum.schemas.generate import DefinitionsResponse
from dictum.schemas.generate import ExamplesRequest
from dictum.schemas.generate import ExamplesResponse

if t.TYPE_CHECKING:
    import collections.abc as cabc

router = fastapi.APIRouter(prefix="/generate", tags=["generate"])


async def _upstream[T](*, call: cabc.Awaitable[T]) -> T:
    try:
        return await call
    except ProviderError as exc:
        raise UpstreamError(detail=str(exc)) from exc


@router.post("/examples", response_model=ExamplesResponse, status_code=fastapi.status.HTTP_200_OK)
async def examples(*, ai: AIDep, payload: ExamplesRequest) -> ExamplesResponse:
    """Example sentences for each definition, written by the AI model; nothing is persisted."""
    groups = await _upstream(
        call=ai.generate_examples(
            word=payload.word,
            part_of_speech=payload.part_of_speech,
            definitions=payload.definitions,
            per_definition=payload.per_definition,
        )
    )
    return ExamplesResponse(examples=groups)


@router.post("/definitions", response_model=DefinitionsResponse, status_code=fastapi.status.HTTP_200_OK)
async def definitions(*, ai: AIDep, payload: DefinitionsRequest) -> DefinitionsResponse:
    """Draft entries written by the AI model; nothing is persisted.

    Covers the reader's `hints` first, then every other distinct sense; `parts_of_speech` narrows the draft.
    """
    entries = await _upstream(
        call=ai.generate_entries(
            text=payload.text,
            per_definition=payload.per_definition,
            hints=payload.hints,
            parts_of_speech=payload.parts_of_speech,
        )
    )
    return DefinitionsResponse(query=payload.text, found=bool(entries), entries=entries)
