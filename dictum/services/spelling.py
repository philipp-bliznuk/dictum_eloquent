import typing as t
import unicodedata

HEADLINE_SEPARATOR: t.Final = " / "
"""Joins alternative spellings in the vault headline, so it may not appear inside one."""


def normalize(*, text: str) -> str:
    """Trim and collapse every whitespace run to a single space (phrases keep inner spaces)."""
    return " ".join(text.split())


def key(*, text: str) -> str:
    """Comparison key: normalised whitespace, NFKC composition, full Unicode case folding.

    Two spellings with equal keys are the same entry (`Éclair` / `éclair`, `Straße` / `STRASSE`).
    """
    return unicodedata.normalize("NFKC", normalize(text=text)).casefold()


def validate(*, text: str) -> str:
    """Normalise a spelling or phrase and reject values that cannot round-trip through export.

    Raises:
        ValueError: blank after normalisation or contains the headline separator.
    """
    clean = normalize(text=text)
    if not clean:
        msg = "spelling must contain visible characters"
        raise ValueError(msg)
    if HEADLINE_SEPARATOR in clean:
        msg = f"spelling must not contain {HEADLINE_SEPARATOR!r}"
        raise ValueError(msg)
    return clean
