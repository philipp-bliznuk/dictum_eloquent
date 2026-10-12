"""Text rules shared by providers, schemas and the AI service."""

import typing as t


def normalize_part_of_speech(*, text: str) -> str:
    """Canonical form of a part-of-speech label: inner whitespace collapsed, case folded.

    Lexemes of one word are unique by this form, so `Noun` and `noun ` are the same lexeme.
    """
    return " ".join(text.split()).casefold()


TERMINAL_PUNCTUATION: t.Final = (".", "!", "?")
"""Characters that end a complete sentence."""

MAX_EXAMPLE_LENGTH: t.Final = 300
"""Longer texts are literary citations rather than usage examples."""

_EDGE_MARKUP: t.Final = " *_\"'\u201c\u201d\u2018\u2019()\u00a1\u00bf"
_REJECTED_FRAGMENTS: t.Final = (" / ", "\u2026", "...", "[", "]")
"""Verse line breaks, elided quotations and editorial insertions."""


def is_clean_sentence(*, text: str) -> bool:
    """Tell whether a provider example reads as one complete, self-contained sentence.

    Mirrors the vault converter: a text is a fragment when it neither starts with a capital letter
    nor ends with terminal punctuation (`with reckless *abandon*`). Additionally rejected: quotations
    with elisions or editorial brackets, verse joined with ` / `, and anything longer than
    `MAX_EXAMPLE_LENGTH`.
    """
    bare = text.strip(_EDGE_MARKUP)
    if not bare or len(bare) > MAX_EXAMPLE_LENGTH:
        return False
    if any(marker in text for marker in _REJECTED_FRAGMENTS):
        return False
    return bare[0].isupper() or bare.endswith(TERMINAL_PUNCTUATION)
