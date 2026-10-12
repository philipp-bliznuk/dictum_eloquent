import argparse
import pathlib
import typing as t

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory

from dictum.models import metadata

if t.TYPE_CHECKING:
    import collections.abc as cabc

ROOT: t.Final = pathlib.Path(__file__).resolve().parent.parent
HEAD: t.Final = "6801ab148fa2"
"""Single initial revision of the version 2 schema."""


def _config(*, db_file: pathlib.Path) -> Config:
    """Alembic config bound to a throwaway SQLite file; `-x ini=1` makes `env.py` take the URL from here."""
    config = Config()
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")
    config.cmd_opts = argparse.Namespace(x=["ini=1"])
    return config


@pytest.fixture
def db_file(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "migrated.db"


@pytest.fixture
def config(db_file: pathlib.Path) -> Config:
    return _config(db_file=db_file)


@pytest.fixture
def migrated(config: Config, db_file: pathlib.Path) -> cabc.Iterator[sa.Connection]:
    """Upgrade the throwaway database to `head`.

    Yields:
        Synchronous connection to the migrated database for inspection.
    """
    command.upgrade(config, "head")
    engine = sa.create_engine(f"sqlite:///{db_file}")
    with engine.connect() as connection:
        yield connection
    engine.dispose()


class TestInitialSchema:
    def test_single_head(self, config: Config) -> None:
        script = ScriptDirectory.from_config(config)
        assert script.get_heads() == [HEAD]
        assert [rev.revision for rev in script.walk_revisions()] == [HEAD]

    def test_upgrade_matches_models(self, migrated: sa.Connection) -> None:
        context = MigrationContext.configure(migrated, opts={"compare_type": True, "render_as_batch": True})
        assert context.get_current_revision() == HEAD
        assert compare_metadata(context, metadata) == []
        assert sorted(sa.inspect(migrated).get_table_names()) == [
            "alembic_version",
            "definition",
            "example",
            "lexeme",
            "spelling",
            "word",
        ]

    @pytest.mark.parametrize(
        ("table", "expected"),
        [
            ("spelling", {"fk_spelling_word_id_word": ("word", "CASCADE")}),
            ("lexeme", {"fk_lexeme_word_id_word": ("word", "CASCADE")}),
            ("definition", {"fk_definition_lexeme_id_lexeme": ("lexeme", "CASCADE")}),
            (
                "example",
                {
                    "fk_example_word_id_word": ("word", "CASCADE"),
                    "fk_example_definition_id_definition": ("definition", "SET NULL"),
                },
            ),
        ],
    )
    def test_foreign_keys_named_with_delete_rules(
        self, migrated: sa.Connection, *, table: str, expected: dict[str, tuple[str, str]]
    ) -> None:
        keys = sa.inspect(migrated).get_foreign_keys(table)
        assert {fk["name"]: (fk["referred_table"], fk["options"].get("ondelete")) for fk in keys} == expected

    @pytest.mark.parametrize(
        ("table", "expected"),
        [
            ("spelling", {"uq_spelling_text_key": ["text_key"]}),
            ("lexeme", {"uq_lexeme_word_id_part_of_speech": ["word_id", "part_of_speech"]}),
            ("word", {}),
            ("definition", {}),
            ("example", {}),
        ],
    )
    def test_unique_constraints(self, migrated: sa.Connection, *, table: str, expected: dict[str, list[str]]) -> None:
        constraints = sa.inspect(migrated).get_unique_constraints(table)
        assert {c["name"]: c["column_names"] for c in constraints} == expected

    def test_indexes(self, migrated: sa.Connection) -> None:
        inspector = sa.inspect(migrated)
        indexed = {
            table: sorted(ix["name"] for ix in inspector.get_indexes(table) if not ix["unique"])
            for table in ("word", "spelling", "lexeme", "definition", "example")
        }
        assert indexed == {
            "word": ["ix_word_status"],
            "spelling": ["ix_spelling_word_id"],
            "lexeme": ["ix_lexeme_word_id"],
            "definition": ["ix_definition_lexeme_id"],
            "example": ["ix_example_definition_id", "ix_example_word_id"],
        }

    def test_example_definition_id_nullable(self, migrated: sa.Connection) -> None:
        columns = {c["name"]: c for c in sa.inspect(migrated).get_columns("example")}
        assert columns["definition_id"]["nullable"] is True
        assert columns["word_id"]["nullable"] is False

    def test_downgrade_drops_everything(self, config: Config, migrated: sa.Connection) -> None:
        command.downgrade(config, "base")
        assert sa.inspect(migrated).get_table_names() == ["alembic_version"]
        assert MigrationContext.configure(migrated).get_current_revision() is None

    def test_upgrade_is_idempotent_at_head(self, config: Config, migrated: sa.Connection) -> None:
        command.upgrade(config, "head")
        assert MigrationContext.configure(migrated).get_current_revision() == HEAD
