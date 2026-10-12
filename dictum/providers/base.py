import dataclasses
import re
import typing as t

import httpx
import pydantic

from dictum.schemas.word import SpellingIn
from dictum.services import spelling as spelling_service
from dictum.services.text import normalize_part_of_speech

if t.TYPE_CHECKING:
    import collections.abc as cabc

    from dictum.enums import SpellingKind
    from dictum.schemas.lookup import DraftEntry


class ProviderError(Exception):
    """A dictionary provider could not answer (network, auth, unexpected payload)."""


def describe(*, exc: BaseException) -> str:
    """Short cause safe to show to clients.

    httpx status errors embed the full request URL (which may carry an API key), so they collapse to the
    status code; httpx timeouts stringify to an empty message, so those fall back to the class name.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return str(exc) or type(exc).__name__


@dataclasses.dataclass(slots=True)
class ProviderResult:
    """What one provider knows about a query: entries when found, else spelling suggestions."""

    entries: list[DraftEntry] = dataclasses.field(default_factory=list)
    suggestions: list[str] = dataclasses.field(default_factory=list)

    @property
    def found(self) -> bool:
        """True when at least one entry was produced."""
        return bool(self.entries)


class DictionaryProvider(t.Protocol):
    """Looks a word or phrase up in one upstream source."""

    name: str

    async def lookup(self, *, text: str) -> ProviderResult:
        """Return entries for `text`, or suggestions when unknown; raise `ProviderError` on failure."""
        ...


def valid_spellings(*, candidates: cabc.Iterable[tuple[str, SpellingKind]]) -> list[SpellingIn]:
    """Build spellings from `(text, kind)` pairs, dropping texts the schema rejects and case-folded duplicates.

    Upstream data is not trusted to be clean: a provider may hand back an empty headword or one that
    contains the vault separator, and the rest of the entry should still be usable.
    """
    result: list[SpellingIn] = []
    seen: set[str] = set()
    for text, kind in candidates:
        try:
            spelling = SpellingIn(text=text, kind=kind)
        except pydantic.ValidationError:
            continue
        key = spelling_service.key(text=spelling.text)
        if key not in seen:
            seen.add(key)
            result.append(spelling)
    return result


def _absorb(*, target: DraftEntry, extra: DraftEntry) -> None:
    pairs = [(s.text, s.kind) for s in (*target.spellings, *extra.spellings)]
    target.spellings = valid_spellings(candidates=pairs)
    target.pronunciation = target.pronunciation or extra.pronunciation
    target.audio_path = target.audio_path or extra.audio_path
    target.definitions = [*target.definitions, *extra.definitions]


def merge_by_part_of_speech(*, entries: cabc.Iterable[DraftEntry]) -> list[DraftEntry]:
    """Fold entries that share a part of speech into one, so every draft maps onto exactly one lexeme.

    Dictionaries split same-POS homographs into separate entries (`bank:1`, `bank:3`); the app keeps one
    lexeme per part of speech, so their definitions are concatenated in order. Spellings are unioned, the
    first pronunciation and audio win, and the part of speech comes out normalised (`Noun ` -> `noun`).
    """
    merged: dict[str, DraftEntry] = {}
    for entry in entries:
        pos = normalize_part_of_speech(text=entry.part_of_speech) or "other"
        existing = merged.get(pos)
        if existing is None:
            merged[pos] = entry.model_copy(update={"part_of_speech": pos})
        else:
            _absorb(target=existing, extra=entry)
    return list(merged.values())


def highlight(*, text: str, target: str) -> str:
    """Wrap the first whole-word occurrence of `target` in `*...*` unless the text already carries markup."""
    if "*" in text or "_" in text or not target:
        return text
    pattern = re.compile(rf"(?<!\w){re.escape(target)}(?!\w)", re.IGNORECASE)
    return pattern.sub(lambda m: f"*{m.group(0)}*", text, count=1)
