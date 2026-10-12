import typing as t

import fastapi
import sqlmodel

from dictum.dependencies.db import AsyncDBSession
from dictum.dependencies.db import mark_search_dirty
from dictum.enums import ConflictPolicy
from dictum.models.word import Word
from dictum.services import export_md
from dictum.services.transfer import ExportFile
from dictum.services.transfer import Importer
from dictum.services.transfer import ImportFile
from dictum.services.transfer import ImportReport
from dictum.services.transfer import LegacyImportFile
from dictum.services.transfer import to_payload
from dictum.services.transfer import upgrade

if t.TYPE_CHECKING:
    import sqlalchemy.ext.asyncio as sa_async

router = fastapi.APIRouter(tags=["transfer"])

ImportBody = t.Annotated[ImportFile | LegacyImportFile, fastapi.Body(discriminator="version")]


async def _all_words(*, session: sa_async.AsyncSession) -> list[Word]:
    return list(await session.scalars(sqlmodel.select(Word).order_by(Word.id)))


@router.post("/import", response_model=ImportReport, status_code=fastapi.status.HTTP_200_OK)
async def import_json(
    *,
    session: AsyncDBSession,
    data: ImportBody,
    on_conflict: ConflictPolicy = ConflictPolicy.SKIP,
) -> ImportReport:
    """Bulk import `{"version": 2, "words": [...]}`; version 1 files are upgraded on the fly."""
    if isinstance(data, LegacyImportFile):
        data = upgrade(legacy=data)
    report = await Importer(session=session, policy=on_conflict).run(data=data)
    mark_search_dirty(session=session)
    return report


@router.get("/export.json", response_model=ExportFile, status_code=fastapi.status.HTTP_200_OK)
async def export_json(*, session: AsyncDBSession) -> ExportFile:
    """Everything, in the same shape `POST /import` accepts."""
    words = await _all_words(session=session)
    return ExportFile(words=[to_payload(word=w) for w in words])


@router.get("/export.md", response_class=fastapi.responses.Response, status_code=fastapi.status.HTTP_200_OK)
async def export_markdown(*, session: AsyncDBSession) -> fastapi.responses.Response:
    """Zip of `A.md`..`Z.md` + `_Inbox.md`, one section per part of speech (one-way export)."""
    words = await _all_words(session=session)
    payload = export_md.zip_vault(files=export_md.render_vault(words=words))
    return fastapi.responses.Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="dictum-vault.zip"'},
    )
