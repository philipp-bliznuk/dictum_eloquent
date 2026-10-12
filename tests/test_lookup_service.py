import asyncio
import typing as t

import pytest

from dictum.enums import DefinitionSource
from dictum.enums import ProviderOutcome
from dictum.exceptions import UpstreamError
from dictum.providers import FreeDictionaryProvider
from dictum.providers import MWProvider
from dictum.providers.base import ProviderError
from dictum.providers.base import ProviderResult
from dictum.schemas.lookup import DraftEntry
from dictum.schemas.lookup import ProviderAttempt
from dictum.schemas.word import SpellingIn
from dictum.services.lookup import LookupOutcome
from dictum.services.lookup import LookupService
from dictum.services.lookup import build_lookup_service
from tests.conftest import MW_API_KEY
from tests.conftest import make_settings

if t.TYPE_CHECKING:
    import pathlib

    import httpx

    from dictum.services.audio import AudioCache


def draft(text: str = "abandon") -> DraftEntry:
    return DraftEntry(
        spellings=[SpellingIn(text=text)],
        part_of_speech="verb",
        definitions=[],
        source=DefinitionSource.MW,
    )


class FakeProvider:
    """Scripted provider: returns `result` or raises `error`; records every query."""

    def __init__(self, name: str, *, result: ProviderResult | None = None, error: Exception | None = None) -> None:
        self.name = name
        self.result = result or ProviderResult()
        self.error = error
        self.calls: list[str] = []

    async def lookup(self, *, text: str) -> ProviderResult:
        self.calls.append(text)
        await asyncio.sleep(0)
        if self.error is not None:
            raise self.error
        return self.result


def found(name: str) -> FakeProvider:
    return FakeProvider(name, result=ProviderResult(entries=[draft(name)]))


def missing(name: str, *suggestions: str) -> FakeProvider:
    return FakeProvider(name, result=ProviderResult(suggestions=list(suggestions)))


def failing(name: str, error: Exception | None = None) -> FakeProvider:
    return FakeProvider(name, error=error or ProviderError(f"{name}: boom"))


class TestLookupOutcome:
    def test_found_tracks_entries(self) -> None:
        empty = LookupOutcome(entries=[], suggestions=["x"], attempts=[])
        assert empty.found is False
        assert LookupOutcome(entries=[draft()], suggestions=[], attempts=[]).found is True


class TestLookupService:
    def test_providers_is_a_copy(self) -> None:
        first = found("a")
        service = LookupService(providers=[first])
        listed = service.providers
        listed.clear()
        assert service.providers == [first]

    async def test_first_hit_wins(self) -> None:
        first, second = found("mw"), found("free_dict")
        outcome = await LookupService(providers=[first, second]).lookup(text="abandon")
        assert outcome.found
        assert [e.spellings[0].text for e in outcome.entries] == ["mw"]
        assert outcome.suggestions == []
        assert outcome.attempts == [ProviderAttempt(provider="mw", outcome=ProviderOutcome.FOUND)]
        assert first.calls == ["abandon"]
        assert second.calls == []

    async def test_miss_then_hit(self) -> None:
        first, second = missing("mw", "abound"), found("free_dict")
        outcome = await LookupService(providers=[first, second]).lookup(text="abandon")
        assert outcome.found
        assert [e.spellings[0].text for e in outcome.entries] == ["free_dict"]
        assert outcome.suggestions == []
        assert [(a.provider, a.outcome) for a in outcome.attempts] == [
            ("mw", ProviderOutcome.NOT_FOUND),
            ("free_dict", ProviderOutcome.FOUND),
        ]
        assert second.calls == ["abandon"]

    async def test_all_miss_keeps_first_suggestions(self) -> None:
        providers = [missing("a"), missing("b", "b1", "b2"), missing("c", "c1")]
        outcome = await LookupService(providers=providers).lookup(text="abandn")
        assert outcome.found is False
        assert outcome.entries == []
        assert outcome.suggestions == ["b1", "b2"]
        assert [a.outcome for a in outcome.attempts] == [ProviderOutcome.NOT_FOUND] * 3
        assert all(a.detail is None for a in outcome.attempts)

    @pytest.mark.parametrize(
        ("error", "detail"),
        [
            (ProviderError("mw: Server error"), "mw: Server error"),
            (KeyError("hwi"), "'hwi'"),
            (TypeError("string indices must be integers"), "string indices must be integers"),
            (IndexError("list index out of range"), "list index out of range"),
            (AttributeError("'str' object has no attribute 'get'"), "'str' object has no attribute 'get'"),
        ],
    )
    async def test_provider_failure_is_skipped(self, *, error: Exception, detail: str) -> None:
        broken, working = failing("mw", error), found("free_dict")
        outcome = await LookupService(providers=[broken, working]).lookup(text="abandon")
        assert outcome.found
        assert outcome.attempts == [
            ProviderAttempt(provider="mw", outcome=ProviderOutcome.ERROR, detail=detail),
            ProviderAttempt(provider="free_dict", outcome=ProviderOutcome.FOUND),
        ]
        assert working.calls == ["abandon"]

    async def test_failure_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        providers = [failing("mw", ProviderError("mw: boom")), found("free_dict")]
        with caplog.at_level("WARNING", logger="dictum.services.lookup"):
            await LookupService(providers=providers).lookup(text="abandon")
        assert "provider mw failed for 'abandon': mw: boom" in caplog.text

    async def test_failure_then_miss_is_not_an_error(self) -> None:
        outcome = await LookupService(providers=[failing("mw"), missing("free_dict")]).lookup(text="abandon")
        assert outcome.found is False
        assert [a.outcome for a in outcome.attempts] == [ProviderOutcome.ERROR, ProviderOutcome.NOT_FOUND]

    async def test_all_failed_raises_upstream_error(self) -> None:
        service = LookupService(providers=[failing("mw"), failing("free_dict")])
        with pytest.raises(UpstreamError) as info:
            await service.lookup(text="abandon")
        assert info.value.status_code == 502
        assert info.value.detail == "mw: boom; free_dict: boom"

    async def test_blank_error_message_falls_back_to_provider_name(self) -> None:
        service = LookupService(providers=[failing("mw", IndexError()), failing("free_dict", KeyError())])
        with pytest.raises(UpstreamError) as info:
            await service.lookup(text="abandon")
        assert info.value.detail == "mw; free_dict"

    async def test_unexpected_exception_propagates(self) -> None:
        service = LookupService(providers=[failing("mw", RuntimeError("bug")), found("free_dict")])
        with pytest.raises(RuntimeError, match="bug"):
            await service.lookup(text="abandon")

    async def test_no_providers(self) -> None:
        outcome = await LookupService(providers=[]).lookup(text="abandon")
        assert outcome == LookupOutcome(entries=[], suggestions=[], attempts=[])
        assert outcome.found is False


class TestBuildLookupService:
    def test_with_mw_key(self, tmp_path: pathlib.Path, http: httpx.AsyncClient, audio_cache: AudioCache) -> None:
        settings = make_settings(tmp_path=tmp_path, mw_api_key=MW_API_KEY)
        service = build_lookup_service(settings=settings, client=http, audio_cache=audio_cache)
        assert [p.name for p in service.providers] == ["mw", "free_dict"]
        assert [type(p) for p in service.providers] == [MWProvider, FreeDictionaryProvider]

    def test_without_mw_key(self, tmp_path: pathlib.Path, http: httpx.AsyncClient, audio_cache: AudioCache) -> None:
        settings = make_settings(tmp_path=tmp_path)
        service = build_lookup_service(settings=settings, client=http, audio_cache=audio_cache)
        assert [p.name for p in service.providers] == ["free_dict"]
        assert isinstance(service.providers[0], FreeDictionaryProvider)

    def test_blank_mw_key_disables_provider(
        self, tmp_path: pathlib.Path, http: httpx.AsyncClient, audio_cache: AudioCache
    ) -> None:
        settings = make_settings(tmp_path=tmp_path, mw_api_key="")
        service = build_lookup_service(settings=settings, client=http, audio_cache=audio_cache)
        assert [p.name for p in service.providers] == ["free_dict"]
