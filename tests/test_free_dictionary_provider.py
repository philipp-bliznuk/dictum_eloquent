import typing as t

import httpx
import pytest

from dictum.enums import DefinitionSource
from dictum.enums import ExampleSource
from dictum.enums import SpellingKind
from dictum.providers.base import ProviderError
from dictum.providers.base import ProviderResult
from dictum.providers.base import describe
from dictum.providers.base import highlight
from dictum.providers.free_dictionary import FreeDictionaryProvider
from dictum.schemas.lookup import DraftDefinition
from dictum.schemas.lookup import DraftEntry
from dictum.schemas.lookup import DraftExample
from dictum.schemas.word import SpellingIn
from tests.conftest import FREE_DICTIONARY_BASE_URL
from tests.conftest import MP3_BYTES
from tests.conftest import free_dictionary_url

if t.TYPE_CHECKING:
    import pathlib

    from dictum.services.audio import AudioCache
    from tests.conftest import FakeUpstream

Json = dict[str, t.Any]

AUDIO_URL: t.Final = "https://media.free.test/pronunciations/en/abandon-us.mp3"
IPA: t.Final = "/\u0259\u02c8b\u00e6nd\u0259n/"
"""IPA for `abandon`; escaped because the stress mark (U+02C8) looks like a backtick to linters."""


def item(**overrides: t.Any) -> Json:
    """One dictionaryapi.dev entry for `abandon`; override any top-level key."""
    base: Json = {
        "word": "abandon",
        "phonetic": IPA,
        "phonetics": [{"text": IPA, "audio": AUDIO_URL}],
        "meanings": [
            {
                "partOfSpeech": "verb",
                "definitions": [{"definition": "To give up", "example": "They abandon the ship.", "synonyms": []}],
            }
        ],
    }
    base.update(overrides)
    return base


@pytest.fixture
def provider(http: httpx.AsyncClient, audio_cache: AudioCache) -> FreeDictionaryProvider:
    return FreeDictionaryProvider(client=http, base_url=FREE_DICTIONARY_BASE_URL, audio_cache=audio_cache)


async def lookup(provider: FreeDictionaryProvider, upstream: FakeUpstream, *, payload: object) -> ProviderResult:
    upstream.json(url=free_dictionary_url(query="abandon"), payload=payload)
    upstream.audio(url=AUDIO_URL)
    return await provider.lookup(text="abandon")


class TestHighlight:
    @pytest.mark.parametrize(
        ("text", "target", "expected"),
        [
            ("they abandon the ship", "abandon", "they *abandon* the ship"),
            ("They Abandon the ship", "abandon", "They *Abandon* the ship"),
            ("(abandon)", "abandon", "(*abandon*)"),
            ("x abandon y abandon", "abandon", "x *abandon* y abandon"),
            ("put paid to it", "put paid to", "*put paid to* it"),
            ("a.b", "a.b", "*a.b*"),
            ("abandonment ship", "abandon", "abandonment ship"),
            ("already *x* abandon", "abandon", "already *x* abandon"),
            ("under_score abandon", "abandon", "under_score abandon"),
            ("abandon", "", "abandon"),
            ("", "abandon", ""),
        ],
    )
    def test_first_whole_word(self, *, text: str, target: str, expected: str) -> None:
        assert highlight(text=text, target=target) == expected


class TestDescribe:
    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (ValueError("bad json"), "bad json"),
            (httpx.ReadTimeout(""), "ReadTimeout"),
            (httpx.ConnectError("refused"), "refused"),
            (KeyError("hwi"), "'hwi'"),
            (IndexError(), "IndexError"),
        ],
    )
    def test_message_or_class_name(self, *, exc: Exception, expected: str) -> None:
        assert describe(exc=exc) == expected


class TestProviderResult:
    def test_empty_is_not_found(self) -> None:
        result = ProviderResult()
        assert result.found is False
        assert result.entries == []
        assert result.suggestions == []

    def test_suggestions_alone_not_found(self) -> None:
        assert ProviderResult(suggestions=["abandon"]).found is False

    def test_entries_found(self) -> None:
        draft = DraftEntry(
            spellings=[SpellingIn(text="abandon")],
            part_of_speech="verb",
            definitions=[],
            source=DefinitionSource.FREE_DICT,
        )
        assert ProviderResult(entries=[draft]).found is True

    def test_defaults_not_shared(self) -> None:
        first = ProviderResult()
        first.entries.append(t.cast("DraftEntry", object()))
        assert ProviderResult().entries == []


class TestLookup:
    async def test_entry_shape(self, provider: FreeDictionaryProvider, upstream: FakeUpstream) -> None:
        result = await lookup(provider, upstream, payload=[item()])
        assert result.found
        assert result.suggestions == []
        assert result.entries == [
            DraftEntry(
                spellings=[SpellingIn(text="abandon", kind=SpellingKind.PRIMARY)],
                part_of_speech="verb",
                pronunciation=IPA,
                audio_path="/audio/abandon-us.mp3",
                definitions=[
                    DraftDefinition(
                        text="To give up",
                        source=DefinitionSource.FREE_DICT,
                        examples=[DraftExample(text="They *abandon* the ship.", source=ExampleSource.FREE_DICT)],
                    )
                ],
                source=DefinitionSource.FREE_DICT,
            )
        ]
        assert upstream.urls == [free_dictionary_url(query="abandon"), AUDIO_URL]
        assert upstream.requests[0].method == "GET"
        assert upstream.requests[0].url.params == httpx.QueryParams()

    async def test_audio_written_to_cache(
        self, provider: FreeDictionaryProvider, upstream: FakeUpstream, tmp_path: pathlib.Path
    ) -> None:
        await lookup(provider, upstream, payload=[item()])
        assert (tmp_path / "audio" / "abandon-us.mp3").read_bytes() == MP3_BYTES

    async def test_entry_per_meaning(self, provider: FreeDictionaryProvider, upstream: FakeUpstream) -> None:
        meanings = [
            {"partOfSpeech": "verb", "definitions": [{"definition": "To give up"}]},
            {"partOfSpeech": "noun", "definitions": [{"definition": "Freedom from inhibitions"}, {"definition": ""}]},
            {"partOfSpeech": "adjective", "definitions": []},
            {"definitions": [{"definition": "Unlabelled"}]},
        ]
        result = await lookup(provider, upstream, payload=[item(meanings=meanings)])
        assert [e.part_of_speech for e in result.entries] == ["verb", "noun", "other"]
        assert [[d.text for d in e.definitions] for e in result.entries] == [
            ["To give up"],
            ["Freedom from inhibitions"],
            ["Unlabelled"],
        ]
        assert all(e.pronunciation == IPA for e in result.entries)
        assert all(e.audio_path == "/audio/abandon-us.mp3" for e in result.entries)
        assert len(upstream.requested(url=AUDIO_URL)) == 1

    async def test_definition_without_example(self, provider: FreeDictionaryProvider, upstream: FakeUpstream) -> None:
        meanings = [{"partOfSpeech": "verb", "definitions": [{"definition": " padded ", "example": "   "}]}]
        result = await lookup(provider, upstream, payload=[item(meanings=meanings)])
        assert result.entries[0].definitions == [DraftDefinition(text="padded", source=DefinitionSource.FREE_DICT)]

    async def test_multiple_items_with_distinct_parts_of_speech_kept(
        self, provider: FreeDictionaryProvider, upstream: FakeUpstream
    ) -> None:
        meanings = [{"partOfSpeech": "noun", "definitions": [{"definition": "Freedom"}]}]
        second = item(word="Abandon", phonetics=[], phonetic=None, meanings=meanings)
        result = await lookup(provider, upstream, payload=[item(), second])
        assert [e.part_of_speech for e in result.entries] == ["verb", "noun"]
        assert [e.spellings[0].text for e in result.entries] == ["abandon", "Abandon"]
        assert [e.pronunciation for e in result.entries] == [IPA, None]
        assert [e.audio_path for e in result.entries] == ["/audio/abandon-us.mp3", None]

    async def test_multiple_items_with_same_part_of_speech_merged(
        self, provider: FreeDictionaryProvider, upstream: FakeUpstream
    ) -> None:
        meanings = [{"partOfSpeech": "verb", "definitions": [{"definition": "To desert"}]}]
        second = item(word="Abandon", phonetics=[], phonetic=None, meanings=meanings)
        result = await lookup(provider, upstream, payload=[second, item()])
        [verb] = result.entries
        assert verb.part_of_speech == "verb"
        assert [(s.text, s.kind) for s in verb.spellings] == [("Abandon", SpellingKind.PRIMARY)]
        assert verb.pronunciation == IPA
        assert verb.audio_path == "/audio/abandon-us.mp3"
        assert [d.text for d in verb.definitions] == ["To desert", "To give up"]

    async def test_same_part_of_speech_meanings_fold_into_one_entry(
        self, provider: FreeDictionaryProvider, upstream: FakeUpstream
    ) -> None:
        meanings = [
            {"partOfSpeech": "verb", "definitions": [{"definition": "To give up"}]},
            {"partOfSpeech": "noun", "definitions": [{"definition": "Freedom"}]},
            {"partOfSpeech": " Verb ", "definitions": [{"definition": "To desert", "example": "They abandon it."}]},
        ]
        result = await lookup(provider, upstream, payload=[item(meanings=meanings)])
        assert [e.part_of_speech for e in result.entries] == ["verb", "noun"]
        verb = result.entries[0]
        assert [d.text for d in verb.definitions] == ["To give up", "To desert"]
        assert verb.definitions[1].examples == [DraftExample(text="They *abandon* it.", source=ExampleSource.FREE_DICT)]
        assert verb.spellings == [SpellingIn(text="abandon", kind=SpellingKind.PRIMARY)]

    @pytest.mark.parametrize(
        ("raw", "clean"), [("Verb", "verb"), (" transitive  verb ", "transitive verb"), ("", "other")]
    )
    async def test_part_of_speech_normalised(
        self, provider: FreeDictionaryProvider, upstream: FakeUpstream, *, raw: str, clean: str
    ) -> None:
        meanings = [{"partOfSpeech": raw, "definitions": [{"definition": "To give up"}]}]
        result = await lookup(provider, upstream, payload=[item(meanings=meanings)])
        assert [e.part_of_speech for e in result.entries] == [clean]

    @pytest.mark.parametrize("word", ["", "   ", "a / b", "x" * 256])
    async def test_invalid_word_skipped(
        self, provider: FreeDictionaryProvider, upstream: FakeUpstream, word: str
    ) -> None:
        result = await lookup(provider, upstream, payload=[item(word=word)])
        assert result.found is False
        assert upstream.urls == [free_dictionary_url(query="abandon")]

    async def test_word_whitespace_trimmed(self, provider: FreeDictionaryProvider, upstream: FakeUpstream) -> None:
        result = await lookup(provider, upstream, payload=[item(word="  abandon ")])
        assert result.entries[0].spellings[0].text == "abandon"
        assert result.entries[0].definitions[0].examples[0].text == "They *abandon* the ship."

    @pytest.mark.parametrize(
        "example",
        [
            "we abandon",
            "they abandon the ship",
            "abandon \u2026 the ship.",
            "They abandon the ship... or not.",
            "They abandon [the ship].",
            "Abandon the ship, / Abandon all hope.",
            "They abandon the ship." * 20,
        ],
    )
    async def test_fragment_example_dropped(
        self, provider: FreeDictionaryProvider, upstream: FakeUpstream, *, example: str
    ) -> None:
        meanings = [{"partOfSpeech": "verb", "definitions": [{"definition": "To give up", "example": example}]}]
        result = await lookup(provider, upstream, payload=[item(meanings=meanings)])
        assert result.entries[0].definitions == [DraftDefinition(text="To give up", source=DefinitionSource.FREE_DICT)]

    @pytest.mark.parametrize(
        ("example", "expected"),
        [
            ("abandon ship!", "*abandon* ship!"),
            ("Abandon ship", "*Abandon* ship"),
            ("  Abandon ship.  ", "*Abandon* ship."),
            ("(Abandon the ship)", "(*Abandon* the ship)"),
        ],
    )
    async def test_highlight_markup_does_not_hide_sentence_shape(
        self, provider: FreeDictionaryProvider, upstream: FakeUpstream, *, example: str, expected: str
    ) -> None:
        meanings = [{"partOfSpeech": "verb", "definitions": [{"definition": "To give up", "example": example}]}]
        result = await lookup(provider, upstream, payload=[item(meanings=meanings)])
        assert result.entries[0].definitions[0].examples == [
            DraftExample(text=expected, source=ExampleSource.FREE_DICT)
        ]

    async def test_phonetic_from_phonetics_list(self, provider: FreeDictionaryProvider, upstream: FakeUpstream) -> None:
        phonetics = [
            "junk",
            {"audio": ""},
            {"text": "", "audio": None},
            {"text": "/x/"},
            {"text": "/y/", "audio": AUDIO_URL},
        ]
        payload = [item(phonetic="", phonetics=phonetics)]
        result = await lookup(provider, upstream, payload=payload)
        assert result.entries[0].pronunciation == "/x/"
        assert result.entries[0].audio_path == "/audio/abandon-us.mp3"

    async def test_missing_phonetics(self, provider: FreeDictionaryProvider, upstream: FakeUpstream) -> None:
        payload = [item(phonetic=None, phonetics=[])]
        result = await lookup(provider, upstream, payload=payload)
        assert result.entries[0].pronunciation is None
        assert result.entries[0].audio_path is None
        assert upstream.urls == [free_dictionary_url(query="abandon")]

    async def test_audio_failure_keeps_entry(self, provider: FreeDictionaryProvider, upstream: FakeUpstream) -> None:
        upstream.json(url=free_dictionary_url(query="abandon"), payload=[item()])
        upstream.text(url=AUDIO_URL, body="gone", status_code=410)
        result = await provider.lookup(text="abandon")
        assert result.found
        assert result.entries[0].audio_path is None
        assert result.entries[0].pronunciation == IPA

    async def test_query_quoted_not_normalised(self, provider: FreeDictionaryProvider, upstream: FakeUpstream) -> None:
        upstream.json(url=free_dictionary_url(query="put  paid to"), payload=[item(word="put paid to", phonetics=[])])
        result = await provider.lookup(text="put  paid to")
        assert upstream.requests[0].url.raw_path == b"/en/put%20%20paid%20to"
        assert result.entries[0].spellings[0].text == "put paid to"

    async def test_not_found(self, provider: FreeDictionaryProvider, upstream: FakeUpstream) -> None:
        body = {"title": "No Definitions Found", "message": "Sorry pal", "resolution": "Try again"}
        upstream.json(url=free_dictionary_url(query="abandon"), payload=body, status_code=404)
        result = await provider.lookup(text="abandon")
        assert result.found is False
        assert result.suggestions == []


class TestLookupFailures:
    async def test_http_error(self, provider: FreeDictionaryProvider, upstream: FakeUpstream) -> None:
        upstream.text(url=free_dictionary_url(query="abandon"), body="boom", status_code=500)
        with pytest.raises(ProviderError, match=r"^free_dict: HTTP 500$"):
            await provider.lookup(text="abandon")

    async def test_rate_limited(self, provider: FreeDictionaryProvider, upstream: FakeUpstream) -> None:
        upstream.text(url=free_dictionary_url(query="abandon"), body="slow down", status_code=429)
        with pytest.raises(ProviderError, match=r"^free_dict: HTTP 429$"):
            await provider.lookup(text="abandon")

    async def test_invalid_json(self, provider: FreeDictionaryProvider, upstream: FakeUpstream) -> None:
        upstream.text(url=free_dictionary_url(query="abandon"), body="<html>not json</html>")
        with pytest.raises(ProviderError, match=r"^free_dict: Expecting value"):
            await provider.lookup(text="abandon")

    @pytest.mark.parametrize("payload", [{}, {"word": "abandon"}, "abandon", 1, ["abandon"], [item(), "x"], [None]])
    async def test_unexpected_shape(
        self, provider: FreeDictionaryProvider, upstream: FakeUpstream, payload: object
    ) -> None:
        upstream.json(url=free_dictionary_url(query="abandon"), payload=payload)
        with pytest.raises(ProviderError, match=r"^free_dict: unexpected payload shape$"):
            await provider.lookup(text="abandon")

    async def test_transport_timeout(self, provider: FreeDictionaryProvider, upstream: FakeUpstream) -> None:
        upstream.fail(url=free_dictionary_url(query="abandon"), exc=httpx.ConnectTimeout(""))
        with pytest.raises(ProviderError, match=r"^free_dict: ConnectTimeout$"):
            await provider.lookup(text="abandon")

    async def test_unreachable_host(self, provider: FreeDictionaryProvider) -> None:
        with pytest.raises(ProviderError, match=r"^free_dict: no fake route for "):
            await provider.lookup(text="abandon")
