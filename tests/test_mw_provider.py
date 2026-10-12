import typing as t

import httpx
import pytest

from dictum.enums import DefinitionSource
from dictum.enums import ExampleSource
from dictum.enums import SpellingKind
from dictum.providers.base import ProviderError
from dictum.providers.mw import MWConfig
from dictum.providers.mw import MWProvider
from dictum.providers.mw import ascii_fold
from dictum.providers.mw import audio_url
from dictum.providers.mw import cognate_target
from dictum.providers.mw import inflection_part_of_speech
from dictum.providers.mw import inflection_stubs
from dictum.providers.mw import is_spelling_redirect
from dictum.providers.mw import prefer_headword
from dictum.providers.mw import render
from dictum.providers.mw import run_on_candidates
from dictum.providers.mw import select_entries
from dictum.schemas.lookup import DraftDefinition
from dictum.schemas.lookup import DraftEntry
from dictum.schemas.lookup import DraftExample
from dictum.schemas.word import SpellingIn
from dictum.services.text import is_clean_sentence
from tests.conftest import MW_API_KEY
from tests.conftest import MW_AUDIO_BASE_URL
from tests.conftest import MW_BASE_URL
from tests.conftest import mw_audio_url
from tests.conftest import mw_fixture
from tests.conftest import mw_url

if t.TYPE_CHECKING:
    import collections.abc as cabc

    from dictum.providers.base import ProviderResult
    from dictum.services.audio import AudioCache
    from tests.conftest import FakeUpstream

Json = dict[str, t.Any]

QUERY: t.Final = "zorb"
STRESS: t.Final = "\u02c8"
"""MW primary stress mark (MODIFIER LETTER VERTICAL LINE); escaped so it is not mistaken for a backtick."""


def serve(upstream: FakeUpstream, *, query: str, name: str | None = None, audio: cabc.Iterable[str] = ()) -> None:
    """Route the recorded MW response for `query` (fixture `name` defaults to the query) plus audio files."""
    upstream.json(url=mw_url(query=query), payload=mw_fixture(name=name or query))
    for ref in audio:
        upstream.audio(url=mw_audio_url(ref=ref))


def sense(text: str, **extra: t.Any) -> list[t.Any]:
    """`["sense", {...}]` pair with one defining text."""
    return ["sense", {"dt": [["text", text]], **extra}]


def entry(*, definitions: list[Json] | None = None, **overrides: t.Any) -> Json:
    """Minimal MW entry for the synthetic headword `zorb`; `definitions` is the `def` array."""
    base: Json = {
        "meta": {"id": QUERY, "stems": [QUERY, "zorbs"]},
        "hwi": {"hw": QUERY},
        "fl": "noun",
        "def": [{"sseq": [[sense("{bc}a thing")]]}] if definitions is None else definitions,
    }
    base.update(overrides)
    return base


def stub(*cross_references: tuple[str, str], **overrides: t.Any) -> Json:
    """Definition-less MW entry for `zorb` whose `cxs` carry one `(label, target)` cross-reference each."""
    base: Json = {
        "meta": {"id": QUERY, "stems": [QUERY]},
        "hwi": {"hw": QUERY},
        "cxs": [{"cxl": label, "cxtis": [{"cxt": target}]} for label, target in cross_references],
    }
    base.update(overrides)
    return base


@pytest.fixture
def provider(http: httpx.AsyncClient, audio_cache: AudioCache) -> MWProvider:
    config = MWConfig(api_key=MW_API_KEY, base_url=MW_BASE_URL, audio_base_url=MW_AUDIO_BASE_URL)
    return MWProvider(client=http, config=config, audio_cache=audio_cache)


async def lookup_synthetic(
    provider: MWProvider, upstream: FakeUpstream, *, payload: list[t.Any], query: str = QUERY
) -> ProviderResult:
    upstream.json(url=mw_url(query=query), payload=payload)
    return await provider.lookup(text=query)


class TestRender:
    @pytest.mark.parametrize(
        ("raw", "rendered"),
        [
            (
                "{bc}a thorough yielding to natural impulses {bc}enthusiasm, exuberance",
                "a thorough yielding to natural impulses: enthusiasm, exuberance",
            ),
            ("{bc}to finish off {bc}bring an end to", "to finish off: bring an end to"),
            ("with reckless {wi}abandon{/wi}", "with reckless *abandon*"),
            ("{bc}{sx|enthusiasm||}, {sx|exuberance||} ", "enthusiasm, exuberance"),
            ("{it}in extenso{/it}", "*in extenso*"),
            ("x {dx}see also {dxt|foo||}{/dx} y", "x y"),
            ("{dx_ety}e{/dx_ety}y", "y"),
            ("{ma}m{/ma}x", "x"),
            ("a {unknown}b{/unknown} c", "a b c"),
            ("{gloss}x{/gloss}", "[x]"),
            ("{b}x{/b}", "**x**"),
            ("{phrase}p{/phrase}", "**p**"),
            ("{parahw}p{/parahw}", "**p**"),
            ("{qword}q{/qword}", "*q*"),
            ("{dx_def}d{/dx_def}", "(d)"),
            ("{ldquo}q{rdquo}", '"q"'),
            ("a{p_br}b", "a b"),
            ("{d_link|text|id}", "text"),
            ("{a_link|alink}", "alink"),
            ("{i_link|il}", "il"),
            ("{et_link|et|id}", "et"),
            ("{mat|m|id}", "m"),
            ("{dxt|d|id}", "d"),
            ("  spaced   out  ,  text ", "spaced out, text"),
            ("", ""),
        ],
    )
    def test_tokens(self, *, raw: str, rendered: str) -> None:
        assert render(text=raw) == rendered


class TestSelectEntries:
    def test_exact_headwords_win_over_idioms(self) -> None:
        selected = select_entries(payload=mw_fixture(name="abandon"), query="abandon")
        assert [e["meta"]["id"] for e in selected] == ["abandon:1", "abandon:2"]

    def test_case_insensitive(self) -> None:
        selected = select_entries(payload=mw_fixture(name="abandon"), query="ABANDON")
        assert [e["meta"]["id"] for e in selected] == ["abandon:1", "abandon:2"]

    def test_falls_back_to_stems(self) -> None:
        selected = select_entries(payload=mw_fixture(name="abandon"), query="abandoning")
        assert [e["meta"]["id"] for e in selected] == ["abandon:1"]

    def test_skips_entries_without_definitions(self) -> None:
        assert select_entries(payload=mw_fixture(name="colour"), query="colour") == []
        assert select_entries(payload=mw_fixture(name="aggrandise"), query="aggrandise") == []

    def test_no_match(self) -> None:
        assert select_entries(payload=mw_fixture(name="abandon"), query="zebra") == []


class TestCognateTarget:
    @pytest.mark.parametrize(
        ("name", "query", "target"),
        [
            ("aggrandise", "aggrandise", "aggrandize"),
            ("colour", "colour", "color"),
            ("colour", "COLOUR", "color"),
            ("abandon", "abandon", None),
            ("aggrandize", "aggrandize", None),
            ("colour", "color bar", None),
        ],
    )
    def test_fixtures(self, *, name: str, query: str, target: str | None) -> None:
        assert cognate_target(payload=mw_fixture(name=name), query=query) == target

    def test_strips_homograph_suffix(self) -> None:
        stub = {"meta": {"id": "colour"}, "cxs": [{"cxl": "British spelling of", "cxtis": [{"cxt": "color:1"}]}]}
        assert cognate_target(payload=[stub], query="colour") == "color"

    def test_stub_without_targets(self) -> None:
        stub = {"meta": {"id": "colour"}, "cxs": [{"cxl": "see", "cxtis": [{"cxr": "x"}]}]}
        assert cognate_target(payload=[stub], query="colour") is None

    def test_inflection_stub_is_not_a_redirect(self) -> None:
        assert cognate_target(payload=mw_fixture(name="couldst"), query="couldst") is None

    @pytest.mark.parametrize(
        ("label", "target"),
        [
            ("British spelling of", "zarb"),
            ("variant of", "zarb"),
            ("past tense of", None),
            ("plural of", None),
            ("see", None),
        ],
    )
    def test_label_decides(self, *, label: str, target: str | None) -> None:
        assert cognate_target(payload=[stub((label, "zarb"))], query=QUERY) == target

    def test_first_spelling_target_wins_over_earlier_inflection(self) -> None:
        payload = [stub(("past tense of", "zarb"), ("less common spelling of", "zorbe"), ("variant of", "zourb"))]
        assert cognate_target(payload=payload, query=QUERY) == "zorbe"


class TestIsSpellingRedirect:
    @pytest.mark.parametrize(
        "label",
        [
            "British spelling of",
            "chiefly British spelling of",
            "less common spelling of",
            "variant of",
            "VARIANT OF",
            "  Spelling   of ",
        ],
    )
    def test_spelling_labels(self, label: str) -> None:
        assert is_spelling_redirect(label=label) is True

    @pytest.mark.parametrize(
        "label",
        [
            "past tense of",
            "plural of",
            "archaic past tense second-person singular of",
            "present participle of",
            "comparative of",
            "see",
            "",
        ],
    )
    def test_inflection_and_other_labels(self, label: str) -> None:
        assert is_spelling_redirect(label=label) is False


class TestInflectionPartOfSpeech:
    @pytest.mark.parametrize(
        ("label", "part_of_speech"),
        [
            ("plural of", "noun"),
            ("comparative of", "adjective"),
            ("superlative of", "adjective"),
            ("past tense of", "verb"),
            ("past participle of", "verb"),
            ("present participle of", "verb"),
            ("archaic present tense second-person singular of", "verb"),
            ("archaic past tense second-person singular of", "verb"),
            ("PAST TENSE OF", "verb"),
            ("see", "other"),
            ("", "other"),
        ],
    )
    def test_marker_lookup(self, *, label: str, part_of_speech: str) -> None:
        assert inflection_part_of_speech(label=label) == part_of_speech

    def test_first_marker_in_table_order_wins(self) -> None:
        assert inflection_part_of_speech(label="past tense plural of") == "noun"


class TestInflectionStubs:
    def test_couldst_fixture(self) -> None:
        payload = mw_fixture(name="couldst")
        assert inflection_stubs(payload=payload, query="couldst") == [
            (payload[0], "archaic past tense second-person singular of", "can")
        ]

    @pytest.mark.parametrize(("name", "query"), [("couldst", "other"), ("couldst", "can"), ("colour", "colour")])
    def test_nothing_for_other_queries_and_spelling_redirects(self, *, name: str, query: str) -> None:
        assert inflection_stubs(payload=mw_fixture(name=name), query=query) == []

    def test_query_matched_case_insensitively(self) -> None:
        payload = mw_fixture(name="couldst")
        assert [t for _, _, t in inflection_stubs(payload=payload, query="COULDST")] == ["can"]

    def test_spelling_redirects_filtered_out_per_label(self) -> None:
        payload = [stub(("British spelling of", "zorbe"), ("past tense of", "zarb"), ("plural of", "zorp"))]
        assert [(label, target) for _, label, target in inflection_stubs(payload=payload, query=QUERY)] == [
            ("past tense of", "zarb"),
            ("plural of", "zorp"),
        ]

    def test_label_whitespace_collapsed_and_homograph_suffix_stripped(self) -> None:
        payload = [stub(("  past \n tense\tof ", "zarb:2"))]
        assert [(label, target) for _, label, target in inflection_stubs(payload=payload, query=QUERY)] == [
            ("past tense of", "zarb")
        ]

    def test_every_target_of_one_cross_reference(self) -> None:
        payload = [stub(cxs=[{"cxl": "past tense of", "cxtis": [{"cxt": "zarb"}, {"cxr": "x"}, {"cxt": "zerb:1"}]}])]
        assert [target for _, _, target in inflection_stubs(payload=payload, query=QUERY)] == ["zarb", "zerb"]

    def test_homograph_id_matches_query(self) -> None:
        payload = [stub(("past tense of", "zarb"), meta={"id": "Zorb:1"})]
        assert [target for _, _, target in inflection_stubs(payload=payload, query=QUERY)] == ["zarb"]

    @pytest.mark.parametrize(
        "cross_reference", [{"cxtis": [{"cxt": "zarb"}]}, {"cxl": None, "cxtis": [{"cxt": "zarb"}]}]
    )
    def test_missing_label_is_empty(self, cross_reference: Json) -> None:
        payload = [stub(cxs=[cross_reference])]
        assert [(label, target) for _, label, target in inflection_stubs(payload=payload, query=QUERY)] == [
            ("", "zarb")
        ]

    @pytest.mark.parametrize(
        "payload",
        [
            [stub(("past tense of", "zarb"), meta={"id": "zorbs", "stems": [QUERY]})],
            [stub(("past tense of", "zarb"), **{"def": [{"sseq": [[sense("{bc}a thing")]]}]})],
            [stub()],
            [stub(cxs=[{"cxl": "past tense of"}]), stub(cxs=[{"cxl": "past tense of", "cxtis": []}])],
            [entry()],
            [],
        ],
    )
    def test_defined_other_or_targetless_entries_skipped(self, payload: list[Json]) -> None:
        assert inflection_stubs(payload=payload, query=QUERY) == []


class TestAsciiFold:
    @pytest.mark.parametrize(
        ("text", "folded"),
        [
            ("crèche", "creche"),
            ("Décolletage", "Decolletage"),
            ("coup d'état", "coup d'etat"),
            ("naïve", "naive"),
            ("Ångström", "Angstrom"),
            ("abandon", "abandon"),
            ("put paid to", "put paid to"),
            ("", ""),
            ("ß", "ß"),
            ("Straße", "Straße"),
        ],
    )
    def test_strips_combining_marks_only(self, *, text: str, folded: str) -> None:
        assert ascii_fold(text=text) == folded

    def test_result_is_composed(self) -> None:
        decomposed = "cre\u0300che"
        assert ascii_fold(text=decomposed) == "creche"
        assert ascii_fold(text="e\u0301le\u0300ve") == "eleve"


class TestAudioUrl:
    @pytest.mark.parametrize(
        ("ref", "subdir"),
        [
            ("abando01", "a"),
            ("troth001", "t"),
            ("bixcat01", "bix"),
            ("ggxyz", "gg"),
            ("_1thing", "number"),
            ("3d", "number"),
        ],
    )
    def test_subdirectory(self, *, ref: str, subdir: str) -> None:
        assert audio_url(base_url="https://m/", ref=ref) == f"https://m/{subdir}/{ref}.mp3"


class TestLookupFixtures:
    async def test_abandon_verb_entry(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        serve(upstream, query="abandon", audio=["abando01"])
        result = await provider.lookup(text="abandon")
        assert result.found
        assert result.suggestions == []
        assert len(result.entries) == 2
        verb = result.entries[0]
        assert verb.spellings == [SpellingIn(text="abandon", kind=SpellingKind.PRIMARY)]
        assert verb.part_of_speech == "verb"
        assert verb.pronunciation == f"ə-{STRESS}ban-dən"
        assert verb.audio_path == "/audio/abando01.mp3"
        assert verb.source is DefinitionSource.MW
        assert len(verb.definitions) == 7
        assert verb.definitions[0].text == "to give up to the control or influence of another person or agent"
        assert verb.definitions[0].examples == []
        assert verb.definitions[1].text == "to give up with the intent of never again claiming a right or interest in"
        assert verb.definitions[1].examples == []
        assert verb.definitions[2].text.startswith("to withdraw from often in the face of danger")
        assert all(not d.examples for d in verb.definitions)
        assert {d.source for d in verb.definitions} == {DefinitionSource.MW}

    async def test_abandon_fragment_examples_dropped(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        """Every recorded `vis` for `abandon` is a fragment (`*abandon* property`), so no draft keeps an example."""
        serve(upstream, query="abandon", audio=["abando01"])
        result = await provider.lookup(text="abandon")
        raw = mw_fixture(name="abandon")
        assert "{wi}abandon{/wi} property" in str(raw)
        assert "with reckless {wi}abandon{/wi}" in str(raw)
        assert [len(e.definitions) for e in result.entries] == [7, 1]
        assert all(not d.examples for e in result.entries for d in e.definitions)

    async def test_abandon_noun_inherits_sound(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        serve(upstream, query="abandon", audio=["abando01"])
        noun = (await provider.lookup(text="abandon")).entries[1]
        assert noun.part_of_speech == "noun"
        assert noun.pronunciation == f"ə-{STRESS}ban-dən"
        assert noun.audio_path == "/audio/abando01.mp3"
        assert noun.definitions == [
            DraftDefinition(
                text="a thorough yielding to natural impulses; especially: enthusiasm, exuberance",
                source=DefinitionSource.MW,
            )
        ]
        assert len(upstream.requested(url=mw_audio_url(ref="abando01"))) == 1

    async def test_request_carries_key_and_quoted_query(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        serve(upstream, query="put paid to", name="put_paid_to")
        await provider.lookup(text="put paid to")
        assert upstream.urls == [mw_url(query="put paid to")]
        request = upstream.requests[0]
        assert request.method == "GET"
        assert request.url.params["key"] == MW_API_KEY
        assert request.url.raw_path == b"/json/put%20paid%20to?key=test-key"

    async def test_phrase_without_pronunciation(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        serve(upstream, query="put paid to", name="put_paid_to")
        result = await provider.lookup(text="put paid to")
        assert len(result.entries) == 1
        phrase = result.entries[0]
        assert phrase.spellings == [SpellingIn(text="put paid to", kind=SpellingKind.PRIMARY)]
        assert phrase.part_of_speech == "phrase"
        assert phrase.pronunciation is None
        assert phrase.audio_path is None
        assert phrase.definitions == [
            DraftDefinition(text="(chiefly British) to finish off: bring an end to", source=DefinitionSource.MW)
        ]

    async def test_whitespace_normalised_case_kept(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        serve(upstream, query="Put paid TO", name="put_paid_to")
        result = await provider.lookup(text="  Put \t paid   TO ")
        assert upstream.requests[0].url.path == "/json/Put paid TO"
        assert result.found
        assert result.entries[0].spellings[0].text == "put paid to"

    async def test_unknown_word_returns_suggestions(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        serve(upstream, query="abandn")
        result = await provider.lookup(text="abandn")
        assert result.found is False
        assert result.entries == []
        assert result.suggestions == mw_fixture(name="abandn")
        assert len(result.suggestions) == 20
        assert result.suggestions[:2] == ["abound in", "abandon"]
        assert upstream.urls == [mw_url(query="abandn")]

    async def test_couldst_inflection_stub_drafted_on_its_own(
        self, provider: MWProvider, upstream: FakeUpstream
    ) -> None:
        serve(upstream, query="couldst", audio=["coulds01"])
        result = await provider.lookup(text="couldst")
        assert result.found
        assert result.suggestions == []
        assert result.entries == [
            DraftEntry(
                spellings=[SpellingIn(text="couldst", kind=SpellingKind.PRIMARY)],
                part_of_speech="verb",
                pronunciation=f"{STRESS}ku\u0307dst",
                audio_path="/audio/coulds01.mp3",
                definitions=[
                    DraftDefinition(
                        text="archaic past tense second-person singular of *can*", source=DefinitionSource.MW
                    )
                ],
                source=DefinitionSource.MW,
            )
        ]
        assert upstream.urls == [mw_url(query="couldst"), mw_audio_url(ref="coulds01")]

    async def test_couldst_voiced_respelling_preferred(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        """`couldst` lists the weak form `kədst` first; the stressed form carries the sound file and wins."""
        serve(upstream, query="couldst", audio=["coulds01"])
        prs = mw_fixture(name="couldst")[0]["hwi"]["prs"]
        assert [p.get("sound", {}).get("audio") for p in prs] == [None, "coulds01", None, None]
        [verb] = (await provider.lookup(text="couldst")).entries
        assert verb.pronunciation == prs[1]["mw"] != prs[0]["mw"]

    async def test_empty_payload_is_not_found(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        result = await lookup_synthetic(provider, upstream, payload=[])
        assert result.found is False
        assert result.suggestions == []
        assert upstream.urls == [mw_url(query=QUERY)]

    async def test_cognate_redirect_keeps_query_as_variant(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        serve(upstream, query="aggrandise")
        serve(upstream, query="aggrandize", audio=["aggran01"])
        result = await provider.lookup(text="aggrandise")
        assert upstream.urls == [
            mw_url(query="aggrandise"),
            mw_url(query="aggrandize"),
            mw_audio_url(ref="aggran01"),
        ]
        assert len(result.entries) == 1
        verb = result.entries[0]
        assert verb.spellings == [
            SpellingIn(text="aggrandize", kind=SpellingKind.PRIMARY),
            SpellingIn(text="aggrandise", kind=SpellingKind.VARIANT),
        ]
        assert verb.part_of_speech == "verb"
        assert verb.pronunciation == f"ə-{STRESS}gran-ˌdīz"
        assert verb.audio_path == "/audio/aggran01.mp3"
        assert verb.definitions[0].text == "to make great or greater: increase, enlarge"
        assert verb.definitions[0].examples == []

    async def test_colour_redirects_to_color(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        serve(upstream, query="colour")
        color = entry(
            meta={"id": "color:1", "stems": ["color", "colors"]},
            hwi={"hw": "col*or", "prs": [{"mw": f"{STRESS}kə-lər", "sound": {"audio": "color001"}}]},
        )
        upstream.json(url=mw_url(query="color"), payload=[color])
        upstream.audio(url=mw_audio_url(ref="color001"))
        result = await provider.lookup(text="colour")
        assert [(s.text, s.kind) for s in result.entries[0].spellings] == [
            ("color", SpellingKind.PRIMARY),
            ("colour", SpellingKind.VARIANT),
        ]
        assert result.entries[0].audio_path == "/audio/color001.mp3"

    async def test_troth_first_full_respelling_and_audio(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        serve(upstream, query="troth", audio=["troth001"])
        result = await provider.lookup(text="troth")
        noun, verb = result.entries
        assert noun.part_of_speech == "noun"
        assert noun.pronunciation == f"{STRESS}träth"
        assert noun.audio_path == "/audio/troth001.mp3"
        assert verb.part_of_speech == "verb"
        assert verb.pronunciation == f"{STRESS}träth"
        assert verb.audio_path == "/audio/troth001.mp3"
        assert upstream.urls == [mw_url(query="troth"), mw_audio_url(ref="troth001")]
        assert noun.definitions[1].text == "one's pledged word; also: betrothal"

    async def test_troth_elided_quotation_dropped(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        """The recorded `troth` entry quotes a passage elided with `\u2026`; such citations are not usage examples."""
        serve(upstream, query="troth", audio=["troth001"])
        noun = (await provider.lookup(text="troth")).entries[0]
        assert "\u2026 I don't remember the details" in str(mw_fixture(name="troth"))
        assert all(not d.examples for d in noun.definitions)

    async def test_in_extenso_italic_fragment_dropped(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        serve(upstream, query="in extenso", name="in_extenso", audio=["inexte01"])
        adverb = (await provider.lookup(text="in extenso")).entries[0]
        assert adverb.part_of_speech == "adverb"
        assert adverb.spellings[0].text == "in extenso"
        rendered = render(text="the passage was quoted {it}in extenso{/it}")
        assert rendered == "the passage was quoted *in extenso*"
        assert is_clean_sentence(text=rendered) is False
        assert adverb.definitions == [DraftDefinition(text="at full length", source=DefinitionSource.MW)]

    async def test_aide_de_camp(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        serve(upstream, query="aide-de-camp", name="aide_de_camp", audio=["aide_d02"])
        noun = (await provider.lookup(text="aide-de-camp")).entries[0]
        assert upstream.requests[0].url.path == "/json/aide-de-camp"
        assert noun.spellings[0].text == "aide-de-camp"
        assert noun.pronunciation == f"ˌād-di-{STRESS}kamp"
        assert noun.audio_path == "/audio/aide_d02.mp3"
        assert noun.definitions[0].text == "a military aide; also: a civilian aide usually to an executive"

    async def test_stem_query_keeps_headword(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        serve(upstream, query="abandoning", name="abandon", audio=["abando01"])
        result = await provider.lookup(text="abandoning")
        assert [e.part_of_speech for e in result.entries] == ["verb"]
        assert result.entries[0].spellings == [SpellingIn(text="abandon", kind=SpellingKind.PRIMARY)]


class TestLookupFailures:
    async def test_http_error(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        upstream.text(url=mw_url(query=QUERY), body="boom", status_code=500)
        with pytest.raises(ProviderError, match=r"^mw: HTTP 500$"):
            await provider.lookup(text=QUERY)

    async def test_invalid_json(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        upstream.text(url=mw_url(query=QUERY), body="<html>not json</html>")
        with pytest.raises(ProviderError, match=r"^mw: "):
            await provider.lookup(text=QUERY)

    @pytest.mark.parametrize("payload", [{}, {"meta": {}}, "abandon", 42])
    async def test_non_list_payload(self, provider: MWProvider, upstream: FakeUpstream, payload: object) -> None:
        upstream.json(url=mw_url(query=QUERY), payload=payload)
        with pytest.raises(ProviderError, match=r"^mw: unexpected payload shape$"):
            await provider.lookup(text=QUERY)

    async def test_transport_timeout(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        upstream.fail(url=mw_url(query=QUERY), exc=httpx.ReadTimeout(""))
        with pytest.raises(ProviderError, match=r"^mw: ReadTimeout$"):
            await provider.lookup(text=QUERY)

    async def test_unreachable_host(self, provider: MWProvider) -> None:
        with pytest.raises(ProviderError, match=r"^mw: no fake route for "):
            await provider.lookup(text=QUERY)

    async def test_error_message_hides_api_key(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        upstream.text(url=mw_url(query=QUERY), body="boom", status_code=500)
        with pytest.raises(ProviderError) as info:
            await provider.lookup(text=QUERY)
        assert MW_API_KEY not in str(info.value)

    async def test_redirect_to_unknown_target_is_not_found(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        serve(upstream, query="aggrandise")
        upstream.json(url=mw_url(query="aggrandize"), payload=["aggrandizement", "aggrandizer"])
        result = await provider.lookup(text="aggrandise")
        assert result.found is False


class TestAudioFailures:
    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(500, content=b"ID3", headers={"content-type": "audio/mpeg"}),
            httpx.Response(404, content=b"<html>missing</html>", headers={"content-type": "text/html"}),
            httpx.Response(200, content=b"<html>not audio</html>", headers={"content-type": "text/html"}),
            httpx.Response(200, content=b"", headers={"content-type": "audio/mpeg"}),
        ],
    )
    async def test_entry_survives_bad_audio(
        self, provider: MWProvider, upstream: FakeUpstream, response: httpx.Response
    ) -> None:
        serve(upstream, query="abandon")
        upstream.route(url=mw_audio_url(ref="abando01"), responder=lambda _request: response)
        result = await provider.lookup(text="abandon")
        assert [e.part_of_speech for e in result.entries] == ["verb", "noun"]
        assert [e.audio_path for e in result.entries] == [None, None]
        assert result.entries[0].pronunciation == f"ə-{STRESS}ban-dən"
        assert len(upstream.requested(url=mw_audio_url(ref="abando01"))) == 1

    async def test_audio_fetched_once_across_lookups(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        serve(upstream, query="abandon", audio=["abando01"])
        first = await provider.lookup(text="abandon")
        second = await provider.lookup(text="abandon")
        assert first.entries[0].audio_path == second.entries[0].audio_path == "/audio/abando01.mp3"
        assert len(upstream.requested(url=mw_audio_url(ref="abando01"))) == 1
        assert len(upstream.requested(url=mw_url(query="abandon"))) == 2


class TestSyntheticEntries:
    async def test_sseq_kinds_flattened(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        sseq = [
            [["bs", {"sense": {"dt": [["text", "{bc}binding sense"]]}}], sense("{bc}sub a", sn="a")],
            [["pseq", [sense("{bc}in sequence"), ["sen", {"sn": "2"}]]]],
            [["sen", {"sn": "3", "sls": ["archaic"]}]],
            [
                [
                    "sense",
                    {
                        "dt": [
                            ["uns", [[["text", "used in {it}phrases{/it}"], ["vis", [{"t": "A {wi}zorb{/wi} ride."}]]]]]
                        ]
                    },
                ]
            ],
            [["sense", {"dt": [["wsgram", "count"]]}]],
        ]
        result = await lookup_synthetic(provider, upstream, payload=[entry(definitions=[{"sseq": sseq}])])
        definitions = result.entries[0].definitions
        assert [d.text for d in definitions] == ["binding sense", "sub a", "in sequence", "used in *phrases*"]
        assert definitions[3].examples == [DraftExample(text="A *zorb* ride.", source=ExampleSource.MW)]

    async def test_divided_sense_without_label_says_also(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        divided = sense(
            "{bc}a thing",
            sdsense={"dt": [["text", "{bc}a {sx|gadget||}"], ["vis", [{"t": "A {wi}zorb{/wi} of sorts."}]]]},
        )
        result = await lookup_synthetic(provider, upstream, payload=[entry(definitions=[{"sseq": [[divided]]}])])
        assert result.entries[0].definitions == [
            DraftDefinition(
                text="a thing; also: a gadget",
                source=DefinitionSource.MW,
                examples=[DraftExample(text="A *zorb* of sorts.", source=ExampleSource.MW)],
            )
        ]

    async def test_only_clean_sentences_kept_as_examples(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        vis = [
            {"t": "a {wi}zorb{/wi} ride"},
            {"t": "She rolled downhill in a {wi}zorb{/wi}."},
            {"t": "Inside the {wi}zorb{/wi} I spun, / And tumbled with the hill."},
            {"t": "\u2026 the {wi}zorb{/wi} came to rest \u2026"},
            {"t": "They {wi}zorbed{/wi} {gloss}sic{/gloss} all afternoon."},
        ]
        sdsense = {"dt": [["text", "{bc}a ride in one"], ["vis", [{"t": "{wi}zorbing{/wi} downhill"}]]]}
        body = {"dt": [["text", "{bc}a ball"], ["vis", vis]], "sdsense": sdsense}
        payload = [entry(definitions=[{"sseq": [[["sense", body]]]}])]
        result = await lookup_synthetic(provider, upstream, payload=payload)
        assert result.entries[0].definitions == [
            DraftDefinition(
                text="a ball; also: a ride in one",
                source=DefinitionSource.MW,
                examples=[DraftExample(text="She rolled downhill in a *zorb*.", source=ExampleSource.MW)],
            )
        ]

    async def test_labels_from_block_and_sense(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        block = {"sls": ["chiefly British"], "sseq": [[sense("{bc}a thing", sls=["slang"])], [sense("{bc}other")]]}
        result = await lookup_synthetic(provider, upstream, payload=[entry(definitions=[block])])
        assert [d.text for d in result.entries[0].definitions] == [
            "(chiefly British, slang) a thing",
            "(chiefly British) other",
        ]

    async def test_empty_definition_blocks(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        payload = [entry(definitions=[{"sseq": [[["sense", {"dt": [["text", "{bc} "]]}]]]}, {}])]
        result = await lookup_synthetic(provider, upstream, payload=payload)
        assert result.found
        assert result.entries[0].definitions == []

    async def test_variants_validated_and_deduped(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        vrs = [
            {"vl": "or", "va": "zorb*ing"},
            {"vl": "also", "va": "ZORB"},
            {"vl": "or", "va": "zor / b"},
            {"vl": "or"},
            {"va": "zorb*ing"},
        ]
        result = await lookup_synthetic(provider, upstream, payload=[entry(vrs=vrs)])
        assert [(s.text, s.kind) for s in result.entries[0].spellings] == [
            (QUERY, SpellingKind.PRIMARY),
            ("zorbing", SpellingKind.VARIANT),
        ]

    @pytest.mark.parametrize("bad_first", [True, False])
    async def test_invalid_headword_drops_entry(
        self, provider: MWProvider, upstream: FakeUpstream, *, bad_first: bool
    ) -> None:
        bad = entry(meta={"id": "zorb:1", "stems": [QUERY]}, hwi={"hw": "zor / b"}, fl="verb")
        good = entry(meta={"id": "zorb:2", "stems": [QUERY]})
        payload = [bad, good] if bad_first else [good, bad]
        result = await lookup_synthetic(provider, upstream, payload=payload)
        assert [(e.spellings[0].text, e.part_of_speech) for e in result.entries] == [(QUERY, "noun")]

    async def test_invalid_headword_dropped_when_nothing_is_spelled_like_the_query(
        self, provider: MWProvider, upstream: FakeUpstream
    ) -> None:
        """Entries reached through `stems` all get drafted; a broken headword then vanishes at draft time."""
        bad = entry(meta={"id": "zorb:1", "stems": [QUERY, "zorbs"]}, hwi={"hw": "zor / b"}, fl="verb")
        result = await lookup_synthetic(provider, upstream, payload=[bad, entry()], query="zorbs")
        assert [(e.spellings[0].text, e.part_of_speech) for e in result.entries] == [(QUERY, "noun")]

    async def test_unselected_entries_ignored(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        other = entry(meta={"id": "a / b", "stems": [QUERY]}, hwi={"hw": "a / b"})
        result = await lookup_synthetic(provider, upstream, payload=[other, entry()])
        assert [e.spellings[0].text for e in result.entries] == [QUERY]

    async def test_headword_falls_back_to_meta_id(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        payload = [entry(meta={"id": "Zorb:1", "stems": [QUERY]}, hwi={}), entry(meta={"id": "Zorb:2"}, fl="verb")]
        payload[1].pop("hwi")
        result = await lookup_synthetic(provider, upstream, payload=payload)
        assert [(e.spellings[0].text, e.part_of_speech) for e in result.entries] == [(QUERY, "noun"), (QUERY, "verb")]

    async def test_missing_functional_label_is_phrase(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        payload = [entry(fl=None), entry(meta={"id": "zorb:2"})]
        payload[1].pop("fl")
        result = await lookup_synthetic(provider, upstream, payload=payload)
        assert [e.part_of_speech for e in result.entries] == ["phrase"]
        assert [d.text for d in result.entries[0].definitions] == ["a thing", "a thing"]

    async def test_accented_headword_gains_folded_variant(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        creche = entry(
            meta={"id": "creche", "stems": ["creche"]},
            hwi={"hw": "crèche"},
            vrs=[{"vl": "or", "va": "krèche"}, {"vl": "also", "va": "CRECHE"}],
        )
        result = await lookup_synthetic(provider, upstream, payload=[creche], query="creche")
        assert [(s.text, s.kind) for s in result.entries[0].spellings] == [
            ("crèche", SpellingKind.PRIMARY),
            ("creche", SpellingKind.VARIANT),
            ("krèche", SpellingKind.VARIANT),
        ]

    async def test_folded_variant_after_headword_before_redirect_query(
        self, provider: MWProvider, upstream: FakeUpstream
    ) -> None:
        upstream.json(url=mw_url(query="kresh"), payload=[stub(("variant of", "crèche"), meta={"id": "kresh"})])
        creche = entry(meta={"id": "crèche", "stems": ["crèche"]}, hwi={"hw": "crèche"}, vrs=[{"va": "krèche"}])
        upstream.json(url=mw_url(query="crèche"), payload=[creche])
        result = await provider.lookup(text="kresh")
        assert upstream.urls == [mw_url(query="kresh"), mw_url(query="crèche")]
        assert [(s.text, s.kind) for s in result.entries[0].spellings] == [
            ("crèche", SpellingKind.PRIMARY),
            ("creche", SpellingKind.VARIANT),
            ("krèche", SpellingKind.VARIANT),
            ("kresh", SpellingKind.VARIANT),
        ]

    @pytest.mark.parametrize("headword", ["zorb", "Zorb", "zor*b", "put paid to", "coup d'etat", "Straße"])
    async def test_plain_headword_has_no_folded_variant(
        self, provider: MWProvider, upstream: FakeUpstream, headword: str
    ) -> None:
        result = await lookup_synthetic(provider, upstream, payload=[entry(hwi={"hw": headword})])
        assert len(result.entries[0].spellings) == 1
        assert result.entries[0].spellings[0].kind is SpellingKind.PRIMARY

    async def test_homographs_share_sound_from_later_entry(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        stems = {"stems": ["zorbing"]}
        silent = entry(meta={"id": "zorb:1", **stems})
        voiced = entry(meta={"id": "zorb:2", **stems}, hwi={"hw": QUERY, "prs": [{"mw": f"{STRESS}zȯrb"}]}, fl="verb")
        other = entry(meta={"id": "zorbs", **stems}, hwi={"hw": "zorbs"}, fl="plural noun")
        result = await lookup_synthetic(provider, upstream, payload=[silent, voiced, other], query="zorbing")
        assert [e.spellings[0].text for e in result.entries] == [QUERY, QUERY, "zorbs"]
        assert [e.part_of_speech for e in result.entries] == ["noun", "verb", "plural noun"]
        assert [e.pronunciation for e in result.entries] == [f"{STRESS}zȯrb", f"{STRESS}zȯrb", None]
        assert [e.audio_path for e in result.entries] == [None, None, None]

    async def test_same_part_of_speech_homographs_merged(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        first = entry(
            meta={"id": "zorb:1", "stems": [QUERY]},
            hwi={"hw": QUERY, "prs": [{"mw": f"{STRESS}zȯrb", "sound": {"audio": "zorb0001"}}]},
            vrs=[{"vl": "or", "va": "zorbe"}],
            definitions=[{"sseq": [[sense("{bc}a ball")]]}],
        )
        second = entry(
            meta={"id": "zorb:2", "stems": [QUERY]},
            hwi={"hw": QUERY, "prs": [{"mw": f"{STRESS}zorb-two", "sound": {"audio": "zorb0002"}}]},
            vrs=[{"vl": "or", "va": "ZORBE"}, {"vl": "also", "va": "zorbing"}],
            definitions=[{"sseq": [[sense("{bc}a ride"), sense("{bc}a sport")]]}],
        )
        upstream.audio(url=mw_audio_url(ref="zorb0001", subdir="z"))
        upstream.audio(url=mw_audio_url(ref="zorb0002", subdir="z"))
        result = await lookup_synthetic(provider, upstream, payload=[first, second])
        [noun] = result.entries
        assert noun.part_of_speech == "noun"
        assert [(s.text, s.kind) for s in noun.spellings] == [
            (QUERY, SpellingKind.PRIMARY),
            ("zorbe", SpellingKind.VARIANT),
            ("zorbing", SpellingKind.VARIANT),
        ]
        assert noun.pronunciation == f"{STRESS}zȯrb"
        assert noun.audio_path == "/audio/zorb0001.mp3"
        assert [d.text for d in noun.definitions] == ["a ball", "a ride", "a sport"]
        assert noun.source is DefinitionSource.MW

    async def test_functional_label_normalised(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        payload = [entry(fl=" Transitive  Verb "), entry(meta={"id": "zorb:2"}, fl="transitive verb")]
        result = await lookup_synthetic(provider, upstream, payload=payload)
        assert [e.part_of_speech for e in result.entries] == ["transitive verb"]
        assert len(result.entries[0].definitions) == 2


class TestPronunciationSelection:
    """Which `hwi.prs` item supplies the respelling and which the sound file."""

    async def test_partial_respelling_skipped(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        prs = [{"mw": "-ˌbing", "sound": {}}, {"sound": {"audio": "zorb0001"}}, {"mw": f"{STRESS}zȯrb"}]
        upstream.audio(url=mw_audio_url(ref="zorb0001", subdir="z"))
        result = await lookup_synthetic(provider, upstream, payload=[entry(hwi={"hw": QUERY, "prs": prs})])
        assert result.entries[0].pronunciation == f"{STRESS}zȯrb"
        assert result.entries[0].audio_path == "/audio/zorb0001.mp3"

    async def test_voiced_trailing_partial_skipped(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        """`nascent` voices only its opening syllable (`nA-`); the full unvoiced respelling must still win."""
        prs = [{"mw": f"{STRESS}zȯ-", "sound": {"audio": "zorb0001"}}, {"mw": f"{STRESS}zȯrb"}, {"mw": "zərb"}]
        upstream.audio(url=mw_audio_url(ref="zorb0001", subdir="z"))
        result = await lookup_synthetic(provider, upstream, payload=[entry(hwi={"hw": QUERY, "prs": prs})])
        assert result.entries[0].pronunciation == f"{STRESS}zȯrb"
        assert result.entries[0].audio_path == "/audio/zorb0001.mp3"

    async def test_only_partials_leave_pronunciation_empty(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        prs = [{"mw": f"{STRESS}zȯ-", "sound": {"audio": "zorb0001"}}, {"mw": "-ˌbing"}, {"mw": "-"}, {"mw": ""}]
        upstream.audio(url=mw_audio_url(ref="zorb0001", subdir="z"))
        result = await lookup_synthetic(provider, upstream, payload=[entry(hwi={"hw": QUERY, "prs": prs})])
        assert result.entries[0].pronunciation is None
        assert result.entries[0].audio_path == "/audio/zorb0001.mp3"

    async def test_voiced_respelling_preferred_over_earlier_silent_one(
        self, provider: MWProvider, upstream: FakeUpstream
    ) -> None:
        """Respelling: first complete form with a sound file. Audio: first `sound.audio` anywhere, partial or not."""
        prs = [
            {"mw": "zərb"},
            {"mw": "-ˌbing", "sound": {"audio": "zorb0002"}},
            {"mw": f"{STRESS}zȯrb", "sound": {}},
            {"mw": "zȯrb-two", "sound": {"audio": "zorb0001"}},
        ]
        upstream.audio(url=mw_audio_url(ref="zorb0001", subdir="z"))
        upstream.audio(url=mw_audio_url(ref="zorb0002", subdir="z"))
        result = await lookup_synthetic(provider, upstream, payload=[entry(hwi={"hw": QUERY, "prs": prs})])
        assert result.entries[0].pronunciation == "zȯrb-two"
        assert result.entries[0].audio_path == "/audio/zorb0002.mp3"
        assert upstream.urls == [mw_url(query=QUERY), mw_audio_url(ref="zorb0002", subdir="z")]

    @pytest.mark.parametrize(
        "prs",
        [
            [{"mw": "zərb"}, {"mw": f"{STRESS}zȯrb"}],
            [{"mw": "zərb"}, {"mw": f"{STRESS}zȯrb", "sound": {}}, {"mw": "-ˌbing", "sound": {"audio": ""}}],
            [{"mw": "zərb", "sound": {"ref": "z"}}, {"sound": {"audio": None}}],
        ],
    )
    async def test_without_sound_first_complete_respelling_kept(
        self, provider: MWProvider, upstream: FakeUpstream, prs: list[Json]
    ) -> None:
        result = await lookup_synthetic(provider, upstream, payload=[entry(hwi={"hw": QUERY, "prs": prs})])
        assert result.entries[0].pronunciation == "zərb"
        assert result.entries[0].audio_path is None
        assert upstream.urls == [mw_url(query=QUERY)]


class TestInflectionStubDrafts:
    async def test_cross_reference_becomes_the_only_definition(
        self, provider: MWProvider, upstream: FakeUpstream
    ) -> None:
        result = await lookup_synthetic(provider, upstream, payload=[stub(("past tense of", "zarb:1"))])
        assert result.found
        assert result.suggestions == []
        assert result.entries == [
            DraftEntry(
                spellings=[SpellingIn(text=QUERY, kind=SpellingKind.PRIMARY)],
                part_of_speech="verb",
                pronunciation=None,
                audio_path=None,
                definitions=[DraftDefinition(text="past tense of *zarb*", source=DefinitionSource.MW)],
                source=DefinitionSource.MW,
            )
        ]
        assert upstream.urls == [mw_url(query=QUERY)]

    async def test_unrecognised_label_is_other(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        result = await lookup_synthetic(provider, upstream, payload=[stub(("see also", "foo"))])
        [other] = result.entries
        assert other.part_of_speech == "other"
        assert [d.text for d in other.definitions] == ["see also *foo*"]
        assert other.definitions[0].examples == []

    async def test_cross_references_merged_by_part_of_speech(
        self, provider: MWProvider, upstream: FakeUpstream
    ) -> None:
        payload = [stub(("past tense of", "zarb"), ("plural of", "zorp"), ("past participle of", "zarb"))]
        result = await lookup_synthetic(provider, upstream, payload=payload)
        assert [(e.part_of_speech, [d.text for d in e.definitions]) for e in result.entries] == [
            ("verb", ["past tense of *zarb*", "past participle of *zarb*"]),
            ("noun", ["plural of *zorp*"]),
        ]

    async def test_homograph_stubs_share_sound(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        voiced = stub(("past tense of", "zarb"), meta={"id": "zorb:1"}, hwi={"hw": QUERY, "prs": [{"mw": "zərb"}]})
        silent = stub(("plural of", "zorp"), meta={"id": "zorb:2"})
        result = await lookup_synthetic(provider, upstream, payload=[voiced, silent])
        assert [(e.part_of_speech, e.pronunciation) for e in result.entries] == [("verb", "zərb"), ("noun", "zərb")]

    async def test_stub_keeps_own_sound_and_variants(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        prs = [{"mw": "zərb"}, {"mw": f"{STRESS}zȯrb", "sound": {"audio": "zorb0001"}}]
        voiced = stub(("past tense of", "zarb"), hwi={"hw": "zo*rb", "prs": prs}, vrs=[{"vl": "or", "va": "zorbe"}])
        upstream.audio(url=mw_audio_url(ref="zorb0001", subdir="z"))
        result = await lookup_synthetic(provider, upstream, payload=[voiced])
        [verb] = result.entries
        assert [(s.text, s.kind) for s in verb.spellings] == [
            (QUERY, SpellingKind.PRIMARY),
            ("zorbe", SpellingKind.VARIANT),
        ]
        assert verb.pronunciation == f"{STRESS}zȯrb"
        assert verb.audio_path == "/audio/zorb0001.mp3"
        assert upstream.urls == [mw_url(query=QUERY), mw_audio_url(ref="zorb0001", subdir="z")]

    @pytest.mark.parametrize("stub_first", [True, False])
    async def test_stub_ignored_when_an_entry_is_defined(
        self, provider: MWProvider, upstream: FakeUpstream, *, stub_first: bool
    ) -> None:
        inflection = stub(("past tense of", "zarb"), meta={"id": "zorb:2", "stems": [QUERY]}, fl="verb")
        payload = [inflection, entry()] if stub_first else [entry(), inflection]
        result = await lookup_synthetic(provider, upstream, payload=payload)
        assert [(e.part_of_speech, [d.text for d in e.definitions]) for e in result.entries] == [("noun", ["a thing"])]

    async def test_stub_ignored_when_a_stem_entry_is_defined(
        self, provider: MWProvider, upstream: FakeUpstream
    ) -> None:
        zorbs = stub(("plural of", QUERY), meta={"id": "zorbs", "stems": ["zorbs"]})
        result = await lookup_synthetic(provider, upstream, payload=[zorbs, entry()], query="zorbs")
        assert [(e.spellings[0].text, [d.text for d in e.definitions]) for e in result.entries] == [
            (QUERY, ["a thing"])
        ]

    @pytest.mark.parametrize(
        "payload",
        [
            [stub()],
            [stub(cxs=[{"cxl": "past tense of", "cxtis": [{"cxr": "zarb"}]}])],
            [stub(("past tense of", "zarb"), meta={"id": "zorbs", "stems": [QUERY]})],
        ],
    )
    async def test_stub_without_usable_cross_reference_is_not_found(
        self, provider: MWProvider, upstream: FakeUpstream, payload: list[Json]
    ) -> None:
        result = await lookup_synthetic(provider, upstream, payload=payload)
        assert result.found is False
        assert result.entries == []
        assert result.suggestions == []
        assert upstream.urls == [mw_url(query=QUERY)]

    async def test_spelling_redirect_wins_over_inflection_stub(
        self, provider: MWProvider, upstream: FakeUpstream
    ) -> None:
        payload = [stub(("British spelling of", "zorbe")), stub(("past tense of", "zarb"), meta={"id": "zorb:2"})]
        zorbe = entry(meta={"id": "zorbe", "stems": ["zorbe"]}, hwi={"hw": "zorbe"})
        upstream.json(url=mw_url(query="zorbe"), payload=[zorbe])
        result = await lookup_synthetic(provider, upstream, payload=payload)
        assert upstream.urls == [mw_url(query=QUERY), mw_url(query="zorbe")]
        [noun] = result.entries
        assert [(s.text, s.kind) for s in noun.spellings] == [
            ("zorbe", SpellingKind.PRIMARY),
            (QUERY, SpellingKind.VARIANT),
        ]
        assert [d.text for d in noun.definitions] == ["a thing"]

    async def test_inflection_stub_used_when_redirect_target_unknown(
        self, provider: MWProvider, upstream: FakeUpstream
    ) -> None:
        payload = [stub(("British spelling of", "zorbe")), stub(("past tense of", "zarb"), meta={"id": "zorb:2"})]
        upstream.json(url=mw_url(query="zorbe"), payload=["zorbs", "zorro"])
        result = await lookup_synthetic(provider, upstream, payload=payload)
        assert upstream.urls == [mw_url(query=QUERY), mw_url(query="zorbe")]
        assert result.suggestions == []
        [verb] = result.entries
        assert verb.spellings == [SpellingIn(text=QUERY, kind=SpellingKind.PRIMARY)]
        assert [d.text for d in verb.definitions] == ["past tense of *zarb*"]

    async def test_stub_with_invalid_headword_dropped(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        result = await lookup_synthetic(
            provider, upstream, payload=[stub(("past tense of", "zarb"), hwi={"hw": "zor / b"})]
        )
        assert result.found is False
        assert result.entries == []


def creche(**overrides: t.Any) -> Json:
    """MW noun entry for `crèche`: accented headword, ASCII `meta.id`, one voiced respelling."""
    base = entry(
        meta={"id": "creche", "stems": ["creche", "creches"]},
        hwi={"hw": "crèche", "prs": [{"mw": f"{STRESS}kresh", "sound": {"audio": "creche02"}}]},
        definitions=[{"sseq": [[sense("{bc}a day nursery")]]}],
    )
    base.update(overrides)
    return base


class TestAccentedQueries:
    async def test_folded_query_fetched_when_accented_one_is_unknown(
        self, provider: MWProvider, upstream: FakeUpstream
    ) -> None:
        upstream.json(url=mw_url(query="Crèche"), payload=["creche", "crèches"])
        upstream.json(url=mw_url(query="Creche"), payload=[creche()])
        upstream.audio(url=mw_audio_url(ref="creche02"))
        result = await provider.lookup(text="Crèche")
        assert upstream.urls == [mw_url(query="Crèche"), mw_url(query="Creche"), mw_audio_url(ref="creche02")]
        assert upstream.requests[0].url.raw_path == b"/json/Cr%C3%A8che?key=test-key"
        assert result.suggestions == []
        assert result.entries == [
            DraftEntry(
                spellings=[
                    SpellingIn(text="crèche", kind=SpellingKind.PRIMARY),
                    SpellingIn(text="creche", kind=SpellingKind.VARIANT),
                ],
                part_of_speech="noun",
                pronunciation=f"{STRESS}kresh",
                audio_path="/audio/creche02.mp3",
                definitions=[DraftDefinition(text="a day nursery", source=DefinitionSource.MW)],
                source=DefinitionSource.MW,
            )
        ]

    @pytest.mark.parametrize(
        ("answers", "suggestions"),
        [
            ((["crèches", "crèche"], ["creche"]), ["crèches", "crèche"]),
            (([], ["creche", "crèches"]), ["creche", "crèches"]),
            (([], []), []),
        ],
    )
    async def test_suggestions_from_first_fetch_that_has_any(
        self,
        provider: MWProvider,
        upstream: FakeUpstream,
        *,
        answers: tuple[list[str], list[str]],
        suggestions: list[str],
    ) -> None:
        """`answers` are the suggestion lists MW returns for the accented and the folded query, in order."""
        accented, folded = answers
        upstream.json(url=mw_url(query="crèche"), payload=accented)
        upstream.json(url=mw_url(query="creche"), payload=folded)
        result = await provider.lookup(text="crèche")
        assert result.found is False
        assert result.entries == []
        assert result.suggestions == suggestions
        assert upstream.urls == [mw_url(query="crèche"), mw_url(query="creche")]

    async def test_accented_query_known_directly_is_not_refetched(
        self, provider: MWProvider, upstream: FakeUpstream
    ) -> None:
        upstream.json(url=mw_url(query="crèche"), payload=[creche(meta={"id": "crèche", "stems": ["crèche"]})])
        upstream.audio(url=mw_audio_url(ref="creche02"))
        result = await provider.lookup(text="crèche")
        assert upstream.urls == [mw_url(query="crèche"), mw_audio_url(ref="creche02")]
        assert [(s.text, s.kind) for s in result.entries[0].spellings] == [
            ("crèche", SpellingKind.PRIMARY),
            ("creche", SpellingKind.VARIANT),
        ]

    async def test_folded_query_failure_is_a_provider_error(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        upstream.json(url=mw_url(query="crèche"), payload=[])
        upstream.text(url=mw_url(query="creche"), body="boom", status_code=500)
        with pytest.raises(ProviderError, match=r"^mw: HTTP 500$"):
            await provider.lookup(text="crèche")

    async def test_folded_query_drives_redirect_and_variant(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        """After folding, the ASCII query is what gets redirected and kept as variant; the accented input is dropped."""
        upstream.json(url=mw_url(query="colöur"), payload=[])
        serve(upstream, query="colour")
        upstream.json(
            url=mw_url(query="color"), payload=[entry(meta={"id": "color:1", "stems": ["color"]}, hwi={"hw": "col*or"})]
        )
        result = await provider.lookup(text="colöur")
        assert upstream.urls == [mw_url(query="colöur"), mw_url(query="colour"), mw_url(query="color")]
        assert [(s.text, s.kind) for s in result.entries[0].spellings] == [
            ("color", SpellingKind.PRIMARY),
            ("colour", SpellingKind.VARIANT),
        ]

    async def test_folded_query_drives_inflection_stub(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        upstream.json(url=mw_url(query="coüldst"), payload=["couldst"])
        serve(upstream, query="couldst", audio=["coulds01"])
        [verb] = (await provider.lookup(text="coüldst")).entries
        assert verb.spellings == [SpellingIn(text="couldst", kind=SpellingKind.PRIMARY)]
        assert [d.text for d in verb.definitions] == ["archaic past tense second-person singular of *can*"]


def homograph(*, ident: str, hw: str, fl: str, gloss: str) -> Json:
    """Defined MW entry; accented headwords share their ASCII `meta.id` with plain ones (`lame` / `lamé`)."""
    return entry(
        meta={"id": ident, "stems": [hw.replace("*", "")]},
        hwi={"hw": hw},
        fl=fl,
        definitions=[{"sseq": [[sense(f"{{bc}}{gloss}")]]}],
    )


def voiced(item: Json, *, mw: str, audio: str) -> Json:
    """Give `item` a single respelling that carries a sound file."""
    item["hwi"]["prs"] = [{"mw": mw, "sound": {"audio": audio}}]
    return item


LAM: t.Final = f"{STRESS}lām"
LAME: t.Final = [
    voiced(homograph(ident="lame:1", hw="lame", fl="adjective", gloss="unable to walk"), mw=LAM, audio="lame0001"),
    voiced(homograph(ident="lame:2", hw="lame", fl="noun", gloss="a thin plate"), mw=LAM, audio="lame0001"),
    voiced(
        homograph(ident="lame:3", hw="la*mé", fl="noun", gloss="a brocaded fabric"),
        mw=f"lä-{STRESS}mā",
        audio="lame0002",
    ),
]


class TestPreferHeadword:
    def test_keeps_entries_spelled_like_the_query(self) -> None:
        assert prefer_headword(entries=LAME, query="Lame") == LAME[:2]

    def test_keeps_the_accented_entry_for_an_accented_query(self) -> None:
        assert prefer_headword(entries=LAME, query="LAMÉ") == LAME[2:]

    def test_keeps_everything_when_nothing_is_spelled_like_the_query(self) -> None:
        assert prefer_headword(entries=LAME, query="elan") == LAME
        assert prefer_headword(entries=[], query="lame") == []

    def test_falls_back_to_the_id_without_headword(self) -> None:
        bare = entry(meta={"id": "zorb:1", "stems": [QUERY]}, hwi={})
        assert prefer_headword(entries=[bare], query=QUERY) == [bare]


class TestAccentedHomographs:
    async def test_plain_query_leaves_the_accented_homograph_out(
        self, provider: MWProvider, upstream: FakeUpstream
    ) -> None:
        upstream.audio(url=mw_audio_url(ref="lame0001"))
        result = await lookup_synthetic(provider, upstream, payload=LAME, query="Lame")
        assert [(e.part_of_speech, [s.text for s in e.spellings], e.pronunciation) for e in result.entries] == [
            ("adjective", ["lame"], f"{STRESS}lām"),
            ("noun", ["lame"], f"{STRESS}lām"),
        ]
        assert [d.text for d in result.entries[1].definitions] == ["a thin plate"]
        assert mw_audio_url(ref="lame0002") not in upstream.urls

    async def test_accented_query_gets_only_its_own_entry(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        upstream.json(url=mw_url(query="Lamé"), payload=["lame", "lamed"])
        upstream.json(url=mw_url(query="Lame"), payload=LAME)
        upstream.audio(url=mw_audio_url(ref="lame0002"))
        [fabric] = (await provider.lookup(text="Lamé")).entries
        assert fabric.part_of_speech == "noun"
        assert [(s.text, s.kind) for s in fabric.spellings] == [
            ("lamé", SpellingKind.PRIMARY),
            ("lame", SpellingKind.VARIANT),
        ]
        assert fabric.pronunciation == f"lä-{STRESS}mā"
        assert fabric.audio_path == "/audio/lame0002.mp3"
        assert [d.text for d in fabric.definitions] == ["a brocaded fabric"]

    async def test_accented_only_entry_answers_the_plain_query(
        self, provider: MWProvider, upstream: FakeUpstream
    ) -> None:
        elan = voiced(
            homograph(ident="elan", hw="élan", fl="noun", gloss="vigorous spirit"),
            mw=f"ā-{STRESS}län",
            audio="elan0001",
        )
        upstream.audio(url=mw_audio_url(ref="elan0001"))
        [noun] = (await lookup_synthetic(provider, upstream, payload=[elan], query="Elan")).entries
        assert [(s.text, s.kind) for s in noun.spellings] == [
            ("élan", SpellingKind.PRIMARY),
            ("elan", SpellingKind.VARIANT),
        ]
        assert noun.audio_path == "/audio/elan0001.mp3"

    async def test_redirect_target_is_filtered_too(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        serve(upstream, query="colour")
        color = voiced(
            homograph(ident="color:1", hw="col*or", fl="noun", gloss="a hue"), mw=f"{STRESS}kə-lər", audio="color001"
        )
        colore = voiced(
            homograph(ident="color:2", hw="col*oré", fl="adjective", gloss="dyed"),
            mw=f"kə-lə-{STRESS}rā",
            audio="color002",
        )
        upstream.json(url=mw_url(query="color"), payload=[color, colore])
        upstream.audio(url=mw_audio_url(ref="color001"))
        [noun] = (await provider.lookup(text="colour")).entries
        assert noun.part_of_speech == "noun"
        assert [(s.text, s.kind) for s in noun.spellings] == [
            ("color", SpellingKind.PRIMARY),
            ("colour", SpellingKind.VARIANT),
        ]
        assert mw_audio_url(ref="color002") not in upstream.urls


def draft(*spellings: str, part_of_speech: str = "verb") -> DraftEntry:
    """Draft with `spellings` (first primary) and no definitions, as `MWProvider._draft` would build it."""
    return DraftEntry(
        spellings=[
            SpellingIn(text=s, kind=SpellingKind.PRIMARY if i == 0 else SpellingKind.VARIANT)
            for i, s in enumerate(spellings)
        ],
        part_of_speech=part_of_speech,
        definitions=[],
        source=DefinitionSource.MW,
    )


def uro(ure: str, fl: str | None = "noun", **extra: t.Any) -> Json:
    """One undefined run-on (`uros` item); `fl=None` leaves the functional label out."""
    item: Json = {"ure": ure, **extra}
    if fl is not None:
        item["fl"] = fl
    return item


NITPICK_PRS: t.Final[list[Json]] = [{"mw": f"{STRESS}nit-\u02ccpik", "sound": {"audio": "nitpic02"}}]


def nitpick(*, uros: list[Json] | None = None, **overrides: t.Any) -> Json:
    """MW verb entry for `nitpick` whose noun exists only as a run-on, next to the derived `nitpicker`."""
    base: Json = {
        "meta": {"id": "nitpick", "stems": ["nitpick", "nitpicks", "nitpicker"]},
        "hwi": {"hw": "nit*pick", "prs": [{"mw": f"{STRESS}nit-\u02ccpik", "sound": {"audio": "nitpic01"}}]},
        "fl": "verb",
        "def": [{"sseq": [[sense("{bc}to criticize by {d_link|nit-picking|nit-picking}")]]}],
        "uros": [uro("nit*pick", prs=NITPICK_PRS), uro("nit*pick*er")] if uros is None else uros,
    }
    base.update(overrides)
    return base


class TestRunOnCandidates:
    def test_same_spelling_with_label_is_a_candidate(self) -> None:
        candidates = run_on_candidates(entry=nitpick(), draft=draft("nitpick"))
        assert candidates == [("noun", NITPICK_PRS)]

    @pytest.mark.parametrize("ure", ["nit*pick*er", "nitpicky", "nit-pick", "nit*picks", "", "picknit"])
    def test_other_spellings_skipped(self, ure: str) -> None:
        assert run_on_candidates(entry=nitpick(uros=[uro(ure)]), draft=draft("nitpick")) == []

    @pytest.mark.parametrize("ure", ["nit*pick", "nitpick", "NitPick", "Nit*Pick", " nitpick ", "nit*pick  "])
    def test_spelling_compared_by_key(self, ure: str) -> None:
        assert run_on_candidates(entry=nitpick(uros=[uro(ure)]), draft=draft("Nitpick")) == [("noun", [])]

    @pytest.mark.parametrize("fl", [None, ""])
    def test_without_functional_label_skipped(self, fl: str | None) -> None:
        assert run_on_candidates(entry=nitpick(uros=[uro("nit*pick", fl=fl)]), draft=draft("nitpick")) == []

    def test_label_kept_verbatim_and_prs_default_empty(self) -> None:
        entry_ = nitpick(uros=[uro("nitpick", fl=" Plural Noun "), uro("nitpick", fl="adjective", prs=NITPICK_PRS)])
        assert run_on_candidates(entry=entry_, draft=draft("nitpick")) == [
            (" Plural Noun ", []),
            ("adjective", NITPICK_PRS),
        ]

    @pytest.mark.parametrize("payload", [{}, {"uros": []}, {"uros": [{}]}, {"uros": [{"fl": "noun"}]}])
    def test_no_usable_run_ons(self, payload: Json) -> None:
        assert run_on_candidates(entry=payload, draft=draft("nitpick")) == []

    def test_compares_against_the_headword_only(self) -> None:
        candidates = run_on_candidates(entry=nitpick(uros=[uro("nit-pick")]), draft=draft("nitpick", "nit-pick"))
        assert candidates == []

    def test_draft_without_spellings_matches_nothing(self) -> None:
        empty = DraftEntry(spellings=[], part_of_speech="verb", definitions=[], source=DefinitionSource.MW)
        assert run_on_candidates(entry=nitpick(), draft=empty) == []

    def test_non_string_fields_coerced(self) -> None:
        entry_ = nitpick(uros=[{"ure": 42, "fl": 7}])
        assert run_on_candidates(entry=entry_, draft=draft("42")) == [("7", [])]


class TestRunOns:
    async def test_run_on_becomes_empty_draft(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        upstream.audio(url=mw_audio_url(ref="nitpic01"))
        upstream.audio(url=mw_audio_url(ref="nitpic02"))
        result = await lookup_synthetic(provider, upstream, payload=[nitpick()], query="nitpick")
        verb, noun = result.entries
        assert verb.part_of_speech == "verb"
        assert [d.text for d in verb.definitions] == ["to criticize by nit-picking"]
        assert verb.audio_path == "/audio/nitpic01.mp3"
        assert noun == DraftEntry(
            spellings=[SpellingIn(text="nitpick", kind=SpellingKind.PRIMARY)],
            part_of_speech="noun",
            pronunciation=f"{STRESS}nit-\u02ccpik",
            audio_path="/audio/nitpic02.mp3",
            definitions=[],
            source=DefinitionSource.MW,
        )
        assert upstream.urls == [mw_url(query="nitpick"), mw_audio_url(ref="nitpic01"), mw_audio_url(ref="nitpic02")]

    async def test_derived_words_not_included(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        upstream.audio(url=mw_audio_url(ref="nitpic01"))
        upstream.audio(url=mw_audio_url(ref="nitpic02"))
        result = await lookup_synthetic(provider, upstream, payload=[nitpick()], query="nitpick")
        assert [e.part_of_speech for e in result.entries] == ["verb", "noun"]
        assert {s.text for e in result.entries for s in e.spellings} == {"nitpick"}

    async def test_run_on_without_sound_inherits_from_headword(
        self, provider: MWProvider, upstream: FakeUpstream
    ) -> None:
        upstream.audio(url=mw_audio_url(ref="nitpic01"))
        payload = [nitpick(uros=[uro("nit*pick"), uro("nit*pick", fl="adjective")])]
        result = await lookup_synthetic(provider, upstream, payload=payload, query="nitpick")
        assert [e.part_of_speech for e in result.entries] == ["verb", "noun", "adjective"]
        assert [e.pronunciation for e in result.entries] == [f"{STRESS}nit-\u02ccpik"] * 3
        assert [e.audio_path for e in result.entries] == ["/audio/nitpic01.mp3"] * 3
        assert len(upstream.requested(url=mw_audio_url(ref="nitpic01"))) == 1

    async def test_existing_part_of_speech_not_duplicated(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        upstream.audio(url=mw_audio_url(ref="nitpic01"))
        upstream.audio(url=mw_audio_url(ref="nitpic02"))
        noun_entry = {
            "meta": {"id": "nitpick:2", "stems": ["nitpick"]},
            "hwi": {"hw": "nit*pick"},
            "fl": "noun",
            "def": [{"sseq": [[sense("{bc}a petty criticism")]]}],
        }
        result = await lookup_synthetic(provider, upstream, payload=[nitpick(), noun_entry], query="nitpick")
        assert [e.part_of_speech for e in result.entries] == ["verb", "noun"]
        noun = result.entries[1]
        assert [d.text for d in noun.definitions] == ["a petty criticism"]
        assert noun.pronunciation == f"{STRESS}nit-\u02ccpik"
        assert noun.audio_path == "/audio/nitpic02.mp3"
        assert noun.spellings == [SpellingIn(text="nitpick", kind=SpellingKind.PRIMARY)]

    async def test_run_on_label_normalised_on_merge(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        upstream.audio(url=mw_audio_url(ref="nitpic01"))
        payload = [nitpick(uros=[uro("nitpick", fl=" Noun "), uro("nitpick", fl="NOUN")])]
        result = await lookup_synthetic(provider, upstream, payload=payload, query="nitpick")
        assert [e.part_of_speech for e in result.entries] == ["verb", "noun"]
        assert result.entries[1].definitions == []

    async def test_run_on_keeps_cognate_variant(self, provider: MWProvider, upstream: FakeUpstream) -> None:
        serve(upstream, query="colour")
        color = entry(
            meta={"id": "color:1", "stems": ["color", "colors"]},
            hwi={"hw": "col*or"},
            uros=[uro("col*or", fl="verb"), uro("col*or*ful", fl="adjective")],
        )
        upstream.json(url=mw_url(query="color"), payload=[color])
        result = await provider.lookup(text="colour")
        assert [e.part_of_speech for e in result.entries] == ["noun", "verb"]
        for drafted in result.entries:
            assert [(s.text, s.kind) for s in drafted.spellings] == [
                ("color", SpellingKind.PRIMARY),
                ("colour", SpellingKind.VARIANT),
            ]

    async def test_entries_without_definitions_have_no_run_ons(
        self, provider: MWProvider, upstream: FakeUpstream
    ) -> None:
        stub = nitpick()
        stub.pop("def")
        result = await lookup_synthetic(provider, upstream, payload=[stub], query="nitpick")
        assert result.found is False
        assert upstream.urls == [mw_url(query="nitpick")]
