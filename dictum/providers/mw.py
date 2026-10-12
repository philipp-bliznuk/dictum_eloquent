"""Merriam-Webster Collegiate provider.

Payload reference: https://dictionaryapi.com/products/json. A query returns either a list of entry
objects, or a list of suggestion strings when nothing matched.
"""

import dataclasses
import logging
import re
import typing as t
import unicodedata
import urllib.parse

import httpx

from dictum.enums import DefinitionSource
from dictum.enums import ExampleSource
from dictum.enums import SpellingKind
from dictum.providers.base import ProviderError
from dictum.providers.base import ProviderResult
from dictum.providers.base import describe
from dictum.providers.base import merge_by_part_of_speech
from dictum.providers.base import valid_spellings
from dictum.schemas.lookup import DraftDefinition
from dictum.schemas.lookup import DraftEntry
from dictum.schemas.lookup import DraftExample
from dictum.services import spelling as spelling_service
from dictum.services.text import is_clean_sentence

if t.TYPE_CHECKING:
    import collections.abc as cabc

    from dictum.schemas.word import SpellingIn
    from dictum.services.audio import AudioCache

log = logging.getLogger(__name__)

Json = dict[str, t.Any]
Fragments = tuple[list[str], list[str]]

_HOMOGRAPH = re.compile(r":\d+$")
_TOKEN = re.compile(r"\{([^{}]*)\}")
_DROP_BLOCKS = re.compile(r"\{(dx|dx_ety|ma)\}.*?\{/\1\}", re.DOTALL)
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([:;,.)\]])")
_LINKS: t.Final = frozenset({"a_link", "d_link", "i_link", "et_link", "mat", "sx", "dxt"})
_SPELLING_LABELS: t.Final = ("spelling", "variant")
_INFLECTION_POS: t.Final[tuple[tuple[str, str], ...]] = (
    ("plural", "noun"),
    ("comparative", "adjective"),
    ("superlative", "adjective"),
    ("tense", "verb"),
    ("participle", "verb"),
    ("person", "verb"),
)
_TOKENS: t.Final[dict[str, tuple[str, str]]] = {
    "b": ("**", "**"),
    "phrase": ("**", "**"),
    "parahw": ("**", "**"),
    "it": ("*", "*"),
    "wi": ("*", "*"),
    "qword": ("*", "*"),
    "gloss": ("[", "]"),
    "dx_def": ("(", ")"),
    "ldquo": ('"', '"'),
    "rdquo": ('"', '"'),
    "p_br": (" ", " "),
    "bc": (" : ", " : "),
}


def _render_token(match: re.Match[str]) -> str:
    name, _, args = match.group(1).partition("|")
    if name in _LINKS:
        return args.partition("|")[0]
    closing = name.startswith("/")
    return _TOKENS.get(name.lstrip("/"), ("", ""))[closing]


def render(*, text: str) -> str:
    """Convert MW inline tokens (`{bc}`, `{it}..{/it}`, `{sx|word||}`...) to the app's markdown subset."""
    rendered = _TOKEN.sub(_render_token, _DROP_BLOCKS.sub("", text))
    collapsed = _SPACE_BEFORE_PUNCT.sub(r"\1", " ".join(rendered.split()))
    return collapsed.lstrip(": ").strip()


def _expand(*, kind: str, body: Json | list[t.Any]) -> cabc.Iterator[Json]:
    if kind == "sense":
        yield body
    elif kind == "bs":
        yield body["sense"]
    elif kind == "pseq":
        yield from _iter_senses([body])


def _iter_senses(sseq: list[t.Any]) -> cabc.Iterator[Json]:
    """Flatten the nested `sseq` structure.

    Yields:
        Plain sense objects; `sen` stubs without defining text are dropped.
    """
    for group in sseq:
        for kind, body in group:
            yield from _expand(kind=kind, body=body)


def _render_notes(notes: list[t.Any]) -> Fragments:
    texts: list[str] = []
    examples: list[str] = []
    for note in notes:
        more_texts, more_examples = _render_dt(note)
        texts.extend(more_texts)
        examples.extend(more_examples)
    return texts, examples


def _render_dt_item(*, kind: str, body: str | list[t.Any]) -> Fragments:
    if kind == "text":
        return [render(text=body)], []
    if kind == "vis":
        return [], [render(text=v["t"]) for v in body]
    if kind == "uns":
        return _render_notes(body)
    return [], []


def _render_dt(dt: list[t.Any]) -> Fragments:
    """Split a defining-text array into definition fragments and example sentences."""
    texts: list[str] = []
    examples: list[str] = []
    for kind, body in dt:
        more_texts, more_examples = _render_dt_item(kind=kind, body=body)
        texts.extend(more_texts)
        examples.extend(more_examples)
    return texts, examples


def _labelled(*, text: str, labels: list[str]) -> str:
    return f"({', '.join(labels)}) {text}" if labels else text


def _definition(*, sense: Json, labels: list[str]) -> DraftDefinition | None:
    texts, examples = _render_dt(sense.get("dt", []))
    if divided := sense.get("sdsense"):
        sd_texts, sd_examples = _render_dt(divided.get("dt", []))
        texts.append(f"{divided.get('sd', 'also')}: {' '.join(sd_texts)}")
        examples.extend(sd_examples)
    text = "; ".join(fragment for fragment in texts if fragment)
    if not text:
        return None
    return DraftDefinition(
        text=_labelled(text=text, labels=[*labels, *sense.get("sls", [])]),
        source=DefinitionSource.MW,
        examples=[DraftExample(text=e, source=ExampleSource.MW) for e in examples if is_clean_sentence(text=e)],
    )


def _definitions(entry: Json) -> list[DraftDefinition]:
    result: list[DraftDefinition] = []
    for block in entry.get("def", []):
        labels = list(block.get("sls", []))
        senses = _iter_senses(block.get("sseq", []))
        result.extend(definition for sense in senses if (definition := _definition(sense=sense, labels=labels)))
    return result


def _base_id(entry: Json) -> str:
    return _HOMOGRAPH.sub("", entry.get("meta", {}).get("id", "")).casefold()


def _has_definitions(entry: Json) -> bool:
    return bool(entry.get("def"))


def _stems(entry: Json) -> set[str]:
    return {s.casefold() for s in entry.get("meta", {}).get("stems", [])}


def select_entries(*, payload: list[Json], query: str) -> list[Json]:
    """Entries defining `query`: exact headword matches first, else entries listing it among their stems."""
    key = query.casefold()
    exact = [e for e in payload if _has_definitions(e) and _base_id(e) == key]
    if exact:
        return exact
    return [e for e in payload if _has_definitions(e) and key in _stems(e)]


def _headword(entry: Json) -> str:
    return entry.get("hwi", {}).get("hw", "").replace("*", "") or _base_id(entry)


def prefer_headword(*, entries: list[Json], query: str) -> list[Json]:
    """Among homographs sharing an ASCII id (`lame`, `lamé`), keep the ones spelled exactly like `query`.

    MW ids drop diacritics, so an exact id match may be a different word. When no entry is spelled like the
    query (`elan` resolves to `élan` alone), all of them stay.
    """
    wanted = spelling_service.key(text=query)
    exact = [e for e in entries if spelling_service.key(text=_headword(e)) == wanted]
    return exact or entries


def _cross_references(entry: Json) -> cabc.Iterator[tuple[str, str]]:
    """Walk an entry's cognate cross-references (`cxs`).

    Yields:
        `(label, target)` pairs with homograph suffixes stripped from the target.
    """
    for cx in entry.get("cxs", []):
        label = " ".join(str(cx.get("cxl") or "").split())
        for cxt in cx.get("cxtis", []):
            if target := cxt.get("cxt"):
                yield label, _HOMOGRAPH.sub("", target)


def is_spelling_redirect(*, label: str) -> bool:
    """`British spelling of`, `less common spelling of`, `variant of`: the same word under another spelling.

    Every other label (`past tense of`, `plural of`, `archaic past tense second-person singular of`...)
    points at a different word's inflection and must not be followed.
    """
    folded = label.casefold()
    return any(marker in folded for marker in _SPELLING_LABELS)


def inflection_part_of_speech(*, label: str) -> str:
    """Part of speech implied by a cross-reference label (`plural of` -> noun, `past tense of` -> verb)."""
    folded = label.casefold()
    return next((pos for marker, pos in _INFLECTION_POS if marker in folded), "other")


def _stubs(*, payload: list[Json], query: str) -> cabc.Iterator[tuple[Json, str, str]]:
    key = query.casefold()
    for entry in payload:
        if _has_definitions(entry) or _base_id(entry) != key:
            continue
        for label, target in _cross_references(entry):
            yield entry, label, target


def cognate_target(*, payload: list[Json], query: str) -> str | None:
    """Headword a definition-less exact entry redirects to (`colour` -> `color`); spelling redirects only."""
    stubs = _stubs(payload=payload, query=query)
    return next((target for _, label, target in stubs if is_spelling_redirect(label=label)), None)


def inflection_stubs(*, payload: list[Json], query: str) -> list[tuple[Json, str, str]]:
    """Definition-less exact entries that are inflections of another headword (`couldst` -> `can`).

    They keep their own pronunciation and audio, so they become drafts of their own instead of a redirect.
    """
    stubs = _stubs(payload=payload, query=query)
    return [(entry, label, target) for entry, label, target in stubs if not is_spelling_redirect(label=label)]


def ascii_fold(*, text: str) -> str:
    """Strip diacritics (`crèche` -> `creche`); MW resolves ASCII queries only."""
    decomposed = unicodedata.normalize("NFD", text)
    return unicodedata.normalize("NFC", "".join(c for c in decomposed if unicodedata.category(c) != "Mn"))


def _spellings(*, entry: Json, extra: str | None) -> list[SpellingIn]:
    headword = _headword(entry)
    candidates = [(headword, SpellingKind.PRIMARY), (ascii_fold(text=headword), SpellingKind.VARIANT)]
    candidates += [(v["va"].replace("*", ""), SpellingKind.VARIANT) for v in entry.get("vrs", []) if v.get("va")]
    if extra:
        candidates.append((extra, SpellingKind.VARIANT))
    return valid_spellings(candidates=candidates)


def _is_complete(*, respelling: str) -> bool:
    """Whether a respelling stands on its own rather than continuing or preceding another form."""
    return bool(respelling) and not respelling.startswith("-") and not respelling.endswith("-")


def _pronunciation(prs: list[Json]) -> tuple[str | None, str | None]:
    """Respelling and audio reference from `hwi.prs`.

    The first complete respelling that carries a sound file wins (`couldst` lists its weak form first but
    voices the stressed one); otherwise the first complete respelling. Partial forms, which start or end with
    `-` (`nascent` voices `'nA-` before spelling out `'na-s&nt`), are skipped.
    """
    complete = [p for p in prs if _is_complete(respelling=p.get("mw", ""))]
    voiced = [p for p in complete if p.get("sound", {}).get("audio")]
    text = next((p["mw"] for p in [*voiced, *complete]), None)
    audio = next((p["sound"]["audio"] for p in prs if p.get("sound", {}).get("audio")), None)
    return text, audio


def audio_url(*, base_url: str, ref: str) -> str:
    """Media URL for an audio reference; subdirectory rules per MW docs."""
    if ref.startswith("bix"):
        subdir = "bix"
    elif ref.startswith("gg"):
        subdir = "gg"
    elif ref[:1].isalpha():
        subdir = ref[0]
    else:
        subdir = "number"
    return f"{base_url}{subdir}/{ref}.mp3"


def _head(draft: DraftEntry) -> str:
    return spelling_service.key(text=draft.spellings[0].text) if draft.spellings else ""


def run_on_candidates(*, entry: Json, draft: DraftEntry) -> list[tuple[str, list[Json]]]:
    """Undefined run-ons (`uros`) spelled exactly like the headword, as `(part of speech, prs)` pairs.

    MW lists `nitpick` the noun only as a run-on under the verb, without definitions. Derived words
    (`nitpicker`, `nitpicky`) are skipped: they are separate headwords, not another part of speech.
    """
    head = _head(draft)
    result: list[tuple[str, list[Json]]] = []
    for uro in entry.get("uros", []):
        ure = str(uro.get("ure", "")).replace("*", "")
        pos = str(uro.get("fl") or "")
        if pos and spelling_service.key(text=ure) == head:
            result.append((pos, uro.get("prs", [])))
    return result


def _share_pronunciation(drafts: list[DraftEntry]) -> list[DraftEntry]:
    """Homographs carry the sound once; copy pronunciation and audio to siblings with the same headword."""
    known: dict[str, tuple[str | None, str | None]] = {}
    for draft in drafts:
        pron, audio = known.get(_head(draft), (None, None))
        known[_head(draft)] = (pron or draft.pronunciation, audio or draft.audio_path)
    for draft in drafts:
        pron, audio = known[_head(draft)]
        draft.pronunciation = draft.pronunciation or pron
        draft.audio_path = draft.audio_path or audio
    return drafts


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class MWConfig:
    """Endpoint settings for `MWProvider`."""

    api_key: str
    base_url: str
    audio_base_url: str


class MWProvider:
    """Merriam-Webster Collegiate dictionary (API key required, 1000 queries/day on the free tier)."""

    name = "mw"

    def __init__(self, *, client: httpx.AsyncClient, config: MWConfig, audio_cache: AudioCache) -> None:
        self._client = client
        self._config = config
        self._audio_cache = audio_cache

    def _redact(self, *, message: str) -> str:
        key = self._config.api_key
        return message.replace(key, "***") if key else message

    async def _fetch(self, *, query: str) -> list[t.Any]:
        url = f"{self._config.base_url}{urllib.parse.quote(query, safe='')}"
        try:
            response = await self._client.get(url, params={"key": self._config.api_key})
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            msg = self._redact(message=f"mw: {describe(exc=exc)}")
            raise ProviderError(msg) from exc
        if not isinstance(payload, list):
            msg = "mw: unexpected payload shape"
            raise ProviderError(msg)
        return payload

    async def _fetch_entries(self, *, query: str) -> tuple[list[Json], list[str]]:
        """Split a response into entry dicts and spelling suggestions (MW returns one or the other)."""
        payload = await self._fetch(query=query)
        entries = [e for e in payload if isinstance(e, dict)]
        suggestions = [s for s in payload if isinstance(s, str)]
        return entries, suggestions

    async def _audio(self, *, ref: str | None) -> str | None:
        if not ref:
            return None
        return await self._audio_cache.fetch(url=audio_url(base_url=self._config.audio_base_url, ref=ref), name=ref)

    async def _build(
        self, *, entry: Json, variant: str | None, part_of_speech: str, definitions: list[DraftDefinition]
    ) -> DraftEntry:
        pronunciation, audio_ref = _pronunciation(entry.get("hwi", {}).get("prs", []))
        return DraftEntry(
            spellings=_spellings(entry=entry, extra=variant),
            part_of_speech=part_of_speech,
            pronunciation=pronunciation,
            audio_path=await self._audio(ref=audio_ref),
            definitions=definitions,
            source=DefinitionSource.MW,
        )

    async def _draft(self, *, entry: Json, variant: str | None) -> DraftEntry:
        part_of_speech = entry.get("fl") or "phrase"
        return await self._build(
            entry=entry, variant=variant, part_of_speech=part_of_speech, definitions=_definitions(entry)
        )

    async def _stub_draft(self, *, entry: Json, label: str, target: str) -> DraftEntry:
        """Draft for an inflection stub: its own sound, one definition pointing at the base word."""
        definition = DraftDefinition(text=f"{label} *{target}*", source=DefinitionSource.MW)
        part_of_speech = inflection_part_of_speech(label=label)
        return await self._build(entry=entry, variant=None, part_of_speech=part_of_speech, definitions=[definition])

    async def _stub_drafts(self, *, entries: list[Json], query: str) -> list[DraftEntry]:
        stubs = inflection_stubs(payload=entries, query=query)
        drafts = [await self._stub_draft(entry=entry, label=label, target=target) for entry, label, target in stubs]
        return [draft for draft in drafts if draft.spellings]

    async def _run_ons(self, *, entry: Json, draft: DraftEntry) -> list[DraftEntry]:
        """Empty drafts for the headword's other parts of speech, to be filled by AI drafting later."""
        drafts: list[DraftEntry] = []
        for pos, prs in run_on_candidates(entry=entry, draft=draft):
            pronunciation, audio_ref = _pronunciation(prs)
            drafts.append(
                DraftEntry(
                    spellings=list(draft.spellings),
                    part_of_speech=pos,
                    pronunciation=pronunciation,
                    audio_path=await self._audio(ref=audio_ref),
                    definitions=[],
                    source=DefinitionSource.MW,
                )
            )
        return drafts

    async def _drafts(self, *, selected: list[Json], variant: str | None) -> list[DraftEntry]:
        drafts: list[DraftEntry] = []
        for entry in selected:
            draft = await self._draft(entry=entry, variant=variant)
            if draft.spellings:
                drafts.append(draft)
                drafts.extend(await self._run_ons(entry=entry, draft=draft))
        return drafts

    async def _resolve(self, *, query: str) -> tuple[str, list[Json], list[str]]:
        """Fetch `query`; MW resolves ASCII only, so an accented query falls back to its folded form."""
        entries, suggestions = await self._fetch_entries(query=query)
        folded = ascii_fold(text=query)
        if entries or folded == query:
            return query, entries, suggestions
        entries, more = await self._fetch_entries(query=folded)
        return folded, entries, suggestions or more

    async def lookup(self, *, text: str) -> ProviderResult:
        """Resolve `text`; follows one spelling redirect (`colour` -> `color`) keeping the query as a variant.

        Homographs are folded into one draft per part of speech; run-ons spelled like the headword
        (`nitpick` noun under the verb) come back as drafts without definitions. Inflection stubs
        (`couldst`, `didst`) are drafted on their own instead of being redirected to the base word, and
        accented queries (`crèche`) are retried without diacritics. Accented homographs sharing an ASCII
        id (`lame` / `lamé`) are kept apart: only entries spelled like the query are drafted.
        """
        original = spelling_service.normalize(text=text)
        query, entries, suggestions = await self._resolve(query=original)
        if not entries:
            return ProviderResult(suggestions=suggestions)
        target = cognate_target(payload=entries, query=query)
        selected = prefer_headword(entries=select_entries(payload=entries, query=query), query=original)
        if not selected and target:
            redirected, _ = await self._fetch_entries(query=target)
            selected = prefer_headword(entries=select_entries(payload=redirected, query=target), query=target)
        drafts = await self._drafts(selected=selected, variant=query if target else None)
        drafts = drafts or await self._stub_drafts(entries=entries, query=query)
        return ProviderResult(entries=merge_by_part_of_speech(entries=_share_pronunciation(drafts)))
