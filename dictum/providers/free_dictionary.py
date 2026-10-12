"""dictionaryapi.dev provider (no key, community data, English only)."""

import logging
import typing as t
import urllib.parse

import httpx

from dictum.enums import DefinitionSource
from dictum.enums import ExampleSource
from dictum.enums import SpellingKind
from dictum.providers.base import ProviderError
from dictum.providers.base import ProviderResult
from dictum.providers.base import describe
from dictum.providers.base import highlight
from dictum.providers.base import merge_by_part_of_speech
from dictum.providers.base import valid_spellings
from dictum.schemas.lookup import DraftDefinition
from dictum.schemas.lookup import DraftEntry
from dictum.schemas.lookup import DraftExample
from dictum.services.text import is_clean_sentence

if t.TYPE_CHECKING:
    from dictum.services.audio import AudioCache

log = logging.getLogger(__name__)

Json = dict[str, t.Any]


def _definition(*, item: Json, word: str) -> DraftDefinition | None:
    text = str(item.get("definition", "")).strip()
    if not text:
        return None
    example = highlight(text=str(item.get("example", "")).strip(), target=word)
    examples = [DraftExample(text=example, source=ExampleSource.FREE_DICT)] if is_clean_sentence(text=example) else []
    return DraftDefinition(text=text, source=DefinitionSource.FREE_DICT, examples=examples)


def _pronunciation(item: Json) -> tuple[str | None, str | None]:
    """Phonetic text and the first non-empty audio URL."""
    phonetics = [p for p in item.get("phonetics", []) if isinstance(p, dict)]
    text = item.get("phonetic") or next((p["text"] for p in phonetics if p.get("text")), None)
    audio = next((p["audio"] for p in phonetics if p.get("audio")), None)
    return text, audio


class FreeDictionaryProvider:
    """Free Dictionary API; definitions are terse but it needs no credentials."""

    name = "free_dict"

    def __init__(self, *, client: httpx.AsyncClient, base_url: str, audio_cache: AudioCache) -> None:
        self._client = client
        self._base_url = base_url
        self._audio_cache = audio_cache

    async def _fetch(self, *, query: str) -> list[Json] | None:
        url = f"{self._base_url}{urllib.parse.quote(query, safe='')}"
        try:
            response = await self._client.get(url)
            if response.status_code == httpx.codes.NOT_FOUND:
                return None
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            msg = f"free_dict: {describe(exc=exc)}"
            raise ProviderError(msg) from exc
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            msg = "free_dict: unexpected payload shape"
            raise ProviderError(msg)
        return payload

    async def _entries(self, *, item: Json) -> list[DraftEntry]:
        word = str(item.get("word", "")).strip()
        spellings = valid_spellings(candidates=[(word, SpellingKind.PRIMARY)])
        if not spellings:
            return []
        pronunciation, audio_url = _pronunciation(item)
        audio_path = await self._audio(url=audio_url)
        entries: list[DraftEntry] = []
        for meaning in item.get("meanings", []):
            definitions = [_definition(item=d, word=word) for d in meaning.get("definitions", [])]
            entries.append(
                DraftEntry(
                    spellings=spellings,
                    part_of_speech=str(meaning.get("partOfSpeech") or "other"),
                    pronunciation=pronunciation,
                    audio_path=audio_path,
                    definitions=[d for d in definitions if d],
                    source=DefinitionSource.FREE_DICT,
                )
            )
        return entries

    async def _audio(self, *, url: str | None) -> str | None:
        if not url:
            return None
        name = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
        return await self._audio_cache.fetch(url=url, name=name)

    async def lookup(self, *, text: str) -> ProviderResult:
        """Resolve `text`; the API has no suggestion feature, so misses come back empty."""
        payload = await self._fetch(query=text)
        if payload is None:
            return ProviderResult()
        entries: list[DraftEntry] = []
        for item in payload:
            entries.extend(await self._entries(item=item))
        return ProviderResult(entries=merge_by_part_of_speech(entries=[e for e in entries if e.definitions]))
