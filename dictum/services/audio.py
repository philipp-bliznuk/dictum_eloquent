import logging
import re
import typing as t

import httpx

if t.TYPE_CHECKING:
    import pathlib

log = logging.getLogger(__name__)

AUDIO_URL_PREFIX: t.Final = "/audio/"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_ACCEPTED_TYPES: t.Final = ("audio/", "application/octet-stream")


def safe_name(*, name: str) -> str:
    """Reduce `name` to a filename matching `AUDIO_PATH_PATTERN`, always ending in `.mp3`."""
    stem = _UNSAFE.sub("_", name).strip("._-") or "audio"
    stem = stem.removesuffix(".mp3")
    return f"{stem}.mp3"


class AudioCache:
    """Downloads pronunciation files once into `directory`; served by the `/audio` static mount."""

    def __init__(self, *, client: httpx.AsyncClient, directory: pathlib.Path) -> None:
        self._client = client
        self._directory = directory

    def path_for(self, *, name: str) -> pathlib.Path:
        """Local file for a (sanitised) name."""
        return self._directory / safe_name(name=name)

    async def fetch(self, *, url: str, name: str) -> str | None:
        """Ensure `url` is cached under `name`; return its public `/audio/...` path, or None on failure."""
        target = self.path_for(name=name)
        public = f"{AUDIO_URL_PREFIX}{target.name}"
        if target.is_file():
            return public
        try:
            response = await self._client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("audio fetch failed url=%s error=%s", url, exc)
            return None
        content_type = response.headers.get("content-type", "")
        if not response.content or not content_type.startswith(_ACCEPTED_TYPES):
            log.warning("audio fetch rejected url=%s type=%s bytes=%d", url, content_type, len(response.content))
            return None
        self._directory.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(".part")
        partial.write_bytes(response.content)
        partial.replace(target)
        return public
