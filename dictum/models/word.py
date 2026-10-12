import typing as t

import pydantic
import sqlalchemy as sa
import sqlmodel

from dictum.enums import DefinitionSource
from dictum.enums import ExampleSource
from dictum.enums import SpellingKind
from dictum.enums import WordStatus
from dictum.models.base import BaseModel
from dictum.services import spelling as spelling_service

if t.TYPE_CHECKING:
    import sqlalchemy.orm as sa_orm

_SELECTIN: t.Final = "selectin"
_CASCADE: t.Final = "all, delete-orphan"
SPELLING_MAX_LENGTH: t.Final = 255
"""Room for phrases such as `put paid to` or `By my troth!`, not only single words."""
SPELLING_KEY_MAX_LENGTH: t.Final = 1024
"""Case folding and NFKC may expand a string (`ß` -> `ss`), so the key column is wider than `text`."""
PART_OF_SPEECH_MAX_LENGTH: t.Final = 64
TEXT_MAX_LENGTH: t.Final = 2000
"""Upper bound for one definition or example."""
NOTES_MAX_LENGTH: t.Final = 10_000
AUDIO_PATH_PATTERN: t.Final = r"^/audio/[A-Za-z0-9._-]+$"
"""Only files served by this app; keeps the browser from being pointed at arbitrary URLs."""

AudioPath = t.Annotated[str, pydantic.StringConstraints(pattern=AUDIO_PATH_PATTERN)]
"""`sqlmodel.Field(regex=...)` is silently dropped under pydantic v2, so the pattern lives in the annotation."""


class WordBase(sqlmodel.SQLModel):
    """Editable scalar fields of a word; everything grammatical lives on its lexemes."""

    notes: str | None = sqlmodel.Field(default=None, max_length=NOTES_MAX_LENGTH)
    status: WordStatus = sqlmodel.Field(default=WordStatus.LEARNED, sa_type=sa.String(16), index=True)


class Word(WordBase, BaseModel, table=True):
    __tablename__ = "word"

    spellings: list["Spelling"] = sqlmodel.Relationship(
        back_populates="word",
        sa_relationship_kwargs={"lazy": _SELECTIN, "cascade": _CASCADE, "order_by": "Spelling.id"},
    )
    lexemes: list["Lexeme"] = sqlmodel.Relationship(
        back_populates="word",
        sa_relationship_kwargs={"lazy": _SELECTIN, "cascade": _CASCADE, "order_by": "Lexeme.position"},
    )
    examples: list["Example"] = sqlmodel.Relationship(
        back_populates="word",
        sa_relationship_kwargs={"lazy": _SELECTIN, "cascade": _CASCADE, "order_by": "Example.position"},
    )


class SpellingBase(sqlmodel.SQLModel):
    text: str = sqlmodel.Field(max_length=SPELLING_MAX_LENGTH)
    kind: SpellingKind = sqlmodel.Field(default=SpellingKind.PRIMARY, sa_type=sa.String(16))


class Spelling(SpellingBase, BaseModel, table=True):
    """One spelling or phrase; `text_key` is derived from `text` on every write and carries the uniqueness rule."""

    __tablename__ = "spelling"
    __table_args__ = (sa.UniqueConstraint("text_key", name="uq_spelling_text_key"),)

    text_key: str = sqlmodel.Field(sa_type=sa.String(SPELLING_KEY_MAX_LENGTH))
    word_id: int = sqlmodel.Field(
        sa_column=sa.Column(
            sa.Integer,
            sa.ForeignKey("word.id", ondelete="CASCADE", name="fk_spelling_word_id_word"),
            nullable=False,
            index=True,
        ),
    )
    word: Word = sqlmodel.Relationship(back_populates="spellings")


@sa.event.listens_for(Spelling, "before_insert")
@sa.event.listens_for(Spelling, "before_update")
def _derive_spelling_key(_mapper: sa_orm.Mapper[Spelling], _connection: sa.Connection, target: Spelling) -> None:
    target.text_key = spelling_service.key(text=target.text)


class LexemeBase(sqlmodel.SQLModel):
    """One part of speech of a word with its own pronunciation (`record` the noun vs `record` the verb)."""

    part_of_speech: str = sqlmodel.Field(max_length=PART_OF_SPEECH_MAX_LENGTH)
    pronunciation: str | None = sqlmodel.Field(default=None, max_length=255)
    audio_path: AudioPath | None = sqlmodel.Field(default=None, max_length=255)


class Lexeme(LexemeBase, BaseModel, table=True):
    __tablename__ = "lexeme"
    __table_args__ = (sa.UniqueConstraint("word_id", "part_of_speech", name="uq_lexeme_word_id_part_of_speech"),)

    position: int = sqlmodel.Field(default=0, ge=0)
    word_id: int = sqlmodel.Field(
        sa_column=sa.Column(
            sa.Integer,
            sa.ForeignKey("word.id", ondelete="CASCADE", name="fk_lexeme_word_id_word"),
            nullable=False,
            index=True,
        ),
    )
    word: Word = sqlmodel.Relationship(back_populates="lexemes")
    definitions: list["Definition"] = sqlmodel.Relationship(
        back_populates="lexeme",
        sa_relationship_kwargs={"lazy": _SELECTIN, "cascade": _CASCADE, "order_by": "Definition.position"},
    )


class DefinitionBase(sqlmodel.SQLModel):
    text: str = sqlmodel.Field(max_length=TEXT_MAX_LENGTH)
    source: DefinitionSource = sqlmodel.Field(default=DefinitionSource.USER, sa_type=sa.String(16))


class Definition(DefinitionBase, BaseModel, table=True):
    __tablename__ = "definition"

    position: int = sqlmodel.Field(default=0, ge=0)
    lexeme_id: int = sqlmodel.Field(
        sa_column=sa.Column(
            sa.Integer,
            sa.ForeignKey("lexeme.id", ondelete="CASCADE", name="fk_definition_lexeme_id_lexeme"),
            nullable=False,
            index=True,
        ),
    )
    lexeme: Lexeme = sqlmodel.Relationship(back_populates="definitions")
    examples: list["Example"] = sqlmodel.Relationship(
        back_populates="definition",
        sa_relationship_kwargs={"lazy": _SELECTIN, "order_by": "Example.position"},
    )


class ExampleBase(sqlmodel.SQLModel):
    text: str = sqlmodel.Field(max_length=TEXT_MAX_LENGTH)
    source: ExampleSource = sqlmodel.Field(default=ExampleSource.USER, sa_type=sa.String(16))


class Example(ExampleBase, BaseModel, table=True):
    """Owned by the word; `definition_id` is set when the sentence illustrates one definition in particular."""

    __tablename__ = "example"

    position: int = sqlmodel.Field(default=0, ge=0)
    word_id: int = sqlmodel.Field(
        sa_column=sa.Column(
            sa.Integer,
            sa.ForeignKey("word.id", ondelete="CASCADE", name="fk_example_word_id_word"),
            nullable=False,
            index=True,
        ),
    )
    definition_id: int | None = sqlmodel.Field(
        default=None,
        sa_column=sa.Column(
            sa.Integer,
            sa.ForeignKey("definition.id", ondelete="SET NULL", name="fk_example_definition_id_definition"),
            nullable=True,
            index=True,
        ),
    )
    word: Word = sqlmodel.Relationship(back_populates="examples")
    definition: Definition | None = sqlmodel.Relationship(back_populates="examples")
