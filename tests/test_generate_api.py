import typing as t

import fastapi
import pytest

from dictum.dependencies.ai import get_ai
from dictum.exceptions import ProviderNotConfiguredError
from dictum.schemas.generate import MAX_DEFINITIONS
from dictum.schemas.generate import MAX_PARTS_OF_SPEECH
from dictum.schemas.generate import MAX_SENSES_PER_POS
from dictum.services.ai import PARTS_OF_SPEECH
from dictum.services.ai import SENSE_POLICY
from dictum.services.ai import SYSTEM_PROMPT
from dictum.services.ai import AIService
from tests.conftest import MW_API_KEY
from tests.conftest import FakeBedrock
from tests.conftest import client_error
from tests.conftest import definitions_schema
from tests.conftest import make_ai_settings
from tests.conftest import make_settings
from tests.conftest import part_of_speech_enum
from tests.conftest import tool_use_response

if t.TYPE_CHECKING:
    import pathlib

    import httpx

    from dictum.settings import Settings

Json = dict[str, t.Any]

ARN: t.Final = "arn:aws:iam::123456789012:user/dictum"
IPA: t.Final = "/\u0259\u02c8b\u00e6nd\u0259n/"
"""IPA for `abandon`; escaped because the stress mark (U+02C8) looks like a backtick to linters."""
DISABLED_DETAIL: t.Final = "AI generation is disabled: set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY"


@pytest.fixture
def settings(tmp_path: pathlib.Path) -> Settings:
    return make_ai_settings(tmp_path=tmp_path)


@pytest.fixture
def bedrock() -> FakeBedrock:
    return FakeBedrock()


def examples_body(**overrides: t.Any) -> Json:
    body: Json = {"word": "abandon", "part_of_speech": "verb", "definitions": ["to give up"], "per_definition": 2}
    body.update(overrides)
    return body


def examples_response(*groups: Json) -> Json:
    return tool_use_response(name="record_examples", payload={"examples": list(groups)})


def entries_response(*entries: Json, known: bool = True) -> Json:
    return tool_use_response(name="record_entries", payload={"known": known, "entries": list(entries)})


def entry(**overrides: t.Any) -> Json:
    base: Json = {
        "spellings": ["abandon"],
        "part_of_speech": "verb",
        "pronunciation": IPA,
        "definitions": [{"text": "to give up", "examples": ["They abandon the ship."]}],
    }
    base.update(overrides)
    return base


class TestAIDependency:
    def test_returns_app_state_service(self, app: fastapi.FastAPI) -> None:
        request = fastapi.Request({"type": "http", "app": app})
        assert isinstance(app.state.ai, AIService)
        assert get_ai(request) is app.state.ai

    def test_raises_503_when_unconfigured(self) -> None:
        app = fastapi.FastAPI()
        app.state.ai = None
        with pytest.raises(ProviderNotConfiguredError) as info:
            get_ai(fastapi.Request({"type": "http", "app": app}))
        assert info.value.status_code == 503
        assert info.value.detail == DISABLED_DETAIL


class TestGenerateExamples:
    async def test_returns_examples_per_definition(self, client: httpx.AsyncClient, bedrock: FakeBedrock) -> None:
        bedrock.queue(
            examples_response(
                {"index": 1, "sentences": ["They abandon the ship.", "We **abandon** hope."]},
                {"index": 3, "sentences": ["Third   abandon."]},
            )
        )
        body = examples_body(definitions=["to give up", "to leave behind", "to yield to"])
        response = await client.post("/api/generate/examples", json=body)
        assert response.status_code == 200, response.text
        assert response.json() == {
            "examples": [
                [
                    {"text": "They *abandon* the ship.", "source": "ai"},
                    {"text": "We *abandon* hope.", "source": "ai"},
                ],
                [],
                [{"text": "Third *abandon*.", "source": "ai"}],
            ]
        }

    async def test_request_forwarded_to_bedrock(
        self, client: httpx.AsyncClient, bedrock: FakeBedrock, settings: Settings
    ) -> None:
        bedrock.queue(examples_response())
        body = examples_body(word="  abandon ", definitions=["  to give up ", "to leave behind"], per_definition=3)
        response = await client.post("/api/generate/examples", json=body)
        assert response.status_code == 200, response.text
        assert response.json() == {"examples": [[], []]}
        assert len(bedrock.calls) == 1
        call = bedrock.calls[0]
        assert call["modelId"] == settings.bedrock_model_id
        assert call["system"] == [{"text": SYSTEM_PROMPT}]
        assert call["inferenceConfig"] == {"maxTokens": 2048, "temperature": 0.7}
        assert bedrock.tools == ["record_examples"]
        prompt = bedrock.prompts[0]
        assert prompt.startswith(
            "Word or phrase: abandon\nPart of speech: verb\nDefinitions:\n1. to give up\n2. to leave behind\n\n"
        )
        assert "Write 3 example sentences" in prompt

    async def test_per_definition_defaults_to_two(self, client: httpx.AsyncClient, bedrock: FakeBedrock) -> None:
        bedrock.queue(examples_response({"index": 1, "sentences": ["a abandon", "b abandon", "c abandon"]}))
        body = examples_body()
        del body["per_definition"]
        response = await client.post("/api/generate/examples", json=body)
        assert response.status_code == 200, response.text
        assert [e["text"] for e in response.json()["examples"][0]] == ["a *abandon*", "b *abandon*"]
        assert "Write 2 example sentences" in bedrock.prompts[0]

    async def test_nothing_persisted(self, client: httpx.AsyncClient, bedrock: FakeBedrock) -> None:
        bedrock.queue(examples_response({"index": 1, "sentences": ["They abandon it."]}))
        assert (await client.post("/api/generate/examples", json=examples_body())).status_code == 200
        assert (await client.get("/api/words")).json()["total"] == 0

    @pytest.mark.parametrize(
        ("overrides", "fragment"),
        [
            ({"word": ""}, "at least 1"),
            ({"word": "   "}, "visible characters"),
            ({"word": "Color / Colour"}, "must not contain ' / '"),
            ({"word": "x" * 256}, "at most 255"),
            ({"part_of_speech": ""}, "at least 1"),
            ({"part_of_speech": "x" * 65}, "at most 64"),
            ({"definitions": []}, "at least 1"),
            ({"definitions": ["x"] * (MAX_DEFINITIONS + 1)}, f"at most {MAX_DEFINITIONS}"),
            ({"definitions": [""]}, "at least 1"),
            ({"definitions": ["   "]}, "at least 1"),
            ({"definitions": ["to give up", "\n\t"]}, "at least 1"),
            ({"definitions": ["x" * 2001]}, "at most 2000"),
            ({"definitions": "to give up"}, "list"),
            ({"per_definition": 0}, "greater than or equal to 1"),
            ({"per_definition": 6}, "less than or equal to 5"),
            ({"per_definition": "two"}, "integer"),
        ],
    )
    async def test_invalid_body_returns_422(
        self, client: httpx.AsyncClient, bedrock: FakeBedrock, *, overrides: Json, fragment: str
    ) -> None:
        response = await client.post("/api/generate/examples", json=examples_body(**overrides))
        assert response.status_code == 422, response.text
        assert fragment in response.text
        assert bedrock.calls == []

    @pytest.mark.parametrize("missing", ["word", "part_of_speech", "definitions"])
    async def test_required_fields(self, client: httpx.AsyncClient, bedrock: FakeBedrock, missing: str) -> None:
        body = examples_body()
        del body[missing]
        response = await client.post("/api/generate/examples", json=body)
        assert response.status_code == 422
        assert missing in response.text
        assert bedrock.calls == []

    async def test_boundary_values_accepted(self, client: httpx.AsyncClient, bedrock: FakeBedrock) -> None:
        bedrock.queue(examples_response())
        body = examples_body(definitions=["x" * 2000] * MAX_DEFINITIONS, per_definition=5)
        response = await client.post("/api/generate/examples", json=body)
        assert response.status_code == 200, response.text
        assert response.json() == {"examples": [[] for _ in range(MAX_DEFINITIONS)]}
        assert f"{MAX_DEFINITIONS}. {'x' * 2000}\n\nWrite 5 example sentences" in bedrock.prompts[0]

    async def test_provider_error_returns_502_without_aws_message(
        self, client: httpx.AsyncClient, bedrock: FakeBedrock, caplog: pytest.LogCaptureFixture
    ) -> None:
        bedrock.queue(client_error(code="AccessDeniedException", message=f"User: {ARN} is not authorized"))
        with caplog.at_level("WARNING", logger="dictum.services.ai"):
            response = await client.post("/api/generate/examples", json=examples_body())
        assert response.status_code == 502
        assert response.json() == {"detail": "bedrock: AccessDeniedException"}
        assert ARN not in response.text
        assert ARN in caplog.text

    @pytest.mark.parametrize(
        ("response", "detail"),
        [
            (
                tool_use_response(name="record_examples", payload={"examples": []}, stop_reason="max_tokens"),
                "bedrock: generation stopped (max_tokens)",
            ),
            (
                tool_use_response(name="record_entries", payload={"known": False, "entries": []}),
                "bedrock: no tool output",
            ),
            (tool_use_response(name="record_examples", payload={"examples": "nope"}), "bedrock: unusable model output"),
        ],
    )
    async def test_bad_model_output_returns_502(
        self, client: httpx.AsyncClient, bedrock: FakeBedrock, *, response: Json, detail: str
    ) -> None:
        bedrock.queue(response)
        result = await client.post("/api/generate/examples", json=examples_body())
        assert result.status_code == 502
        assert result.json() == {"detail": detail}


class TestGenerateDefinitions:
    async def test_returns_drafted_entries(self, client: httpx.AsyncClient, bedrock: FakeBedrock) -> None:
        bedrock.queue(entries_response(entry(spellings=["abandon", "abandonne"])))
        response = await client.post("/api/generate/definitions", json={"text": "abandon"})
        assert response.status_code == 200, response.text
        assert response.json() == {
            "query": "abandon",
            "found": True,
            "entries": [
                {
                    "spellings": [{"text": "abandon", "kind": "primary"}, {"text": "abandonne", "kind": "variant"}],
                    "part_of_speech": "verb",
                    "pronunciation": IPA,
                    "audio_path": None,
                    "definitions": [
                        {
                            "text": "to give up",
                            "source": "ai",
                            "examples": [{"text": "They *abandon* the ship.", "source": "ai"}],
                        }
                    ],
                    "source": "ai",
                }
            ],
        }

    async def test_request_forwarded_to_bedrock(
        self, client: httpx.AsyncClient, bedrock: FakeBedrock, settings: Settings
    ) -> None:
        bedrock.queue(entries_response(known=False))
        response = await client.post("/api/generate/definitions", json={"text": "  put \t paid  to "})
        assert response.status_code == 200, response.text
        assert response.json() == {"query": "put paid to", "found": False, "entries": []}
        assert bedrock.calls[0]["modelId"] == settings.bedrock_model_id
        assert bedrock.calls[0]["toolConfig"]["toolChoice"] == {"tool": {"name": "record_entries"}}
        assert bedrock.calls[0]["inferenceConfig"] == {"maxTokens": 4096, "temperature": 0.7}
        assert bedrock.prompts[0].startswith("Text: put paid to\n\n")
        assert "each with 2 example sentences" in bedrock.prompts[0]

    async def test_defaults_leave_draft_unrestricted(self, client: httpx.AsyncClient, bedrock: FakeBedrock) -> None:
        bedrock.queue(entries_response(entry()))
        response = await client.post("/api/generate/definitions", json={"text": "abandon"})
        assert response.status_code == 200, response.text
        assert response.json()["found"] is True
        prompt = bedrock.prompts[0]
        assert SENSE_POLICY in prompt
        assert "only when used as" not in prompt
        assert "noted these senses" not in prompt
        assert "record one entry per part of speech: canonical spelling first" in prompt
        [tool] = bedrock.tool_specs
        assert part_of_speech_enum(tool=tool) == list(PARTS_OF_SPEECH)
        assert definitions_schema(tool=tool)["maxItems"] == MAX_SENSES_PER_POS

    async def test_steering_forwarded_to_bedrock(self, client: httpx.AsyncClient, bedrock: FakeBedrock) -> None:
        definitions = [{"text": "a petty criticism", "examples": ["That is a mere nitpick, not a flaw."]}]
        bedrock.queue(entries_response(entry(spellings=["nitpick"], part_of_speech="noun", definitions=definitions)))
        body = {"text": "nitpick", "hints": ["a petty criticism"], "parts_of_speech": ["noun"], "per_definition": 1}
        response = await client.post("/api/generate/definitions", json=body)
        assert response.status_code == 200, response.text
        assert response.json()["found"] is True
        [drafted] = response.json()["entries"]
        assert drafted["part_of_speech"] == "noun"
        assert drafted["definitions"][0]["examples"] == [
            {"text": "That is a mere *nitpick*, not a flaw.", "source": "ai"}
        ]
        prompt = bedrock.prompts[0]
        assert prompt.startswith("Text: nitpick\n\n")
        assert "(only when used as noun; record no entries if it never is)" in prompt
        assert "each with 1 example sentences" in prompt
        assert "any part of speech):\n1. a petty criticism\nCover each of them first" in prompt
        assert part_of_speech_enum(tool=bedrock.tool_specs[0]) == ["noun"]
        assert bedrock.calls[0]["inferenceConfig"] == {"maxTokens": 4096, "temperature": 0.7}

    async def test_hints_and_parts_of_speech_stripped(self, client: httpx.AsyncClient, bedrock: FakeBedrock) -> None:
        bedrock.queue(entries_response(known=False))
        body = {
            "text": "nitpick",
            "hints": ["  a petty criticism \n", "\tto find fault with trifles "],
            "parts_of_speech": [" Noun ", "verb\n"],
        }
        response = await client.post("/api/generate/definitions", json=body)
        assert response.status_code == 200, response.text
        prompt = bedrock.prompts[0]
        assert "\n1. a petty criticism\n2. to find fault with trifles\nCover each of them first" in prompt
        assert "(only when used as Noun or verb; record no entries if it never is)" in prompt
        assert part_of_speech_enum(tool=bedrock.tool_specs[0]) == ["noun", "verb"]

    async def test_unknown_part_of_speech_label_keeps_full_enum(
        self, client: httpx.AsyncClient, bedrock: FakeBedrock
    ) -> None:
        bedrock.queue(entries_response(known=False))
        body = {"text": "scissors", "parts_of_speech": ["plural noun"]}
        response = await client.post("/api/generate/definitions", json=body)
        assert response.status_code == 200, response.text
        assert "(only when used as plural noun; record no entries if it never is)" in bedrock.prompts[0]
        assert part_of_speech_enum(tool=bedrock.tool_specs[0]) == list(PARTS_OF_SPEECH)

    async def test_boundary_values_accepted(self, client: httpx.AsyncClient, bedrock: FakeBedrock) -> None:
        bedrock.queue(entries_response(known=False))
        body = {
            "text": "abandon",
            "hints": [f"sense {i}" for i in range(1, MAX_DEFINITIONS + 1)],
            "parts_of_speech": list(PARTS_OF_SPEECH),
            "per_definition": 5,
        }
        assert len(body["parts_of_speech"]) == MAX_PARTS_OF_SPEECH
        response = await client.post("/api/generate/definitions", json=body)
        assert response.status_code == 200, response.text
        prompt = bedrock.prompts[0]
        assert f"\n{MAX_DEFINITIONS}. sense {MAX_DEFINITIONS}\nCover each of them first" in prompt
        assert "each with 5 example sentences" in prompt
        assert f"(only when used as {' or '.join(PARTS_OF_SPEECH)}; record no entries if it never is)" in prompt
        assert part_of_speech_enum(tool=bedrock.tool_specs[0]) == list(PARTS_OF_SPEECH)

    async def test_drafted_definitions_capped_at_max_senses(
        self, client: httpx.AsyncClient, bedrock: FakeBedrock
    ) -> None:
        definitions = [{"text": f"sense {i}", "examples": []} for i in range(1, 11)]
        bedrock.queue(entries_response(entry(definitions=definitions)))
        response = await client.post("/api/generate/definitions", json={"text": "abandon"})
        assert response.status_code == 200, response.text
        [drafted] = response.json()["entries"]
        assert [d["text"] for d in drafted["definitions"]] == [f"sense {i}" for i in range(1, MAX_SENSES_PER_POS + 1)]

    async def test_unknown_text_is_not_found(self, client: httpx.AsyncClient, bedrock: FakeBedrock) -> None:
        bedrock.queue(entries_response(entry(), known=False))
        response = await client.post("/api/generate/definitions", json={"text": "abandn"})
        assert response.status_code == 200
        assert response.json() == {"query": "abandn", "found": False, "entries": []}

    async def test_known_but_unusable_entries_is_not_found(
        self, client: httpx.AsyncClient, bedrock: FakeBedrock
    ) -> None:
        bedrock.queue(entries_response(entry(spellings=["Color / Colour"]), entry(definitions=[])))
        response = await client.post("/api/generate/definitions", json={"text": "colour"})
        assert response.status_code == 200
        assert response.json() == {"query": "colour", "found": False, "entries": []}

    async def test_nothing_persisted(self, client: httpx.AsyncClient, bedrock: FakeBedrock) -> None:
        bedrock.queue(entries_response(entry()))
        assert (await client.post("/api/generate/definitions", json={"text": "abandon"})).status_code == 200
        assert (await client.get("/api/words")).json()["total"] == 0
        assert (await client.get("/api/words/by-spelling", params={"text": "abandon"})).status_code == 404

    @pytest.mark.parametrize(
        ("body", "fragment"),
        [
            ({"text": ""}, "at least 1"),
            ({"text": "   "}, "visible characters"),
            ({"text": "Color / Colour"}, "must not contain ' / '"),
            ({"text": "x" * 256}, "at most 255"),
            ({"text": None}, "text"),
            ({}, "text"),
            ({"text": "abandon", "hints": ["h"] * (MAX_DEFINITIONS + 1)}, f"at most {MAX_DEFINITIONS}"),
            ({"text": "abandon", "hints": [""]}, "at least 1"),
            ({"text": "abandon", "hints": ["   "]}, "at least 1"),
            ({"text": "abandon", "hints": ["to give up", "\n\t"]}, "at least 1"),
            ({"text": "abandon", "hints": ["x" * 2001]}, "at most 2000"),
            ({"text": "abandon", "hints": "to give up"}, "list"),
            ({"text": "abandon", "hints": None}, "list"),
            (
                {"text": "abandon", "parts_of_speech": ["noun"] * (MAX_PARTS_OF_SPEECH + 1)},
                f"at most {MAX_PARTS_OF_SPEECH}",
            ),
            ({"text": "abandon", "parts_of_speech": [""]}, "at least 1"),
            ({"text": "abandon", "parts_of_speech": ["  "]}, "at least 1"),
            ({"text": "abandon", "parts_of_speech": ["n" * 65]}, "at most 64"),
            ({"text": "abandon", "parts_of_speech": "noun"}, "list"),
            ({"text": "abandon", "per_definition": 0}, "greater than or equal to 1"),
            ({"text": "abandon", "per_definition": 6}, "less than or equal to 5"),
            ({"text": "abandon", "per_definition": "two"}, "integer"),
        ],
    )
    async def test_invalid_body_returns_422(
        self, client: httpx.AsyncClient, bedrock: FakeBedrock, *, body: Json, fragment: str
    ) -> None:
        response = await client.post("/api/generate/definitions", json=body)
        assert response.status_code == 422
        assert fragment in response.text
        assert bedrock.calls == []

    async def test_provider_error_returns_502(self, client: httpx.AsyncClient, bedrock: FakeBedrock) -> None:
        bedrock.queue(client_error(code="ThrottlingException", message=f"Rate exceeded for {ARN}"))
        response = await client.post("/api/generate/definitions", json={"text": "abandon"})
        assert response.status_code == 502
        assert response.json() == {"detail": "bedrock: ThrottlingException"}
        assert ARN not in response.text


class TestGenerateDisabled:
    @pytest.fixture
    def settings(self, tmp_path: pathlib.Path) -> Settings:
        return make_settings(tmp_path=tmp_path)

    @pytest.fixture
    def bedrock(self) -> None:
        return None

    async def test_state_has_no_service(self, app: fastapi.FastAPI) -> None:
        assert app.state.ai is None

    @pytest.mark.parametrize(
        ("path", "body"),
        [("/api/generate/examples", examples_body()), ("/api/generate/definitions", {"text": "abandon"})],
    )
    async def test_returns_503(self, client: httpx.AsyncClient, *, path: str, body: Json) -> None:
        response = await client.post(path, json=body)
        assert response.status_code == 503
        assert response.json() == {"detail": DISABLED_DETAIL}

    async def test_health_reports_ai_off(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/api/health")).json()["ai"] is False


class TestHealthWithAI:
    @pytest.fixture
    def settings(self, tmp_path: pathlib.Path) -> Settings:
        return make_ai_settings(tmp_path=tmp_path, mw_api_key=MW_API_KEY)

    async def test_reports_providers_and_ai(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "database": "ok", "providers": ["mw", "free_dict"], "ai": True}
