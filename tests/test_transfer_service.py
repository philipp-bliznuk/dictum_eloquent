import datetime as dt
import typing as t

import pydantic
import pytest
import sqlalchemy as sa
import sqlalchemy.exc

from dictum.controllers.word import word_controller
from dictum.enums import ConflictPolicy
from dictum.exceptions import ValidationError
from dictum.models.word import Definition
from dictum.models.word import Example
from dictum.models.word import Lexeme
from dictum.models.word import Spelling
from dictum.models.word import Word
from dictum.schemas.word import WordCreate
from dictum.services.transfer import IMPORT_MAX_WORDS
from dictum.services.transfer import IMPORT_VERSION
from dictum.services.transfer import LEGACY_VERSION
from dictum.services.transfer import ExportFile
from dictum.services.transfer import Importer
from dictum.services.transfer import ImportFile
from dictum.services.transfer import LegacyImportFile
from dictum.services.transfer import LegacyWord
from dictum.services.transfer import WordImport
from dictum.services.transfer import to_payload
from dictum.services.transfer import upgrade
from tests.conftest import LOOSE_EXAMPLE
from tests.conftest import legacy_word_payload
from tests.conftest import lexeme_payload
from tests.conftest import word_payload

if t.TYPE_CHECKING:
    import sqlalchemy.ext.asyncio as sa_async
    from pytest_mock import MockerFixture

Json = dict[str, t.Any]

CREATED_AT: t.Final = dt.datetime(2020, 2, 2, 10, 0, tzinfo=dt.UTC)


def _import_file(*words: Json) -> ImportFile:
    return ImportFile.model_validate({"version": 2, "words": list(words)})


def _legacy_file(*words: Json) -> LegacyImportFile:
    return LegacyImportFile.model_validate({"version": 1, "words": list(words)})


def _definition(text: str, *examples: str) -> Json:
    return {"text": text, "examples": [{"text": e} for e in examples]}


async def _count(session: sa_async.AsyncSession, model: type[t.Any]) -> int:
    return await session.scalar(sa.select(sa.func.count()).select_from(model)) or 0


class TestSchemas:
    def test_versions(self) -> None:
        assert (IMPORT_VERSION, LEGACY_VERSION) == (2, 1)

    @pytest.mark.parametrize("version", [1, 3, "2", None])
    def test_import_file_rejects_other_version(self, version: object) -> None:
        with pytest.raises(pydantic.ValidationError, match="version"):
            ImportFile.model_validate({"version": version, "words": []})

    @pytest.mark.parametrize("version", [2, 0, "1", None])
    def test_legacy_import_file_rejects_other_version(self, version: object) -> None:
        with pytest.raises(pydantic.ValidationError, match="version"):
            LegacyImportFile.model_validate({"version": version, "words": []})

    def test_import_file_caps_word_count(self) -> None:
        words = [{"spellings": [{"text": f"w{i}"}]} for i in range(IMPORT_MAX_WORDS + 1)]
        with pytest.raises(pydantic.ValidationError, match=f"at most {IMPORT_MAX_WORDS} items"):
            ImportFile.model_validate({"version": 2, "words": words})

    def test_legacy_import_file_caps_word_count(self) -> None:
        words = [{"spellings": [{"text": f"w{i}"}], "part_of_speech": "noun"} for i in range(IMPORT_MAX_WORDS + 1)]
        with pytest.raises(pydantic.ValidationError, match=f"at most {IMPORT_MAX_WORDS} items"):
            LegacyImportFile.model_validate({"version": 1, "words": words})

    def test_export_file_defaults_version(self) -> None:
        assert ExportFile(words=[]).version == 2
        assert ExportFile(words=[]).model_dump() == {"version": 2, "words": []}

    def test_word_import_extends_word_create(self) -> None:
        assert issubclass(WordImport, WordCreate)
        word = WordImport.model_validate(word_payload())
        assert word.created_at is None
        assert (
            WordImport.model_validate({**word_payload(), "created_at": "2020-02-02T10:00:00Z"}).created_at == CREATED_AT
        )

    def test_word_import_validates_like_word_create(self) -> None:
        with pytest.raises(pydantic.ValidationError, match="duplicate part of speech: noun"):
            WordImport.model_validate(
                word_payload(lexemes=[lexeme_payload(part_of_speech="noun"), lexeme_payload(part_of_speech="NOUN")])
            )

    def test_legacy_word_valid(self) -> None:
        word = LegacyWord.model_validate(legacy_word_payload())
        assert word.part_of_speech == "verb"
        assert [e.definition_index for e in word.examples] == [0, None]
        assert word.definitions[0].source == "user"

    def test_legacy_word_defaults(self) -> None:
        word = LegacyWord.model_validate({"spellings": [{"text": "Cat"}], "part_of_speech": "noun"})
        assert (word.pronunciation, word.audio_path, word.notes) == (None, None, None)
        assert (word.status, word.definitions, word.examples) == ("learned", [], [])

    @pytest.mark.parametrize(
        ("overrides", "match"),
        [
            ({"examples": [{"text": "x", "definition_index": 1}]}, r"definition_index out of range: \[1\]"),
            (
                {"definitions": [], "examples": [{"text": "x", "definition_index": 0}]},
                r"definition_index out of range: \[0\]",
            ),
            (
                {"examples": [{"text": "x", "definition_index": 3}, {"text": "y", "definition_index": 1}]},
                r"definition_index out of range: \[3, 1\]",
            ),
            ({"examples": [{"text": "x", "definition_index": -1}]}, "greater than or equal to 0"),
            ({"part_of_speech": ""}, "at least 1"),
            ({"part_of_speech": "x" * 65}, "at most 64"),
            ({"spellings": []}, "at least 1"),
            ({"spellings": [{"text": "a / b"}]}, "must not contain ' / '"),
            ({"audio_path": "https://evil.example/x.mp3"}, "pattern"),
            ({"pronunciation": "x" * 256}, "at most 255"),
            ({"notes": "x" * 10_001}, "at most 10000"),
            ({"definitions": [{"text": ""}]}, "at least 1"),
            ({"examples": [{"text": "x" * 2001}]}, "at most 2000"),
            ({"status": "bogus"}, "status"),
        ],
    )
    def test_legacy_word_rejected(self, *, overrides: Json, match: str) -> None:
        with pytest.raises(pydantic.ValidationError, match=match):
            LegacyWord.model_validate(legacy_word_payload(**overrides))


class TestUpgrade:
    def test_single_lexeme_with_grammar_and_linked_examples(self) -> None:
        legacy = _legacy_file(
            legacy_word_payload(
                part_of_speech=" Verb / Noun ",
                audio_path="/audio/abando01.mp3",
                notes="n",
                status="inbox",
                spellings=[{"text": "Abandon"}, {"text": "abandonne", "kind": "variant"}],
                definitions=[{"text": "to give up"}, {"text": "freedom", "source": "mw"}],
                examples=[
                    {"text": "He *abandoned* ship.", "source": "ai", "definition_index": 0},
                    {"text": LOOSE_EXAMPLE, "source": "vault", "definition_index": None},
                    {"text": "with *abandon*", "definition_index": 1},
                    {"text": "Also first.", "definition_index": 0},
                    {"text": "Loose too."},
                ],
            )
        )
        upgraded = upgrade(legacy=legacy)
        assert isinstance(upgraded, ImportFile)
        assert upgraded.version == 2
        assert upgraded.model_dump(mode="json") == {
            "version": 2,
            "words": [
                {
                    "spellings": [{"text": "Abandon", "kind": "primary"}, {"text": "abandonne", "kind": "variant"}],
                    "notes": "n",
                    "status": "inbox",
                    "created_at": None,
                    "lexemes": [
                        {
                            "part_of_speech": "verb / noun",
                            "pronunciation": "uh-BAN-dun",
                            "audio_path": "/audio/abando01.mp3",
                            "definitions": [
                                {
                                    "text": "to give up",
                                    "source": "user",
                                    "examples": [
                                        {"text": "He *abandoned* ship.", "source": "ai"},
                                        {"text": "Also first.", "source": "user"},
                                    ],
                                },
                                {
                                    "text": "freedom",
                                    "source": "mw",
                                    "examples": [{"text": "with *abandon*", "source": "user"}],
                                },
                            ],
                        }
                    ],
                    "examples": [{"text": LOOSE_EXAMPLE, "source": "vault"}, {"text": "Loose too.", "source": "user"}],
                }
            ],
        }

    def test_word_without_definitions_or_examples(self) -> None:
        legacy = _legacy_file({"spellings": [{"text": "Zeal"}], "part_of_speech": "noun"})
        [word] = upgrade(legacy=legacy).words
        assert word.model_dump(mode="json") == {
            "spellings": [{"text": "Zeal", "kind": "primary"}],
            "notes": None,
            "status": "learned",
            "created_at": None,
            "lexemes": [{"part_of_speech": "noun", "pronunciation": None, "audio_path": None, "definitions": []}],
            "examples": [],
        }

    def test_keeps_word_order(self) -> None:
        legacy = _legacy_file(*(legacy_word_payload(spellings=[{"text": f"w{i}"}]) for i in range(3)))
        upgraded = upgrade(legacy=legacy)
        assert [w.spellings[0].text for w in upgraded.words] == ["w0", "w1", "w2"]

    def test_empty_file(self) -> None:
        assert upgrade(legacy=_legacy_file()).model_dump() == {"version": 2, "words": []}

    @pytest.mark.parametrize(
        ("overrides", "detail"),
        [
            (
                {"spellings": [{"text": "Abandon"}, {"text": "abandon"}]},
                "words.0.spellings: Value error, duplicate spelling: abandon",
            ),
            (
                {"part_of_speech": "   "},
                "words.0.lexemes.0.part_of_speech: Value error, part of speech must contain visible characters",
            ),
        ],
    )
    def test_failure_raises_422(self, *, overrides: Json, detail: str) -> None:
        second = legacy_word_payload(**{"spellings": [{"text": "B"}], **overrides})
        legacy = _legacy_file(legacy_word_payload(), second)
        with pytest.raises(ValidationError) as info:
            upgrade(legacy=legacy)
        assert info.value.status_code == 422
        assert info.value.detail == f"version 1 payload cannot be upgraded: {detail.replace('words.0', 'words.1')}"
        assert isinstance(info.value.__cause__, pydantic.ValidationError)

    def test_failure_lists_every_error(self) -> None:
        legacy = _legacy_file(
            legacy_word_payload(spellings=[{"text": "A"}, {"text": "a"}]),
            legacy_word_payload(spellings=[{"text": "B"}], part_of_speech=" "),
        )
        with pytest.raises(ValidationError) as info:
            upgrade(legacy=legacy)
        assert info.value.detail == (
            "version 1 payload cannot be upgraded: "
            "words.0.spellings: Value error, duplicate spelling: a; "
            "words.1.lexemes.0.part_of_speech: Value error, part of speech must contain visible characters"
        )


class TestToPayload:
    async def test_roundtrip(self, session: sa_async.AsyncSession) -> None:
        original = WordImport.model_validate(
            word_payload(
                notes="n",
                status="inbox",
                spellings=[{"text": "Color"}, {"text": "Colour", "kind": "variant"}],
                lexemes=[
                    lexeme_payload(
                        part_of_speech="noun",
                        definitions=[
                            _definition("d1"),
                            {"text": "d2", "source": "mw", "examples": [{"text": "e-d2", "source": "ai"}]},
                        ],
                    ),
                    lexeme_payload(
                        part_of_speech="verb",
                        pronunciation=None,
                        audio_path="/audio/color01.mp3",
                        definitions=[_definition("d3", "e-d3")],
                    ),
                ],
                examples=[{"text": "e-none"}, {"text": "e-vault", "source": "vault"}],
            )
        )
        word = await word_controller.create_from_payload(session=session, payload=original)
        payload = to_payload(word=word)
        assert payload.model_dump(exclude={"created_at"}) == original.model_dump(exclude={"created_at"})
        assert payload.created_at == word.created_at
        assert payload.created_at is not None
        assert payload.created_at.tzinfo is not None
        dumped = payload.model_dump()
        assert "position" not in dumped["lexemes"][0]
        assert "position" not in dumped["lexemes"][0]["definitions"][0]
        assert "position" not in dumped["lexemes"][0]["definitions"][1]["examples"][0]
        assert "position" not in dumped["examples"][0]
        assert "id" not in dumped["spellings"][0]

    async def test_default_payload(self, session: sa_async.AsyncSession) -> None:
        word = await word_controller.create_from_payload(
            session=session, payload=WordCreate.model_validate(word_payload())
        )
        dumped = to_payload(word=word).model_dump(mode="json")
        assert dumped == {**word_payload(), "created_at": word.created_at.isoformat().replace("+00:00", "Z")}

    def test_nested_examples_only_under_their_definition(self) -> None:
        word = Word(created_at=CREATED_AT)
        word.spellings = [Spelling(text="Cat")]
        definition = Definition(id=1, text="d", position=0)
        nested = Example(id=1, text="nested", position=0, definition_id=1)
        loose = Example(id=2, text="loose", position=1, definition_id=None)
        definition.examples = [nested]
        lexeme = Lexeme(part_of_speech="noun", position=0)
        lexeme.definitions = [definition]
        word.lexemes = [lexeme]
        word.examples = [nested, loose]
        payload = to_payload(word=word)
        assert [e.text for e in payload.lexemes[0].definitions[0].examples] == ["nested"]
        assert [e.text for e in payload.examples] == ["loose"]
        assert payload.created_at == CREATED_AT

    def test_word_without_children(self) -> None:
        word = Word(created_at=CREATED_AT)
        word.spellings = [Spelling(text="Cat")]
        word.lexemes = []
        word.examples = []
        assert to_payload(word=word).model_dump(mode="json") == {
            "spellings": [{"text": "Cat", "kind": "primary"}],
            "notes": None,
            "status": "learned",
            "created_at": "2020-02-02T10:00:00Z",
            "lexemes": [],
            "examples": [],
        }


class TestImporter:
    async def test_creates_all(self, session: sa_async.AsyncSession) -> None:
        data = _import_file(word_payload(), word_payload(spellings=[{"text": "Betray"}]))
        report = await Importer(session=session, policy=ConflictPolicy.SKIP).run(data=data)
        assert report.model_dump() == {"created": 2, "replaced": 0, "skipped": 0, "failures": []}
        assert await _count(session, Word) == 2
        assert await _count(session, Lexeme) == 2
        assert await _count(session, Definition) == 2
        assert await _count(session, Example) == 4

    async def test_nested_structure_stored(self, session: sa_async.AsyncSession) -> None:
        data = _import_file(
            word_payload(
                lexemes=[
                    lexeme_payload(part_of_speech="noun", definitions=[_definition("a", "a1")]),
                    lexeme_payload(part_of_speech="verb", definitions=[_definition("b", "b1")]),
                ],
                examples=[{"text": "l"}],
            )
        )
        await Importer(session=session, policy=ConflictPolicy.FAIL).run(data=data)
        word = await word_controller.find_by_spelling(session=session, text="abandon")
        assert word is not None
        assert [x.part_of_speech for x in word.lexemes] == ["noun", "verb"]
        assert [(e.text, e.position, e.definition_id is None) for e in word.examples] == [
            ("a1", 0, False),
            ("b1", 1, False),
            ("l", 2, True),
        ]

    async def test_honours_created_at(self, session: sa_async.AsyncSession) -> None:
        data = _import_file({**word_payload(), "created_at": "2020-02-02T10:00:00Z"})
        await Importer(session=session, policy=ConflictPolicy.FAIL).run(data=data)
        word = await word_controller.find_by_spelling(session=session, text="abandon")
        assert word is not None
        assert word.created_at == CREATED_AT

    async def test_created_at_defaults_to_now(self, session: sa_async.AsyncSession) -> None:
        before = dt.datetime.now(tz=dt.UTC)
        await Importer(session=session, policy=ConflictPolicy.FAIL).run(data=_import_file(word_payload()))
        word = await word_controller.find_by_spelling(session=session, text="abandon")
        assert word is not None
        assert word.created_at.tzinfo is not None
        assert before <= word.created_at <= dt.datetime.now(tz=dt.UTC)

    async def test_empty_file(self, session: sa_async.AsyncSession) -> None:
        report = await Importer(session=session, policy=ConflictPolicy.FAIL).run(data=_import_file())
        assert report.created == 0

    async def test_skip_conflict(self, session: sa_async.AsyncSession) -> None:
        existing = await word_controller.create_from_payload(
            session=session, payload=WordCreate.model_validate(word_payload())
        )
        data = _import_file(word_payload(spellings=[{"text": "abandon"}], notes="new"))
        report = await Importer(session=session, policy=ConflictPolicy.SKIP).run(data=data)
        assert report.skipped == 1
        assert report.created == 0
        assert existing.notes is None

    async def test_replace_conflict(self, session: sa_async.AsyncSession) -> None:
        existing = await word_controller.create_from_payload(
            session=session, payload=WordCreate.model_validate(word_payload())
        )
        created_at = existing.created_at
        data = _import_file({
            **word_payload(
                spellings=[{"text": "abandon"}],
                notes="new",
                lexemes=[lexeme_payload(part_of_speech="noun", definitions=[_definition("x"), _definition("y", "y1")])],
                examples=[],
            ),
            "created_at": "2020-02-02T10:00:00Z",
        })
        report = await Importer(session=session, policy=ConflictPolicy.REPLACE).run(data=data)
        assert report.replaced == 1
        assert report.created == 0
        assert existing.notes == "new"
        assert existing.created_at == created_at
        assert [x.part_of_speech for x in existing.lexemes] == ["noun"]
        assert [d.text for d in existing.lexemes[0].definitions] == ["x", "y"]
        assert [e.text for e in existing.examples] == ["y1"]
        assert await _count(session, Word) == 1
        assert await _count(session, Definition) == 2
        assert await _count(session, Example) == 1

    async def test_fail_conflict(self, session: sa_async.AsyncSession) -> None:
        await word_controller.create_from_payload(session=session, payload=WordCreate.model_validate(word_payload()))
        data = _import_file(word_payload(spellings=[{"text": "abandon"}]))
        with pytest.raises(ValidationError, match="already belongs to word") as info:
            await Importer(session=session, policy=ConflictPolicy.FAIL).run(data=data)
        assert info.value.status_code == 422

    async def test_conflict_within_batch(self, session: sa_async.AsyncSession) -> None:
        data = _import_file(word_payload(), word_payload(spellings=[{"text": "ABANDON"}]))
        report = await Importer(session=session, policy=ConflictPolicy.SKIP).run(data=data)
        assert (report.created, report.skipped) == (1, 1)

    async def test_replace_within_batch(self, session: sa_async.AsyncSession) -> None:
        data = _import_file(word_payload(notes="first"), word_payload(spellings=[{"text": "ABANDON"}], notes="second"))
        report = await Importer(session=session, policy=ConflictPolicy.REPLACE).run(data=data)
        assert (report.created, report.replaced) == (1, 1)
        word = await word_controller.find_by_spelling(session=session, text="abandon")
        assert word is not None
        assert word.notes == "second"
        assert [s.text for s in word.spellings] == ["ABANDON"]

    async def test_integrity_error_recorded(self, session: sa_async.AsyncSession, mocker: MockerFixture) -> None:
        real = word_controller.create_from_payload
        calls = 0

        async def flaky(*, session: sa_async.AsyncSession, payload: WordCreate) -> Word:
            nonlocal calls
            calls += 1
            if calls == 1:
                stmt = "INSERT"
                raise sqlalchemy.exc.IntegrityError(stmt, {}, Exception("UNIQUE constraint failed"))
            return await real(session=session, payload=payload)

        mocker.patch.object(word_controller, "create_from_payload", side_effect=flaky)
        data = _import_file(word_payload(), word_payload(spellings=[{"text": "Betray"}]))
        report = await Importer(session=session, policy=ConflictPolicy.SKIP).run(data=data)
        assert report.created == 1
        assert len(report.failures) == 1
        failure = report.failures[0]
        assert failure.index == 0
        assert failure.spellings == ["Abandon"]
        assert "UNIQUE constraint failed" in failure.error
        assert await _count(session, Word) == 1
