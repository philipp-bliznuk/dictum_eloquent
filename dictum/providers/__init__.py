from dictum.providers.base import DictionaryProvider
from dictum.providers.base import ProviderError
from dictum.providers.base import ProviderResult
from dictum.providers.free_dictionary import FreeDictionaryProvider
from dictum.providers.mw import MWConfig
from dictum.providers.mw import MWProvider

__all__ = [
    "DictionaryProvider",
    "FreeDictionaryProvider",
    "MWConfig",
    "MWProvider",
    "ProviderError",
    "ProviderResult",
]
