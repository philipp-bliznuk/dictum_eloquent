import re
import typing as t

import httpx
import pytest

from dictum.models.word import AUDIO_PATH_PATTERN
from dictum.services.audio import safe_name
from tests.conftest import MP3_BYTES

if t.TYPE_CHECKING:
    import pathlib

    from dictum.services.audio import AudioCache
    from tests.conftest import FakeUpstream

URL: t.Final = "https://audio.test/mp3/a/abando01.mp3"


class TestSafeName:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("abando01", "abando01.mp3"),
            ("abandon-us.mp3", "abandon-us.mp3"),
            ("foo.mp3", "foo.mp3"),
            ("foo.mp3.mp3", "foo.mp3.mp3"),
            ("../x y.mp3", "x_y.mp3"),
            ("a/b\\c", "a_b_c.mp3"),
            (".hidden", "hidden.mp3"),
            ("--x--", "x.mp3"),
            ("", "audio.mp3"),
            ("ÄÖ", "audio.mp3"),
            ("...", "audio.mp3"),
            ("zorb (1).mp3", "zorb_1_.mp3"),
        ],
    )
    def test_sanitised(self, *, name: str, expected: str) -> None:
        assert safe_name(name=name) == expected

    @pytest.mark.parametrize("name", ["abando01", "../../etc/passwd", "a b", "x.mp3"])
    def test_matches_audio_path_pattern(self, *, name: str) -> None:
        assert re.fullmatch(AUDIO_PATH_PATTERN, f"/audio/{safe_name(name=name)}")


class TestAudioCache:
    def test_path_for_sanitises(self, audio_cache: AudioCache, tmp_path: pathlib.Path) -> None:
        assert audio_cache.path_for(name="../x y") == tmp_path / "audio" / "x_y.mp3"
        assert audio_cache.path_for(name="abando01") == tmp_path / "audio" / "abando01.mp3"

    async def test_fetch_downloads_and_returns_public_path(
        self, audio_cache: AudioCache, upstream: FakeUpstream, tmp_path: pathlib.Path
    ) -> None:
        upstream.audio(url=URL)
        assert not (tmp_path / "audio").exists()
        public = await audio_cache.fetch(url=URL, name="abando01")
        assert public == "/audio/abando01.mp3"
        assert (tmp_path / "audio" / "abando01.mp3").read_bytes() == MP3_BYTES
        assert sorted(p.name for p in (tmp_path / "audio").iterdir()) == ["abando01.mp3"]
        assert upstream.urls == [URL]

    async def test_fetch_sanitises_name(self, audio_cache: AudioCache, upstream: FakeUpstream) -> None:
        upstream.audio(url=URL)
        assert await audio_cache.fetch(url=URL, name="../x y") == "/audio/x_y.mp3"

    async def test_fetch_uses_cache_on_second_call(self, audio_cache: AudioCache, upstream: FakeUpstream) -> None:
        upstream.audio(url=URL)
        first = await audio_cache.fetch(url=URL, name="abando01")
        second = await audio_cache.fetch(url=URL, name="abando01")
        assert first == second == "/audio/abando01.mp3"
        assert len(upstream.requests) == 1

    async def test_existing_file_skips_http(
        self, audio_cache: AudioCache, upstream: FakeUpstream, tmp_path: pathlib.Path
    ) -> None:
        (tmp_path / "audio").mkdir()
        (tmp_path / "audio" / "abando01.mp3").write_bytes(b"old")
        assert await audio_cache.fetch(url=URL, name="abando01") == "/audio/abando01.mp3"
        assert upstream.requests == []
        assert (tmp_path / "audio" / "abando01.mp3").read_bytes() == b"old"

    @pytest.mark.parametrize("content_type", ["audio/mpeg", "audio/mp3; charset=binary", "application/octet-stream"])
    async def test_accepted_content_types(
        self, audio_cache: AudioCache, upstream: FakeUpstream, content_type: str
    ) -> None:
        upstream.audio(url=URL, content_type=content_type)
        assert await audio_cache.fetch(url=URL, name="abando01") == "/audio/abando01.mp3"

    @pytest.mark.parametrize(
        ("content", "content_type"),
        [
            (b"<html>not audio</html>", "text/html"),
            (b'{"error": "x"}', "application/json"),
            (MP3_BYTES, ""),
            (b"", "audio/mpeg"),
        ],
    )
    async def test_rejects_bad_responses(
        self, audio_cache: AudioCache, upstream: FakeUpstream, *, content: bytes, content_type: str
    ) -> None:
        upstream.audio(url=URL, content=content, content_type=content_type)
        assert await audio_cache.fetch(url=URL, name="abando01") is None
        assert not audio_cache.path_for(name="abando01").exists()

    async def test_missing_content_type_header(self, audio_cache: AudioCache, upstream: FakeUpstream) -> None:
        upstream.route(url=URL, responder=lambda _request: httpx.Response(200, content=MP3_BYTES))
        assert await audio_cache.fetch(url=URL, name="abando01") is None

    @pytest.mark.parametrize("status_code", [301, 404, 500])
    async def test_http_status_error_returns_none(
        self, audio_cache: AudioCache, upstream: FakeUpstream, status_code: int
    ) -> None:
        upstream.audio(url=URL)
        upstream.route(
            url=URL,
            responder=lambda _request: httpx.Response(
                status_code, content=MP3_BYTES, headers={"content-type": "audio/mpeg"}
            ),
        )
        assert await audio_cache.fetch(url=URL, name="abando01") is None
        assert not audio_cache.path_for(name="abando01").exists()

    async def test_transport_error_returns_none(self, audio_cache: AudioCache, upstream: FakeUpstream) -> None:
        upstream.fail(url=URL, exc=httpx.ReadTimeout(""))
        assert await audio_cache.fetch(url=URL, name="abando01") is None

    async def test_unrouted_url_returns_none(self, audio_cache: AudioCache) -> None:
        assert await audio_cache.fetch(url=URL, name="abando01") is None

    async def test_no_partial_file_left(
        self, audio_cache: AudioCache, upstream: FakeUpstream, tmp_path: pathlib.Path
    ) -> None:
        upstream.audio(url=URL)
        await audio_cache.fetch(url=URL, name="abando01")
        assert not (tmp_path / "audio" / "abando01.part").exists()
        assert [p.suffix for p in (tmp_path / "audio").iterdir()] == [".mp3"]
