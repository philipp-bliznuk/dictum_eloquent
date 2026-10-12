import asyncio
import typing as t

import pytest

from dictum.enums import SearchHitKind
from dictum.services.search import DEFINITION_CUTOFF
from dictum.services.search import DEFINITION_SHARE
from dictum.services.search import SPELLING_CUTOFF
from dictum.services.search import Hit
from dictum.services.search import SearchIndex
from dictum.services.search import starts_word

if t.TYPE_CHECKING:
    from dictum.services.search import Loader
    from dictum.services.search import Rows

LONG: str = "a" + "b" * 50
ROWS: list[tuple[str, int]] = [
    ("Abandon", 1),
    ("Abandonment", 2),
    ("Color", 3),
    ("Colour", 3),
    ("Zebra", 4),
    (LONG, 5),
    ("By my troth!", 6),
    ("put paid to", 7),
    ("in extenso", 8),
]
BURSAR_DEFINITION: str = "an official in charge of funds, a **treasurer**"
"""Markup must survive into `Hit.matched`; the processed copy is only used for scoring."""
SPELLINGS: list[tuple[str, int]] = [("Abandon", 1), ("Bursar", 2), ("Treasurer", 3), ("Zeal", 4), ("Ring", 5)]
DEFINITIONS: list[tuple[str, int]] = [
    ("to give up completely", 1),
    (BURSAR_DEFINITION, 2),
    ("a person who manages the money of an institution", 3),
    ("eagerness and ardent interest", 4),
    ("to make the bells ring", 5),
]
SPELLING = SearchHitKind.SPELLING
DEFINITION = SearchHitKind.DEFINITION


async def _load() -> list[tuple[str, int]]:
    await asyncio.sleep(0)
    return ROWS


async def _fresh_index() -> SearchIndex:
    index = SearchIndex()
    await index.ensure_fresh(spellings=_load)
    return index


def _loader(rows: Rows) -> Loader:
    """Loader returning `rows` by reference, so a test can swap the contents between rebuilds."""

    async def load() -> Rows:
        await asyncio.sleep(0)
        return rows

    return load


async def _index(*, spellings: Rows, definitions: Rows | None = None) -> SearchIndex:
    index = SearchIndex()
    await index.ensure_fresh(
        spellings=_loader(spellings),
        definitions=_loader(definitions) if definitions is not None else None,
    )
    return index


def _brief(hits: list[Hit]) -> list[tuple[int, SearchHitKind]]:
    return [(h.word_id, h.kind) for h in hits]


class _Counter:
    def __init__(self) -> None:
        self.calls = 0
        self.definition_calls = 0
        self.gate = asyncio.Event()
        self.gate.set()

    async def load(self) -> list[tuple[str, int]]:
        self.calls += 1
        await self.gate.wait()
        return ROWS

    async def load_definitions(self) -> list[tuple[str, int]]:
        self.definition_calls += 1
        return DEFINITIONS


class TestScoring:
    async def test_exact_match_scores_100(self) -> None:
        index = await _fresh_index()
        assert index.query(text="abandon")[0].score == pytest.approx(100.0)

    async def test_prefix_penalised_by_length(self) -> None:
        index = await _fresh_index()
        hits = {h.word_id: h.score for h in index.query(text="aban")}
        assert hits[1] == pytest.approx(98.5)
        assert hits[2] == pytest.approx(96.5)

    async def test_prefix_penalty_capped(self) -> None:
        index = await _fresh_index()
        hits = {h.word_id: h.score for h in index.query(text="a")}
        assert hits[5] == pytest.approx(90.0)

    async def test_fuzzy_below_prefix(self) -> None:
        index = await _fresh_index()
        hits = index.query(text="abandn")
        assert hits[0].word_id == 1
        assert SPELLING_CUTOFF < hits[0].score < 100

    async def test_long_query_ignores_short_lookalikes(self) -> None:
        """`fuzz.ratio` keeps `trek`, `ere`, `tor` out; WRatio's partial scaling used to let them in."""
        index = await _index(spellings=[("Trek", 1), ("Ere", 2), ("Tor", 3), ("Tress", 4), ("Beau", 5)])
        assert index.query(text="treasurer") == []

    @pytest.mark.parametrize(
        ("text", "word_id", "score"),
        [("troth", 6, 99.0), ("paid", 7, 99.0), ("tro", 6, 98.0), ("by my troth", 6, 100.0)],
    )
    async def test_phrase_token_containment(self, *, text: str, word_id: int, score: float) -> None:
        """A query equal to (or prefixing) one word of a phrase ranks just below the whole phrase."""
        index = await _fresh_index()
        hit = next(h for h in index.query(text=text) if h.word_id == word_id)
        assert hit.score == pytest.approx(score)

    @pytest.mark.parametrize(("text", "score"), [("pad", 84.7), ("put payed to", 87.0)])
    async def test_fuzzy_match_inside_phrase(self, *, text: str, score: float) -> None:
        index = await _fresh_index()
        hit = next(h for h in index.query(text=text) if h.word_id == 7)
        assert hit.score == pytest.approx(score, abs=0.1)


FLOOD_SPELLINGS: list[tuple[str, int]] = [
    (word, number + 1)
    for number, word in enumerate([
        "Ring",
        "Rings",
        "Ringer",
        "Ringlet",
        "Ringo",
        "Wring",
        "Bring",
        "Rang",
        "Rung",
        "Ting",
        "King",
        "Sing",
        "Wing",
        "Ding",
        "Zing",
        "Ping",
    ])
]
"""Sixteen spellings that all clear `SPELLING_CUTOFF` for the query `ring`."""
FLOOD_DEFINITIONS: list[tuple[str, int]] = [
    ("a ring of keys", 100),
    ("the boxing ring", 101),
    ("to ring a bell", 102),
    ("a wedding ring", 103),
    ("ring the changes", 104),
]


class TestDefinitionShare:
    async def test_definitions_keep_a_third_of_the_limit(self) -> None:
        index = await _index(spellings=FLOOD_SPELLINGS, definitions=FLOOD_DEFINITIONS)
        hits = index.query(text="ring", limit=12)
        assert len(hits) == 12
        assert [h.kind for h in hits] == [SPELLING] * 8 + [DEFINITION] * (12 // DEFINITION_SHARE)
        assert [h.word_id for h in hits[8:]] == [100, 103, 104, 101]

    @pytest.mark.parametrize(
        ("limit", "expected"),
        [
            (1, [SPELLING]),
            (2, [SPELLING, SPELLING]),
            (3, [SPELLING, SPELLING, DEFINITION]),
            (20, [SPELLING] * 15 + [DEFINITION] * 5),
        ],
    )
    async def test_small_limits_favour_spellings(self, *, limit: int, expected: list[SearchHitKind]) -> None:
        index = await _index(spellings=FLOOD_SPELLINGS, definitions=FLOOD_DEFINITIONS)
        assert [h.kind for h in index.query(text="ring", limit=limit)] == expected

    async def test_without_definition_hits_spellings_fill_the_limit(self) -> None:
        index = await _index(spellings=FLOOD_SPELLINGS, definitions=[("to give up completely", 100)])
        hits = index.query(text="ring", limit=12)
        assert len(hits) == 12
        assert {h.kind for h in hits} == {SPELLING}


class TestStartsWord:
    @pytest.mark.parametrize(
        ("query", "choice", "expected"),
        [
            ("ring", "during", False),
            ("ring", "bring", False),
            ("ness", "happiness", False),
            ("ring", "bells ring", True),
            ("ring", "ring the bell", True),
            ("ring", "the ringing", True),
            ("ring", "bring a ring", True),
            ("ring", "ring", True),
            ("zealous", "zeal", True),
            ("tresurer", "an official in charge of funds, a treasurer", True),
            ("tresurer", "xtreasurer", True),
            ("tresurer", "xxtreasurer", False),
            ("ring", "qqqqqq", False),
            ("treasurer", "a treasury", False),
        ],
        ids=[
            "exact-inside-word",
            "exact-one-char-in-no-skip",
            "exact-suffix",
            "exact-after-space",
            "exact-at-start",
            "exact-word-prefix",
            "exact-later-occurrence-starts-word",
            "equal-length",
            "choice-shorter-than-query",
            "fuzzy-after-space",
            "fuzzy-skips-one-leading-char",
            "fuzzy-inside-word",
            "no-alignment",
            "alignment-below-cutoff",
        ],
    )
    def test_starts_word(self, *, query: str, choice: str, expected: bool) -> None:
        assert starts_word(query=query, choice=choice) is expected


class TestSearchIndex:
    def test_starts_dirty(self) -> None:
        index = SearchIndex()
        assert index.dirty is True

    async def test_ensure_fresh_builds_once(self) -> None:
        counter = _Counter()
        index = SearchIndex()
        await index.ensure_fresh(spellings=counter.load, definitions=counter.load_definitions)
        await index.ensure_fresh(spellings=counter.load, definitions=counter.load_definitions)
        assert counter.calls == 1
        assert counter.definition_calls == 1
        assert index.dirty is False

    async def test_ensure_fresh_without_definitions_loader(self) -> None:
        counter = _Counter()
        index = SearchIndex()
        await index.ensure_fresh(spellings=counter.load)
        assert counter.calls == 1
        assert counter.definition_calls == 0
        assert index.dirty is False

    async def test_invalidate_triggers_rebuild(self) -> None:
        counter = _Counter()
        index = SearchIndex()
        await index.ensure_fresh(spellings=counter.load, definitions=counter.load_definitions)
        index.invalidate()
        assert index.dirty is True
        await index.ensure_fresh(spellings=counter.load, definitions=counter.load_definitions)
        assert counter.calls == 2
        assert counter.definition_calls == 2

    async def test_loader_failure_keeps_dirty_and_retries(self) -> None:
        counter = _Counter()
        index = SearchIndex()

        async def broken() -> Rows:
            await asyncio.sleep(0)
            msg = "db down"
            raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="db down"):
            await index.ensure_fresh(spellings=counter.load, definitions=broken)
        assert counter.calls == 1
        assert index.dirty is True
        assert index.query(text="abandon") == []
        await index.ensure_fresh(spellings=counter.load, definitions=counter.load_definitions)
        assert counter.calls == 2
        assert index.dirty is False
        assert _brief(index.query(text="funds")) == [(2, DEFINITION)]

    async def test_rebuild_replaces_both_tiers(self) -> None:
        spellings = [("Bursar", 2)]
        definitions = [("keeper of funds", 2)]
        index = SearchIndex()
        await index.ensure_fresh(spellings=_loader(spellings), definitions=_loader(definitions))
        assert _brief(index.query(text="funds")) == [(2, DEFINITION)]
        spellings[:] = [("Treasurer", 3)]
        definitions[:] = [("keeper of money", 3)]
        index.invalidate()
        await index.ensure_fresh(spellings=_loader(spellings), definitions=_loader(definitions))
        assert index.query(text="funds") == []
        assert index.query(text="bursar") == []
        assert _brief(index.query(text="money")) == [(3, DEFINITION)]
        assert _brief(index.query(text="treasurer")) == [(3, SPELLING)]

    async def test_concurrent_ensure_fresh_loads_once(self) -> None:
        counter = _Counter()
        counter.gate.clear()
        index = SearchIndex()
        first = asyncio.ensure_future(index.ensure_fresh(spellings=counter.load, definitions=counter.load_definitions))
        second = asyncio.ensure_future(index.ensure_fresh(spellings=counter.load, definitions=counter.load_definitions))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        counter.gate.set()
        await asyncio.gather(first, second)
        assert counter.calls == 1
        assert counter.definition_calls == 1
        assert index.dirty is False

    async def test_invalidate_during_rebuild_keeps_dirty(self) -> None:
        counter = _Counter()
        counter.gate.clear()
        index = SearchIndex()
        rebuild = asyncio.ensure_future(
            index.ensure_fresh(spellings=counter.load, definitions=counter.load_definitions)
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        index.invalidate()
        counter.gate.set()
        await rebuild
        assert counter.calls == 1
        assert counter.definition_calls == 1
        assert index.dirty is True
        assert index.query(text="abandon")[0].word_id == 1
        assert _brief(index.query(text="funds")) == [(2, DEFINITION)]
        await index.ensure_fresh(spellings=counter.load, definitions=counter.load_definitions)
        assert counter.calls == 2
        assert counter.definition_calls == 2
        assert index.dirty is False

    async def test_query_prefix_ranks_first(self) -> None:
        index = await _fresh_index()
        hits = index.query(text="aban")
        assert [h.word_id for h in hits] == [1, 2]
        assert hits[0].matched == "Abandon"
        assert hits[0].score > hits[1].score

    async def test_query_dedupes_by_word(self) -> None:
        index = await _fresh_index()
        hits = index.query(text="colo")
        assert len(hits) == 1
        assert hits[0] == Hit(word_id=3, matched="Color", score=99.5, kind=SPELLING)

    async def test_query_limit(self) -> None:
        index = await _fresh_index()
        assert len(index.query(text="aban", limit=1)) == 1

    @pytest.mark.parametrize("text", ["", "   ", "!!!"])
    async def test_query_blank_returns_empty(self, *, text: str) -> None:
        index = await _fresh_index()
        assert index.query(text=text) == []

    async def test_query_no_match(self) -> None:
        index = await _fresh_index()
        assert index.query(text="qqqqqq") == []

    async def test_query_case_insensitive(self) -> None:
        index = await _fresh_index()
        assert index.query(text="ZEBRA")[0].word_id == 4

    @pytest.mark.parametrize(
        ("text", "word_id"),
        [
            ("troth", 6),
            ("by my troth", 6),
            ("BY MY TROTH!!", 6),
            ("paid", 7),
            ("put paid", 7),
            ("extenso", 8),
            ("in ext", 8),
        ],
    )
    async def test_query_phrases(self, *, text: str, word_id: int) -> None:
        index = await _fresh_index()
        hits = index.query(text=text)
        assert hits[0].word_id == word_id
        assert hits[0].score >= 90

    async def test_query_full_phrase_scores_100(self) -> None:
        index = await _fresh_index()
        assert index.query(text="put paid to")[0].score == pytest.approx(100.0)

    def test_query_before_build_is_empty(self) -> None:
        assert SearchIndex().query(text="abandon") == []

    async def test_every_spelling_hit_is_kind_spelling(self) -> None:
        index = await _fresh_index()
        hits = index.query(text="a")
        assert hits
        assert {h.kind for h in hits} == {SPELLING}


class TestDefinitionTier:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("treasurer", [(3, SPELLING), (2, DEFINITION)]),
            ("tresurer", [(3, SPELLING), (2, DEFINITION)]),
            ("funds", [(2, DEFINITION)]),
            ("ring", [(5, SPELLING)]),
            ("bells", [(5, DEFINITION)]),
            ("money of", [(3, DEFINITION)]),
            ("ardent", [(4, DEFINITION)]),
            ("abandon", [(1, SPELLING)]),
            ("gives", []),
        ],
    )
    async def test_query_kinds(self, *, text: str, expected: list[tuple[int, SearchHitKind]]) -> None:
        index = await _index(spellings=SPELLINGS, definitions=DEFINITIONS)
        assert _brief(index.query(text=text)) == expected

    async def test_exact_definition_word_scores_100(self) -> None:
        index = await _index(spellings=SPELLINGS, definitions=DEFINITIONS)
        spelling, definition = index.query(text="treasurer")
        assert spelling == Hit(word_id=3, matched="Treasurer", score=100.0, kind=SPELLING)
        assert definition == Hit(word_id=2, matched=BURSAR_DEFINITION, score=100.0, kind=DEFINITION)

    async def test_fuzzy_query_hits_both_tiers(self) -> None:
        index = await _index(spellings=SPELLINGS, definitions=DEFINITIONS)
        spelling, definition = index.query(text="tresurer")
        assert spelling.score == pytest.approx(94.1, abs=0.1)
        assert definition.score == pytest.approx(87.5)
        assert definition.score >= DEFINITION_CUTOFF

    async def test_matched_is_original_definition_text(self) -> None:
        index = await _index(spellings=SPELLINGS, definitions=DEFINITIONS)
        (hit,) = index.query(text="funds")
        assert hit.matched == BURSAR_DEFINITION
        assert "**" in hit.matched

    async def test_spelling_hits_rank_before_higher_scoring_definition_hits(self) -> None:
        index = await _index(spellings=[("Treasurers", 1), ("Bursar", 2)], definitions=[("a treasurer", 2)])
        hits = index.query(text="treasurer")
        assert _brief(hits) == [(1, SPELLING), (2, DEFINITION)]
        assert hits[0].score == pytest.approx(99.5)
        assert hits[1].score == pytest.approx(100.0)
        assert hits[0].score < hits[1].score

    async def test_word_hit_by_spelling_excluded_from_definition_tier(self) -> None:
        spellings = [("Ring", 5), ("Bunch", 9)]
        definitions = [("to make the bells ring", 5), ("a ring of keys", 9)]
        index = await _index(spellings=spellings, definitions=definitions)
        hits = index.query(text="ring")
        assert _brief(hits) == [(5, SPELLING), (9, DEFINITION)]
        assert hits[0].matched == "Ring"
        assert hits[1].matched == "a ring of keys"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("rin", []),
            ("rin!", []),
            ("ring", [(2, DEFINITION)]),
            ("RING!!", [(2, DEFINITION)]),
        ],
        ids=["three-chars", "three-chars-after-processing", "four-chars", "four-chars-after-processing"],
    )
    async def test_min_definition_query_uses_processed_length(
        self, *, text: str, expected: list[tuple[int, SearchHitKind]]
    ) -> None:
        index = await _index(spellings=[("Zzz", 1)], definitions=[("ring the bell", 2)])
        assert _brief(index.query(text=text)) == expected

    @pytest.mark.parametrize(
        ("limit", "expected"),
        [
            (1, [(3, SPELLING)]),
            (2, [(3, SPELLING), (2, DEFINITION)]),
            (3, [(3, SPELLING), (2, DEFINITION), (7, DEFINITION)]),
            (20, [(3, SPELLING), (2, DEFINITION), (7, DEFINITION), (6, DEFINITION)]),
        ],
    )
    async def test_limit_spans_both_tiers(self, *, limit: int, expected: list[tuple[int, SearchHitKind]]) -> None:
        definitions = [("a treasurer", 2), ("the treasurer", 6), ("chief treasurer", 7)]
        index = await _index(spellings=[("Treasurer", 3)], definitions=definitions)
        assert _brief(index.query(text="treasurer", limit=limit)) == expected

    async def test_definition_tier_keeps_best_score_per_word(self) -> None:
        index = await _index(spellings=[("Bursar", 2)], definitions=[("a tresurer", 2), ("the treasurer", 2)])
        hits = index.query(text="treasurer")
        assert hits == [Hit(word_id=2, matched="the treasurer", score=100.0, kind=DEFINITION)]

    async def test_definition_hits_sorted_by_score_then_text(self) -> None:
        definitions = [("b treasurer", 2), ("a treasurer", 3), ("a tresurer", 4)]
        index = await _index(spellings=[("Zzz", 1)], definitions=definitions)
        hits = index.query(text="treasurer")
        assert [h.matched for h in hits] == ["a treasurer", "b treasurer", "a tresurer"]
        assert hits[0].score == hits[1].score == pytest.approx(100.0)
        assert hits[2].score < hits[1].score

    async def test_definition_match_must_start_a_word(self) -> None:
        definitions = [("during the day", 2), ("bring it", 3), ("the ringing", 4)]
        index = await _index(spellings=[("Zzz", 1)], definitions=definitions)
        assert _brief(index.query(text="ring")) == [(4, DEFINITION)]

    async def test_without_definitions_loader_only_spelling_hits(self) -> None:
        index = await _index(spellings=SPELLINGS)
        assert index.query(text="funds") == []
        assert _brief(index.query(text="treasurer")) == [(3, SPELLING)]

    async def test_blank_query_skips_definition_tier(self) -> None:
        index = await _index(spellings=SPELLINGS, definitions=DEFINITIONS)
        assert index.query(text="   ") == []

    def test_definition_query_before_build_is_empty(self) -> None:
        assert SearchIndex().query(text="funds") == []
