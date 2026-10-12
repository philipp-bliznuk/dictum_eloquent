import typing as t

import fastapi
import pytest

from dictum.dependencies.lookup import get_lookup
from tests.conftest import MP3_BYTES
from tests.conftest import MW_API_KEY
from tests.conftest import free_dictionary_url
from tests.conftest import lexeme_payload
from tests.conftest import make_settings
from tests.conftest import mw_audio_url
from tests.conftest import mw_fixture
from tests.conftest import mw_url
from tests.conftest import word_payload

if t.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib

    import httpx

    from dictum.settings import Settings
    from tests.conftest import FakeUpstream

Json = dict[str, t.Any]

STRESS: t.Final = "\u02c8"
"""MW primary stress mark (MODIFIER LETTER VERTICAL LINE); escaped so it is not mistaken for a backtick."""
NOT_FOUND_BODY: t.Final = {"title": "No Definitions Found", "message": "Sorry pal", "resolution": "Try again"}


@pytest.fixture
def settings(tmp_path: pathlib.Path) -> Settings:
    return make_settings(tmp_path=tmp_path, mw_api_key=MW_API_KEY)


def serve_mw(upstream: FakeUpstream, *, query: str, name: str | None = None, audio: cabc.Iterable[str] = ()) -> None:
    upstream.json(url=mw_url(query=query), payload=mw_fixture(name=name or query))
    for ref in audio:
        upstream.audio(url=mw_audio_url(ref=ref))


def serve_free(upstream: FakeUpstream, *, query: str, payload: object = None) -> None:
    """dictionaryapi.dev answer for `query`; `None` serves its 404 body."""
    if payload is None:
        upstream.json(url=free_dictionary_url(query=query), payload=NOT_FOUND_BODY, status_code=404)
    else:
        upstream.json(url=free_dictionary_url(query=query), payload=payload)


def free_item(word: str = "abandon") -> Json:
    example = "They abandon the ship."
    return {
        "word": word,
        "phonetics": [],
        "meanings": [{"partOfSpeech": "verb", "definitions": [{"definition": "To give up", "example": example}]}],
    }


def creche_entry() -> Json:
    """MW noun entry for `crèche`: accented headword under an ASCII `meta.id`, one voiced respelling."""
    return {
        "meta": {"id": "creche", "stems": ["creche", "creches"]},
        "hwi": {"hw": "crèche", "prs": [{"mw": f"{STRESS}kresh", "sound": {"audio": "creche02"}}]},
        "fl": "noun",
        "def": [{"sseq": [[["sense", {"dt": [["text", "{bc}a day nursery"]]}]]]}],
    }


async def post_word(client: httpx.AsyncClient, **overrides: t.Any) -> Json:
    response = await client.post("/api/words", json=word_payload(**overrides))
    assert response.status_code == 201, response.text
    return response.json()


class TestLookupDependency:
    def test_returns_app_state_service(self, app: fastapi.FastAPI) -> None:
        request = fastapi.Request({"type": "http", "app": app})
        assert get_lookup(request) is app.state.lookup


class TestLookupEndpoint:
    async def test_found_via_mw(self, client: httpx.AsyncClient, upstream: FakeUpstream) -> None:
        serve_mw(upstream, query="abandon", audio=["abando01"])
        response = await client.post("/api/lookup", json={"text": "abandon"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["query"] == "abandon"
        assert body["found"] is True
        assert body["suggestions"] == []
        assert body["attempts"] == [{"provider": "mw", "outcome": "found", "detail": None}]
        assert body["existing_word_id"] is None
        assert len(body["entries"]) == 2
        verb = body["entries"][0]
        assert verb["spellings"] == [{"text": "abandon", "kind": "primary"}]
        assert verb["part_of_speech"] == "verb"
        assert verb["pronunciation"] == f"ə-{STRESS}ban-dən"
        assert verb["audio_path"] == "/audio/abando01.mp3"
        assert verb["source"] == "mw"
        assert verb["definitions"][1] == {
            "text": "to give up with the intent of never again claiming a right or interest in",
            "source": "mw",
            "examples": [],
        }
        assert all(d["examples"] == [] for e in body["entries"] for d in e["definitions"])
        assert body["entries"][1]["part_of_speech"] == "noun"
        assert upstream.urls == [mw_url(query="abandon"), mw_audio_url(ref="abando01")]

    async def test_outbound_requests_identify_the_app(self, client: httpx.AsyncClient, upstream: FakeUpstream) -> None:
        serve_mw(upstream, query="abandon", audio=["abando01"])
        await client.post("/api/lookup", json={"text": "abandon"})
        assert {r.headers["user-agent"] for r in upstream.requests} == {"dictum-eloquent/0.1"}
        assert upstream.requests[0].url.params["key"] == MW_API_KEY

    async def test_audio_served_after_lookup(self, client: httpx.AsyncClient, upstream: FakeUpstream) -> None:
        serve_mw(upstream, query="abandon", audio=["abando01"])
        body = (await client.post("/api/lookup", json={"text": "abandon"})).json()
        response = await client.get(body["entries"][0]["audio_path"])
        assert response.status_code == 200
        assert response.content == MP3_BYTES
        assert response.headers["content-type"] == "audio/mpeg"

    async def test_text_normalised(self, client: httpx.AsyncClient, upstream: FakeUpstream) -> None:
        serve_mw(upstream, query="put paid to", name="put_paid_to")
        response = await client.post("/api/lookup", json={"text": "  put \t  paid to "})
        assert response.status_code == 200
        body = response.json()
        assert body["query"] == "put paid to"
        assert body["found"] is True
        assert body["entries"][0]["spellings"] == [{"text": "put paid to", "kind": "primary"}]
        assert body["entries"][0]["pronunciation"] is None
        assert body["entries"][0]["audio_path"] is None
        assert upstream.requests[0].url.raw_path == b"/json/put%20paid%20to?key=test-key"

    @pytest.mark.parametrize(
        ("body", "fragment"),
        [
            ({"text": ""}, "at least 1"),
            ({"text": "   "}, "visible characters"),
            ({"text": "Color / Colour"}, "must not contain ' / '"),
            ({"text": "x" * 256}, "at most 255"),
            ({"text": None}, "text"),
            ({}, "text"),
        ],
    )
    async def test_invalid_body_returns_422(
        self, client: httpx.AsyncClient, upstream: FakeUpstream, *, body: Json, fragment: str
    ) -> None:
        response = await client.post("/api/lookup", json=body)
        assert response.status_code == 422
        assert fragment in response.text
        assert upstream.requests == []

    async def test_existing_word_id_for_query(self, client: httpx.AsyncClient, upstream: FakeUpstream) -> None:
        created = await post_word(client, spellings=[{"text": "Abandon"}])
        serve_mw(upstream, query="ABANDON", name="abandon", audio=["abando01"])
        response = await client.post("/api/lookup", json={"text": "ABANDON"})
        assert response.status_code == 200
        assert response.json()["existing_word_id"] == created["id"]

    async def test_existing_word_id_from_drafted_spelling(
        self, client: httpx.AsyncClient, upstream: FakeUpstream
    ) -> None:
        created = await post_word(client, spellings=[{"text": "Aggrandize"}])
        serve_mw(upstream, query="aggrandise")
        serve_mw(upstream, query="aggrandize", audio=["aggran01"])
        body = (await client.post("/api/lookup", json={"text": "aggrandise"})).json()
        assert body["found"] is True
        assert body["entries"][0]["spellings"] == [
            {"text": "aggrandize", "kind": "primary"},
            {"text": "aggrandise", "kind": "variant"},
        ]
        assert body["existing_word_id"] == created["id"]

    async def test_existing_word_id_when_not_found(self, client: httpx.AsyncClient, upstream: FakeUpstream) -> None:
        created = await post_word(client, spellings=[{"text": "abandn"}])
        serve_mw(upstream, query="abandn")
        serve_free(upstream, query="abandn")
        body = (await client.post("/api/lookup", json={"text": "abandn"})).json()
        assert body["found"] is False
        assert body["existing_word_id"] == created["id"]

    async def test_existing_word_id_null_for_other_words(
        self, client: httpx.AsyncClient, upstream: FakeUpstream
    ) -> None:
        await post_word(client, spellings=[{"text": "Betray"}])
        serve_mw(upstream, query="abandon", audio=["abando01"])
        body = (await client.post("/api/lookup", json={"text": "abandon"})).json()
        assert body["existing_word_id"] is None

    async def test_not_found_with_suggestions(self, client: httpx.AsyncClient, upstream: FakeUpstream) -> None:
        serve_mw(upstream, query="abandn")
        serve_free(upstream, query="abandn")
        response = await client.post("/api/lookup", json={"text": "abandn"})
        assert response.status_code == 200
        body = response.json()
        assert body == {
            "query": "abandn",
            "found": False,
            "entries": [],
            "suggestions": mw_fixture(name="abandn"),
            "attempts": [
                {"provider": "mw", "outcome": "not_found", "detail": None},
                {"provider": "free_dict", "outcome": "not_found", "detail": None},
            ],
            "existing_word_id": None,
        }
        assert upstream.urls == [mw_url(query="abandn"), free_dictionary_url(query="abandn")]

    async def test_mw_error_falls_back_to_free_dict(self, client: httpx.AsyncClient, upstream: FakeUpstream) -> None:
        upstream.text(url=mw_url(query="abandon"), body="boom", status_code=500)
        serve_free(upstream, query="abandon", payload=[free_item()])
        response = await client.post("/api/lookup", json={"text": "abandon"})
        assert response.status_code == 200
        body = response.json()
        assert body["found"] is True
        assert [a["provider"] for a in body["attempts"]] == ["mw", "free_dict"]
        assert body["attempts"][0]["outcome"] == "error"
        assert body["attempts"][0]["detail"] == "mw: HTTP 500"
        assert body["attempts"][1] == {"provider": "free_dict", "outcome": "found", "detail": None}
        assert body["entries"][0]["source"] == "free_dict"
        assert body["entries"][0]["definitions"][0]["examples"] == [
            {"text": "They *abandon* the ship.", "source": "free_dict"}
        ]

    async def test_all_providers_fail_returns_502(self, client: httpx.AsyncClient, upstream: FakeUpstream) -> None:
        upstream.text(url=mw_url(query="abandon"), body="boom", status_code=500)
        upstream.text(url=free_dictionary_url(query="abandon"), body="down", status_code=503)
        response = await client.post("/api/lookup", json={"text": "abandon"})
        assert response.status_code == 502
        detail = response.json()["detail"]
        assert detail == "mw: HTTP 500; free_dict: HTTP 503"

    async def test_unreachable_upstreams_return_502(self, client: httpx.AsyncClient, upstream: FakeUpstream) -> None:
        response = await client.post("/api/lookup", json={"text": "abandon"})
        assert response.status_code == 502
        assert response.json()["detail"].startswith("mw: no fake route for ")
        assert upstream.urls == [mw_url(query="abandon"), free_dictionary_url(query="abandon")]

    async def test_error_details_hide_api_key(self, client: httpx.AsyncClient, upstream: FakeUpstream) -> None:
        upstream.text(url=mw_url(query="abandon"), body="boom", status_code=500)
        serve_free(upstream, query="abandon", payload=[free_item()])
        response = await client.post("/api/lookup", json={"text": "abandon"})
        assert response.status_code == 200
        assert MW_API_KEY not in response.text

    async def test_inflection_stub_found_via_mw(self, client: httpx.AsyncClient, upstream: FakeUpstream) -> None:
        serve_mw(upstream, query="couldst", audio=["coulds01"])
        response = await client.post("/api/lookup", json={"text": "couldst"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["query"] == "couldst"
        assert body["found"] is True
        assert body["suggestions"] == []
        assert body["attempts"] == [{"provider": "mw", "outcome": "found", "detail": None}]
        assert body["existing_word_id"] is None
        assert body["entries"] == [
            {
                "spellings": [{"text": "couldst", "kind": "primary"}],
                "part_of_speech": "verb",
                "pronunciation": f"{STRESS}ku\u0307dst",
                "audio_path": "/audio/coulds01.mp3",
                "definitions": [
                    {"text": "archaic past tense second-person singular of *can*", "source": "mw", "examples": []}
                ],
                "source": "mw",
            }
        ]
        assert upstream.urls == [mw_url(query="couldst"), mw_audio_url(ref="coulds01")]

    async def test_accented_query_retried_without_diacritics(
        self, client: httpx.AsyncClient, upstream: FakeUpstream
    ) -> None:
        created = await post_word(client, spellings=[{"text": "Creche"}])
        upstream.json(url=mw_url(query="Crèche"), payload=["creche", "crèches"])
        upstream.json(url=mw_url(query="Creche"), payload=[creche_entry()])
        upstream.audio(url=mw_audio_url(ref="creche02"))
        response = await client.post("/api/lookup", json={"text": " Crèche "})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["query"] == "Crèche"
        assert body["found"] is True
        assert body["suggestions"] == []
        assert body["attempts"] == [{"provider": "mw", "outcome": "found", "detail": None}]
        [noun] = body["entries"]
        assert noun["spellings"] == [{"text": "crèche", "kind": "primary"}, {"text": "creche", "kind": "variant"}]
        assert noun["part_of_speech"] == "noun"
        assert noun["pronunciation"] == f"{STRESS}kresh"
        assert noun["audio_path"] == "/audio/creche02.mp3"
        assert noun["definitions"] == [{"text": "a day nursery", "source": "mw", "examples": []}]
        assert body["existing_word_id"] == created["id"]
        assert upstream.urls == [mw_url(query="Crèche"), mw_url(query="Creche"), mw_audio_url(ref="creche02")]
        assert upstream.requests[0].url.raw_path == b"/json/Cr%C3%A8che?key=test-key"

    async def test_accented_query_unknown_under_both_spellings(
        self, client: httpx.AsyncClient, upstream: FakeUpstream
    ) -> None:
        upstream.json(url=mw_url(query="crèche"), payload=[])
        upstream.json(url=mw_url(query="creche"), payload=["creche", "crèches"])
        serve_free(upstream, query="crèche")
        response = await client.post("/api/lookup", json={"text": "crèche"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["found"] is False
        assert body["entries"] == []
        assert body["suggestions"] == ["creche", "crèches"]
        assert [a["outcome"] for a in body["attempts"]] == ["not_found", "not_found"]
        assert upstream.urls == [mw_url(query="crèche"), mw_url(query="creche"), free_dictionary_url(query="crèche")]


class TestLookupWithoutMWKey:
    @pytest.fixture
    def settings(self, tmp_path: pathlib.Path) -> Settings:
        return make_settings(tmp_path=tmp_path)

    async def test_only_free_dict_is_asked(self, client: httpx.AsyncClient, upstream: FakeUpstream) -> None:
        serve_free(upstream, query="abandon", payload=[free_item()])
        body = (await client.post("/api/lookup", json={"text": "abandon"})).json()
        assert body["found"] is True
        assert body["attempts"] == [{"provider": "free_dict", "outcome": "found", "detail": None}]
        assert upstream.urls == [free_dictionary_url(query="abandon")]

    async def test_not_found_has_no_suggestions(self, client: httpx.AsyncClient, upstream: FakeUpstream) -> None:
        serve_free(upstream, query="abandn")
        body = (await client.post("/api/lookup", json={"text": "abandn"})).json()
        assert body["found"] is False
        assert body["suggestions"] == []
        assert body["attempts"] == [{"provider": "free_dict", "outcome": "not_found", "detail": None}]


def zorb_entry(*, part_of_speech: str, ref: str | None, respelling: str | None = None, headword: str = "zorb") -> Json:
    """Minimal MW entry for `zorb` with one definition and, when `ref` is given, pronunciation audio."""
    prs: list[Json] = []
    if ref or respelling:
        prs.append({"mw": respelling, "sound": {"audio": ref}} if ref else {"mw": respelling})
    return {
        "meta": {"id": f"{headword}:{part_of_speech}", "stems": [headword, "zorbing"]},
        "hwi": {"hw": headword, "prs": prs},
        "fl": part_of_speech,
        "def": [{"sseq": [[["sense", {"dt": [["text", "{bc}a thing"]]}]]]}],
    }


def lexemes_of(body: Json) -> list[tuple[str, str | None, str | None]]:
    """`(part_of_speech, pronunciation, audio_path)` per lexeme of a word response."""
    return [(x["part_of_speech"], x["pronunciation"], x["audio_path"]) for x in body["lexemes"]]


class TestFetchAudioEndpoint:
    async def test_fills_audio_and_missing_pronunciation(
        self, client: httpx.AsyncClient, upstream: FakeUpstream
    ) -> None:
        created = await post_word(client, spellings=[{"text": "abandon"}], lexemes=[lexeme_payload(pronunciation=None)])
        serve_mw(upstream, query="abandon", audio=["abando01"])
        response = await client.post(f"/api/words/{created['id']}/audio")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["id"] == created["id"]
        assert lexemes_of(body) == [("verb", f"ə-{STRESS}ban-dən", "/audio/abando01.mp3")]
        assert body["spellings"] == created["spellings"]
        assert body["lexemes"][0]["definitions"] == created["lexemes"][0]["definitions"]
        assert body["examples"] == created["examples"]
        reloaded = (await client.get(f"/api/words/{created['id']}")).json()
        assert lexemes_of(reloaded) == [("verb", f"ə-{STRESS}ban-dən", "/audio/abando01.mp3")]
        assert (await client.get("/audio/abando01.mp3")).content == MP3_BYTES

    async def test_keeps_existing_pronunciation(
        self, client: httpx.AsyncClient, word_id: int, upstream: FakeUpstream
    ) -> None:
        serve_mw(upstream, query="Abandon", name="abandon", audio=["abando01"])
        response = await client.post(f"/api/words/{word_id}/audio")
        assert response.status_code == 200
        assert lexemes_of(response.json()) == [("verb", "uh-BAN-dun", "/audio/abando01.mp3")]

    async def test_each_lexeme_gets_audio_of_its_own_part_of_speech(
        self, client: httpx.AsyncClient, upstream: FakeUpstream
    ) -> None:
        lexemes = [
            lexeme_payload(part_of_speech="verb", pronunciation=None),
            lexeme_payload(part_of_speech="Noun", pronunciation=None),
        ]
        created = await post_word(client, spellings=[{"text": "zorb"}], lexemes=lexemes)
        payload = [
            zorb_entry(part_of_speech="noun", ref="zorb0001", respelling="zorb-n"),
            zorb_entry(part_of_speech="verb", ref="zorb0002", respelling="zorb-v"),
        ]
        upstream.json(url=mw_url(query="zorb"), payload=payload)
        upstream.audio(url=mw_audio_url(ref="zorb0001", subdir="z"))
        upstream.audio(url=mw_audio_url(ref="zorb0002", subdir="z"))
        response = await client.post(f"/api/words/{created['id']}/audio")
        assert response.status_code == 200, response.text
        assert lexemes_of(response.json()) == [
            ("verb", "zorb-v", "/audio/zorb0002.mp3"),
            ("noun", "zorb-n", "/audio/zorb0001.mp3"),
        ]

    async def test_unmatched_part_of_speech_falls_back_to_first_entry_with_audio(
        self, client: httpx.AsyncClient, upstream: FakeUpstream
    ) -> None:
        lexemes = [
            lexeme_payload(part_of_speech="adjective", pronunciation=None),
            lexeme_payload(part_of_speech="verb", pronunciation="kept"),
        ]
        created = await post_word(client, spellings=[{"text": "zorb"}], lexemes=lexemes)
        payload = [
            zorb_entry(part_of_speech="verb", ref="zorb0002", respelling="zorb-v"),
            zorb_entry(part_of_speech="adverb", ref="zorb0003", respelling="zorb-a"),
        ]
        upstream.json(url=mw_url(query="zorb"), payload=payload)
        upstream.audio(url=mw_audio_url(ref="zorb0002", subdir="z"))
        upstream.audio(url=mw_audio_url(ref="zorb0003", subdir="z"))
        response = await client.post(f"/api/words/{created['id']}/audio")
        assert response.status_code == 200, response.text
        assert lexemes_of(response.json()) == [
            ("adjective", "zorb-v", "/audio/zorb0002.mp3"),
            ("verb", "kept", "/audio/zorb0002.mp3"),
        ]

    async def test_only_lexemes_without_audio_are_touched(
        self, client: httpx.AsyncClient, upstream: FakeUpstream
    ) -> None:
        lexemes = [
            lexeme_payload(part_of_speech="noun", pronunciation="mine", audio_path="/audio/mine.mp3"),
            lexeme_payload(part_of_speech="verb", pronunciation=None),
        ]
        created = await post_word(client, spellings=[{"text": "zorb"}], lexemes=lexemes)
        payload = [
            zorb_entry(part_of_speech="noun", ref="zorb0001", respelling="zorb-n"),
            zorb_entry(part_of_speech="verb", ref="zorb0002", respelling="zorb-v"),
        ]
        upstream.json(url=mw_url(query="zorb"), payload=payload)
        upstream.audio(url=mw_audio_url(ref="zorb0001", subdir="z"))
        upstream.audio(url=mw_audio_url(ref="zorb0002", subdir="z"))
        response = await client.post(f"/api/words/{created['id']}/audio")
        assert response.status_code == 200, response.text
        assert lexemes_of(response.json()) == [
            ("noun", "mine", "/audio/mine.mp3"),
            ("verb", "zorb-v", "/audio/zorb0002.mp3"),
        ]

    async def test_all_lexemes_with_audio_returns_word_unchanged_without_lookup(
        self, client: httpx.AsyncClient, upstream: FakeUpstream
    ) -> None:
        lexemes = [
            lexeme_payload(part_of_speech="noun", audio_path="/audio/a.mp3"),
            lexeme_payload(part_of_speech="verb", pronunciation=None, audio_path="/audio/b.mp3"),
        ]
        created = await post_word(client, spellings=[{"text": "zorb"}], lexemes=lexemes)
        response = await client.post(f"/api/words/{created['id']}/audio")
        assert response.status_code == 200
        assert response.json() == created
        assert upstream.requests == []

    async def test_word_without_lexemes_returns_unchanged_without_lookup(
        self, client: httpx.AsyncClient, upstream: FakeUpstream
    ) -> None:
        created = await post_word(client, lexemes=[])
        response = await client.post(f"/api/words/{created['id']}/audio")
        assert response.status_code == 200
        assert response.json() == created
        assert upstream.requests == []

    async def test_looks_up_primary_spelling(self, client: httpx.AsyncClient, upstream: FakeUpstream) -> None:
        spellings = [{"text": "aggrandise", "kind": "variant"}, {"text": "aggrandize", "kind": "primary"}]
        created = await post_word(client, spellings=spellings)
        serve_mw(upstream, query="aggrandize", audio=["aggran01"])
        response = await client.post(f"/api/words/{created['id']}/audio")
        assert response.status_code == 200
        assert response.json()["lexemes"][0]["audio_path"] == "/audio/aggran01.mp3"
        assert upstream.urls == [mw_url(query="aggrandize"), mw_audio_url(ref="aggran01")]

    async def test_falls_back_to_first_spelling(self, client: httpx.AsyncClient, upstream: FakeUpstream) -> None:
        spellings = [{"text": "troth", "kind": "variant"}, {"text": "trothe", "kind": "variant"}]
        created = await post_word(client, spellings=spellings)
        serve_mw(upstream, query="troth", audio=["troth001"])
        response = await client.post(f"/api/words/{created['id']}/audio")
        assert response.status_code == 200
        assert response.json()["lexemes"][0]["audio_path"] == "/audio/troth001.mp3"
        assert upstream.urls[0] == mw_url(query="troth")

    async def test_skips_entries_without_audio(self, client: httpx.AsyncClient, upstream: FakeUpstream) -> None:
        created = await post_word(client, spellings=[{"text": "zorbing"}], lexemes=[lexeme_payload(pronunciation=None)])
        silent = {"meta": {"id": "zorb", "stems": ["zorbing"]}, "hwi": {"hw": "zorb"}, "fl": "noun", "def": [{}]}
        voiced = {
            "meta": {"id": "zorbs", "stems": ["zorbing"]},
            "hwi": {"hw": "zorbs", "prs": [{"mw": "zorbz", "sound": {"audio": "zorb0001"}}]},
            "fl": "noun",
            "def": [{}],
        }
        upstream.json(url=mw_url(query="zorbing"), payload=[silent, voiced])
        upstream.audio(url=mw_audio_url(ref="zorb0001", subdir="z"))
        response = await client.post(f"/api/words/{created['id']}/audio")
        assert response.status_code == 200
        assert lexemes_of(response.json()) == [("verb", "zorbz", "/audio/zorb0001.mp3")]

    async def test_404_when_no_entry_has_audio(self, client: httpx.AsyncClient, upstream: FakeUpstream) -> None:
        created = await post_word(client, spellings=[{"text": "put paid to"}])
        serve_mw(upstream, query="put paid to", name="put_paid_to")
        response = await client.post(f"/api/words/{created['id']}/audio")
        assert response.status_code == 404
        assert response.json() == {"detail": "No pronunciation audio available"}
        reloaded = (await client.get(f"/api/words/{created['id']}")).json()
        assert lexemes_of(reloaded) == [("verb", "uh-BAN-dun", None)]

    async def test_404_when_word_unknown_upstream(self, client: httpx.AsyncClient, upstream: FakeUpstream) -> None:
        created = await post_word(client, spellings=[{"text": "abandn"}])
        serve_mw(upstream, query="abandn")
        serve_free(upstream, query="abandn")
        response = await client.post(f"/api/words/{created['id']}/audio")
        assert response.status_code == 404
        assert response.json() == {"detail": "No pronunciation audio available"}

    async def test_404_when_audio_download_fails(self, client: httpx.AsyncClient, upstream: FakeUpstream) -> None:
        created = await post_word(client, spellings=[{"text": "abandon"}])
        serve_mw(upstream, query="abandon")
        upstream.text(url=mw_audio_url(ref="abando01"), body="gone", status_code=404)
        serve_free(upstream, query="abandon", payload=[free_item()])
        response = await client.post(f"/api/words/{created['id']}/audio")
        assert response.status_code == 404
        assert response.json() == {"detail": "No pronunciation audio available"}
        assert upstream.urls == [mw_url(query="abandon"), mw_audio_url(ref="abando01")]

    async def test_404_leaves_partial_lexemes_untouched(
        self, client: httpx.AsyncClient, upstream: FakeUpstream
    ) -> None:
        lexemes = [
            lexeme_payload(part_of_speech="noun", audio_path="/audio/mine.mp3"),
            lexeme_payload(part_of_speech="verb"),
        ]
        created = await post_word(client, spellings=[{"text": "zorb"}], lexemes=lexemes)
        upstream.json(url=mw_url(query="zorb"), payload=[zorb_entry(part_of_speech="verb", ref=None, respelling="x")])
        response = await client.post(f"/api/words/{created['id']}/audio")
        assert response.status_code == 404
        assert (await client.get(f"/api/words/{created['id']}")).json() == created

    async def test_404_for_unknown_word(self, client: httpx.AsyncClient, upstream: FakeUpstream) -> None:
        response = await client.post("/api/words/42/audio")
        assert response.status_code == 404
        assert response.json() == {"detail": "Word not found"}
        assert upstream.requests == []

    async def test_502_when_all_providers_fail(self, client: httpx.AsyncClient, word_id: int) -> None:
        response = await client.post(f"/api/words/{word_id}/audio")
        assert response.status_code == 502
        assert response.json()["detail"].startswith("mw: no fake route for ")
        assert (await client.get(f"/api/words/{word_id}")).json()["lexemes"][0]["audio_path"] is None
