import asyncio
import dataclasses
import re
import typing as t

from rapidfuzz import fuzz
from rapidfuzz import process
from rapidfuzz import utils

from dictum.enums import SearchHitKind

if t.TYPE_CHECKING:
    import collections.abc as cabc

type Rows = list[tuple[str, int]]
type Loader = cabc.Callable[[], cabc.Awaitable[Rows]]

SPELLING_CUTOFF: t.Final = 70
DEFINITION_CUTOFF: t.Final = 85
MIN_DEFINITION_QUERY: t.Final = 4
"""Shorter queries skip the definition tier: `the` or `ring` would match most of the dictionary."""
DEFINITION_SHARE: t.Final = 3
"""Definition hits get at least one slot in `DEFINITION_SHARE` so spelling noise cannot crowd them out."""
_FULL_SCORE: t.Final = 100.0
_TOKEN_PENALTY: t.Final = 1.0
"""A spelling that merely contains the query as a word ranks just below one that equals it."""


@dataclasses.dataclass(frozen=True, slots=True)
class Hit:
    """One search result before DB hydration."""

    word_id: int
    matched: str
    score: float
    kind: SearchHitKind


def _single(query: str, choice: str, *, processor: object, score_cutoff: float | None) -> float:
    """Prefix matches score 90..100 by length difference; anything else is the plain ratio.

    `fuzz.ratio` (not `WRatio`) on purpose: the weighted variant rewards partial overlaps, so a
    long query such as `treasurer` lit up `trek`, `ere` and `tor` and buried everything else.
    """
    if choice.startswith(query):
        return _FULL_SCORE - min(len(choice) - len(query), 20) * 0.5
    return fuzz.ratio(query, choice, processor=processor, score_cutoff=score_cutoff)


def _scorer(query: str, choice: str, *, processor: object = None, score_cutoff: float | None = None) -> float:
    """Score a spelling against the whole choice and against each word of a phrase.

    `troth` scores 99 against `by my troth` (whole-word containment), `tro` 98 (prefix of a word).
    """
    scores = [_single(query, choice, processor=processor, score_cutoff=score_cutoff)]
    tokens = choice.split()
    if len(tokens) > 1:
        scores.extend(
            _single(query, token, processor=processor, score_cutoff=score_cutoff) - _TOKEN_PENALTY for token in tokens
        )
    return max(scores)


def starts_word(*, query: str, choice: str) -> bool:
    """Whether `query` matches `choice` at the start of a word.

    Both strings must already be processed (lower case, punctuation replaced by spaces). An exact
    substring has to begin a word somewhere in `choice`; a fuzzy alignment may skip one leading
    character (`tresurer` lands on `reasurer`). Guards against `ring` matching `during` or `bring`.
    """
    alignment = fuzz.partial_ratio_alignment(query, choice, score_cutoff=DEFINITION_CUTOFF)
    if alignment is None:
        return False
    if alignment.score < _FULL_SCORE or len(choice) <= len(query):
        start = alignment.dest_start
        return start <= 1 or " " in choice[start - 2 : start]
    return re.search(rf"(?<!\S){re.escape(query)}", choice) is not None


@dataclasses.dataclass(frozen=True, slots=True)
class _Tier:
    choices: list[str]
    texts: list[str]
    word_ids: list[int]

    @classmethod
    def from_rows(cls, *, rows: Rows) -> t.Self:
        return cls(
            choices=[utils.default_process(text) for text, _ in rows],
            texts=[text for text, _ in rows],
            word_ids=[word_id for _, word_id in rows],
        )

    def hit(self, *, index: int, score: float, kind: SearchHitKind) -> Hit:
        return Hit(word_id=self.word_ids[index], matched=self.texts[index], score=float(score), kind=kind)


_EMPTY = _Tier(choices=[], texts=[], word_ids=[])


async def _rows(*, loader: Loader | None) -> Rows:
    return await loader() if loader else []


def _best(*, hits: cabc.Iterable[Hit], limit: int) -> list[Hit]:
    """Keep the strongest hit per word, ordered by score then text."""
    best: dict[int, Hit] = {}
    for hit in hits:
        if hit.word_id not in best or best[hit.word_id].score < hit.score:
            best[hit.word_id] = hit
    return sorted(best.values(), key=lambda h: (-h.score, h.matched))[:limit]


class SearchIndex:
    """In-memory fuzzy index over spellings and definitions; rebuilt lazily after writes.

    Spelling hits (exact, prefix, fuzzy) always rank above definition hits, so searching
    `treasurer` lists the word itself first and `bursar` (defined as a treasurer) after it.
    At least a third of `limit` is held back for definition hits whenever there are any.
    """

    def __init__(self) -> None:
        self._spellings = _EMPTY
        self._definitions = _EMPTY
        self._dirty = True
        self._generation = 0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        """Mark stale; next query triggers a rebuild via `ensure_fresh`."""
        self._generation += 1
        self._dirty = True

    @property
    def dirty(self) -> bool:
        """True when the index must be rebuilt before use."""
        return self._dirty

    async def ensure_fresh(self, *, spellings: Loader, definitions: Loader | None = None) -> None:
        """Rebuild from the loaders if dirty. Serialized by a lock.

        An `invalidate()` that lands while a loader is running leaves the index dirty, so the
        next query rebuilds again instead of serving the snapshot that missed the write.
        """
        if not self._dirty:
            return
        async with self._lock:
            if self._dirty:
                await self._reload(spellings=spellings, definitions=definitions)

    async def _reload(self, *, spellings: Loader, definitions: Loader | None) -> None:
        generation = self._generation
        spelling_rows = await spellings()
        definition_rows = await _rows(loader=definitions)
        self._spellings = _Tier.from_rows(rows=spelling_rows)
        self._definitions = _Tier.from_rows(rows=definition_rows)
        if generation == self._generation:
            self._dirty = False

    def query(self, *, text: str, limit: int = 20) -> list[Hit]:
        """Best `limit` distinct words for `text`: spelling matches first, then definition matches."""
        q = utils.default_process(text)
        if not q:
            return []
        spellings = self._spelling_hits(query=q, limit=limit)
        definitions: list[Hit] = []
        if len(q) >= MIN_DEFINITION_QUERY:
            taken = {h.word_id for h in spellings}
            definitions = [h for h in self._definition_hits(query=q, limit=limit) if h.word_id not in taken]
        keep = limit - min(len(definitions), limit // DEFINITION_SHARE)
        return [*spellings[:keep], *definitions][:limit]

    def _spelling_hits(self, *, query: str, limit: int) -> list[Hit]:
        tier = self._spellings
        raw = process.extract(query, tier.choices, scorer=_scorer, limit=limit * 3, score_cutoff=SPELLING_CUTOFF)
        return _best(hits=(tier.hit(index=i, score=s, kind=SearchHitKind.SPELLING) for _, s, i in raw), limit=limit)

    def _definition_hits(self, *, query: str, limit: int) -> list[Hit]:
        tier = self._definitions
        # No limit here: the word-start filter below would otherwise starve on a pool full of
        # `during` / `bring` style substring hits before any real match gets a look.
        raw = process.extract(
            query, tier.choices, scorer=fuzz.partial_ratio, limit=None, score_cutoff=DEFINITION_CUTOFF
        )
        hits = (
            tier.hit(index=i, score=s, kind=SearchHitKind.DEFINITION)
            for choice, s, i in raw
            if starts_word(query=query, choice=choice)
        )
        return _best(hits=hits, limit=limit)
