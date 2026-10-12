import logging

import pytest

from dictum.hosts import HostAllowList
from dictum.hosts import host_from_header
from dictum.hosts import normalize_pattern
from dictum.hosts import parse_allowed_hosts
from dictum.middleware import AllowedHostsMiddleware


class TestHostFromHeader:
    @pytest.mark.parametrize(
        ("value", "host"),
        [
            ("dictum.local", "dictum.local"),
            (" Dictum.LOCAL:3000 ", "dictum.local"),
            ("192.168.50.199:3000", "192.168.50.199"),
            ("127.0.0.1", "127.0.0.1"),
            ("[::1]:3000", "::1"),
            ("[fe80::1]", "fe80::1"),
            ("[::1", "::1"),
            ("::1", "::1"),
            ("host:", "host:"),
            ("host:abc", "host:abc"),
            ("", ""),
        ],
    )
    def test_strips_port_and_brackets(self, *, value: str, host: str) -> None:
        assert host_from_header(value=value) == host


class TestNormalizePattern:
    @pytest.mark.parametrize(
        ("raw", "pattern"),
        [
            ("dictum.local", "dictum.local"),
            (" https://Dictum.LOCAL:3000/words/1 ", "dictum.local"),
            ('"192.168.50.199:3000"', "192.168.50.199"),
            ("'[::1]:3000'", "::1"),
            ("*", "*"),
            ("*.lan:3000", "*.lan"),
            ("", None),
            ("   ", None),
            ("http://", None),
            ("''", None),
        ],
    )
    def test_human_input(self, *, raw: str, pattern: str | None) -> None:
        assert normalize_pattern(raw=raw) == pattern


class TestParseAllowedHosts:
    def test_separators(self) -> None:
        assert parse_allowed_hosts(value="a.lan, b.lan;c.lan d.lan\n e.lan,") == [
            "a.lan",
            "b.lan",
            "c.lan",
            "d.lan",
            "e.lan",
        ]

    def test_dedupes_after_normalising(self) -> None:
        assert parse_allowed_hosts(value="A.lan,a.lan:3000,http://a.lan/,b.lan") == ["a.lan", "b.lan"]

    def test_list_input(self) -> None:
        assert parse_allowed_hosts(value=["Dictum.local:3000", "", "*"]) == ["dictum.local", "*"]

    @pytest.mark.parametrize("value", ["", " ,; ", [], [""]])
    def test_empty(self, *, value: str | list[str]) -> None:
        assert parse_allowed_hosts(value=value) == []


class TestHostAllowList:
    def test_wildcard(self) -> None:
        assert HostAllowList(patterns=["*"]).allows(host="evil.example")

    def test_exact(self) -> None:
        allow = HostAllowList(patterns=["dictum.local", "192.168.50.199"])
        assert allow.allows(host="dictum.local")
        assert allow.allows(host="192.168.50.199")
        assert not allow.allows(host="evil.example")
        assert not allow.allows(host="sub.dictum.local")

    @pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
    def test_loopback_always(self, *, host: str) -> None:
        assert HostAllowList(patterns=["dictum.local"]).allows(host=host)
        assert HostAllowList(patterns=[]).allows(host=host)

    def test_suffix_wildcard(self) -> None:
        allow = HostAllowList(patterns=["*.lan"])
        assert allow.allows(host="pi.lan")
        assert allow.allows(host="a.b.lan")
        assert not allow.allows(host="lan")
        assert not allow.allows(host="pi.lan.evil")

    def test_patterns_kept(self) -> None:
        assert HostAllowList(patterns=("a", "b")).patterns == ("a", "b")


class _Recorder:
    def __init__(self) -> None:
        self.scopes: list[dict[str, object]] = []

    async def __call__(self, scope: dict[str, object], receive: object, send: object) -> None:
        self.scopes.append(scope)


class _Sink:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def __call__(self, message: dict[str, object]) -> None:
        self.messages.append(message)


class TestAllowedHostsMiddleware:
    async def test_non_http_passes_through(self) -> None:
        inner = _Recorder()
        middleware = AllowedHostsMiddleware(inner, allowed_hosts=["dictum.local"])
        await middleware({"type": "lifespan"}, None, None)
        assert [s["type"] for s in inner.scopes] == ["lifespan"]

    async def test_missing_host_header_rejected(self, caplog: pytest.LogCaptureFixture) -> None:
        inner = _Recorder()
        middleware = AllowedHostsMiddleware(inner, allowed_hosts=["dictum.local"])
        sink = _Sink()
        with caplog.at_level(logging.WARNING, logger="dictum.hosts"):
            await middleware({"type": "http", "headers": []}, None, sink)
        assert inner.scopes == []
        assert sink.messages[0]["status"] == 400
        assert b"not in ALLOWED_HOSTS" in sink.messages[1]["body"]
        assert "rejected Host ''" in caplog.text
        assert "ALLOWED_HOSTS=dictum.local" in caplog.text

    async def test_matching_host_passes(self) -> None:
        inner = _Recorder()
        middleware = AllowedHostsMiddleware(inner, allowed_hosts=["dictum.local"])
        scope = {"type": "http", "headers": [(b"accept", b"*/*"), (b"Host", b"Dictum.LOCAL:3000")]}
        await middleware(scope, None, None)
        assert inner.scopes == [scope]
