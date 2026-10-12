import dataclasses
import logging
import typing as t

from dictum.enums import ProviderOutcome
from dictum.exceptions import UpstreamError
from dictum.providers import FreeDictionaryProvider
from dictum.providers import MWConfig
from dictum.providers import MWProvider
from dictum.providers import ProviderError
from dictum.schemas.lookup import ProviderAttempt

if t.TYPE_CHECKING:
    import collections.abc as cabc

    import httpx

    from dictum.providers import DictionaryProvider
    from dictum.schemas.lookup import DraftEntry
    from dictum.services.audio import AudioCache
    from dictum.settings import Settings

log = logging.getLogger(__name__)

# Parsing a third-party payload must never surface as a 500; these are the failures a provider can raise.
_PROVIDER_FAILURES: t.Final = (ProviderError, KeyError, TypeError, IndexError, AttributeError)


@dataclasses.dataclass(slots=True)
class LookupOutcome:
    """What the provider chain produced for one query."""

    entries: list[DraftEntry]
    suggestions: list[str]
    attempts: list[ProviderAttempt]

    @property
    def found(self) -> bool:
        """True when some provider produced entries."""
        return bool(self.entries)


class LookupService:
    """Asks providers in order and stops at the first one that knows the word."""

    def __init__(self, *, providers: cabc.Sequence[DictionaryProvider]) -> None:
        self._providers = list(providers)

    @property
    def providers(self) -> list[DictionaryProvider]:
        """Configured providers, in query order."""
        return list(self._providers)

    async def lookup(self, *, text: str) -> LookupOutcome:
        """Resolve `text`; raises `UpstreamError` only when every provider failed."""
        attempts: list[ProviderAttempt] = []
        suggestions: list[str] = []
        for provider in self._providers:
            try:
                result = await provider.lookup(text=text)
            except _PROVIDER_FAILURES as exc:
                log.warning("provider %s failed for %r: %s", provider.name, text, exc)
                attempts.append(ProviderAttempt(provider=provider.name, outcome=ProviderOutcome.ERROR, detail=str(exc)))
                continue
            if result.found:
                attempts.append(ProviderAttempt(provider=provider.name, outcome=ProviderOutcome.FOUND))
                return LookupOutcome(entries=result.entries, suggestions=[], attempts=attempts)
            attempts.append(ProviderAttempt(provider=provider.name, outcome=ProviderOutcome.NOT_FOUND))
            suggestions = suggestions or result.suggestions
        _raise_if_all_failed(attempts=attempts)
        return LookupOutcome(entries=[], suggestions=suggestions, attempts=attempts)


def _raise_if_all_failed(*, attempts: list[ProviderAttempt]) -> None:
    if attempts and all(a.outcome is ProviderOutcome.ERROR for a in attempts):
        raise UpstreamError(detail="; ".join(a.detail or a.provider for a in attempts))


def build_lookup_service(*, settings: Settings, client: httpx.AsyncClient, audio_cache: AudioCache) -> LookupService:
    """Assemble the provider chain from settings: MW when a key is present, then dictionaryapi.dev."""
    providers: list[DictionaryProvider] = []
    if settings.mw_api_key:
        config = MWConfig(
            api_key=settings.mw_api_key.get_secret_value(),
            base_url=str(settings.mw_base_url),
            audio_base_url=str(settings.mw_audio_base_url),
        )
        providers.append(MWProvider(client=client, config=config, audio_cache=audio_cache))
    providers.append(
        FreeDictionaryProvider(client=client, base_url=str(settings.free_dictionary_base_url), audio_cache=audio_cache)
    )
    return LookupService(providers=providers)
