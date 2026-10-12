import collections.abc as cabc
import functools
import pathlib
import typing as t

import pydantic
import pydantic_settings

from dictum import hosts
from dictum.enums import Environment

LogLevel = t.Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def _any_host() -> list[str]:
    return [hosts.ANY_HOST]


class Settings(pydantic_settings.BaseSettings):
    """Application configuration loaded from environment / `.env`."""

    model_config = pydantic_settings.SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    environment: Environment = Environment.PROD
    log_level: LogLevel = "INFO"
    data_dir: pathlib.Path = pathlib.Path("/data")
    database_url: str = "sqlite+aiosqlite:////data/dictum.db"
    ui_dist: pathlib.Path = pathlib.Path("ui/dist")
    allowed_hosts: t.Annotated[list[str], pydantic_settings.NoDecode] = pydantic.Field(default_factory=_any_host)
    """Accepted `Host` headers, `,` `;` or space separated; `*` = any, `*.lan` = suffix.

    Ports, schemes and quotes are stripped. Loopback names always pass. Set real names on a LAN
    box to block DNS rebinding.
    """

    mw_api_key: pydantic.SecretStr | None = None
    mw_base_url: pydantic.AnyHttpUrl = pydantic.AnyHttpUrl(
        "https://www.dictionaryapi.com/api/v3/references/collegiate/json/"
    )
    mw_audio_base_url: pydantic.AnyHttpUrl = pydantic.AnyHttpUrl(
        "https://media.merriam-webster.com/audio/prons/en/us/mp3/"
    )
    free_dictionary_base_url: pydantic.AnyHttpUrl = pydantic.AnyHttpUrl(
        "https://api.dictionaryapi.dev/api/v2/entries/en/"
    )
    http_timeout: float = pydantic.Field(default=10.0, gt=0)
    """Seconds allowed per upstream request (dictionaries, audio)."""

    aws_region: str = "us-east-1"
    aws_access_key_id: pydantic.SecretStr | None = None
    aws_secret_access_key: pydantic.SecretStr | None = None
    """AI generation (Bedrock) is enabled only when both keys are present; set `ALLOWED_HOSTS` too."""
    bedrock_model_id: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

    @pydantic.field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @pydantic.field_validator("allowed_hosts", mode="before")
    @classmethod
    def _parse_hosts(cls, value: object) -> object:
        if not isinstance(value, cabc.Iterable):
            return value
        parsed = hosts.parse_allowed_hosts(value=value)
        if not parsed:
            msg = "ALLOWED_HOSTS is empty: list hosts (dictum.local,192.168.1.20) or use *"
            raise ValueError(msg)
        return parsed

    @property
    def ai_enabled(self) -> bool:
        """True when Bedrock credentials are configured."""
        return bool(self.aws_access_key_id and self.aws_secret_access_key)

    @property
    def audio_dir(self) -> pathlib.Path:
        """Directory for cached pronunciation files."""
        return self.data_dir / "audio"

    @property
    def is_local(self) -> bool:
        """True when running on a developer machine."""
        return self.environment is Environment.LOCAL

    @property
    def is_test(self) -> bool:
        """True under pytest."""
        return self.environment is Environment.TEST


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return process-wide settings singleton."""
    return Settings()
