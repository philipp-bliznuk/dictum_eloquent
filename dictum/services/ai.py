"""Bedrock-backed drafting of definitions and example sentences.

Every call uses the Converse API with forced tool use, so the model has to answer with JSON matching the
tool schema. Anything else (truncated output, content filter, unexpected shape, AWS errors) becomes a
`ProviderError` that the router turns into a 502; clients only ever see the AWS error code, never the
message, because messages can carry the account ARN.
"""

import asyncio
import copy
import dataclasses
import json
import logging
import re
import typing as t

import boto3
import botocore.config
import botocore.exceptions
import pydantic

from dictum.enums import DefinitionSource
from dictum.enums import ExampleSource
from dictum.enums import SpellingKind
from dictum.providers.base import ProviderError
from dictum.providers.base import highlight
from dictum.providers.base import merge_by_part_of_speech
from dictum.providers.base import valid_spellings
from dictum.schemas.generate import MAX_EXAMPLES_PER_DEFINITION
from dictum.schemas.generate import MAX_SENSES_PER_POS
from dictum.schemas.lookup import DraftDefinition
from dictum.schemas.lookup import DraftEntry
from dictum.schemas.lookup import DraftExample

if t.TYPE_CHECKING:
    import collections.abc as cabc

    from dictum.settings import Settings

log = logging.getLogger(__name__)

Json = dict[str, t.Any]

PROVIDER_NAME: t.Final = "bedrock"
PARTS_OF_SPEECH: t.Final = (
    "noun",
    "verb",
    "adjective",
    "adverb",
    "pronoun",
    "preposition",
    "conjunction",
    "interjection",
    "phrase",
    "other",
)
SYSTEM_PROMPT: t.Final = (
    "You are a careful lexicographer helping an advanced English learner keep a personal dictionary. "
    "Write in the register of Merriam-Webster: concise, lower-case definitions without a leading article. "
    "Answer only by calling the provided tool."
)
SENSE_POLICY: t.Final = (
    "Cover every distinct sense a well-read speaker may meet: common, literary, and archaic or obsolete ones alike. "
    "Merge closely related senses into one definition instead of splitting hairs, and include technical or regional "
    f"jargon only when it is the main use. Order definitions from most to least common, at most {MAX_SENSES_PER_POS} "
    "per part of speech."
)

_BOTO_CONFIG: t.Final = botocore.config.Config(
    connect_timeout=5,
    read_timeout=20,
    retries={"total_max_attempts": 2, "mode": "standard"},
)
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_SHAPE_FAILURES: t.Final = (pydantic.ValidationError, KeyError, TypeError, IndexError, AttributeError)

_EXAMPLES_TOOL: t.Final[Json] = {
    "name": "record_examples",
    "description": "Record example sentences for each numbered definition.",
    "inputSchema": {
        "json": {
            "type": "object",
            "properties": {
                "examples": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "index": {
                                "type": "integer",
                                "minimum": 1,
                                "description": "Number of the definition these sentences illustrate.",
                            },
                            "sentences": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                        },
                        "required": ["index", "sentences"],
                    },
                },
            },
            "required": ["examples"],
        },
    },
}

_ENTRIES_TOOL: t.Final[Json] = {
    "name": "record_entries",
    "description": "Record dictionary entries for a word or phrase, one entry per part of speech.",
    "inputSchema": {
        "json": {
            "type": "object",
            "properties": {
                "known": {
                    "type": "boolean",
                    "description": (
                        "False when the text is not an English word or phrase you can define with confidence."
                    ),
                },
                "entries": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "spellings": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 1,
                                "description": "Canonical spelling first, then accepted variants.",
                            },
                            "part_of_speech": {"type": "string", "enum": list(PARTS_OF_SPEECH)},
                            "pronunciation": {"type": "string", "description": "IPA transcription between slashes."},
                            "definitions": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": MAX_SENSES_PER_POS,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "text": {"type": "string"},
                                        "examples": {"type": "array", "items": {"type": "string"}},
                                    },
                                    "required": ["text", "examples"],
                                },
                            },
                        },
                        "required": ["spellings", "part_of_speech", "definitions"],
                    },
                },
            },
            "required": ["known", "entries"],
        },
    },
}


def _decode_json(value: object) -> object:
    """Decode a nested array the model serialised as a JSON string (rare Claude tool-use quirk).

    Non-strings and strings that are not JSON are returned untouched so the field's
    own validation reports them.
    """
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value


_JSON: t.Final = pydantic.BeforeValidator(_decode_json)


class _ExampleGroup(pydantic.BaseModel):
    index: int
    sentences: t.Annotated[list[str], _JSON]


class _ExamplesOutput(pydantic.BaseModel):
    examples: t.Annotated[list[_ExampleGroup], _JSON]


class _DefinitionOutput(pydantic.BaseModel):
    text: str
    examples: t.Annotated[list[str], _JSON] = []


class _EntryOutput(pydantic.BaseModel):
    spellings: t.Annotated[list[str], _JSON]
    part_of_speech: str
    pronunciation: str | None = None
    definitions: t.Annotated[list[_DefinitionOutput], _JSON]


class _EntriesOutput(pydantic.BaseModel):
    known: bool
    entries: t.Annotated[list[_EntryOutput], _JSON] = []


class BedrockClient(t.Protocol):
    """The slice of the boto3 `bedrock-runtime` client used here; tests substitute a fake."""

    def converse(self, **kwargs: object) -> Json:
        """Converse API call; raises `botocore.exceptions.ClientError` on AWS errors."""
        ...


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class AIConfig:
    """Model and generation settings."""

    model_id: str
    max_tokens: int = 2048
    """Output budget for example sentences."""
    draft_max_tokens: int = 4096
    """Output budget for whole entries: several parts of speech, up to `MAX_SENSES_PER_POS` senses each."""
    temperature: float = 0.7
    concurrency: int = 2
    """Simultaneous Bedrock calls; also caps the worker threads blocked in `to_thread`."""


def _examples_prompt(*, word: str, part_of_speech: str, definitions: cabc.Sequence[str], per_definition: int) -> str:
    numbered = "\n".join(f"{i}. {text}" for i, text in enumerate(definitions, start=1))
    return (
        f"Word or phrase: {word}\nPart of speech: {part_of_speech}\nDefinitions:\n{numbered}\n\n"
        f"Write {per_definition} example sentences for every definition above, using its number as `index`. "
        f'Each sentence must be a complete, natural sentence of 8 to 20 words that uses "{word}" '
        "(an inflected form is fine) in exactly that sense, and must wrap the word or phrase as used in single "
        f"asterisks, e.g. *{word}*. Vary context and register; never explain or define the word inside the sentence."
    )


def _entries_tool(*, parts_of_speech: cabc.Sequence[str]) -> Json:
    """`_ENTRIES_TOOL` with its part-of-speech enum narrowed when every requested value is a known one.

    Unknown labels (`plural noun`) leave the full enum in place; the prompt still asks for the restriction.
    """
    wanted = {p.casefold() for p in parts_of_speech}
    if not wanted or not wanted <= set(PARTS_OF_SPEECH):
        return _ENTRIES_TOOL
    tool = copy.deepcopy(_ENTRIES_TOOL)
    entry_schema = tool["inputSchema"]["json"]["properties"]["entries"]["items"]
    entry_schema["properties"]["part_of_speech"]["enum"] = [p for p in PARTS_OF_SPEECH if p in wanted]
    return tool


def _hints_block(*, hints: cabc.Sequence[str]) -> str:
    if not hints:
        return ""
    noted = "\n".join(f"{i}. {hint}" for i, hint in enumerate(hints, start=1))
    return (
        f"\n\nThe reader noted these senses earlier (loosely worded, any part of speech):\n{noted}\n"
        "Cover each of them first, rewritten as a clean definition under the right part of speech, "
        "then add the senses still missing."
    )


def _entries_prompt(
    *, text: str, per_definition: int, hints: cabc.Sequence[str] = (), parts_of_speech: cabc.Sequence[str] = ()
) -> str:
    scope = (
        f" (only when used as {' or '.join(parts_of_speech)}; record no entries if it never is)"
        if parts_of_speech
        else ""
    )
    return (
        f"Text: {text}\n\n"
        "If this is an English word or phrase (idioms, loan phrases and archaic expressions count) that you can "
        f"define with confidence, record one entry per part of speech{scope}: canonical spelling first followed by "
        "accepted variants, an IPA pronunciation, and concise dictionary-style definitions, "
        f"each with {per_definition} example sentences of 8 to 20 words that wrap the word or phrase as used in "
        f"single asterisks, e.g. *{text}*. {SENSE_POLICY}{_hints_block(hints=hints)}\n\n"
        "If it looks like a misspelling, a proper name, or is not English, set known to false and record no entries; "
        "never guess a correction."
    )


def _examples(*, sentences: cabc.Iterable[str], word: str, limit: int) -> list[DraftExample]:
    cleaned = (_BOLD.sub(r"*\1*", " ".join(s.split())) for s in sentences)
    texts = [s for s in cleaned if s][:limit]
    return [DraftExample(text=highlight(text=s, target=word), source=ExampleSource.AI) for s in texts]


def _definition(*, item: _DefinitionOutput, word: str) -> DraftDefinition | None:
    text = " ".join(item.text.split())
    if not text:
        return None
    examples = _examples(sentences=item.examples, word=word, limit=MAX_EXAMPLES_PER_DEFINITION)
    return DraftDefinition(text=text, source=DefinitionSource.AI, examples=examples)


def _entry(*, item: _EntryOutput, word: str) -> DraftEntry | None:
    kinds = (SpellingKind.PRIMARY if i == 0 else SpellingKind.VARIANT for i in range(len(item.spellings)))
    spellings = valid_spellings(candidates=zip(item.spellings, kinds, strict=True))
    drafted = (_definition(item=d, word=word) for d in item.definitions[:MAX_SENSES_PER_POS])
    definitions = [d for d in drafted if d]
    if not spellings or not definitions:
        return None
    return DraftEntry(
        spellings=spellings,
        part_of_speech=item.part_of_speech if item.part_of_speech in PARTS_OF_SPEECH else "other",
        pronunciation=(item.pronunciation or "").strip() or None,
        definitions=definitions,
        source=DefinitionSource.AI,
    )


def _tool_input(*, response: Json, tool: Json) -> Json:
    stop = response.get("stopReason")
    if stop != "tool_use":
        msg = f"{PROVIDER_NAME}: generation stopped ({stop})"
        raise ProviderError(msg)
    for block in response["output"]["message"]["content"]:
        use = block.get("toolUse")
        if use and use.get("name") == tool["name"]:
            return use["input"]
    msg = f"{PROVIDER_NAME}: no tool output"
    raise ProviderError(msg)


def _parse[T: pydantic.BaseModel](*, response: Json, tool: Json, output: type[T]) -> T:
    try:
        return output.model_validate(_tool_input(response=response, tool=tool))
    except _SHAPE_FAILURES as exc:
        log.warning("%s returned an unusable payload: %s", PROVIDER_NAME, exc)
        msg = f"{PROVIDER_NAME}: unusable model output"
        raise ProviderError(msg) from exc


def _error_code(*, exc: botocore.exceptions.ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code") or "ClientError")


class AIService:
    """Drafts dictionary content with a Bedrock model; `name` mirrors the dictionary providers."""

    name = PROVIDER_NAME

    def __init__(self, *, client: BedrockClient, config: AIConfig) -> None:
        self._client = client
        self._config = config
        self._semaphore = asyncio.Semaphore(config.concurrency)

    async def generate_examples(
        self, *, word: str, part_of_speech: str, definitions: cabc.Sequence[str], per_definition: int = 2
    ) -> list[list[DraftExample]]:
        """Example sentences per definition, aligned with `definitions`; a skipped definition yields an empty list."""
        prompt = _examples_prompt(
            word=word, part_of_speech=part_of_speech, definitions=definitions, per_definition=per_definition
        )
        output = await self._call(
            tool=_EXAMPLES_TOOL, prompt=prompt, model=_ExamplesOutput, max_tokens=self._config.max_tokens
        )
        groups: dict[int, list[str]] = {}
        for group in output.examples:
            groups.setdefault(group.index, []).extend(group.sentences)
        return [
            _examples(sentences=groups.get(index, []), word=word, limit=per_definition)
            for index in range(1, len(definitions) + 1)
        ]

    async def generate_entries(
        self,
        *,
        text: str,
        per_definition: int = 2,
        hints: cabc.Sequence[str] = (),
        parts_of_speech: cabc.Sequence[str] = (),
    ) -> list[DraftEntry]:
        """Draft entries for a word or phrase; empty when the model does not recognise it.

        Args:
            text: Word or phrase to define.
            per_definition: Example sentences per definition.
            hints: Senses the reader noted earlier; covered first, rewritten cleanly.
            parts_of_speech: Restrict drafting to these parts of speech; empty means all.
        """
        prompt = _entries_prompt(text=text, per_definition=per_definition, hints=hints, parts_of_speech=parts_of_speech)
        output = await self._call(
            tool=_entries_tool(parts_of_speech=parts_of_speech),
            prompt=prompt,
            model=_EntriesOutput,
            max_tokens=self._config.draft_max_tokens,
        )
        if not output.known:
            return []
        drafts = (_entry(item=item, word=text) for item in output.entries)
        return merge_by_part_of_speech(entries=(d for d in drafts if d is not None))

    async def _call[T: pydantic.BaseModel](self, *, tool: Json, prompt: str, model: type[T], max_tokens: int) -> T:
        async with self._semaphore:
            response = await asyncio.to_thread(self._converse, tool=tool, prompt=prompt, max_tokens=max_tokens)
        return _parse(response=response, tool=tool, output=model)

    def _converse(self, *, tool: Json, prompt: str, max_tokens: int) -> Json:
        try:
            return self._client.converse(
                modelId=self._config.model_id,
                system=[{"text": SYSTEM_PROMPT}],
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                toolConfig={"tools": [{"toolSpec": tool}], "toolChoice": {"tool": {"name": tool["name"]}}},
                inferenceConfig={"maxTokens": max_tokens, "temperature": self._config.temperature},
            )
        except botocore.exceptions.ClientError as exc:
            code = _error_code(exc=exc)
            log.warning("%s call failed with %s: %s", PROVIDER_NAME, code, exc)
            msg = f"{PROVIDER_NAME}: {code}"
            raise ProviderError(msg) from exc
        except botocore.exceptions.BotoCoreError as exc:
            log.warning("%s call failed: %s", PROVIDER_NAME, exc)
            msg = f"{PROVIDER_NAME}: {type(exc).__name__}"
            raise ProviderError(msg) from exc


def _credentials(*, settings: Settings) -> tuple[str, str] | None:
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        return settings.aws_access_key_id.get_secret_value(), settings.aws_secret_access_key.get_secret_value()
    return None


def build_ai_service(*, settings: Settings, client: BedrockClient | None = None) -> AIService | None:
    """`AIService` when Bedrock credentials are configured, else None.

    Args:
        settings: Source of region, model id and credentials.
        client: Replaces the boto3 client (tests); still requires credentials so the gate stays in one place.
    """
    credentials = _credentials(settings=settings)
    if credentials is None:
        return None
    if client is None:
        access_key, secret_key = credentials
        client = boto3.client(
            "bedrock-runtime",
            region_name=settings.aws_region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=_BOTO_CONFIG,
        )
    return AIService(client=client, config=AIConfig(model_id=settings.bedrock_model_id))
