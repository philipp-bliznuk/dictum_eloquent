import asyncio
import contextlib
import copy
import dataclasses
import json
import threading
import typing as t

import botocore.config
import botocore.exceptions
import pydantic
import pytest

from dictum.enums import DefinitionSource
from dictum.enums import ExampleSource
from dictum.enums import SpellingKind
from dictum.providers.base import ProviderError
from dictum.schemas.generate import MAX_SENSES_PER_POS
from dictum.schemas.lookup import DraftDefinition
from dictum.schemas.lookup import DraftEntry
from dictum.schemas.lookup import DraftExample
from dictum.schemas.word import SpellingIn
from dictum.services.ai import PARTS_OF_SPEECH
from dictum.services.ai import SENSE_POLICY
from dictum.services.ai import SYSTEM_PROMPT
from dictum.services.ai import AIConfig
from dictum.services.ai import AIService
from dictum.services.ai import build_ai_service
from tests.conftest import AWS_ACCESS_KEY_ID
from tests.conftest import AWS_SECRET_ACCESS_KEY
from tests.conftest import FakeBedrock
from tests.conftest import client_error
from tests.conftest import definitions_schema
from tests.conftest import make_ai_settings
from tests.conftest import make_settings
from tests.conftest import part_of_speech_enum
from tests.conftest import tool_use_response

if t.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib
    from unittest.mock import MagicMock

    from pytest_mock import MockerFixture

Json = dict[str, t.Any]

MODEL_ID: t.Final = "us.anthropic.test-model-v1:0"
ARN: t.Final = "arn:aws:iam::123456789012:user/dictum"
IPA: t.Final = "/\u0259\u02c8b\u00e6nd\u0259n/"
"""IPA for `abandon`; escaped because the stress mark (U+02C8) looks like a backtick to linters."""
LOGGER: t.Final = "dictum.services.ai"


def examples_response(*groups: Json, stop_reason: str = "tool_use") -> Json:
    """`record_examples` tool call carrying `groups` of `{"index": n, "sentences": [...]}`."""
    return tool_use_response(name="record_examples", payload={"examples": list(groups)}, stop_reason=stop_reason)


def group(index: int, *sentences: str) -> Json:
    return {"index": index, "sentences": list(sentences)}


def entry(**overrides: t.Any) -> Json:
    """One `record_entries` entry for `abandon`; override any top-level key."""
    base: Json = {
        "spellings": ["abandon"],
        "part_of_speech": "verb",
        "pronunciation": IPA,
        "definitions": [{"text": "to give up", "examples": ["They abandon the ship."]}],
    }
    base.update(overrides)
    return base


def entries_response(*entries: Json, known: bool = True) -> Json:
    return tool_use_response(name="record_entries", payload={"known": known, "entries": list(entries)})


def content_response(content: object, *, stop_reason: str = "tool_use") -> Json:
    """Converse answer with an arbitrary `content` block list (shape failures)."""
    return {"stopReason": stop_reason, "output": {"message": {"role": "assistant", "content": content}}}


def ai(text: str) -> DraftExample:
    return DraftExample(text=text, source=ExampleSource.AI)


def spelling(text: str, kind: SpellingKind = SpellingKind.PRIMARY) -> SpellingIn:
    return SpellingIn(text=text, kind=kind)


@pytest.fixture
def bedrock() -> FakeBedrock:
    return FakeBedrock()


@pytest.fixture
def service(bedrock: FakeBedrock) -> AIService:
    return AIService(client=bedrock, config=AIConfig(model_id=MODEL_ID))


async def draft_examples(
    service: AIService, bedrock: FakeBedrock, *responses: Json, **request: t.Any
) -> list[list[DraftExample]]:
    """Queue `responses` and ask for examples; `request` overrides the default call arguments."""
    bedrock.queue(*responses)
    call: Json = {"word": "abandon", "part_of_speech": "verb", "definitions": ["to give up"], "per_definition": 2}
    call.update(request)
    return await service.generate_examples(**call)


async def draft_entries(
    service: AIService, bedrock: FakeBedrock, *responses: Json, **request: t.Any
) -> list[DraftEntry]:
    bedrock.queue(*responses)
    call: Json = {"text": "abandon"}
    call.update(request)
    return await service.generate_entries(**call)


class TestAIConfig:
    def test_defaults(self) -> None:
        config = AIConfig(model_id=MODEL_ID)
        assert (config.max_tokens, config.draft_max_tokens) == (2048, 4096)
        assert (config.temperature, config.concurrency) == (0.7, 2)

    def test_frozen(self) -> None:
        config = AIConfig(model_id=MODEL_ID)
        with pytest.raises(dataclasses.FrozenInstanceError):
            config.model_id = "other"  # type: ignore[misc]

    def test_service_name_mirrors_providers(self, service: AIService) -> None:
        assert service.name == "bedrock"
        assert AIService.name == "bedrock"


class TestGenerateExamples:
    async def test_request_forwarded_to_converse(self, service: AIService, bedrock: FakeBedrock) -> None:
        await draft_examples(service, bedrock, examples_response(), definitions=["to give up", "to leave behind"])
        assert len(bedrock.calls) == 1
        call = bedrock.calls[0]
        assert call["modelId"] == MODEL_ID
        assert call["system"] == [{"text": SYSTEM_PROMPT}]
        assert call["inferenceConfig"] == {"maxTokens": 2048, "temperature": 0.7}
        assert call["toolConfig"]["toolChoice"] == {"tool": {"name": "record_examples"}}
        [tool] = call["toolConfig"]["tools"]
        assert tool["toolSpec"]["name"] == "record_examples"
        assert tool["toolSpec"]["inputSchema"]["json"]["required"] == ["examples"]
        [message] = call["messages"]
        assert message["role"] == "user"
        [block] = message["content"]
        assert block["text"].startswith(
            "Word or phrase: abandon\nPart of speech: verb\nDefinitions:\n1. to give up\n2. to leave behind\n\n"
        )
        assert "Write 2 example sentences for every definition above" in block["text"]
        assert 'uses "abandon"' in block["text"]
        assert "e.g. *abandon*." in block["text"]

    async def test_inference_config_from_settings(self, bedrock: FakeBedrock) -> None:
        service = AIService(client=bedrock, config=AIConfig(model_id="m", max_tokens=99, temperature=0.1))
        await draft_examples(service, bedrock, examples_response(), per_definition=3)
        assert bedrock.calls[0]["modelId"] == "m"
        assert bedrock.calls[0]["inferenceConfig"] == {"maxTokens": 99, "temperature": 0.1}
        assert "Write 3 example sentences" in bedrock.prompts[0]

    async def test_per_definition_defaults_to_two(self, service: AIService, bedrock: FakeBedrock) -> None:
        bedrock.queue(examples_response(group(1, "a abandon", "b abandon", "c abandon")))
        result = await service.generate_examples(word="abandon", part_of_speech="verb", definitions=["x"])
        assert result == [[ai("a *abandon*"), ai("b *abandon*")]]
        assert "Write 2 example sentences" in bedrock.prompts[0]

    async def test_aligned_with_definitions(self, service: AIService, bedrock: FakeBedrock) -> None:
        response = examples_response(
            group(3, "Third abandon."),
            group(1, "First abandon.", "Second abandon."),
        )
        result = await draft_examples(service, bedrock, response, definitions=["one", "two", "three"])
        assert result == [[ai("First *abandon*."), ai("Second *abandon*.")], [], [ai("Third *abandon*.")]]

    async def test_no_groups_yields_empty_lists(self, service: AIService, bedrock: FakeBedrock) -> None:
        result = await draft_examples(service, bedrock, examples_response(), definitions=["one", "two"])
        assert result == [[], []]

    @pytest.mark.parametrize("index", [0, -1, 2, 99])
    async def test_out_of_range_index_ignored(self, service: AIService, bedrock: FakeBedrock, index: int) -> None:
        result = await draft_examples(service, bedrock, examples_response(group(index, "They abandon it.")))
        assert result == [[]]

    async def test_capped_at_per_definition(self, service: AIService, bedrock: FakeBedrock) -> None:
        response = examples_response(group(1, "a abandon", "b abandon", "c abandon", "d abandon"))
        result = await draft_examples(service, bedrock, response, per_definition=3)
        assert result == [[ai("a *abandon*"), ai("b *abandon*"), ai("c *abandon*")]]

    async def test_groups_with_same_index_merged_in_order(self, service: AIService, bedrock: FakeBedrock) -> None:
        response = examples_response(group(1, "a abandon"), group(2, "x abandon"), group(1, "b abandon", "c abandon"))
        result = await draft_examples(service, bedrock, response, definitions=["one", "two"], per_definition=2)
        assert result == [[ai("a *abandon*"), ai("b *abandon*")], [ai("x *abandon*")]]

    @pytest.mark.parametrize(
        ("raw", "clean"),
        [
            ("They  abandon\n\tthe ship. ", "They *abandon* the ship."),
            ("They **abandon** the ship.", "They *abandon* the ship."),
            ("**Abandon** hope, **all** ye who enter.", "*Abandon* hope, *all* ye who enter."),
            ("They *abandon* the ship.", "They *abandon* the ship."),
            ("They _abandon_ the ship.", "They _abandon_ the ship."),
            ("ABANDON all hope.", "*ABANDON* all hope."),
            ("Abandonment is hard.", "Abandonment is hard."),
            ("She abandoned the plan.", "She abandoned the plan."),
        ],
    )
    async def test_sentence_cleaned_and_highlighted(
        self, service: AIService, bedrock: FakeBedrock, *, raw: str, clean: str
    ) -> None:
        result = await draft_examples(service, bedrock, examples_response(group(1, raw)))
        assert result == [[ai(clean)]]

    async def test_phrase_highlighted(self, service: AIService, bedrock: FakeBedrock) -> None:
        response = examples_response(group(1, "That put paid to the plan."))
        result = await draft_examples(service, bedrock, response, word="put paid to", part_of_speech="phrase")
        assert result == [[ai("That *put paid to* the plan.")]]
        assert "Word or phrase: put paid to\nPart of speech: phrase\n" in bedrock.prompts[0]

    async def test_blank_sentences_dropped_before_capping(self, service: AIService, bedrock: FakeBedrock) -> None:
        response = examples_response(group(1, "", "   ", "\n", "a abandon", "b abandon", "c abandon"))
        result = await draft_examples(service, bedrock, response, per_definition=2)
        assert result == [[ai("a *abandon*"), ai("b *abandon*")]]

    async def test_only_blank_sentences_yield_empty_list(self, service: AIService, bedrock: FakeBedrock) -> None:
        result = await draft_examples(service, bedrock, examples_response(group(1, "", "  ")))
        assert result == [[]]

    async def test_matching_tool_found_after_text_block(self, service: AIService, bedrock: FakeBedrock) -> None:
        tool_use = {"toolUseId": "t2", "name": "record_examples", "input": {"examples": [group(1, "x abandon")]}}
        other = {"toolUseId": "t1", "name": "record_entries", "input": {"known": False, "entries": []}}
        response = content_response([{"text": "Here you go."}, {"toolUse": other}, {"toolUse": tool_use}])
        result = await draft_examples(service, bedrock, response)
        assert result == [[ai("x *abandon*")]]


class TestGenerateEntries:
    async def test_request_forwarded_to_converse(self, service: AIService, bedrock: FakeBedrock) -> None:
        await draft_entries(service, bedrock, entries_response(known=False))
        assert bedrock.tools == ["record_entries"]
        call = bedrock.calls[0]
        assert call["modelId"] == MODEL_ID
        assert call["system"] == [{"text": SYSTEM_PROMPT}]
        assert call["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"]["required"] == ["known", "entries"]
        prompt = bedrock.prompts[0]
        assert prompt.startswith("Text: abandon\n\n")
        assert "each with 2 example sentences" in prompt
        assert "e.g. *abandon*." in prompt

    async def test_per_definition_in_prompt(self, service: AIService, bedrock: FakeBedrock) -> None:
        await draft_entries(service, bedrock, entries_response(known=False), text="put paid to", per_definition=4)
        assert bedrock.prompts[0].startswith("Text: put paid to\n\n")
        assert "each with 4 example sentences" in bedrock.prompts[0]
        assert "e.g. *put paid to*." in bedrock.prompts[0]

    async def test_known_entry_drafted(self, service: AIService, bedrock: FakeBedrock) -> None:
        response = entries_response(
            entry(
                spellings=["abandon", "abandonne"],
                definitions=[
                    {"text": "to give up", "examples": ["They abandon the ship.", "We **abandon** hope."]},
                    {"text": "to leave behind", "examples": []},
                ],
            )
        )
        result = await draft_entries(service, bedrock, response)
        assert result == [
            DraftEntry(
                spellings=[spelling("abandon"), spelling("abandonne", SpellingKind.VARIANT)],
                part_of_speech="verb",
                pronunciation=IPA,
                audio_path=None,
                definitions=[
                    DraftDefinition(
                        text="to give up",
                        source=DefinitionSource.AI,
                        examples=[ai("They *abandon* the ship."), ai("We *abandon* hope.")],
                    ),
                    DraftDefinition(text="to leave behind", source=DefinitionSource.AI, examples=[]),
                ],
                source=DefinitionSource.AI,
            )
        ]

    async def test_multiple_entries_keep_order(self, service: AIService, bedrock: FakeBedrock) -> None:
        response = entries_response(entry(part_of_speech="verb"), entry(part_of_speech="noun"))
        result = await draft_entries(service, bedrock, response)
        assert [e.part_of_speech for e in result] == ["verb", "noun"]


class TestEntriesMerge:
    """Entries sharing a part of speech fold into one draft, like the dictionary providers."""

    async def test_entries_with_same_part_of_speech_merged(self, service: AIService, bedrock: FakeBedrock) -> None:
        response = entries_response(
            entry(spellings=["abandon"], pronunciation=None, definitions=[{"text": "to give up", "examples": []}]),
            entry(part_of_speech="noun", definitions=[{"text": "freedom", "examples": []}]),
            entry(
                spellings=["Abandon", "abandonne"],
                definitions=[{"text": "to desert", "examples": ["They abandon ship."]}],
            ),
        )
        result = await draft_entries(service, bedrock, response)
        assert [e.part_of_speech for e in result] == ["verb", "noun"]
        verb = result[0]
        assert verb.spellings == [spelling("abandon"), spelling("abandonne", SpellingKind.VARIANT)]
        assert verb.pronunciation == IPA
        assert verb.definitions == [
            DraftDefinition(text="to give up", source=DefinitionSource.AI, examples=[]),
            DraftDefinition(text="to desert", source=DefinitionSource.AI, examples=[ai("They *abandon* ship.")]),
        ]
        assert result[1].definitions == [DraftDefinition(text="freedom", source=DefinitionSource.AI, examples=[])]

    async def test_unknown_labels_merge_into_one_other_entry(self, service: AIService, bedrock: FakeBedrock) -> None:
        response = entries_response(
            entry(part_of_speech="determiner", definitions=[{"text": "d1", "examples": []}]),
            entry(part_of_speech="Verb", definitions=[{"text": "d2", "examples": []}]),
        )
        [other] = await draft_entries(service, bedrock, response)
        assert other.part_of_speech == "other"
        assert [d.text for d in other.definitions] == ["d1", "d2"]

    async def test_dropped_entries_do_not_merge(self, service: AIService, bedrock: FakeBedrock) -> None:
        response = entries_response(
            entry(spellings=["Color / Colour"], definitions=[{"text": "invalid", "examples": []}]),
            entry(definitions=[]),
            entry(definitions=[{"text": "kept", "examples": []}]),
        )
        [verb] = await draft_entries(service, bedrock, response)
        assert [d.text for d in verb.definitions] == ["kept"]
        assert verb.spellings == [spelling("abandon")]

    async def test_unknown_returns_nothing_even_with_entries(self, service: AIService, bedrock: FakeBedrock) -> None:
        assert await draft_entries(service, bedrock, entries_response(entry(), known=False)) == []

    async def test_entries_default_to_empty(self, service: AIService, bedrock: FakeBedrock) -> None:
        response = tool_use_response(name="record_entries", payload={"known": True})
        assert await draft_entries(service, bedrock, response) == []

    async def test_spellings_validated_and_deduplicated(self, service: AIService, bedrock: FakeBedrock) -> None:
        raw = ["  abandon ", "Abandon", "Color / Colour", "", "   ", "x" * 256, "abandonne"]
        [result] = await draft_entries(service, bedrock, entries_response(entry(spellings=raw)))
        assert result.spellings == [spelling("abandon"), spelling("abandonne", SpellingKind.VARIANT)]

    async def test_invalid_canonical_spelling_keeps_variants_as_variants(
        self, service: AIService, bedrock: FakeBedrock
    ) -> None:
        response = entries_response(entry(spellings=["Color / Colour", "colour"]))
        [result] = await draft_entries(service, bedrock, response)
        assert result.spellings == [spelling("colour", SpellingKind.VARIANT)]

    @pytest.mark.parametrize("spellings", [[], ["", "   "], ["Color / Colour"]])
    async def test_entry_without_valid_spelling_dropped(
        self, service: AIService, bedrock: FakeBedrock, spellings: list[str]
    ) -> None:
        response = entries_response(entry(spellings=spellings), entry(spellings=["betray"]))
        result = await draft_entries(service, bedrock, response)
        assert [e.spellings for e in result] == [[spelling("betray")]]

    @pytest.mark.parametrize(
        ("raw", "clean"),
        [
            ("verb", "verb"),
            ("noun", "noun"),
            ("phrase", "phrase"),
            ("other", "other"),
            ("Verb", "other"),
            ("determiner", "other"),
            ("", "other"),
            ("transitive verb", "other"),
        ],
    )
    async def test_part_of_speech_normalised(
        self, service: AIService, bedrock: FakeBedrock, *, raw: str, clean: str
    ) -> None:
        [result] = await draft_entries(service, bedrock, entries_response(entry(part_of_speech=raw)))
        assert result.part_of_speech == clean

    @pytest.mark.parametrize(
        ("raw", "clean"),
        [(None, None), ("", None), ("   ", None), (f"  {IPA} \n", IPA), ("uh-BAN-dun", "uh-BAN-dun")],
    )
    async def test_pronunciation_stripped(
        self, service: AIService, bedrock: FakeBedrock, *, raw: str | None, clean: str | None
    ) -> None:
        [result] = await draft_entries(service, bedrock, entries_response(entry(pronunciation=raw)))
        assert result.pronunciation == clean

    async def test_pronunciation_optional(self, service: AIService, bedrock: FakeBedrock) -> None:
        item = entry()
        del item["pronunciation"]
        [result] = await draft_entries(service, bedrock, entries_response(item))
        assert result.pronunciation is None

    async def test_blank_definitions_dropped_and_text_collapsed(self, service: AIService, bedrock: FakeBedrock) -> None:
        definitions = [
            {"text": "   ", "examples": ["ignored abandon"]},
            {"text": " to  give\n up ", "examples": []},
            {"text": "", "examples": []},
        ]
        [result] = await draft_entries(service, bedrock, entries_response(entry(definitions=definitions)))
        assert result.definitions == [DraftDefinition(text="to give up", source=DefinitionSource.AI, examples=[])]

    @pytest.mark.parametrize("definitions", [[], [{"text": "", "examples": []}], [{"text": "  \n", "examples": []}]])
    async def test_entry_without_definitions_dropped(
        self, service: AIService, bedrock: FakeBedrock, definitions: list[Json]
    ) -> None:
        response = entries_response(entry(definitions=definitions), entry(spellings=["betray"]))
        result = await draft_entries(service, bedrock, response)
        assert [e.spellings[0].text for e in result] == ["betray"]

    async def test_definition_examples_cleaned_and_capped_at_five(
        self, service: AIService, bedrock: FakeBedrock
    ) -> None:
        sentences = ["", "They  **abandon**  it.", *(f"Sentence {i} abandon." for i in range(1, 7))]
        response = entries_response(entry(definitions=[{"text": "to give up", "examples": sentences}]))
        [result] = await draft_entries(service, bedrock, response)
        assert result.definitions[0].examples == [
            ai("They *abandon* it."),
            *(ai(f"Sentence {i} *abandon*.") for i in range(1, 5)),
        ]

    async def test_definition_examples_optional(self, service: AIService, bedrock: FakeBedrock) -> None:
        response = entries_response(entry(definitions=[{"text": "to give up"}]))
        [result] = await draft_entries(service, bedrock, response)
        assert result.definitions == [DraftDefinition(text="to give up", source=DefinitionSource.AI, examples=[])]

    async def test_examples_highlight_the_query_text(self, service: AIService, bedrock: FakeBedrock) -> None:
        definitions = [{"text": "to end something", "examples": ["That put paid to the plan."]}]
        response = entries_response(entry(spellings=["put paid to"], part_of_speech="phrase", definitions=definitions))
        [result] = await draft_entries(service, bedrock, response, text="put paid to")
        assert result.definitions[0].examples == [ai("That *put paid to* the plan.")]

    @pytest.mark.parametrize(("returned", "kept"), [(1, 1), (MAX_SENSES_PER_POS, 8), (9, 8), (10, 8), (30, 8)])
    async def test_definitions_capped_at_max_senses_in_order(
        self, service: AIService, bedrock: FakeBedrock, *, returned: int, kept: int
    ) -> None:
        definitions = [{"text": f"sense {i}", "examples": [f"Sentence {i} abandon."]} for i in range(1, returned + 1)]
        [result] = await draft_entries(service, bedrock, entries_response(entry(definitions=definitions)))
        assert [d.text for d in result.definitions] == [f"sense {i}" for i in range(1, kept + 1)]
        assert result.definitions[-1].examples == [ai(f"Sentence {kept} *abandon*.")]

    async def test_definitions_capped_per_entry(self, service: AIService, bedrock: FakeBedrock) -> None:
        many = [{"text": f"sense {i}", "examples": []} for i in range(1, 11)]
        response = entries_response(entry(part_of_speech="verb", definitions=many), entry(part_of_speech="noun"))
        result = await draft_entries(service, bedrock, response)
        assert [(e.part_of_speech, len(e.definitions)) for e in result] == [("verb", MAX_SENSES_PER_POS), ("noun", 1)]

    async def test_tool_schema_caps_definitions_at_max_senses(self, service: AIService, bedrock: FakeBedrock) -> None:
        await draft_entries(service, bedrock, entries_response(known=False))
        [tool] = bedrock.tool_specs
        schema = definitions_schema(tool=tool)
        assert (schema["minItems"], schema["maxItems"]) == (1, MAX_SENSES_PER_POS)
        assert MAX_SENSES_PER_POS == 8


class TestEntriesSteering:
    """Hints, part-of-speech restriction and sense policy shape the `record_entries` prompt and tool."""

    async def test_sense_policy_in_prompt(self, service: AIService, bedrock: FakeBedrock) -> None:
        await draft_entries(service, bedrock, entries_response(known=False))
        prompt = bedrock.prompts[0]
        assert SENSE_POLICY in prompt
        assert f"at most {MAX_SENSES_PER_POS} per part of speech" in SENSE_POLICY
        assert "at most 8 per part of speech" in prompt
        assert "archaic or obsolete" in prompt
        assert (
            prompt.index("e.g. *abandon*. ")
            < prompt.index(SENSE_POLICY)
            < prompt.index("If it looks like a misspelling")
        )

    async def test_per_definition_in_prompt(self, service: AIService, bedrock: FakeBedrock) -> None:
        await draft_entries(service, bedrock, entries_response(known=False), per_definition=3)
        assert "each with 3 example sentences of 8 to 20 words" in bedrock.prompts[0]

    async def test_hints_listed_in_order_after_policy(self, service: AIService, bedrock: FakeBedrock) -> None:
        hints = ["a petty criticism", "to find fault with trifles", "nit-picking"]
        await draft_entries(service, bedrock, entries_response(known=False), text="nitpick", hints=hints)
        prompt = bedrock.prompts[0]
        assert (
            "The reader noted these senses earlier (loosely worded, any part of speech):\n"
            "1. a petty criticism\n2. to find fault with trifles\n3. nit-picking\n"
            "Cover each of them first, rewritten as a clean definition under the right part of speech, "
            "then add the senses still missing.\n\nIf it looks like a misspelling"
        ) in prompt
        assert prompt.index(SENSE_POLICY) < prompt.index("\n\nThe reader noted these senses")

    @pytest.mark.parametrize("hints", [(), []])
    async def test_without_hints_no_hint_block(
        self, service: AIService, bedrock: FakeBedrock, hints: cabc.Sequence[str]
    ) -> None:
        await draft_entries(service, bedrock, entries_response(known=False), hints=hints)
        prompt = bedrock.prompts[0]
        assert "noted these senses" not in prompt
        assert "Cover each of them first" not in prompt
        assert f"{SENSE_POLICY}\n\nIf it looks like a misspelling" in prompt

    @pytest.mark.parametrize(
        ("parts_of_speech", "scope"),
        [
            (["noun"], "noun"),
            (["noun", "verb"], "noun or verb"),
            (("Noun", "plural noun", "verb"), "Noun or plural noun or verb"),
        ],
    )
    async def test_parts_of_speech_scope_in_prompt(
        self, service: AIService, bedrock: FakeBedrock, *, parts_of_speech: cabc.Sequence[str], scope: str
    ) -> None:
        await draft_entries(service, bedrock, entries_response(known=False), parts_of_speech=parts_of_speech)
        assert (
            f"record one entry per part of speech (only when used as {scope}; record no entries if it never is): "
            "canonical spelling first"
        ) in bedrock.prompts[0]

    @pytest.mark.parametrize("parts_of_speech", [(), []])
    async def test_without_restriction_no_scope(
        self, service: AIService, bedrock: FakeBedrock, parts_of_speech: cabc.Sequence[str]
    ) -> None:
        await draft_entries(service, bedrock, entries_response(known=False), parts_of_speech=parts_of_speech)
        assert "only when used as" not in bedrock.prompts[0]
        assert "record one entry per part of speech: canonical spelling first" in bedrock.prompts[0]

    async def test_hints_and_scope_combined(self, service: AIService, bedrock: FakeBedrock) -> None:
        response = entries_response(known=False)
        await draft_entries(
            service, bedrock, response, text="nitpick", hints=["a petty criticism"], parts_of_speech=["noun"]
        )
        prompt = bedrock.prompts[0]
        assert prompt.startswith("Text: nitpick\n\n")
        assert "(only when used as noun; record no entries if it never is)" in prompt
        assert "\n1. a petty criticism\nCover each of them first" in prompt
        assert part_of_speech_enum(tool=bedrock.tool_specs[0]) == ["noun"]

    @pytest.mark.parametrize(
        ("parts_of_speech", "enum"),
        [
            (["noun"], ["noun"]),
            (["Noun", "verb"], ["noun", "verb"]),
            (["verb", "noun"], ["noun", "verb"]),
            (["NOUN", "noun", "Noun"], ["noun"]),
            (("phrase", "other", "adjective"), ["adjective", "phrase", "other"]),
            (list(PARTS_OF_SPEECH), list(PARTS_OF_SPEECH)),
        ],
        ids=lambda value: "-".join(value) if len(value) <= 3 else f"{len(value)}-items",
    )
    async def test_known_labels_narrow_tool_enum_in_canonical_order(
        self, service: AIService, bedrock: FakeBedrock, *, parts_of_speech: cabc.Sequence[str], enum: list[str]
    ) -> None:
        await draft_entries(service, bedrock, entries_response(known=False), parts_of_speech=parts_of_speech)
        [tool] = bedrock.tool_specs
        assert part_of_speech_enum(tool=tool) == enum

    async def test_narrowed_tool_otherwise_identical(self, service: AIService, bedrock: FakeBedrock) -> None:
        await draft_entries(service, bedrock, entries_response(known=False), parts_of_speech=["noun"])
        await draft_entries(service, bedrock, entries_response(known=False))
        narrowed, full = bedrock.tool_specs
        assert narrowed is not full
        assert narrowed["name"] == full["name"] == "record_entries"
        restored = copy.deepcopy(narrowed)
        restored["inputSchema"]["json"]["properties"]["entries"]["items"]["properties"]["part_of_speech"]["enum"] = (
            list(PARTS_OF_SPEECH)
        )
        assert restored == full
        assert definitions_schema(tool=narrowed)["maxItems"] == MAX_SENSES_PER_POS

    async def test_narrowing_leaves_shared_tool_untouched(self, service: AIService, bedrock: FakeBedrock) -> None:
        await draft_entries(service, bedrock, entries_response(known=False), parts_of_speech=["Noun", "verb"])
        await draft_entries(service, bedrock, entries_response(known=False))
        await draft_entries(service, bedrock, entries_response(known=False), parts_of_speech=["adjective"])
        assert [part_of_speech_enum(tool=tool) for tool in bedrock.tool_specs] == [
            ["noun", "verb"],
            list(PARTS_OF_SPEECH),
            ["adjective"],
        ]
        assert len(PARTS_OF_SPEECH) == 10

    @pytest.mark.parametrize(
        "parts_of_speech",
        [["plural noun"], ["noun", "determiner"], ["transitive verb", "verb"], ["Nouns"]],
        ids="+".join,
    )
    async def test_unknown_label_keeps_full_enum_but_scopes_prompt(
        self, service: AIService, bedrock: FakeBedrock, parts_of_speech: list[str]
    ) -> None:
        await draft_entries(service, bedrock, entries_response(known=False), parts_of_speech=parts_of_speech)
        [tool] = bedrock.tool_specs
        assert part_of_speech_enum(tool=tool) == list(PARTS_OF_SPEECH)
        assert (
            f"(only when used as {' or '.join(parts_of_speech)}; record no entries if it never is)"
            in bedrock.prompts[0]
        )

    async def test_unrestricted_and_unknown_labels_share_one_tool_object(
        self, service: AIService, bedrock: FakeBedrock
    ) -> None:
        await draft_entries(service, bedrock, entries_response(known=False))
        await draft_entries(service, bedrock, entries_response(known=False), parts_of_speech=["plural noun"])
        await draft_entries(service, bedrock, entries_response(known=False), parts_of_speech=["noun", "plural noun"])
        first, second, third = bedrock.tool_specs
        assert first is second
        assert second is third
        assert part_of_speech_enum(tool=first) == list(PARTS_OF_SPEECH)

    async def test_entries_use_draft_token_budget(self, service: AIService, bedrock: FakeBedrock) -> None:
        await draft_entries(service, bedrock, entries_response(known=False))
        await draft_examples(service, bedrock, examples_response())
        assert [call["inferenceConfig"] for call in bedrock.calls] == [
            {"maxTokens": 4096, "temperature": 0.7},
            {"maxTokens": 2048, "temperature": 0.7},
        ]

    async def test_custom_draft_token_budget(self, bedrock: FakeBedrock) -> None:
        config = AIConfig(model_id="m", max_tokens=99, draft_max_tokens=77, temperature=0.1)
        service = AIService(client=bedrock, config=config)
        await draft_entries(service, bedrock, entries_response(known=False))
        await draft_examples(service, bedrock, examples_response())
        assert [call["inferenceConfig"] for call in bedrock.calls] == [
            {"maxTokens": 77, "temperature": 0.1},
            {"maxTokens": 99, "temperature": 0.1},
        ]


class TestModelOutputFailures:
    @pytest.mark.parametrize(
        "stop_reason", ["max_tokens", "end_turn", "content_filtered", "guardrail_intervened", "stop_sequence"]
    )
    async def test_generation_stopped(self, service: AIService, bedrock: FakeBedrock, stop_reason: str) -> None:
        bedrock.queue(examples_response(group(1, "x abandon"), stop_reason=stop_reason))
        with pytest.raises(ProviderError, match=rf"^bedrock: generation stopped \({stop_reason}\)$"):
            await draft_examples(service, bedrock)

    async def test_missing_stop_reason(self, service: AIService, bedrock: FakeBedrock) -> None:
        response = examples_response(group(1, "x abandon"))
        del response["stopReason"]
        with pytest.raises(ProviderError, match=r"^bedrock: generation stopped \(None\)$"):
            await draft_examples(service, bedrock, response)

    @pytest.mark.parametrize(
        "content",
        [
            [],
            [{"text": "I cannot help with that."}],
            [{"toolUse": {}}],
            [{"toolUse": None}],
            [{"toolUse": {"toolUseId": "t1", "name": "record_entries", "input": {"known": False, "entries": []}}}],
            [{"toolUse": {"toolUseId": "t1", "input": {"examples": []}}}],
        ],
    )
    async def test_no_tool_output(self, service: AIService, bedrock: FakeBedrock, content: list[Json]) -> None:
        with pytest.raises(ProviderError, match=r"^bedrock: no tool output$"):
            await draft_examples(service, bedrock, content_response(content))

    async def test_wrong_tool_for_entries(self, service: AIService, bedrock: FakeBedrock) -> None:
        with pytest.raises(ProviderError, match=r"^bedrock: no tool output$"):
            await draft_entries(service, bedrock, examples_response(group(1, "x")))

    @pytest.mark.parametrize(
        ("response", "cause"),
        [
            (tool_use_response(name="record_examples", payload={"examples": "nope"}), pydantic.ValidationError),
            (tool_use_response(name="record_examples", payload={"examples": "7"}), pydantic.ValidationError),
            (tool_use_response(name="record_examples", payload={"examples": '{"index": 1}'}), pydantic.ValidationError),
            (tool_use_response(name="record_examples", payload={"examples": "[1, 2]"}), pydantic.ValidationError),
            (
                tool_use_response(name="record_examples", payload={"examples": '[{"index": 1}]'}),
                pydantic.ValidationError,
            ),
            (tool_use_response(name="record_examples", payload={}), pydantic.ValidationError),
            (tool_use_response(name="record_examples", payload="nope"), pydantic.ValidationError),
            (examples_response({"index": "one", "sentences": ["x"]}), pydantic.ValidationError),
            (examples_response({"index": 1}), pydantic.ValidationError),
            (examples_response({"index": 1, "sentences": "x"}), pydantic.ValidationError),
            ({"stopReason": "tool_use"}, KeyError),
            ({"stopReason": "tool_use", "output": {}}, KeyError),
            (content_response([{"toolUse": {"toolUseId": "t1", "name": "record_examples"}}]), KeyError),
            (content_response(None), TypeError),
            (content_response(7), TypeError),
            (content_response(["text"]), AttributeError),
            (content_response([None]), AttributeError),
        ],
    )
    async def test_unusable_output(
        self, service: AIService, bedrock: FakeBedrock, *, response: Json, cause: type[Exception]
    ) -> None:
        with pytest.raises(ProviderError, match=r"^bedrock: unusable model output$") as info:
            await draft_examples(service, bedrock, response)
        assert isinstance(info.value.__cause__, cause)

    async def test_unusable_output_logged(
        self, service: AIService, bedrock: FakeBedrock, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING", logger=LOGGER), pytest.raises(ProviderError):
            await draft_examples(service, bedrock, examples_response({"index": "one", "sentences": ["x"]}))
        assert "bedrock returned an unusable payload: 1 validation error for _ExamplesOutput" in caplog.text

    @pytest.mark.parametrize(
        "payload",
        [
            {"known": "maybe", "entries": []},
            {"entries": []},
            {"known": True, "entries": [{"spellings": ["abandon"], "part_of_speech": "verb"}]},
            {"known": True, "entries": [{"spellings": "abandon", "part_of_speech": "verb", "definitions": []}]},
            {"known": True, "entries": [{"spellings": ["abandon"], "part_of_speech": "verb", "definitions": [{}]}]},
        ],
    )
    async def test_unusable_entries_output(self, service: AIService, bedrock: FakeBedrock, payload: Json) -> None:
        with pytest.raises(ProviderError, match=r"^bedrock: unusable model output$") as info:
            await draft_entries(service, bedrock, tool_use_response(name="record_entries", payload=payload))
        assert isinstance(info.value.__cause__, pydantic.ValidationError)


class TestStringifiedJson:
    """Claude occasionally returns a nested array as a JSON string; the service decodes it."""

    async def test_examples_list_as_string(self, service: AIService, bedrock: FakeBedrock) -> None:
        groups = [group(1, "They *abandon* ship."), group(2, "We *abandon* hope.")]
        response = tool_use_response(name="record_examples", payload={"examples": json.dumps(groups)})
        result = await draft_examples(service, bedrock, response, definitions=["to leave", "to give up"])
        assert result == [[ai("They *abandon* ship.")], [ai("We *abandon* hope.")]]

    async def test_sentences_as_string(self, service: AIService, bedrock: FakeBedrock) -> None:
        response = examples_response({"index": 1, "sentences": json.dumps(["They *abandon* ship."])})
        result = await draft_examples(service, bedrock, response)
        assert result == [[ai("They *abandon* ship.")]]

    async def test_entries_list_as_string(self, service: AIService, bedrock: FakeBedrock) -> None:
        payload = {"known": True, "entries": json.dumps([entry()])}
        [draft] = await draft_entries(service, bedrock, tool_use_response(name="record_entries", payload=payload))
        assert draft.spellings == [spelling("abandon")]
        assert draft.definitions[0].text == "to give up"

    async def test_nested_entry_fields_as_strings(self, service: AIService, bedrock: FakeBedrock) -> None:
        definitions = [{"text": "to give up", "examples": json.dumps(["They *abandon* ship."])}]
        response = entries_response(entry(spellings=json.dumps(["abandon"]), definitions=json.dumps(definitions)))
        [draft] = await draft_entries(service, bedrock, response)
        assert draft.spellings == [spelling("abandon")]
        assert draft.definitions[0].examples == [ai("They *abandon* ship.")]

    @pytest.mark.parametrize("entries", ["abandon", "7", "null", '{"spellings": ["abandon"]}'])
    async def test_non_list_strings_still_rejected(
        self, service: AIService, bedrock: FakeBedrock, entries: str
    ) -> None:
        payload = {"known": True, "entries": entries}
        with pytest.raises(ProviderError, match=r"^bedrock: unusable model output$") as info:
            await draft_entries(service, bedrock, tool_use_response(name="record_entries", payload=payload))
        assert isinstance(info.value.__cause__, pydantic.ValidationError)


class TestAWSFailures:
    async def test_client_error_exposes_code_only(
        self, service: AIService, bedrock: FakeBedrock, caplog: pytest.LogCaptureFixture
    ) -> None:
        error = client_error(
            code="AccessDeniedException",
            message=f"User: {ARN} is not authorized to perform: bedrock:InvokeModel",
        )
        with caplog.at_level("WARNING", logger=LOGGER), pytest.raises(ProviderError) as info:
            await draft_examples(service, bedrock, error)
        assert str(info.value) == "bedrock: AccessDeniedException"
        assert ARN not in str(info.value)
        assert info.value.__cause__ is error
        assert "bedrock call failed with AccessDeniedException: " in caplog.text
        assert ARN in caplog.text

    @pytest.mark.parametrize(
        "code", ["ThrottlingException", "ValidationException", "ModelNotReadyException", "ServiceUnavailableException"]
    )
    async def test_client_error_codes(self, service: AIService, bedrock: FakeBedrock, code: str) -> None:
        with pytest.raises(ProviderError, match=rf"^bedrock: {code}$"):
            await draft_entries(service, bedrock, client_error(code=code))

    @pytest.mark.parametrize(
        "error_response",
        [{}, {"Error": {}}, {"Error": {"Code": ""}}, {"Error": {"Code": None, "Message": "x"}}],
    )
    async def test_client_error_without_code(
        self, service: AIService, bedrock: FakeBedrock, error_response: Json
    ) -> None:
        bedrock.queue(botocore.exceptions.ClientError(error_response, "Converse"))
        with pytest.raises(ProviderError, match=r"^bedrock: ClientError$"):
            await draft_examples(service, bedrock)

    @pytest.mark.parametrize(
        "error",
        [
            botocore.exceptions.EndpointConnectionError(endpoint_url="https://x.test"),
            botocore.exceptions.ReadTimeoutError(endpoint_url="https://x.test"),
            botocore.exceptions.ConnectTimeoutError(endpoint_url="https://x.test"),
            botocore.exceptions.NoCredentialsError(),
            botocore.exceptions.NoRegionError(),
            botocore.exceptions.BotoCoreError(),
        ],
        ids=lambda error: type(error).__name__,
    )
    async def test_botocore_error_exposes_class_name(
        self,
        service: AIService,
        bedrock: FakeBedrock,
        caplog: pytest.LogCaptureFixture,
        *,
        error: botocore.exceptions.BotoCoreError,
    ) -> None:
        with caplog.at_level("WARNING", logger=LOGGER), pytest.raises(ProviderError) as info:
            await draft_examples(service, bedrock, error)
        assert str(info.value) == f"bedrock: {type(error).__name__}"
        assert "x.test" not in str(info.value)
        assert info.value.__cause__ is error
        assert f"bedrock call failed: {error}" in caplog.text

    async def test_unexpected_exception_propagates(self, service: AIService, bedrock: FakeBedrock) -> None:
        with pytest.raises(RuntimeError, match=r"^boom$"):
            await draft_examples(service, bedrock, RuntimeError("boom"))

    async def test_missing_script_is_a_test_bug(self, service: AIService, bedrock: FakeBedrock) -> None:
        with pytest.raises(AssertionError, match="no response queued for call #1"):
            await draft_examples(service, bedrock)


class GatedBedrock(FakeBedrock):
    """`converse` blocks until `release` is set; records how many calls were inside at the same time."""

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()
        self.peak = 0
        self._in_flight = 0
        self._lock = threading.Lock()
        self._entered = threading.Semaphore(0)

    def converse(self, **kwargs: t.Any) -> Json:
        with self._lock:
            self._in_flight += 1
            self.peak = max(self.peak, self._in_flight)
        self._entered.release()
        if not self.release.wait(timeout=1.0):
            msg = "GatedBedrock: never released"
            raise TimeoutError(msg)
        with self._lock:
            self._in_flight -= 1
        return super().converse(**kwargs)

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    async def entered(self, *, count: int) -> None:
        """Wait until `count` more calls are blocked inside `converse`."""
        for _ in range(count):
            assert await asyncio.to_thread(self._entered.acquire, True, 1.0), "call never reached converse"


@contextlib.asynccontextmanager
async def in_flight(
    *, gated: GatedBedrock, concurrency: int, calls: int
) -> cabc.AsyncGenerator[list[asyncio.Task[t.Any]]]:
    """Run `calls` concurrent `generate_examples` against `gated`; releases and drains them on exit.

    Yields:
        The running tasks, in start order.
    """
    service = AIService(client=gated, config=AIConfig(model_id=MODEL_ID, concurrency=concurrency))
    gated.queue(*(examples_response(group(1, f"Sentence {i} abandon")) for i in range(calls)))
    tasks = [
        asyncio.create_task(service.generate_examples(word="abandon", part_of_speech="verb", definitions=["x"]))
        for _ in range(calls)
    ]
    try:
        yield tasks
    finally:
        gated.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


class TestConcurrency:
    async def test_concurrency_one_serialises_calls(self) -> None:
        gated = GatedBedrock()
        async with in_flight(gated=gated, concurrency=1, calls=2) as tasks:
            await gated.entered(count=1)
            await asyncio.sleep(0.05)
            assert gated.in_flight == 1
            assert not any(task.done() for task in tasks)
            gated.release.set()
            results = await asyncio.gather(*tasks)
        assert gated.peak == 1
        assert len(gated.calls) == 2
        assert sorted(r[0][0].text for r in results) == ["Sentence 0 *abandon*", "Sentence 1 *abandon*"]

    async def test_concurrency_two_runs_calls_in_parallel(self) -> None:
        gated = GatedBedrock()
        async with in_flight(gated=gated, concurrency=2, calls=2) as tasks:
            await gated.entered(count=2)
            assert gated.in_flight == 2
            gated.release.set()
            await asyncio.gather(*tasks)
        assert gated.peak == 2
        assert len(gated.calls) == 2

    async def test_third_call_waits_for_a_slot(self) -> None:
        gated = GatedBedrock()
        async with in_flight(gated=gated, concurrency=2, calls=3) as tasks:
            await gated.entered(count=2)
            await asyncio.sleep(0.05)
            assert gated.in_flight == 2
            gated.release.set()
            await asyncio.gather(*tasks)
        assert gated.peak == 2
        assert len(gated.calls) == 3

    async def test_slot_released_after_failure(self, bedrock: FakeBedrock) -> None:
        service = AIService(client=bedrock, config=AIConfig(model_id=MODEL_ID, concurrency=1))
        bedrock.queue(client_error(code="ThrottlingException"), examples_response(group(1, "x abandon")))
        with pytest.raises(ProviderError, match=r"^bedrock: ThrottlingException$"):
            await service.generate_examples(word="abandon", part_of_speech="verb", definitions=["x"])
        result = await asyncio.wait_for(
            service.generate_examples(word="abandon", part_of_speech="verb", definitions=["x"]), timeout=1.0
        )
        assert result == [[ai("x *abandon*")]]
        assert len(bedrock.calls) == 2

    async def test_slot_released_after_unusable_output(self, bedrock: FakeBedrock) -> None:
        service = AIService(client=bedrock, config=AIConfig(model_id=MODEL_ID, concurrency=1))
        bedrock.queue(content_response(None), entries_response(known=False))
        with pytest.raises(ProviderError, match="unusable model output"):
            await service.generate_entries(text="abandon")
        assert await asyncio.wait_for(service.generate_entries(text="abandon"), timeout=1.0) == []


class TestBuildAIService:
    @pytest.fixture
    def boto3_client(self, mocker: MockerFixture) -> MagicMock:
        return mocker.patch("dictum.services.ai.boto3.client", return_value=FakeBedrock())

    @pytest.mark.parametrize(
        ("access_key", "secret_key"),
        [
            (None, None),
            (AWS_ACCESS_KEY_ID, None),
            (None, AWS_SECRET_ACCESS_KEY),
            ("", AWS_SECRET_ACCESS_KEY),
            (AWS_ACCESS_KEY_ID, ""),
        ],
    )
    def test_none_without_both_credentials(
        self, *, tmp_path: pathlib.Path, boto3_client: MagicMock, access_key: str | None, secret_key: str | None
    ) -> None:
        settings = make_settings(tmp_path=tmp_path, aws_access_key_id=access_key, aws_secret_access_key=secret_key)
        assert settings.ai_enabled is False
        assert build_ai_service(settings=settings) is None
        assert build_ai_service(settings=settings, client=FakeBedrock()) is None
        boto3_client.assert_not_called()

    async def test_injected_client_used_with_settings_model(
        self, *, tmp_path: pathlib.Path, boto3_client: MagicMock, bedrock: FakeBedrock
    ) -> None:
        settings = make_ai_settings(tmp_path=tmp_path, bedrock_model_id=MODEL_ID)
        service = build_ai_service(settings=settings, client=bedrock)
        assert isinstance(service, AIService)
        boto3_client.assert_not_called()
        bedrock.queue(examples_response())
        await service.generate_examples(word="abandon", part_of_speech="verb", definitions=["x"])
        assert bedrock.calls[0]["modelId"] == MODEL_ID
        assert bedrock.calls[0]["inferenceConfig"] == {"maxTokens": 2048, "temperature": 0.7}

    async def test_boto3_client_built_from_settings(
        self, *, tmp_path: pathlib.Path, boto3_client: MagicMock, mocker: MockerFixture
    ) -> None:
        settings = make_ai_settings(tmp_path=tmp_path, aws_region="eu-central-1", bedrock_model_id=MODEL_ID)
        service = build_ai_service(settings=settings)
        assert isinstance(service, AIService)
        boto3_client.assert_called_once_with(
            "bedrock-runtime",
            region_name="eu-central-1",
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            config=mocker.ANY,
        )
        config = boto3_client.call_args.kwargs["config"]
        assert isinstance(config, botocore.config.Config)
        assert (config.connect_timeout, config.read_timeout) == (5, 20)
        assert config.retries == {"total_max_attempts": 2, "mode": "standard"}
        fake: FakeBedrock = boto3_client.return_value
        fake.queue(entries_response(known=False))
        assert await service.generate_entries(text="abandon") == []
        assert fake.calls[0]["modelId"] == MODEL_ID

    def test_default_region_and_model(self, *, tmp_path: pathlib.Path, boto3_client: MagicMock) -> None:
        settings = make_ai_settings(tmp_path=tmp_path)
        assert build_ai_service(settings=settings) is not None
        assert boto3_client.call_args.kwargs["region_name"] == "us-east-1"
        assert boto3_client.call_args.args == ("bedrock-runtime",)
