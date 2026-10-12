import io
import typing as t
import zipfile

import pytest

from dictum.models.word import Definition
from dictum.models.word import Example
from dictum.models.word import Lexeme
from dictum.models.word import Spelling
from dictum.models.word import Word
from dictum.services import export_md

type Senses = list[tuple[str, list[str]]]


def _word(*, spellings: list[str], **fields: t.Any) -> Word:
    word = Word(**fields)
    word.spellings = [Spelling(text=s, kind="primary" if i == 0 else "variant") for i, s in enumerate(spellings)]
    word.lexemes = []
    word.examples = []
    return word


def _lexeme(*, word: Word, part_of_speech: str, pronunciation: str | None = None, senses: Senses = ()) -> Lexeme:
    """Attach a lexeme; nested examples are also appended to `word.examples` with a `definition_id`, as stored."""
    lexeme = Lexeme(part_of_speech=part_of_speech, pronunciation=pronunciation, position=len(word.lexemes))
    lexeme.definitions = []
    for text, examples in senses:
        definition = Definition(id=len(word.examples) + 100, text=text, position=len(lexeme.definitions))
        definition.examples = []
        for example in examples:
            nested = Example(text=example, position=len(word.examples), definition_id=definition.id)
            definition.examples.append(nested)
            word.examples.append(nested)
        lexeme.definitions.append(definition)
    word.lexemes.append(lexeme)
    return lexeme


def _loose(*, word: Word, texts: list[str]) -> None:
    word.examples.extend(Example(text=t_, position=len(word.examples) + i) for i, t_ in enumerate(texts))


class TestConstants:
    def test_loose_examples_heading(self) -> None:
        assert export_md.LOOSE_EXAMPLES_HEADING == "###### examples"
        assert export_md.INBOX_FILE == "_Inbox.md"
        assert export_md.OTHER_FILE == "#.md"


class TestRenderWord:
    def test_minimal(self) -> None:
        word = _word(spellings=["Abandon"])
        assert export_md.render_word(word=word) == "##### Abandon\n"

    def test_variants_joined_without_part_of_speech(self) -> None:
        word = _word(spellings=["Color", "Colour"])
        _lexeme(word=word, part_of_speech="noun")
        assert export_md.render_word(word=word) == "##### Color / Colour\n###### noun\n"

    def test_full_entry(self) -> None:
        word = _word(spellings=["Abandon"], notes="see also: forsake")
        _lexeme(
            word=word,
            part_of_speech="verb",
            pronunciation="uh-BAN-dun",
            senses=[("to give up", ["ex for d1", "second for d1"]), ("to leave", ["ex for d2"])],
        )
        _lexeme(word=word, part_of_speech="noun", senses=[("freedom", [])])
        _loose(word=word, texts=["orphan"])
        assert export_md.render_word(word=word) == (
            "##### Abandon\n"
            "###### verb\n"
            "*uh-BAN-dun*\n"
            "- **to give up**\n"
            "\tex for d1\n"
            "\tsecond for d1\n"
            "- **to leave**\n"
            "\tex for d2\n"
            "###### noun\n"
            "- **freedom**\n"
            "###### examples\n"
            "\torphan\n"
            "> see also: forsake\n"
        )

    def test_lexeme_without_pronunciation_has_no_italic_line(self) -> None:
        word = _word(spellings=["Abandon"])
        _lexeme(word=word, part_of_speech="verb", pronunciation=None, senses=[("to give up", [])])
        _lexeme(word=word, part_of_speech="noun", pronunciation="", senses=[])
        assert export_md.render_word(word=word) == "##### Abandon\n###### verb\n- **to give up**\n###### noun\n"

    def test_nested_examples_not_repeated_as_loose(self) -> None:
        word = _word(spellings=["Abandon"])
        _lexeme(word=word, part_of_speech="verb", senses=[("to give up", ["nested"])])
        assert [e.definition_id for e in word.examples] == [100]
        rendered = export_md.render_word(word=word)
        assert rendered == "##### Abandon\n###### verb\n- **to give up**\n\tnested\n"
        assert export_md.LOOSE_EXAMPLES_HEADING not in rendered

    def test_loose_examples_without_lexemes(self) -> None:
        word = _word(spellings=["Abandon"])
        _loose(word=word, texts=["one", "two"])
        assert export_md.render_word(word=word) == "##### Abandon\n###### examples\n\tone\n\ttwo\n"

    def test_loose_examples_follow_every_lexeme(self) -> None:
        word = _word(spellings=["Abandon"])
        _loose(word=word, texts=["loose"])
        _lexeme(word=word, part_of_speech="verb", senses=[("d", ["nested"])])
        assert export_md.render_word(word=word) == (
            "##### Abandon\n###### verb\n- **d**\n\tnested\n###### examples\n\tloose\n"
        )

    def test_notes_as_blockquote_lines(self) -> None:
        word = _word(spellings=["Abandon"], notes="first line\nsecond line\n\nfourth")
        assert export_md.render_word(word=word) == "##### Abandon\n> first line\n> second line\n> \n> fourth\n"

    @pytest.mark.parametrize("notes", [None, ""])
    def test_empty_notes_omitted(self, notes: str | None) -> None:
        word = _word(spellings=["Abandon"], notes=notes)
        assert export_md.render_word(word=word) == "##### Abandon\n"


class TestRenderVault:
    def test_buckets_and_sorting(self) -> None:
        nameless = Word()
        nameless.spellings = []
        nameless.lexemes = []
        nameless.examples = []
        words = [
            _word(spellings=["zebra"]),
            _word(spellings=["Apple"]),
            _word(spellings=["aardvark"]),
            _word(spellings=["Inboxed"], status="inbox"),
            _word(spellings=["1st"]),
            nameless,
        ]
        files = export_md.render_vault(words=words)
        assert set(files) == {"A.md", "Z.md", "_Inbox.md", "#.md"}
        assert files["A.md"] == "##### aardvark\n\n##### Apple\n"
        assert files["Z.md"] == "##### zebra\n"
        assert files["_Inbox.md"] == "##### Inboxed\n"
        assert files["#.md"] == "##### \n\n##### 1st\n"

    def test_empty(self) -> None:
        assert export_md.render_vault(words=[]) == {}

    @pytest.mark.parametrize(
        ("spelling", "status", "expected"),
        [
            ("apple", "learned", "A.md"),
            ("Apple", "learned", "A.md"),
            ("apple", "inbox", "_Inbox.md"),
            ("42", "learned", "#.md"),
            ("#tag", "learned", "T.md"),
            ("ébène", "learned", "E.md"),
            ("'tis", "learned", "T.md"),
            ("¡ay!", "learned", "A.md"),
            ("By my troth!", "learned", "B.md"),
            ("in extenso", "learned", "I.md"),
            ("日本", "learned", "#.md"),
        ],
    )
    def test_bucket(self, *, spelling: str, status: str, expected: str) -> None:
        files = export_md.render_vault(words=[_word(spellings=[spelling], status=status)])
        assert list(files) == [expected]

    def test_inbox_bucket_keeps_full_entry(self) -> None:
        word = _word(spellings=["zeal"], status="inbox", notes="later")
        _lexeme(word=word, part_of_speech="noun", pronunciation="zeel", senses=[("fervour", ["Such *zeal*!"])])
        files = export_md.render_vault(words=[word])
        assert files == {"_Inbox.md": "##### zeal\n###### noun\n*zeel*\n- **fervour**\n\tSuch *zeal*!\n> later\n"}

    def test_phrase_headline_roundtrips(self) -> None:
        word = _word(spellings=["put paid to", "put paid to something"])
        _lexeme(word=word, part_of_speech="verb / phrase")
        headline, section = export_md.render_word(word=word).splitlines()
        assert headline == "##### put paid to / put paid to something"
        assert headline.removeprefix("##### ").split(" / ") == ["put paid to", "put paid to something"]
        assert section == "###### verb / phrase"


class TestZipVault:
    def test_roundtrip_sorted(self) -> None:
        files = {"Z.md": "zed\n", "A.md": "ay\n", "_Inbox.md": "in\n"}
        data = export_md.zip_vault(files=files)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert zf.namelist() == ["A.md", "Z.md", "_Inbox.md"]
            assert zf.read("Z.md").decode() == "zed\n"
            assert zf.testzip() is None

    def test_empty(self) -> None:
        with zipfile.ZipFile(io.BytesIO(export_md.zip_vault(files={}))) as zf:
            assert zf.namelist() == []
