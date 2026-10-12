import typing as t

import fastapi
import pydantic
import pytest

from dictum.enums import ConflictPolicy
from dictum.enums import DefinitionSource
from dictum.enums import Environment
from dictum.enums import ExampleSource
from dictum.enums import ProviderOutcome
from dictum.enums import SpellingKind
from dictum.enums import WordStatus
from dictum.exceptions import AppBaseError
from dictum.exceptions import AudioNotFoundError
from dictum.exceptions import NotFoundError
from dictum.exceptions import ProviderNotConfiguredError
from dictum.exceptions import SpellingConflictError
from dictum.exceptions import UpstreamError
from dictum.exceptions import ValidationError
from dictum.exceptions import WordNotFoundError
from dictum.settings import Settings
from dictum.settings import get_settings
from tests.conftest import AWS_ACCESS_KEY_ID
from tests.conftest import AWS_SECRET_ACCESS_KEY
from tests.conftest import make_ai_settings
from tests.conftest import make_settings

if t.TYPE_CHECKING:
    import pathlib

    from dictum.enums import BaseEnum


class TestSettings:
    def test_audio_dir_under_data_dir(self, *, tmp_path: pathlib.Path) -> None:
        settings = make_settings(tmp_path=tmp_path)
        assert settings.audio_dir == tmp_path / "data" / "audio"

    @pytest.mark.parametrize(
        ("environment", "flags"),
        [
            ("local", {"is_local": True, "is_test": False}),
            ("test", {"is_local": False, "is_test": True}),
            ("prod", {"is_local": False, "is_test": False}),
        ],
    )
    def test_environment_flags(self, *, tmp_path: pathlib.Path, environment: str, flags: dict[str, bool]) -> None:
        settings = make_settings(tmp_path=tmp_path, environment=environment)
        assert settings.environment is Environment(environment)
        assert {"is_local": settings.is_local, "is_test": settings.is_test} == flags

    def test_frozen(self, *, tmp_path: pathlib.Path) -> None:
        settings = make_settings(tmp_path=tmp_path)
        with pytest.raises(Exception, match="frozen"):
            settings.log_level = "DEBUG"  # type: ignore[misc]

    def test_defaults(self) -> None:
        settings = Settings(_env_file=None)
        assert settings.mw_api_key is None
        assert str(settings.mw_base_url).startswith("https://www.dictionaryapi.com/")
        assert settings.aws_region == "us-east-1"
        assert settings.aws_access_key_id is None
        assert settings.aws_secret_access_key is None
        assert settings.bedrock_model_id == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        assert settings.ai_enabled is False
        assert settings.log_level == "INFO"
        assert settings.allowed_hosts == ["*"]

    def test_lookup_defaults(self) -> None:
        settings = Settings(_env_file=None)
        assert str(settings.mw_base_url) == "https://www.dictionaryapi.com/api/v3/references/collegiate/json/"
        assert str(settings.mw_audio_base_url) == "https://media.merriam-webster.com/audio/prons/en/us/mp3/"
        assert str(settings.free_dictionary_base_url) == "https://api.dictionaryapi.dev/api/v2/entries/en/"
        assert settings.http_timeout == pytest.approx(10.0)

    @pytest.mark.parametrize("raw", [0, -1, -0.5, "0"])
    def test_http_timeout_must_be_positive(self, *, tmp_path: pathlib.Path, raw: object) -> None:
        with pytest.raises(pydantic.ValidationError, match="greater than 0"):
            make_settings(tmp_path=tmp_path, http_timeout=raw)

    @pytest.mark.parametrize(("raw", "seconds"), [(0.5, 0.5), ("2", 2.0), (30, 30.0)])
    def test_http_timeout_parsed(self, *, tmp_path: pathlib.Path, raw: object, seconds: float) -> None:
        assert make_settings(tmp_path=tmp_path, http_timeout=raw).http_timeout == seconds

    def test_mw_api_key_is_secret(self, *, tmp_path: pathlib.Path) -> None:
        settings = make_settings(tmp_path=tmp_path, mw_api_key="abc-123")
        assert settings.mw_api_key is not None
        assert settings.mw_api_key.get_secret_value() == "abc-123"
        assert "abc-123" not in str(settings.mw_api_key)
        assert "abc-123" not in repr(settings)

    @pytest.mark.parametrize(
        ("access_key", "secret_key", "enabled"),
        [
            (None, None, False),
            (AWS_ACCESS_KEY_ID, None, False),
            (None, AWS_SECRET_ACCESS_KEY, False),
            ("", AWS_SECRET_ACCESS_KEY, False),
            (AWS_ACCESS_KEY_ID, "", False),
            (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, True),
        ],
    )
    def test_ai_enabled_needs_both_keys(
        self, *, tmp_path: pathlib.Path, access_key: str | None, secret_key: str | None, enabled: bool
    ) -> None:
        settings = make_settings(tmp_path=tmp_path, aws_access_key_id=access_key, aws_secret_access_key=secret_key)
        assert settings.ai_enabled is enabled

    def test_aws_keys_are_secret(self, *, tmp_path: pathlib.Path) -> None:
        settings = make_ai_settings(tmp_path=tmp_path)
        assert settings.aws_access_key_id is not None
        assert settings.aws_secret_access_key is not None
        assert settings.aws_access_key_id.get_secret_value() == AWS_ACCESS_KEY_ID
        assert settings.aws_secret_access_key.get_secret_value() == AWS_SECRET_ACCESS_KEY
        for rendered in (
            repr(settings),
            str(settings),
            str(settings.aws_access_key_id),
            str(settings.aws_secret_access_key),
        ):
            assert AWS_ACCESS_KEY_ID not in rendered
            assert AWS_SECRET_ACCESS_KEY not in rendered
        assert AWS_SECRET_ACCESS_KEY not in str(settings.model_dump())

    def test_aws_settings_from_env(self, *, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", AWS_ACCESS_KEY_ID)
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", AWS_SECRET_ACCESS_KEY)
        monkeypatch.setenv("AWS_REGION", "eu-west-1")
        monkeypatch.setenv("BEDROCK_MODEL_ID", "anthropic.test-model")
        settings = make_settings(tmp_path=tmp_path)
        assert settings.ai_enabled is True
        assert settings.aws_region == "eu-west-1"
        assert settings.bedrock_model_id == "anthropic.test-model"

    @pytest.mark.parametrize("raw", ["ftp://x", "not a url", ""])
    def test_provider_urls_must_be_http(self, *, tmp_path: pathlib.Path, raw: str) -> None:
        with pytest.raises(pydantic.ValidationError):
            make_settings(tmp_path=tmp_path, mw_base_url=raw)

    @pytest.mark.parametrize(("raw", "level"), [("debug", "DEBUG"), ("Warning", "WARNING"), ("ERROR", "ERROR")])
    def test_log_level_case_insensitive(self, *, tmp_path: pathlib.Path, raw: str, level: str) -> None:
        assert make_settings(tmp_path=tmp_path, log_level=raw).log_level == level

    @pytest.mark.parametrize("raw", ["verbose", "", 10])
    def test_log_level_rejects_unknown(self, *, tmp_path: pathlib.Path, raw: object) -> None:
        with pytest.raises(pydantic.ValidationError):
            make_settings(tmp_path=tmp_path, log_level=raw)

    @pytest.mark.parametrize(
        ("raw", "hosts"),
        [
            ("*", ["*"]),
            ("dictum.local, 192.168.1.20 ,127.0.0.1,", ["dictum.local", "192.168.1.20", "127.0.0.1"]),
            (["a.local", "b.local"], ["a.local", "b.local"]),
            ("192.168.50.199,127.0.0.1", ["192.168.50.199", "127.0.0.1"]),
            ("http://Dictum.LOCAL:3000/;'192.168.50.199:3000' [::1]:3000", ["dictum.local", "192.168.50.199", "::1"]),
            ("*.lan:3000 pi.lan", ["*.lan", "pi.lan"]),
            (("a.lan", "A.LAN"), ["a.lan"]),
        ],
    )
    def test_allowed_hosts_parsed(self, *, tmp_path: pathlib.Path, raw: object, hosts: list[str]) -> None:
        assert make_settings(tmp_path=tmp_path, allowed_hosts=raw).allowed_hosts == hosts

    @pytest.mark.parametrize("raw", ["", " ,; ", [], ["''"]])
    def test_allowed_hosts_empty_fails_startup(self, *, tmp_path: pathlib.Path, raw: object) -> None:
        with pytest.raises(pydantic.ValidationError, match="ALLOWED_HOSTS is empty"):
            make_settings(tmp_path=tmp_path, allowed_hosts=raw)

    def test_allowed_hosts_rejects_non_iterable(self, *, tmp_path: pathlib.Path) -> None:
        with pytest.raises(pydantic.ValidationError):
            make_settings(tmp_path=tmp_path, allowed_hosts=42)

    def test_allowed_hosts_from_env(self, *, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
        monkeypatch.setenv("ALLOWED_HOSTS", "192.168.50.199,127.0.0.1")
        monkeypatch.setenv("LOG_LEVEL", "debug")
        settings = make_settings(tmp_path=tmp_path)
        assert settings.allowed_hosts == ["192.168.50.199", "127.0.0.1"]
        assert settings.log_level == "DEBUG"

    def test_allowed_hosts_empty_env_fails(self, *, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
        monkeypatch.setenv("ALLOWED_HOSTS", "")
        with pytest.raises(pydantic.ValidationError, match="ALLOWED_HOSTS is empty"):
            make_settings(tmp_path=tmp_path)

    def test_get_settings_cached(self) -> None:
        get_settings.cache_clear()
        first = get_settings()
        second = get_settings()
        assert first is second
        assert first.is_test
        get_settings.cache_clear()


class TestEnums:
    @pytest.mark.parametrize(
        ("enum", "expected"),
        [
            (Environment, ["local", "test", "prod"]),
            (WordStatus, ["inbox", "learned"]),
            (ConflictPolicy, ["skip", "replace", "fail"]),
            (SpellingKind, ["primary", "variant"]),
            (DefinitionSource, ["user", "mw", "free_dict", "ai"]),
            (ExampleSource, ["user", "vault", "mw", "free_dict", "ai"]),
            (ProviderOutcome, ["found", "not_found", "error"]),
        ],
    )
    def test_values(self, *, enum: type[BaseEnum], expected: list[str]) -> None:
        assert enum.values() == expected

    def test_str_value(self) -> None:
        assert f"{WordStatus.INBOX}" == "inbox"
        assert WordStatus("learned") is WordStatus.LEARNED


class TestExceptions:
    @pytest.mark.parametrize(
        ("exc_type", "status", "detail"),
        [
            (AppBaseError, 500, "Internal error"),
            (NotFoundError, 404, "Not found"),
            (WordNotFoundError, 404, "Word not found"),
            (AudioNotFoundError, 404, "No pronunciation audio available"),
            (ValidationError, 422, "Invalid input"),
            (UpstreamError, 502, "Upstream provider failed"),
            (ProviderNotConfiguredError, 503, "Provider not configured"),
        ],
    )
    def test_defaults(self, *, exc_type: type[AppBaseError], status: int, detail: str) -> None:
        exc = exc_type()
        assert isinstance(exc, fastapi.HTTPException)
        assert exc.status_code == status
        assert exc.detail == detail
        assert exc.headers is None

    def test_custom_detail_and_headers(self) -> None:
        exc = ValidationError(detail="bad", headers={"X-Why": "test"})
        assert exc.detail == "bad"
        assert exc.headers == {"X-Why": "test"}

    def test_spelling_conflict(self) -> None:
        exc = SpellingConflictError(spelling="Abandon", existing_word_id=7)
        assert exc.status_code == 409
        assert exc.spelling == "Abandon"
        assert exc.existing_word_id == 7
        assert exc.detail == "Spelling 'Abandon' already belongs to word 7"
        assert exc.headers == {"Location": "/api/words/7"}

    def test_word_not_found_is_not_found(self) -> None:
        assert issubclass(WordNotFoundError, NotFoundError)
        assert issubclass(AudioNotFoundError, NotFoundError)
        assert issubclass(NotFoundError, AppBaseError)
