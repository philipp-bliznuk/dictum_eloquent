import typing as t

import pytest

from dictum.services import text as text_service
from dictum.services.text import is_clean_sentence
from dictum.services.text import normalize_part_of_speech

ELLIPSIS: t.Final = "\u2026"
"""HORIZONTAL ELLIPSIS; escaped so it is not mistaken for three periods."""
LDQUO: t.Final = "\u201c"
RDQUO: t.Final = "\u201d"
LSQUO: t.Final = "\u2018"
RSQUO: t.Final = "\u2019"
"""Curly quotes; escaped because linters flag them as confusable with ASCII quotes."""
EDGE_MARKUP: t.Final = f" *_\"'{LDQUO}{RDQUO}{LSQUO}{RSQUO}()\u00a1\u00bf"
"""Every character the heuristic strips from both ends before judging the sentence."""
LIMIT: t.Final = text_service.MAX_EXAMPLE_LENGTH


class TestConstants:
    def test_terminal_punctuation(self) -> None:
        assert text_service.TERMINAL_PUNCTUATION == (".", "!", "?")

    def test_max_example_length(self) -> None:
        assert text_service.MAX_EXAMPLE_LENGTH == 300


class TestNormalizePartOfSpeech:
    @pytest.mark.parametrize(
        ("raw", "clean"),
        [
            ("noun", "noun"),
            ("Noun", "noun"),
            ("NOUN", "noun"),
            (" noun ", "noun"),
            ("transitive\tverb", "transitive verb"),
            ("plural  \n noun", "plural noun"),
            ("verb / noun", "verb / noun"),
            ("Straße", "strasse"),
            ("ﬁnal", "final"),
            ("", ""),
            ("   ", ""),
            ("\t\n", ""),
        ],
    )
    def test_collapses_whitespace_and_case_folds(self, *, raw: str, clean: str) -> None:
        assert normalize_part_of_speech(text=raw) == clean

    def test_idempotent(self) -> None:
        once = normalize_part_of_speech(text="  Plural   Noun ")
        assert normalize_part_of_speech(text=once) == once == "plural noun"


class TestIsCleanSentence:
    @pytest.mark.parametrize(
        "text",
        [
            "He *abandoned* himself to despair.",
            "I *beseech* you to have mercy",
            "appear graceful with their *attenuated* figures.",
            '"*Abandon* ship!"',
            "*Abandon* ship.",
            "*Abandon* ship",
            "_Abandon_ ship",
            "'Abandon ship'",
            "(Abandon ship.)",
            "\u00a1Hola!",
            "\u00bfQu\u00e9 pasa?",
            f"{LDQUO}Quoted sentence.{RDQUO}",
            f"{LSQUO}Quoted sentence{RSQUO}",
            "\u00c9lan carried him.",
            "Wow!",
            "Why?",
            "lowercase start but ends well.",
            "lowercase start, exclaimed!",
            "lowercase start, asked?",
            'he said "hi."',
            "3 ships sank.",
            "A/B testing works.",
            "Wait.. what?",
            "   He left.   ",
            "*A*",
            "*a.*",
        ],
    )
    def test_accepts(self, *, text: str) -> None:
        assert is_clean_sentence(text=text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "with reckless *abandon*",
            "*abandon* property",
            "the passage was quoted *in extenso*",
            "he *abandoned* his family",
            "he left,",
            "3 ships sank",
            "*a*",
            "I am *afeared*, / Being in night, all this is but a dream.",
            "Hello / world",
            f"His father {ELLIPSIS} had grown up.",
            f"{ELLIPSIS}and so it goes.",
            f"And so it goes{ELLIPSIS}",
            "Wait... what?",
            "And so it goes...",
            '"Rosetta [LeNoire] did more."',
            "The [sic] text.",
            "[He said.",
            "He said]",
        ],
    )
    def test_rejects(self, *, text: str) -> None:
        assert is_clean_sentence(text=text) is False

    @pytest.mark.parametrize("text", ["", " ", "\t", "**", "__", '""', "''", "()", "\u00a1\u00bf", EDGE_MARKUP])
    def test_rejects_blank_or_markup_only(self, *, text: str) -> None:
        assert is_clean_sentence(text=text) is False

    @pytest.mark.parametrize("mark", text_service.TERMINAL_PUNCTUATION)
    def test_each_terminal_mark_completes_lowercase_start(self, *, mark: str) -> None:
        assert is_clean_sentence(text=f"lowercase start{mark}") is True

    @pytest.mark.parametrize("mark", [",", ";", ":", "-", ")", '"', "*"])
    def test_other_trailing_marks_do_not_complete(self, *, mark: str) -> None:
        assert is_clean_sentence(text=f"lowercase start{mark}") is False

    @pytest.mark.parametrize(
        ("text", "clean"),
        [
            ("A" * LIMIT, True),
            ("A" * (LIMIT + 1), False),
            (f"*{'A' * LIMIT}*", True),
            (f"*{'A' * (LIMIT + 1)}*", False),
            (f"   {'A' * LIMIT}   ", True),
            (f"{'a' * (LIMIT - 1)}.", True),
            (f"{'a' * LIMIT}.", False),
        ],
    )
    def test_length_measured_without_edge_markup(self, *, text: str, clean: bool) -> None:
        assert is_clean_sentence(text=text) is clean
