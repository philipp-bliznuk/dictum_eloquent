import datetime as dt
import typing as t

import pytest
import sqlalchemy as sa
import sqlalchemy.exc

from dictum.controllers.base import Controller
from dictum.controllers.word import headword
from dictum.controllers.word import word_controller
from dictum.exceptions import NotFoundError
from dictum.exceptions import SpellingConflictError
from dictum.exceptions import WordNotFoundError
from dictum.models.word import Definition
from dictum.models.word import Example
from dictum.models.word import Lexeme
from dictum.models.word import Spelling
from dictum.models.word import Word
from dictum.schemas.word import WordCreate
from dictum.services.transfer import WordImport
from tests.conftest import LOOSE_EXAMPLE
from tests.conftest import NESTED_EXAMPLE
from tests.conftest import lexeme_payload
from tests.conftest import word_payload

if t.TYPE_CHECKING:
    import sqlalchemy.ext.asyncio as sa_async


async def _create(session: sa_async.AsyncSession, **overrides: t.Any) -> Word:
    return await word_controller.create_from_payload(
        session=session,
        payload=WordCreate.model_validate(word_payload(**overrides)),
    )


async def _count(session: sa_async.AsyncSession, model: type[t.Any]) -> int:
    return await session.scalar(sa.select(sa.func.count()).select_from(model)) or 0


def _definition(text: str, *examples: str) -> dict[str, t.Any]:
    return {"text": text, "examples": [{"text": e} for e in examples]}


async def _flush_duplicate_lexemes(*, session: sa_async.AsyncSession, word: Word) -> None:
    """Attach two `noun` lexemes inside a savepoint so the unique constraint fires without poisoning the session."""
    async with session.begin_nested():
        word.lexemes = [Lexeme(part_of_speech="noun", position=0), Lexeme(part_of_speech="noun", position=1)]
        await session.flush()


class TestBaseController:
    async def test_get_missing_raises_generic_not_found(self, session: sa_async.AsyncSession) -> None:
        controller = Controller(model=Word)
        with pytest.raises(NotFoundError) as info:
            await controller.get(session=session, object_id=999)
        assert type(info.value) is NotFoundError

    async def test_create_and_get(self, session: sa_async.AsyncSession) -> None:
        controller = Controller(model=Word)
        word = await controller.create(session=session, obj=Word(notes="n"))
        assert word.id is not None
        assert await controller.get(session=session, object_id=word.id) is word

    async def test_list_paging_and_filter(self, session: sa_async.AsyncSession) -> None:
        controller = Controller(model=Word)
        for i in range(5):
            status = "inbox" if i % 2 else "learned"
            await controller.create(session=session, obj=Word(notes=f"p{i}", status=status))
        rows, total = await controller.list(session=session, offset=1, limit=2)
        assert total == 5
        assert [w.notes for w in rows] == ["p3", "p2"]
        rows, total = await controller.list(session=session, where=[Word.status == "inbox"])
        assert total == 2
        assert [w.notes for w in rows] == ["p3", "p1"]

    async def test_get_many(self, session: sa_async.AsyncSession) -> None:
        controller = Controller(model=Word)
        words = [await controller.create(session=session, obj=Word(notes=f"p{i}")) for i in range(3)]
        ids = [w.id or 0 for w in words]
        found = await controller.get_many(session=session, object_ids=[ids[2], ids[0], 999])
        assert set(found) == {ids[0], ids[2]}
        assert found[ids[2]] is words[2]
        assert await controller.get_many(session=session, object_ids=[]) == {}

    async def test_list_empty(self, session: sa_async.AsyncSession) -> None:
        rows, total = await Controller(model=Word).list(session=session)
        assert rows == []
        assert total == 0

    async def test_update(self, session: sa_async.AsyncSession) -> None:
        controller = Controller(model=Word)
        word = await controller.create(session=session, obj=Word())
        updated = await controller.update(session=session, obj=word, values={"notes": "n", "status": "inbox"})
        assert updated.notes == "n"
        assert updated.status == "inbox"

    async def test_delete_and_count(self, session: sa_async.AsyncSession) -> None:
        controller = Controller(model=Word)
        word = await controller.create(session=session, obj=Word())
        assert await controller.count(session=session) == 1
        await controller.delete(session=session, obj=word)
        assert await controller.count(session=session) == 0


class TestWordController:
    async def test_create_from_payload_links_children(self, session: sa_async.AsyncSession) -> None:
        word = await _create(session)
        assert word.id is not None
        assert [s.text for s in word.spellings] == ["Abandon"]
        [lexeme] = word.lexemes
        assert lexeme.word is word
        assert lexeme.part_of_speech == "verb"
        assert lexeme.pronunciation == "uh-BAN-dun"
        [definition] = lexeme.definitions
        assert definition.text == "to give up"
        assert definition.lexeme is lexeme
        nested, loose = word.examples
        assert nested.text == NESTED_EXAMPLE
        assert nested.definition is definition
        assert nested.definition_id == definition.id
        assert definition.examples == [nested]
        assert loose.text == LOOSE_EXAMPLE
        assert loose.definition is None
        assert loose.definition_id is None
        assert nested.word is loose.word is word

    async def test_create_assigns_positions_with_one_example_counter(self, session: sa_async.AsyncSession) -> None:
        word = await _create(
            session,
            lexemes=[
                lexeme_payload(
                    part_of_speech="noun", definitions=[_definition("a", "a1", "a2"), _definition("b", "b1")]
                ),
                lexeme_payload(part_of_speech="verb", definitions=[_definition("c", "c1")]),
                lexeme_payload(part_of_speech="adjective", definitions=[]),
            ],
            examples=[{"text": "l1"}, {"text": "l2"}],
        )
        assert [(x.part_of_speech, x.position) for x in word.lexemes] == [("noun", 0), ("verb", 1), ("adjective", 2)]
        assert [[d.position for d in x.definitions] for x in word.lexemes] == [[0, 1], [0], []]
        assert [(e.text, e.position) for e in word.examples] == [
            ("a1", 0),
            ("a2", 1),
            ("b1", 2),
            ("c1", 3),
            ("l1", 4),
            ("l2", 5),
        ]
        noun, verb, _ = word.lexemes
        assert [[e.text for e in d.examples] for d in noun.definitions] == [["a1", "a2"], ["b1"]]
        assert [[e.text for e in d.examples] for d in verb.definitions] == [["c1"]]
        assert [e.text for e in word.examples if e.definition_id is None] == ["l1", "l2"]

    async def test_create_without_children(self, session: sa_async.AsyncSession) -> None:
        word = await _create(session, lexemes=[], examples=[])
        assert word.lexemes == []
        assert word.examples == []
        assert await _count(session, Lexeme) == 0

    async def test_create_honours_created_at_of_import_payload(self, session: sa_async.AsyncSession) -> None:
        payload = WordImport.model_validate({**word_payload(), "created_at": "2000-01-01T00:00:00Z"})
        word = await word_controller.create_from_payload(session=session, payload=payload)
        assert word.created_at == dt.datetime(2000, 1, 1, tzinfo=dt.UTC)
        assert word.updated_at >= dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

    async def test_create_without_created_at_uses_now(self, session: sa_async.AsyncSession) -> None:
        payload = WordImport.model_validate(word_payload())
        assert payload.created_at is None
        word = await word_controller.create_from_payload(session=session, payload=payload)
        assert word.created_at.tzinfo is not None
        assert word.created_at >= dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

    async def test_fresh_definition_has_initialised_examples(self, session: sa_async.AsyncSession) -> None:
        word = await _create(session, lexemes=[lexeme_payload(definitions=[_definition("bare")])])
        assert word.lexemes[0].definitions[0].examples == []

    async def test_get_missing(self, session: sa_async.AsyncSession) -> None:
        with pytest.raises(WordNotFoundError):
            await word_controller.get(session=session, object_id=1)

    async def test_spelling_key_derived_on_insert_and_update(self, session: sa_async.AsyncSession) -> None:
        word = await _create(session, spellings=[{"text": "  Éclair  "}])
        spelling = word.spellings[0]
        assert spelling.text == "Éclair"
        assert spelling.text_key == "éclair"
        spelling.text = "ÉCLAIRS"
        await session.flush()
        assert spelling.text_key == "éclairs"

    @pytest.mark.parametrize(
        ("stored", "lookup"),
        [
            ("Éclair", "éclair"),
            ("éclair", "ÉCLAIR"),
            ("Straße", "STRASSE"),
            ("ﬁnal", "FINAL"),
        ],
    )
    async def test_find_by_spelling_unicode_folding(
        self, session: sa_async.AsyncSession, *, stored: str, lookup: str
    ) -> None:
        word = await _create(session, spellings=[{"text": stored}])
        found = await word_controller.find_by_spelling(session=session, text=lookup)
        assert found is not None
        assert found.id == word.id
        with pytest.raises(SpellingConflictError):
            await _create(session, spellings=[{"text": lookup}])

    @pytest.mark.parametrize("text", ["Abandon", "abandon", "ABANDON"])
    async def test_find_by_spelling_case_insensitive(self, session: sa_async.AsyncSession, text: str) -> None:
        word = await _create(session)
        found = await word_controller.find_by_spelling(session=session, text=text)
        assert found is not None
        assert found.id == word.id

    async def test_find_by_spelling_missing(self, session: sa_async.AsyncSession) -> None:
        assert await word_controller.find_by_spelling(session=session, text="nope") is None

    async def test_create_conflict(self, session: sa_async.AsyncSession) -> None:
        word = await _create(session)
        with pytest.raises(SpellingConflictError) as info:
            await _create(session, spellings=[{"text": "ABANDON"}])
        assert info.value.existing_word_id == word.id
        assert info.value.spelling == "Abandon"

    async def test_ensure_spellings_free_excludes_own_word(self, session: sa_async.AsyncSession) -> None:
        word = await _create(session)
        assert word.id is not None
        await word_controller.ensure_spellings_free(session=session, texts=["abandon"], exclude_word_id=word.id)
        with pytest.raises(SpellingConflictError):
            await word_controller.ensure_spellings_free(session=session, texts=["abandon"], exclude_word_id=word.id + 1)

    async def test_duplicate_part_of_speech_rejected_by_database(self, session: sa_async.AsyncSession) -> None:
        word = await _create(session, lexemes=[])
        with pytest.raises(sqlalchemy.exc.IntegrityError, match=r"UNIQUE constraint failed: lexeme\.word_id"):
            await _flush_duplicate_lexemes(session=session, word=word)
        assert await _count(session, Lexeme) == 0


class TestWordReplace:
    async def test_replace_from_payload(self, session: sa_async.AsyncSession) -> None:
        word = await _create(session)
        payload = WordCreate.model_validate(
            word_payload(
                notes="note",
                status="inbox",
                spellings=[{"text": "abandon"}, {"text": "Abandonment", "kind": "variant"}],
                lexemes=[
                    lexeme_payload(part_of_speech="noun", pronunciation=None, definitions=[_definition("d1")]),
                    lexeme_payload(part_of_speech="verb", definitions=[_definition("d2", "e2")]),
                ],
                examples=[{"text": "loose"}],
            )
        )
        replaced = await word_controller.replace_from_payload(session=session, word=word, payload=payload)
        assert replaced is word
        assert replaced.notes == "note"
        assert replaced.status == "inbox"
        assert [s.text for s in replaced.spellings] == ["abandon", "Abandonment"]
        assert [(x.part_of_speech, x.pronunciation) for x in replaced.lexemes] == [
            ("noun", None),
            ("verb", "uh-BAN-dun"),
        ]
        assert [[d.text for d in x.definitions] for x in replaced.lexemes] == [["d1"], ["d2"]]
        nested, loose = replaced.examples
        assert nested.definition is replaced.lexemes[1].definitions[0]
        assert (nested.text, nested.position) == ("e2", 0)
        assert (loose.text, loose.position, loose.definition_id) == ("loose", 1, None)
        assert await _count(session, Lexeme) == 2
        assert await _count(session, Definition) == 2
        assert await _count(session, Spelling) == 2
        assert await _count(session, Example) == 2

    async def test_replace_keeps_created_at_and_id(self, session: sa_async.AsyncSession) -> None:
        word = await _create(session)
        created_at = word.created_at
        word_id = word.id
        payload = WordCreate.model_validate(word_payload(notes="later"))
        await word_controller.replace_from_payload(session=session, word=word, payload=payload)
        assert word.id == word_id
        assert word.created_at == created_at
        assert word.notes == "later"

    async def test_replace_ignores_created_at_in_payload(self, session: sa_async.AsyncSession) -> None:
        word = await _create(session)
        original = word.created_at
        payload = WordImport.model_validate({**word_payload(), "created_at": "2000-01-01T00:00:00Z"})
        assert payload.created_at == dt.datetime(2000, 1, 1, tzinfo=dt.UTC)
        await word_controller.replace_from_payload(session=session, word=word, payload=payload)
        assert word.created_at == original

    async def test_replace_with_no_children(self, session: sa_async.AsyncSession) -> None:
        word = await _create(session)
        payload = WordCreate.model_validate(word_payload(lexemes=[], examples=[]))
        await word_controller.replace_from_payload(session=session, word=word, payload=payload)
        assert word.lexemes == []
        assert word.examples == []
        assert await _count(session, Lexeme) == 0
        assert await _count(session, Definition) == 0
        assert await _count(session, Example) == 0

    async def test_replace_conflict_with_other_word(self, session: sa_async.AsyncSession) -> None:
        first = await _create(session)
        second = await _create(session, spellings=[{"text": "Betray"}])
        payload = WordCreate.model_validate(word_payload(spellings=[{"text": "betray"}]))
        with pytest.raises(SpellingConflictError) as info:
            await word_controller.replace_from_payload(session=session, word=first, payload=payload)
        assert info.value.existing_word_id == second.id


class TestWordStats:
    async def test_stats(self, session: sa_async.AsyncSession) -> None:
        await _create(session)
        await _create(session, status="inbox", spellings=[{"text": "Betray"}], lexemes=[], examples=[])
        stats = await word_controller.stats(session=session)
        assert stats.model_dump() == {"words": 2, "inbox": 1, "definitions": 1, "examples": 2}

    async def test_stats_count_nested_and_loose_examples(self, session: sa_async.AsyncSession) -> None:
        await _create(
            session,
            lexemes=[
                lexeme_payload(part_of_speech="noun", definitions=[_definition("a", "a1", "a2"), _definition("b")]),
                lexeme_payload(part_of_speech="verb", definitions=[_definition("c", "c1")]),
            ],
            examples=[{"text": "l1"}],
        )
        stats = await word_controller.stats(session=session)
        assert stats.model_dump() == {"words": 1, "inbox": 0, "definitions": 3, "examples": 4}

    async def test_stats_empty(self, session: sa_async.AsyncSession) -> None:
        stats = await word_controller.stats(session=session)
        assert stats.model_dump() == {"words": 0, "inbox": 0, "definitions": 0, "examples": 0}


class TestWordIndexAndDelete:
    async def test_spelling_index_rows(self, session: sa_async.AsyncSession) -> None:
        word = await _create(session, spellings=[{"text": "Color"}, {"text": "Colour", "kind": "variant"}])
        rows = await word_controller.spelling_index_rows(session=session)
        assert rows == [("Color", word.id), ("Colour", word.id)]

    async def test_definition_index_rows(self, session: sa_async.AsyncSession) -> None:
        lexemes = [
            lexeme_payload(definitions=[_definition("to give up"), _definition("to leave behind")]),
            lexeme_payload(part_of_speech="noun", definitions=[_definition("freedom from restraint")]),
        ]
        abandon = await _create(session, lexemes=lexemes)
        zeal = await _create(session, spellings=[{"text": "Zeal"}], lexemes=[lexeme_payload(part_of_speech="noun")])
        await _create(session, spellings=[{"text": "Bare"}], lexemes=[])
        rows = await word_controller.definition_index_rows(session=session)
        assert sorted(rows) == sorted([
            ("to give up", abandon.id),
            ("to leave behind", abandon.id),
            ("freedom from restraint", abandon.id),
            ("to give up", zeal.id),
        ])

    async def test_definition_index_rows_empty(self, session: sa_async.AsyncSession) -> None:
        await _create(session, lexemes=[])
        assert await word_controller.definition_index_rows(session=session) == []

    async def test_delete_cascades(self, session: sa_async.AsyncSession) -> None:
        word = await _create(session)
        await word_controller.delete(session=session, obj=word)
        for model in (Word, Spelling, Lexeme, Definition, Example):
            assert await _count(session, model) == 0

    async def test_example_outlives_its_definition_as_loose(self, session: sa_async.AsyncSession) -> None:
        word = await _create(session)
        lexeme = word.lexemes[0]
        lexeme.definitions.clear()
        await session.flush()
        assert await _count(session, Definition) == 0
        assert await _count(session, Example) == 2
        assert [e.definition_id for e in word.examples] == [None, None]


class TestHeadword:
    def test_primary_spelling(self) -> None:
        word = Word(spellings=[Spelling(text="Colour", kind="variant"), Spelling(text="Color")])
        assert headword(word=word) == "Color"

    def test_first_primary_wins(self) -> None:
        spellings = [Spelling(text="A"), Spelling(text="B")]
        assert headword(word=Word(spellings=spellings)) == "A"

    def test_falls_back_to_first_spelling(self) -> None:
        spellings = [Spelling(text="troth", kind="variant"), Spelling(text="trothe", kind="variant")]
        assert headword(word=Word(spellings=spellings)) == "troth"

    async def test_persisted_word(self, session: sa_async.AsyncSession) -> None:
        word = await _create(session, spellings=[{"text": "aggrandise", "kind": "variant"}, {"text": "aggrandize"}])
        assert headword(word=word) == "aggrandize"
