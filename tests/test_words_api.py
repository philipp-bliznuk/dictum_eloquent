import typing as t

import pytest

from tests.conftest import LOOSE_EXAMPLE
from tests.conftest import NESTED_EXAMPLE
from tests.conftest import lexeme_payload
from tests.conftest import word_payload

if t.TYPE_CHECKING:
    import httpx

Json = dict[str, t.Any]


async def _post(client: httpx.AsyncClient, **overrides: t.Any) -> Json:
    response = await client.post("/api/words", json=word_payload(**overrides))
    assert response.status_code == 201, response.text
    return response.json()


def _definition(text: str, *examples: str) -> Json:
    return {"text": text, "examples": [{"text": e} for e in examples]}


def _without_timestamps(body: Json) -> Json:
    return {k: v for k, v in body.items() if k not in {"created_at", "updated_at"}}


class TestHealth:
    async def test_ok(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "database": "ok", "providers": ["free_dict"], "ai": False}


class TestCreateWord:
    async def test_returns_201_with_children(self, client: httpx.AsyncClient) -> None:
        body = await _post(client)
        assert body["created_at"].endswith("Z") or "+" in body["created_at"]
        assert body["updated_at"].endswith("Z") or "+" in body["updated_at"]
        assert _without_timestamps(body) == {
            "id": 1,
            "notes": None,
            "status": "learned",
            "spellings": [{"id": 1, "text": "Abandon", "kind": "primary"}],
            "lexemes": [
                {
                    "id": 1,
                    "position": 0,
                    "part_of_speech": "verb",
                    "pronunciation": "uh-BAN-dun",
                    "audio_path": None,
                    "definitions": [
                        {
                            "id": 1,
                            "position": 0,
                            "text": "to give up",
                            "source": "user",
                            "examples": [{"id": 1, "position": 0, "text": NESTED_EXAMPLE, "source": "user"}],
                        }
                    ],
                }
            ],
            "examples": [{"id": 2, "position": 1, "text": LOOSE_EXAMPLE, "source": "vault"}],
        }

    async def test_response_matches_reload(self, client: httpx.AsyncClient) -> None:
        body = await _post(
            client,
            notes="n",
            lexemes=[
                lexeme_payload(part_of_speech="noun", definitions=[_definition("a", "a1", "a2"), _definition("b")]),
                lexeme_payload(part_of_speech="verb", pronunciation=None, definitions=[_definition("c", "c1")]),
            ],
            examples=[{"text": "l1"}, {"text": "l2", "source": "ai"}],
        )
        reloaded = (await client.get(f"/api/words/{body['id']}")).json()
        assert reloaded == body

    async def test_example_positions_use_one_counter(self, client: httpx.AsyncClient) -> None:
        body = await _post(
            client,
            lexemes=[
                lexeme_payload(
                    part_of_speech="noun", definitions=[_definition("a", "a1", "a2"), _definition("b", "b1")]
                ),
                lexeme_payload(part_of_speech="verb", definitions=[_definition("c", "c1")]),
            ],
            examples=[{"text": "l1"}, {"text": "l2"}],
        )
        nested = [(e["text"], e["position"]) for x in body["lexemes"] for d in x["definitions"] for e in d["examples"]]
        assert nested == [("a1", 0), ("a2", 1), ("b1", 2), ("c1", 3)]
        assert [(e["text"], e["position"]) for e in body["examples"]] == [("l1", 4), ("l2", 5)]
        assert [x["position"] for x in body["lexemes"]] == [0, 1]
        assert [[d["position"] for d in x["definitions"]] for x in body["lexemes"]] == [[0, 1], [0]]

    async def test_part_of_speech_normalised(self, client: httpx.AsyncClient) -> None:
        body = await _post(client, lexemes=[lexeme_payload(part_of_speech=" Transitive  Verb ")])
        assert body["lexemes"][0]["part_of_speech"] == "transitive verb"

    async def test_without_lexemes_or_examples(self, client: httpx.AsyncClient) -> None:
        body = await _post(client, lexemes=[], examples=[])
        assert body["lexemes"] == []
        assert body["examples"] == []

    async def test_conflict_returns_409_with_location(self, client: httpx.AsyncClient) -> None:
        created = await _post(client)
        response = await client.post("/api/words", json=word_payload(spellings=[{"text": "abandon"}]))
        assert response.status_code == 409
        assert response.headers["location"] == f"/api/words/{created['id']}"
        assert "already belongs to word" in response.json()["detail"]

    async def test_conflict_on_variant(self, client: httpx.AsyncClient) -> None:
        await _post(client, spellings=[{"text": "Color"}, {"text": "Colour", "kind": "variant"}])
        response = await client.post("/api/words", json=word_payload(spellings=[{"text": "COLOUR"}]))
        assert response.status_code == 409

    @pytest.mark.parametrize(("first", "second"), [("Éclair", "éclair"), ("Straße", "STRASSE")])
    async def test_conflict_folds_unicode_case(self, client: httpx.AsyncClient, first: str, second: str) -> None:
        created = await _post(client, spellings=[{"text": first}])
        response = await client.post("/api/words", json=word_payload(spellings=[{"text": second}]))
        assert response.status_code == 409
        assert response.headers["location"] == f"/api/words/{created['id']}"
        response = await client.get("/api/words/by-spelling", params={"text": second})
        assert response.json()["id"] == created["id"]

    @pytest.mark.parametrize(
        ("overrides", "fragment"),
        [
            ({"spellings": [{"text": "A"}, {"text": "a"}]}, "duplicate spelling"),
            ({"spellings": []}, "at least 1"),
            ({"lexemes": [lexeme_payload(part_of_speech="x" * 65)]}, "at most 64"),
            ({"lexemes": [lexeme_payload(part_of_speech="   ")]}, "part of speech must contain visible characters"),
            (
                {"lexemes": [lexeme_payload(part_of_speech="Noun"), lexeme_payload(part_of_speech=" noun ")]},
                "duplicate part of speech: noun",
            ),
            ({"lexemes": [lexeme_payload(audio_path="https://evil.example/x.mp3")]}, "pattern"),
            ({"lexemes": [lexeme_payload(definitions=[{"text": ""}])]}, "at least 1"),
            ({"lexemes": [lexeme_payload(definitions=[_definition("d", "")])]}, "at least 1"),
            ({"examples": [{"text": ""}]}, "at least 1"),
            ({"notes": "x" * 10_001}, "at most 10000"),
            ({"status": "unknown"}, "status"),
        ],
    )
    async def test_invalid_payload_returns_422(
        self, client: httpx.AsyncClient, overrides: dict[str, t.Any], fragment: str
    ) -> None:
        response = await client.post("/api/words", json=word_payload(**overrides))
        assert response.status_code == 422
        assert fragment in response.text
        assert (await client.get("/api/words/stats")).json()["words"] == 0

    async def test_missing_body_returns_422(self, client: httpx.AsyncClient) -> None:
        response = await client.post("/api/words", json={})
        assert response.status_code == 422


class TestReadWord:
    async def test_get_by_id(self, client: httpx.AsyncClient, word_id: int) -> None:
        response = await client.get(f"/api/words/{word_id}")
        assert response.status_code == 200
        assert response.json()["id"] == word_id

    async def test_get_missing_returns_404(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/words/42")
        assert response.status_code == 404
        assert response.json() == {"detail": "Word not found"}

    async def test_get_invalid_id_returns_422(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/words/abc")
        assert response.status_code == 422

    @pytest.mark.parametrize("text", ["Abandon", "abandon", "ABANDON", "  abandon  "])
    async def test_by_spelling(self, client: httpx.AsyncClient, word_id: int, text: str) -> None:
        response = await client.get("/api/words/by-spelling", params={"text": text})
        assert response.status_code == 200
        assert response.json()["id"] == word_id

    async def test_by_spelling_missing(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/words/by-spelling", params={"text": "nothing"})
        assert response.status_code == 404
        assert response.json() == {"detail": "Word not found"}

    @pytest.mark.parametrize("params", [{}, {"text": ""}, {"text": "x" * 256}])
    async def test_by_spelling_invalid_query_returns_422(
        self, client: httpx.AsyncClient, params: dict[str, str]
    ) -> None:
        response = await client.get("/api/words/by-spelling", params=params)
        assert response.status_code == 422


class TestPhrases:
    PHRASES: t.ClassVar[list[str]] = ["By my troth!", "in extenso", "put paid to", "and/or", "Aide-de-camp"]

    @pytest.mark.parametrize("phrase", PHRASES)
    async def test_phrase_roundtrip(self, client: httpx.AsyncClient, phrase: str) -> None:
        created = await _post(client, spellings=[{"text": phrase}])
        assert created["spellings"][0]["text"] == phrase
        response = await client.get("/api/words/by-spelling", params={"text": phrase.upper()})
        assert response.status_code == 200
        assert response.json()["id"] == created["id"]

    async def test_whitespace_is_normalised_on_write_and_lookup(self, client: httpx.AsyncClient) -> None:
        created = await _post(client, spellings=[{"text": "  put\t paid \n to "}])
        assert created["spellings"][0]["text"] == "put paid to"
        response = await client.get("/api/words/by-spelling", params={"text": "put  paid   to"})
        assert response.json()["id"] == created["id"]

    async def test_phrase_conflict_ignores_case_and_spacing(self, client: httpx.AsyncClient) -> None:
        created = await _post(client, spellings=[{"text": "put paid to"}])
        response = await client.post("/api/words", json=word_payload(spellings=[{"text": "Put  Paid To"}]))
        assert response.status_code == 409
        assert response.headers["location"] == f"/api/words/{created['id']}"

    @pytest.mark.parametrize(
        ("text", "fragment"),
        [
            ("Color / Colour", "must not contain ' / '"),
            ("   ", "visible characters"),
            ("x" * 256, "at most 255"),
        ],
    )
    async def test_invalid_spelling_returns_422(self, client: httpx.AsyncClient, text: str, fragment: str) -> None:
        response = await client.post("/api/words", json=word_payload(spellings=[{"text": text}]))
        assert response.status_code == 422
        assert fragment in response.text

    async def test_phrase_searchable(self, client: httpx.AsyncClient) -> None:
        created = await _post(client, spellings=[{"text": "By my troth!"}])
        response = await client.get("/api/search", params={"q": "troth"})
        hits = response.json()["hits"]
        assert hits[0]["word"]["id"] == created["id"]
        assert hits[0]["matched"] == "By my troth!"

    async def test_phrase_part_of_speech(self, client: httpx.AsyncClient) -> None:
        created = await _post(
            client, spellings=[{"text": "put paid to"}], lexemes=[lexeme_payload(part_of_speech="phrase")]
        )
        assert created["lexemes"][0]["part_of_speech"] == "phrase"
        items = (await client.get("/api/words")).json()["items"]
        assert items[0]["parts_of_speech"] == ["phrase"]


class TestListWords:
    async def test_empty(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/words")
        assert response.status_code == 200
        assert response.json() == {"items": [], "total": 0, "offset": 0, "limit": 50}

    async def test_summary_shape(self, client: httpx.AsyncClient, word_id: int) -> None:
        response = await client.get("/api/words")
        item = response.json()["items"][0]
        assert item == {
            "id": word_id,
            "spellings": [{"id": 1, "text": "Abandon", "kind": "primary"}],
            "parts_of_speech": ["verb"],
            "status": "learned",
            "definition_count": 1,
        }

    async def test_summary_aggregates_lexemes(self, client: httpx.AsyncClient) -> None:
        await _post(
            client,
            lexemes=[
                lexeme_payload(part_of_speech="Noun", definitions=[_definition("a"), _definition("b")]),
                lexeme_payload(part_of_speech="verb", definitions=[_definition("c")]),
                lexeme_payload(part_of_speech="adjective", definitions=[]),
            ],
        )
        item = (await client.get("/api/words")).json()["items"][0]
        assert item["parts_of_speech"] == ["noun", "verb", "adjective"]
        assert item["definition_count"] == 3

    async def test_paging_newest_first(self, client: httpx.AsyncClient) -> None:
        for name in ("A1", "B1", "C1"):
            await _post(client, spellings=[{"text": name}])
        response = await client.get("/api/words", params={"offset": 1, "limit": 1})
        body = response.json()
        assert body["total"] == 3
        assert body["offset"] == 1
        assert body["limit"] == 1
        assert [i["spellings"][0]["text"] for i in body["items"]] == ["B1"]
        response = await client.get("/api/words", params={"limit": 2})
        assert [i["spellings"][0]["text"] for i in response.json()["items"]] == ["C1", "B1"]

    async def test_status_filter(self, client: httpx.AsyncClient) -> None:
        await _post(client, spellings=[{"text": "A1"}], status="inbox")
        await _post(client, spellings=[{"text": "B1"}], status="learned")
        response = await client.get("/api/words", params={"status": "inbox"})
        body = response.json()
        assert body["total"] == 1
        assert body["items"][0]["spellings"][0]["text"] == "A1"

    @pytest.mark.parametrize("params", [{"status": "bogus"}, {"offset": -1}, {"limit": 0}, {"limit": 201}])
    async def test_invalid_query_returns_422(self, client: httpx.AsyncClient, params: dict[str, t.Any]) -> None:
        response = await client.get("/api/words", params=params)
        assert response.status_code == 422


class TestStats:
    async def test_empty(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/words/stats")
        assert response.status_code == 200
        assert response.json() == {"words": 0, "inbox": 0, "definitions": 0, "examples": 0}

    async def test_counts(self, client: httpx.AsyncClient, word_id: int) -> None:
        await _post(client, spellings=[{"text": "B1"}], status="inbox", lexemes=[], examples=[])
        response = await client.get("/api/words/stats")
        assert response.json() == {"words": 2, "inbox": 1, "definitions": 1, "examples": 2}

    async def test_counts_examples_across_lexemes(self, client: httpx.AsyncClient) -> None:
        await _post(
            client,
            lexemes=[
                lexeme_payload(part_of_speech="noun", definitions=[_definition("a", "a1", "a2"), _definition("b")]),
                lexeme_payload(part_of_speech="verb", definitions=[_definition("c", "c1")]),
            ],
            examples=[{"text": "l1"}],
        )
        response = await client.get("/api/words/stats")
        assert response.json() == {"words": 1, "inbox": 0, "definitions": 3, "examples": 4}


class TestReplaceWord:
    async def test_replaces_children(self, client: httpx.AsyncClient, word_id: int) -> None:
        before = (await client.get(f"/api/words/{word_id}")).json()
        payload = word_payload(
            notes="note",
            spellings=[{"text": "abandon"}, {"text": "Abandonment", "kind": "variant"}],
            lexemes=[
                lexeme_payload(part_of_speech="noun", pronunciation=None, definitions=[_definition("d1")]),
                lexeme_payload(part_of_speech="verb", definitions=[_definition("d2", "e2")]),
            ],
            examples=[{"text": "loose"}],
        )
        response = await client.put(f"/api/words/{word_id}", json=payload)
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == word_id
        assert body["notes"] == "note"
        assert [s["text"] for s in body["spellings"]] == ["abandon", "Abandonment"]
        assert [(x["part_of_speech"], x["pronunciation"]) for x in body["lexemes"]] == [
            ("noun", None),
            ("verb", "uh-BAN-dun"),
        ]
        assert [[d["text"] for d in x["definitions"]] for x in body["lexemes"]] == [["d1"], ["d2"]]
        assert body["lexemes"][1]["definitions"][0]["examples"] == [
            {
                "id": body["lexemes"][1]["definitions"][0]["examples"][0]["id"],
                "position": 0,
                "text": "e2",
                "source": "user",
            }
        ]
        assert [(e["text"], e["position"]) for e in body["examples"]] == [("loose", 1)]
        assert body["created_at"] == before["created_at"]
        again = (await client.get(f"/api/words/{word_id}")).json()
        assert again == body

    async def test_preserves_created_at(self, client: httpx.AsyncClient, word_id: int) -> None:
        before = (await client.get(f"/api/words/{word_id}")).json()
        response = await client.put(f"/api/words/{word_id}", json=word_payload(status="inbox"))
        assert response.status_code == 200
        assert response.json()["created_at"] == before["created_at"]
        assert response.json()["status"] == "inbox"
        reloaded = (await client.get(f"/api/words/{word_id}")).json()
        assert reloaded["created_at"] == before["created_at"]

    async def test_can_drop_all_children(self, client: httpx.AsyncClient, word_id: int) -> None:
        response = await client.put(f"/api/words/{word_id}", json=word_payload(lexemes=[], examples=[]))
        assert response.status_code == 200
        assert response.json()["lexemes"] == []
        assert response.json()["examples"] == []
        assert (await client.get("/api/words/stats")).json() == {
            "words": 1,
            "inbox": 0,
            "definitions": 0,
            "examples": 0,
        }

    async def test_timestamps_keep_timezone_after_reload(self, client: httpx.AsyncClient, word_id: int) -> None:
        created = (await client.post("/api/words", json=word_payload(spellings=[{"text": "B1"}]))).json()
        reloaded = (await client.get(f"/api/words/{created['id']}")).json()
        assert reloaded["created_at"] == created["created_at"]

    async def test_missing_returns_404(self, client: httpx.AsyncClient) -> None:
        response = await client.put("/api/words/9", json=word_payload())
        assert response.status_code == 404

    async def test_conflict_with_other_word(self, client: httpx.AsyncClient, word_id: int) -> None:
        other = await _post(client, spellings=[{"text": "Betray"}])
        response = await client.put(f"/api/words/{word_id}", json=word_payload(spellings=[{"text": "betray"}]))
        assert response.status_code == 409
        assert response.headers["location"] == f"/api/words/{other['id']}"

    @pytest.mark.parametrize(
        "overrides",
        [
            {"spellings": []},
            {"lexemes": [lexeme_payload(part_of_speech="noun"), lexeme_payload(part_of_speech="NOUN")]},
            {"lexemes": [lexeme_payload(part_of_speech=" ")]},
        ],
    )
    async def test_invalid_returns_422_and_keeps_word(
        self, client: httpx.AsyncClient, word_id: int, overrides: Json
    ) -> None:
        before = (await client.get(f"/api/words/{word_id}")).json()
        response = await client.put(f"/api/words/{word_id}", json=word_payload(**overrides))
        assert response.status_code == 422
        assert (await client.get(f"/api/words/{word_id}")).json() == before


class TestPatchWord:
    async def test_partial_update(self, client: httpx.AsyncClient, word_id: int) -> None:
        response = await client.patch(f"/api/words/{word_id}", json={"status": "inbox", "notes": "later"})
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "inbox"
        assert body["notes"] == "later"
        assert body["lexemes"][0]["part_of_speech"] == "verb"
        assert body["lexemes"][0]["pronunciation"] == "uh-BAN-dun"
        assert len(body["spellings"]) == 1
        assert len(body["examples"]) == 1

    async def test_empty_patch_is_noop(self, client: httpx.AsyncClient, word_id: int) -> None:
        before = (await client.get(f"/api/words/{word_id}")).json()
        response = await client.patch(f"/api/words/{word_id}", json={})
        assert response.status_code == 200
        after = response.json()
        assert {k: v for k, v in after.items() if k != "updated_at"} == {
            k: v for k, v in before.items() if k != "updated_at"
        }

    async def test_grammar_and_children_rejected(self, client: httpx.AsyncClient, word_id: int) -> None:
        before = (await client.get(f"/api/words/{word_id}")).json()
        body = {
            "status": "learned",
            "part_of_speech": "noun",
            "pronunciation": None,
            "audio_path": "/audio/x.mp3",
            "lexemes": [],
            "examples": [],
        }
        response = await client.patch(f"/api/words/{word_id}", json=body)
        assert response.status_code == 422
        stray = sorted(err["loc"][-1] for err in response.json()["detail"])
        assert stray == ["audio_path", "examples", "lexemes", "part_of_speech", "pronunciation"]
        after = (await client.get(f"/api/words/{word_id}")).json()
        assert _without_timestamps(after) == _without_timestamps(before)

    async def test_missing_returns_404(self, client: httpx.AsyncClient) -> None:
        response = await client.patch("/api/words/9", json={"notes": "x"})
        assert response.status_code == 404

    @pytest.mark.parametrize("body", [{"status": "bogus"}, {"notes": "x" * 10_001}, {"status": None}])
    async def test_invalid_returns_422(self, client: httpx.AsyncClient, word_id: int, body: dict[str, t.Any]) -> None:
        response = await client.patch(f"/api/words/{word_id}", json=body)
        assert response.status_code == 422
        reloaded = (await client.get(f"/api/words/{word_id}")).json()
        assert reloaded["status"] == "learned"
        assert reloaded["notes"] is None

    async def test_null_clears_notes(self, client: httpx.AsyncClient, word_id: int) -> None:
        assert (await client.patch(f"/api/words/{word_id}", json={"notes": "x"})).json()["notes"] == "x"
        response = await client.patch(f"/api/words/{word_id}", json={"notes": None})
        assert response.status_code == 200
        assert response.json()["notes"] is None


class TestDeleteWord:
    async def test_returns_204_and_removes(self, client: httpx.AsyncClient, word_id: int) -> None:
        response = await client.delete(f"/api/words/{word_id}")
        assert response.status_code == 204
        assert response.content == b""
        assert (await client.get(f"/api/words/{word_id}")).status_code == 404
        assert (await client.get("/api/words/stats")).json() == {
            "words": 0,
            "inbox": 0,
            "definitions": 0,
            "examples": 0,
        }

    async def test_missing_returns_404(self, client: httpx.AsyncClient) -> None:
        response = await client.delete("/api/words/9")
        assert response.status_code == 404

    async def test_spelling_free_after_delete(self, client: httpx.AsyncClient, word_id: int) -> None:
        await client.delete(f"/api/words/{word_id}")
        response = await client.post("/api/words", json=word_payload())
        assert response.status_code == 201
