import collections
import io
import typing as t
import unicodedata
import zipfile

from dictum.enums import WordStatus
from dictum.services.spelling import HEADLINE_SEPARATOR

if t.TYPE_CHECKING:
    import collections.abc as cabc

    from dictum.models.word import Lexeme
    from dictum.models.word import Word

INBOX_FILE: t.Final = "_Inbox.md"
OTHER_FILE: t.Final = "#.md"
"""Entries whose headword has no Latin letter at all (digits, symbols, other scripts)."""
LOOSE_EXAMPLES_HEADING: t.Final = "###### examples"
"""Reserved sub-heading for sentences not tied to one definition; parts of speech never use this label."""


def _headline(*, word: Word) -> str:
    names = HEADLINE_SEPARATOR.join(s.text for s in word.spellings)
    return f"##### {names}"


def _lexeme(*, lexeme: Lexeme) -> list[str]:
    lines = [f"###### {lexeme.part_of_speech}"]
    if lexeme.pronunciation:
        lines.append(f"*{lexeme.pronunciation}*")
    for d in lexeme.definitions:
        lines.append(f"- **{d.text}**")
        lines.extend(f"\t{e.text}" for e in d.examples)
    return lines


def render_word(*, word: Word) -> str:
    """One vault entry: headline, a `######` section per part of speech, loose examples, notes as a quote.

    One-way export for reading in Obsidian; the JSON export is the backup format.
    """
    lines = [_headline(word=word)]
    for lexeme in word.lexemes:
        lines.extend(_lexeme(lexeme=lexeme))
    loose = [f"\t{e.text}" for e in word.examples if e.definition_id is None]
    if loose:
        lines.append(LOOSE_EXAMPLES_HEADING)
        lines.extend(loose)
    if word.notes:
        lines.extend(f"> {line}" for line in word.notes.splitlines())
    return "\n".join(lines) + "\n"


def _bucket(*, word: Word) -> str:
    """`A.md`..`Z.md` by the first alphanumeric of the headword (accents stripped, leading punctuation skipped)."""
    if word.status == WordStatus.INBOX:
        return INBOX_FILE
    text = word.spellings[0].text if word.spellings else ""
    first = next((c for c in unicodedata.normalize("NFKD", text) if c.isalnum()), "")
    return f"{first.upper()}.md" if first.isascii() and first.isalpha() else OTHER_FILE


def render_vault(*, words: cabc.Iterable[Word]) -> dict[str, str]:
    """Group words into `A.md`..`Z.md` + `_Inbox.md`, sorted case-insensitively within each file."""
    buckets: dict[str, list[Word]] = collections.defaultdict(list)
    for w in words:
        buckets[_bucket(word=w)].append(w)
    files: dict[str, str] = {}
    for name, items in buckets.items():
        items.sort(key=lambda w: w.spellings[0].text.casefold() if w.spellings else "")
        files[name] = "\n".join(render_word(word=w) for w in items)
    return files


def zip_vault(*, files: dict[str, str]) -> bytes:
    """Zip rendered files in memory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            zf.writestr(name, files[name])
    return buf.getvalue()
