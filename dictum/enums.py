import enum


class BaseEnum(enum.StrEnum):
    """Base for all string enums; values double as API / DB representation."""

    @classmethod
    def values(cls) -> list[str]:
        """All member values."""
        return [m.value for m in cls]


class Environment(BaseEnum):
    LOCAL = "local"
    TEST = "test"
    PROD = "prod"


class WordStatus(BaseEnum):
    INBOX = "inbox"
    LEARNED = "learned"


class SpellingKind(BaseEnum):
    PRIMARY = "primary"
    VARIANT = "variant"


class DefinitionSource(BaseEnum):
    """Where a definition came from; `FREE_DICT` is dictionaryapi.dev."""

    USER = "user"
    MW = "mw"
    FREE_DICT = "free_dict"
    AI = "ai"


class ExampleSource(BaseEnum):
    USER = "user"
    VAULT = "vault"
    MW = "mw"
    FREE_DICT = "free_dict"
    AI = "ai"


class ProviderOutcome(BaseEnum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    ERROR = "error"


class ConflictPolicy(BaseEnum):
    SKIP = "skip"
    REPLACE = "replace"
    FAIL = "fail"


class SearchHitKind(BaseEnum):
    """What a search hit matched on: a spelling of the word or one of its definitions."""

    SPELLING = "spelling"
    DEFINITION = "definition"
