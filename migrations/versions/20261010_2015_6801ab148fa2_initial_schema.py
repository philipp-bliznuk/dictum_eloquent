"""initial_schema

Revision ID: 6801ab148fa2
Revises:
Create Date: 2026-10-10 20:15:38.160917

"""

import typing as t

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "6801ab148fa2"
down_revision: str | t.Sequence[str] | None = None
branch_labels: str | t.Sequence[str] | None = None
depends_on: str | t.Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "word",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notes", sqlmodel.sql.sqltypes.AutoString(length=10000), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_word")),
    )
    with op.batch_alter_table("word", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_word_status"), ["status"], unique=False)

    op.create_table(
        "lexeme",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("part_of_speech", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("pronunciation", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=True),
        sa.Column("audio_path", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("word_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["word_id"], ["word.id"], name="fk_lexeme_word_id_word", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lexeme")),
        sa.UniqueConstraint("word_id", "part_of_speech", name="uq_lexeme_word_id_part_of_speech"),
    )
    with op.batch_alter_table("lexeme", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_lexeme_word_id"), ["word_id"], unique=False)

    op.create_table(
        "spelling",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("text", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("text_key", sa.String(length=1024), nullable=False),
        sa.Column("word_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["word_id"], ["word.id"], name="fk_spelling_word_id_word", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_spelling")),
        sa.UniqueConstraint("text_key", name="uq_spelling_text_key"),
    )
    with op.batch_alter_table("spelling", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_spelling_word_id"), ["word_id"], unique=False)

    op.create_table(
        "definition",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("text", sqlmodel.sql.sqltypes.AutoString(length=2000), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("lexeme_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["lexeme_id"], ["lexeme.id"], name="fk_definition_lexeme_id_lexeme", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_definition")),
    )
    with op.batch_alter_table("definition", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_definition_lexeme_id"), ["lexeme_id"], unique=False)

    op.create_table(
        "example",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("text", sqlmodel.sql.sqltypes.AutoString(length=2000), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("word_id", sa.Integer(), nullable=False),
        sa.Column("definition_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["definition_id"], ["definition.id"], name="fk_example_definition_id_definition", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["word_id"], ["word.id"], name="fk_example_word_id_word", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_example")),
    )
    with op.batch_alter_table("example", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_example_definition_id"), ["definition_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_example_word_id"), ["word_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("example", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_example_word_id"))
        batch_op.drop_index(batch_op.f("ix_example_definition_id"))

    op.drop_table("example")
    with op.batch_alter_table("definition", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_definition_lexeme_id"))

    op.drop_table("definition")
    with op.batch_alter_table("spelling", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_spelling_word_id"))

    op.drop_table("spelling")
    with op.batch_alter_table("lexeme", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_lexeme_word_id"))

    op.drop_table("lexeme")
    with op.batch_alter_table("word", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_word_status"))

    op.drop_table("word")
