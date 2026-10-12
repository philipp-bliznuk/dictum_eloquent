import datetime as dt
import typing as t

import pydantic
import pytest

from dictum.models.word import TEXT_MAX_LENGTH
from dictum.models.word import Definition
from dictum.models.word import Example
from dictum.models.word import Lexeme
from dictum.models.word import Spelling
from dictum.models.word import Word
from dictum.schemas.factory import partial_model
from dictum.schemas.generate import MAX_DEFINITIONS
from dictum.schemas.generate import MAX_EXAMPLES_PER_DEFINITION
from dictum.schemas.generate import MAX_PARTS_OF_SPEECH
from dictum.schemas.generate import MAX_SENSES_PER_POS
from dictum.schemas.generate import DefinitionsRequest
from dictum.schemas.lookup import LookupRequest
from dictum.schemas.word import DefinitionIn
from dictum.schemas.word import ExampleIn
from dictum.schemas.word import LexemeIn
from dictum.schemas.word import WordCreate
from dictum.schemas.word import WordResponse
from dictum.schemas.word import WordSummary
from dictum.schemas.word import WordUpdate
from tests.conftest import LOOSE_EXAMPLE
from tests.conftest import NESTED_EXAMPLE
from tests.conftest import lexeme_payload
from tests.conftest import word_payload

NOW: t.Final = dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.UTC)


class _Source(pydantic.BaseModel):
    name: t.Annotated[str, pydantic.Field(max_length=3)]
    count: int = 5
    note: str | None = None


def _stored_word() -> Word:
    """In-memory word as the ORM would load it: two lexemes, nested examples linked by `definition_id`."""
    word = Word(id=7, created_at=NOW, updated_at=NOW, notes="n", status="inbox")
    word.spellings = [Spelling(id=1, text="Record", kind="primary"), Spelling(id=2, text="Recorde", kind="variant")]
    d1 = Definition(id=1, text="a written account", position=0, source="user")
    d2 = Definition(id=2, text="a disc", position=1, source="mw")
    d3 = Definition(id=3, text="to set down", position=0, source="ai")
    e1 = Example(id=1, text="Keep a *record*.", position=0, source="user", definition_id=1)
    e2 = Example(id=2, text="She *recorded* it.", position=1, source="ai", definition_id=3)
    e3 = Example(id=3, text="Loose *record*.", position=2, source="vault", definition_id=None)
    d1.examples = [e1]
    d2.examples = []
    d3.examples = [e2]
    noun = Lexeme(id=1, part_of_speech="noun", pronunciation="REK-erd", audio_path="/audio/record1.mp3", position=0)
    verb = Lexeme(id=2, part_of_speech="verb", pronunciation=None, audio_path=None, position=1)
    noun.definitions = [d1, d2]
    verb.definitions = [d3]
    word.lexemes = [noun, verb]
    word.examples = [e1, e2, e3]
    return word


class TestPartialModel:
    def test_all_fields_optional(self) -> None:
        patch = partial_model(base=_Source, name="SourcePatch")
        assert patch.__name__ == "SourcePatch"
        instance = patch()
        assert instance.model_dump() == {"name": None, "count": None, "note": None}
        assert instance.model_dump(exclude_unset=True) == {}

    def test_constraints_kept(self) -> None:
        patch = partial_model(base=_Source, name="SourcePatch")
        with pytest.raises(pydantic.ValidationError, match="at most 3"):
            patch(name="long")
        assert patch(name="ok", count=1).model_dump(exclude_unset=True) == {"name": "ok", "count": 1}

    def test_word_update_has_only_word_scalars(self) -> None:
        assert set(WordUpdate.model_fields) == {"notes", "status"}

    def test_word_update_tracks_unset(self) -> None:
        update = WordUpdate(status="inbox")
        assert update.model_dump(exclude_unset=True) == {"status": "inbox"}

    def test_word_update_keeps_max_length(self) -> None:
        with pytest.raises(pydantic.ValidationError, match="at most 10000"):
            WordUpdate(notes="x" * 10_001)

    def test_word_update_rejects_null_on_required(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            WordUpdate.model_validate({"status": None})

    def test_word_update_rejects_unknown_status(self) -> None:
        with pytest.raises(pydantic.ValidationError, match="status"):
            WordUpdate.model_validate({"status": "bogus"})

    def test_word_update_accepts_null_on_nullable(self) -> None:
        assert WordUpdate.model_validate({"notes": None}).model_dump(exclude_unset=True) == {"notes": None}

    @pytest.mark.parametrize(
        "field", ["part_of_speech", "pronunciation", "audio_path", "lexemes", "spellings", "examples", "definitions"]
    )
    def test_word_update_rejects_grammar_and_children(self, field: str) -> None:
        with pytest.raises(pydantic.ValidationError, match=field):
            WordUpdate.model_validate({field: "x"})


class TestWordCreate:
    def test_valid_payload(self) -> None:
        word = WordCreate.model_validate(word_payload())
        assert word.spellings[0].text == "Abandon"
        assert word.notes is None
        assert word.status == "learned"
        [lexeme] = word.lexemes
        assert (lexeme.part_of_speech, lexeme.pronunciation, lexeme.audio_path) == ("verb", "uh-BAN-dun", None)
        [definition] = lexeme.definitions
        assert (definition.text, definition.source) == ("to give up", "user")
        assert [(e.text, e.source) for e in definition.examples] == [(NESTED_EXAMPLE, "user")]
        assert [(e.text, e.source) for e in word.examples] == [(LOOSE_EXAMPLE, "vault")]

    @pytest.mark.parametrize(
        ("raw", "clean"),
        [
            ("By my troth!", "By my troth!"),
            ("  in   extenso ", "in extenso"),
            ("put\tpaid\nto", "put paid to"),
            ("and/or", "and/or"),
        ],
    )
    def test_spelling_normalised(self, raw: str, clean: str) -> None:
        word = WordCreate.model_validate(word_payload(spellings=[{"text": raw}]))
        assert word.spellings[0].text == clean

    @pytest.mark.parametrize(
        ("text", "match"),
        [
            ("Color / Colour", "must not contain ' / '"),
            ("   ", "visible characters"),
            ("x" * 256, "at most 255"),
        ],
    )
    def test_spelling_rejected(self, text: str, match: str) -> None:
        with pytest.raises(pydantic.ValidationError, match=match):
            WordCreate.model_validate(word_payload(spellings=[{"text": text}]))

    def test_duplicate_detected_after_normalisation(self) -> None:
        payload = word_payload(spellings=[{"text": "put paid to"}, {"text": " Put  Paid To "}])
        with pytest.raises(pydantic.ValidationError, match="duplicate spelling"):
            WordCreate.model_validate(payload)

    def test_legacy_definition_index_rejected(self) -> None:
        payload = word_payload(examples=[{"text": "x", "definition_index": 1}])
        with pytest.raises(pydantic.ValidationError, match="definition_index"):
            WordCreate.model_validate(payload)

    @pytest.mark.parametrize("field", ["part_of_speech", "pronunciation", "audio_path", "definitions"])
    def test_legacy_word_level_fields_rejected(self, field: str) -> None:
        with pytest.raises(pydantic.ValidationError, match=field):
            WordCreate.model_validate(word_payload(**{field: "noun"}))

    def test_mislabelled_v1_word_lists_every_stray_field(self) -> None:
        payload = word_payload(part_of_speech="verb", pronunciation="x", definitions=[{"text": "d"}])
        with pytest.raises(pydantic.ValidationError) as exc_info:
            WordCreate.model_validate(payload)
        stray = sorted(str(err["loc"][0]) for err in exc_info.value.errors() if err["type"] == "extra_forbidden")
        assert stray == ["definitions", "part_of_speech", "pronunciation"]

    def test_loose_examples_allowed_without_lexemes(self) -> None:
        payload = word_payload(lexemes=[], examples=[{"text": "x"}])
        word = WordCreate.model_validate(payload)
        assert word.lexemes == []
        assert word.examples[0].text == "x"

    @pytest.mark.parametrize("second", ["Abandon", "abandon", "ABANDON"])
    def test_duplicate_spellings_rejected(self, second: str) -> None:
        payload = word_payload(spellings=[{"text": "Abandon"}, {"text": second}])
        with pytest.raises(pydantic.ValidationError, match="duplicate spelling"):
            WordCreate.model_validate(payload)

    def test_spellings_required(self) -> None:
        with pytest.raises(pydantic.ValidationError, match="at least 1"):
            WordCreate.model_validate(word_payload(spellings=[]))

    @pytest.mark.parametrize(
        ("field", "value", "match"),
        [
            ("spellings", [{"text": ""}], "at least 1"),
            ("lexemes", [lexeme_payload(definitions=[{"text": ""}])], "at least 1"),
            ("lexemes", [lexeme_payload(definitions=[{"text": "x" * 2001}])], "at most 2000"),
            ("lexemes", [lexeme_payload(definitions=[{"text": "d", "examples": [{"text": ""}]}])], "at least 1"),
            (
                "lexemes",
                [lexeme_payload(definitions=[{"text": "d", "examples": [{"text": "x" * 2001}]}])],
                "at most 2000",
            ),
            ("lexemes", [lexeme_payload(definitions=[{"text": "d", "source": "bogus"}])], "source"),
            (
                "lexemes",
                [lexeme_payload(definitions=[{"text": "d", "examples": [{"text": "e", "source": "bogus"}]}])],
                "source",
            ),
            ("lexemes", [lexeme_payload(pronunciation="x" * 256)], "at most 255"),
            ("lexemes", [lexeme_payload(audio_path="https://evil.example/x.mp3")], "pattern"),
            ("lexemes", [lexeme_payload(audio_path="/audio/../etc/passwd")], "pattern"),
            ("lexemes", [lexeme_payload(audio_path="/audio/")], "pattern"),
            ("lexemes", [{"definitions": []}], "part_of_speech"),
            ("examples", [{"text": ""}], "at least 1"),
            ("examples", [{"text": "x" * 2001}], "at most 2000"),
            ("examples", [{"text": "x", "source": "bogus"}], "source"),
            ("notes", "x" * 10_001, "at most 10000"),
            ("status", "bogus", "status"),
        ],
    )
    def test_constraints(self, field: str, value: object, match: str) -> None:
        with pytest.raises(pydantic.ValidationError, match=match):
            WordCreate.model_validate(word_payload(**{field: value}))

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("lexemes", [lexeme_payload(position=9)]),
            ("lexemes", [lexeme_payload(definitions=[{"text": "x", "position": 9}])]),
            ("lexemes", [lexeme_payload(definitions=[{"text": "x", "examples": [{"text": "e", "position": 9}]}])]),
            ("examples", [{"text": "l", "position": 9}]),
            ("spellings", [{"text": "Cat", "id": 9}]),
        ],
    )
    def test_server_side_fields_rejected(self, field: str, value: object) -> None:
        with pytest.raises(pydantic.ValidationError, match="Extra inputs are not permitted"):
            WordCreate.model_validate(word_payload(**{field: value}))

    def test_audio_path_accepted(self) -> None:
        word = WordCreate.model_validate(word_payload(lexemes=[lexeme_payload(audio_path="/audio/aband01_1.mp3")]))
        assert word.lexemes[0].audio_path == "/audio/aband01_1.mp3"

    def test_defaults(self) -> None:
        word = WordCreate.model_validate({"spellings": [{"text": "Cat"}]})
        assert word.status == "learned"
        assert word.notes is None
        assert word.lexemes == []
        assert word.examples == []
        assert word.spellings[0].kind == "primary"

    def test_child_defaults(self) -> None:
        lexeme = LexemeIn.model_validate({"part_of_speech": "noun"})
        assert (lexeme.pronunciation, lexeme.audio_path, lexeme.definitions) == (None, None, [])
        definition = DefinitionIn.model_validate({"text": "d"})
        assert (definition.source, definition.examples) == ("user", [])
        assert ExampleIn.model_validate({"text": "e"}).source == "user"


class TestLexemeIn:
    @pytest.mark.parametrize(
        ("raw", "clean"),
        [
            ("verb", "verb"),
            (" Noun ", "noun"),
            ("NOUN", "noun"),
            ("transitive\t verb", "transitive verb"),
            ("verb / noun", "verb / noun"),
            ("plural\nnoun", "plural noun"),
            ("Straße", "strasse"),
        ],
    )
    def test_part_of_speech_normalised(self, *, raw: str, clean: str) -> None:
        assert LexemeIn.model_validate({"part_of_speech": raw}).part_of_speech == clean

    @pytest.mark.parametrize(
        ("raw", "match"),
        [
            ("", "at least 1"),
            ("   ", "part of speech must contain visible characters"),
            ("\t\n", "part of speech must contain visible characters"),
            ("x" * 65, "at most 64"),
        ],
    )
    def test_part_of_speech_rejected(self, *, raw: str, match: str) -> None:
        with pytest.raises(pydantic.ValidationError, match=match):
            LexemeIn.model_validate({"part_of_speech": raw})

    def test_part_of_speech_required(self) -> None:
        with pytest.raises(pydantic.ValidationError, match="part_of_speech"):
            LexemeIn.model_validate({})

    @pytest.mark.parametrize(
        ("first", "second", "label"),
        [
            ("noun", "noun", "noun"),
            ("Noun", "noun", "noun"),
            ("noun", " NOUN ", "noun"),
            ("transitive verb", "transitive  verb", "transitive verb"),
        ],
    )
    def test_duplicate_part_of_speech_rejected(self, *, first: str, second: str, label: str) -> None:
        payload = word_payload(lexemes=[lexeme_payload(part_of_speech=first), lexeme_payload(part_of_speech=second)])
        with pytest.raises(pydantic.ValidationError, match=f"duplicate part of speech: {label}") as info:
            WordCreate.model_validate(payload)
        [error] = info.value.errors()
        assert error["loc"] == ("lexemes",)

    def test_distinct_parts_of_speech_keep_order(self) -> None:
        payload = word_payload(
            lexemes=[
                lexeme_payload(part_of_speech="Verb"),
                lexeme_payload(part_of_speech="noun"),
                lexeme_payload(part_of_speech="verb / noun"),
            ]
        )
        word = WordCreate.model_validate(payload)
        assert [x.part_of_speech for x in word.lexemes] == ["verb", "noun", "verb / noun"]


class TestWordResponse:
    def test_from_word_splits_examples(self) -> None:
        response = WordResponse.from_word(word=_stored_word())
        assert response.model_dump(mode="json") == {
            "id": 7,
            "created_at": "2026-10-10T12:00:00Z",
            "updated_at": "2026-10-10T12:00:00Z",
            "notes": "n",
            "status": "inbox",
            "spellings": [
                {"id": 1, "text": "Record", "kind": "primary"},
                {"id": 2, "text": "Recorde", "kind": "variant"},
            ],
            "lexemes": [
                {
                    "id": 1,
                    "position": 0,
                    "part_of_speech": "noun",
                    "pronunciation": "REK-erd",
                    "audio_path": "/audio/record1.mp3",
                    "definitions": [
                        {
                            "id": 1,
                            "position": 0,
                            "text": "a written account",
                            "source": "user",
                            "examples": [{"id": 1, "position": 0, "text": "Keep a *record*.", "source": "user"}],
                        },
                        {"id": 2, "position": 1, "text": "a disc", "source": "mw", "examples": []},
                    ],
                },
                {
                    "id": 2,
                    "position": 1,
                    "part_of_speech": "verb",
                    "pronunciation": None,
                    "audio_path": None,
                    "definitions": [
                        {
                            "id": 3,
                            "position": 0,
                            "text": "to set down",
                            "source": "ai",
                            "examples": [{"id": 2, "position": 1, "text": "She *recorded* it.", "source": "ai"}],
                        }
                    ],
                },
            ],
            "examples": [{"id": 3, "position": 2, "text": "Loose *record*.", "source": "vault"}],
        }

    def test_from_word_without_children(self) -> None:
        word = Word(id=1, created_at=NOW, updated_at=NOW)
        word.spellings = [Spelling(id=1, text="Cat")]
        word.lexemes = []
        word.examples = []
        response = WordResponse.from_word(word=word)
        assert (response.lexemes, response.examples, response.notes, response.status) == ([], [], None, "learned")

    def test_no_definition_id_in_output(self) -> None:
        dumped = WordResponse.from_word(word=_stored_word()).model_dump()
        assert "definition_id" not in dumped["examples"][0]
        assert "definition_id" not in dumped["lexemes"][0]["definitions"][0]["examples"][0]
        assert "word_id" not in dumped["examples"][0]


class TestWordSummary:
    def test_from_word(self) -> None:
        summary = WordSummary.from_word(word=_stored_word())
        assert summary.model_dump() == {
            "id": 7,
            "spellings": [
                {"id": 1, "text": "Record", "kind": "primary"},
                {"id": 2, "text": "Recorde", "kind": "variant"},
            ],
            "parts_of_speech": ["noun", "verb"],
            "status": "inbox",
            "definition_count": 3,
        }

    def test_from_word_without_lexemes(self) -> None:
        word = Word(id=1, created_at=NOW, updated_at=NOW)
        word.spellings = [Spelling(id=1, text="Cat")]
        word.lexemes = []
        summary = WordSummary.from_word(word=word)
        assert (summary.parts_of_speech, summary.definition_count, summary.status) == ([], 0, "learned")

    def test_definition_count_defaults_to_zero(self) -> None:
        assert WordSummary(id=1, spellings=[], parts_of_speech=[], status="learned").definition_count == 0


class TestLookupRequest:
    @pytest.mark.parametrize(
        ("raw", "clean"),
        [
            ("abandon", "abandon"),
            ("  put \t paid \n to ", "put paid to"),
            ("By my troth!", "By my troth!"),
            ("and/or", "and/or"),
            ("Aide-de-camp", "Aide-de-camp"),
        ],
    )
    def test_text_normalised(self, raw: str, clean: str) -> None:
        assert LookupRequest(text=raw).text == clean

    @pytest.mark.parametrize(
        ("text", "match"),
        [
            ("", "at least 1"),
            ("   ", "visible characters"),
            ("Color / Colour", "must not contain ' / '"),
            ("x" * 256, "at most 255"),
        ],
    )
    def test_text_rejected(self, text: str, match: str) -> None:
        with pytest.raises(pydantic.ValidationError, match=match):
            LookupRequest(text=text)

    def test_text_required(self) -> None:
        with pytest.raises(pydantic.ValidationError, match="text"):
            LookupRequest.model_validate({})


class TestDefinitionsRequest:
    def test_limits(self) -> None:
        assert (MAX_DEFINITIONS, MAX_PARTS_OF_SPEECH, MAX_SENSES_PER_POS, MAX_EXAMPLES_PER_DEFINITION) == (20, 10, 8, 5)

    def test_defaults(self) -> None:
        request = DefinitionsRequest(text="abandon")
        assert request.model_dump() == {"text": "abandon", "hints": [], "parts_of_speech": [], "per_definition": 2}
        assert request.model_dump(exclude_unset=True) == {"text": "abandon"}

    def test_is_a_lookup_request(self) -> None:
        assert issubclass(DefinitionsRequest, LookupRequest)
        assert DefinitionsRequest(text="  put \t paid \n to ").text == "put paid to"

    @pytest.mark.parametrize(
        ("raw", "clean"),
        [
            (["  a petty criticism \n"], ["a petty criticism"]),
            (["\tto find  fault "], ["to find  fault"]),
            (["first", " second ", "third\n"], ["first", "second", "third"]),
        ],
    )
    def test_hints_stripped_in_order(self, *, raw: list[str], clean: list[str]) -> None:
        assert DefinitionsRequest(text="x", hints=raw).hints == clean

    @pytest.mark.parametrize(
        ("raw", "clean"),
        [([" noun "], ["noun"]), (["Noun", "\tverb\n"], ["Noun", "verb"]), (["plural noun"], ["plural noun"])],
    )
    def test_parts_of_speech_stripped_not_normalised(self, *, raw: list[str], clean: list[str]) -> None:
        assert DefinitionsRequest(text="x", parts_of_speech=raw).parts_of_speech == clean

    @pytest.mark.parametrize("per_definition", [1, 3, MAX_EXAMPLES_PER_DEFINITION])
    def test_per_definition_in_range(self, per_definition: int) -> None:
        assert DefinitionsRequest(text="x", per_definition=per_definition).per_definition == per_definition

    def test_upper_bounds_accepted(self) -> None:
        request = DefinitionsRequest(
            text="x",
            hints=["h" * TEXT_MAX_LENGTH] * MAX_DEFINITIONS,
            parts_of_speech=["p" * 64] * MAX_PARTS_OF_SPEECH,
            per_definition=MAX_EXAMPLES_PER_DEFINITION,
        )
        assert (len(request.hints), len(request.parts_of_speech)) == (MAX_DEFINITIONS, MAX_PARTS_OF_SPEECH)

    @pytest.mark.parametrize(
        ("overrides", "match"),
        [
            ({"hints": ["h"] * (MAX_DEFINITIONS + 1)}, f"at most {MAX_DEFINITIONS} items"),
            ({"hints": [""]}, "at least 1 character"),
            ({"hints": ["   "]}, "at least 1 character"),
            ({"hints": ["fine", "\n\t"]}, "at least 1 character"),
            ({"hints": ["h" * (TEXT_MAX_LENGTH + 1)]}, f"at most {TEXT_MAX_LENGTH} characters"),
            ({"hints": "a petty criticism"}, "valid list"),
            ({"hints": None}, "valid list"),
            ({"hints": [7]}, "valid string"),
            ({"parts_of_speech": ["noun"] * (MAX_PARTS_OF_SPEECH + 1)}, f"at most {MAX_PARTS_OF_SPEECH} items"),
            ({"parts_of_speech": [""]}, "at least 1 character"),
            ({"parts_of_speech": ["  "]}, "at least 1 character"),
            ({"parts_of_speech": ["n" * 65]}, "at most 64 characters"),
            ({"parts_of_speech": "noun"}, "valid list"),
            ({"parts_of_speech": None}, "valid list"),
            ({"per_definition": 0}, "greater than or equal to 1"),
            (
                {"per_definition": MAX_EXAMPLES_PER_DEFINITION + 1},
                f"less than or equal to {MAX_EXAMPLES_PER_DEFINITION}",
            ),
            ({"per_definition": "two"}, "valid integer"),
            ({"per_definition": None}, "valid integer"),
        ],
    )
    def test_rejected(self, *, overrides: dict[str, t.Any], match: str) -> None:
        with pytest.raises(pydantic.ValidationError, match=match):
            DefinitionsRequest.model_validate({"text": "x", **overrides})

    def test_errors_point_at_the_offending_item(self) -> None:
        with pytest.raises(pydantic.ValidationError) as info:
            DefinitionsRequest(text="x", hints=["fine", "  "], parts_of_speech=["noun", ""])
        assert [e["loc"] for e in info.value.errors()] == [("hints", 1), ("parts_of_speech", 1)]
