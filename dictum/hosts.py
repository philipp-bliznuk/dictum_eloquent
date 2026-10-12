"""`ALLOWED_HOSTS` parsing and `Host` header matching."""

import re
import typing as t

if t.TYPE_CHECKING:
    import collections.abc as cabc

ANY_HOST: t.Final = "*"
LOOPBACK: t.Final = frozenset({"localhost", "127.0.0.1", "::1"})
"""Always accepted: the container healthcheck uses them and a rebinding page cannot make a browser send them."""
_SEPARATORS = re.compile(r"[\s,;]+")


def host_from_header(*, value: str) -> str:
    """Bare lowercase host from a `Host` header value: port and IPv6 brackets dropped."""
    host = value.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end > 0 else host[1:]
    name, sep, port = host.rpartition(":")
    if sep and port.isdigit() and ":" not in name:
        return name
    return host


def normalize_pattern(*, raw: str) -> str | None:
    """One allow-list entry as typed by a human, reduced to what `host_from_header` produces.

    `" https://Dictum.LOCAL:3000/x "` -> `dictum.local`; `*` and `*.suffix` survive; empty -> None.
    """
    text = raw.strip().strip("\"'").lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0]
    return host_from_header(value=text) or None


def parse_allowed_hosts(*, value: str | cabc.Iterable[str]) -> list[str]:
    """Normalised, de-duplicated patterns from an env string (`,` `;` or whitespace separated) or a list."""
    parts = _SEPARATORS.split(value) if isinstance(value, str) else [str(item) for item in value]
    hosts: dict[str, None] = {}
    for part in parts:
        pattern = normalize_pattern(raw=part)
        if pattern:
            hosts[pattern] = None
    return list(hosts)


class HostAllowList:
    """Request-time matcher for parsed `ALLOWED_HOSTS` patterns."""

    def __init__(self, *, patterns: cabc.Iterable[str]) -> None:
        self.patterns = tuple(patterns)
        self._any = ANY_HOST in self.patterns
        self._exact = frozenset(p for p in self.patterns if not p.startswith("*"))
        self._suffixes = tuple(p[1:] for p in self.patterns if p.startswith("*."))

    def allows(self, *, host: str) -> bool:
        """True for loopback, an exact entry, a `*.suffix` match, or when `*` is listed."""
        if self._any or host in LOOPBACK or host in self._exact:
            return True
        return any(host.endswith(suffix) for suffix in self._suffixes)
