import typing as t

import pytest

from dictum.controllers.word import word_controller
from tests.conftest import legacy_word_payload
from tests.conftest import lexeme_payload
from tests.conftest import word_payload

if t.TYPE_CHECKING:
    import fastapi
    import httpx
    from pytest_mock import MockerFixture

BURSAR_DEFINITION: t.Final = "an official in charge of funds, a **treasurer**"


async def _post(client: httpx.AsyncClient, *spellings: str, **overrides: t.Any) -> int:
    payload = word_payload(
        spellings=[{"text": s, "kind": "primary" if i == 0 else "variant"} for i, s in enumerate(spellings)],
        **overrides,
    )
    response = await client.post("/api/words", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _defined(*texts: str) -> list[dict[str, t.Any]]:
    """One verb lexeme whose definitions are `texts`."""
    return [lexeme_payload(definitions=[{"text": text, "source": "user", "examples": []} for text in texts])]


async def _hits(client: httpx.AsyncClient, *, q: str, **params: t.Any) -> list[dict[str, t.Any]]:
    response = await client.get("/api/search", params={"q": q, **params})
    assert response.status_code == 200, response.text
    return response.json()["hits"]


class TestSearch:
    async def test_empty_index(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/search", params={"q": "abandon"})
        assert response.status_code == 200
        assert response.json() == {"query": "abandon", "hits": []}

    async def test_prefix_hit_shape(self, client: httpx.AsyncClient) -> None:
        word_id = await _post(client, "Abandon")
        response = await client.get("/api/search", params={"q": "aban"})
        body = response.json()
        assert body["query"] == "aban"
        assert len(body["hits"]) == 1
        hit = body["hits"][0]
        assert hit["matched"] == "Abandon"
        assert hit["score"] == pytest.approx(98.5)
        assert hit["kind"] == "spelling"
        assert hit["word"] == {
            "id": word_id,
            "spellings": [{"id": 1, "text": "Abandon", "kind": "primary"}],
            "parts_of_speech": ["verb"],
            "status": "learned",
            "definition_count": 1,
        }

    async def test_ranking_and_dedupe(self, client: httpx.AsyncClient) -> None:
        colour = await _post(client, "Color", "Colour")
        polo = await _post(client, "Polo")
        response = await client.get("/api/search", params={"q": "colo"})
        hits = response.json()["hits"]
        assert [h["word"]["id"] for h in hits][:1] == [colour]
        assert hits[0]["matched"] == "Color"
        assert polo in {h["word"]["id"] for h in hits}
        assert len([h for h in hits if h["word"]["id"] == colour]) == 1
        assert {h["kind"] for h in hits} == {"spelling"}

    async def test_fuzzy(self, client: httpx.AsyncClient) -> None:
        word_id = await _post(client, "Abandon")
        response = await client.get("/api/search", params={"q": "abandn"})
        assert response.json()["hits"][0]["word"]["id"] == word_id

    async def test_limit(self, client: httpx.AsyncClient) -> None:
        for name in ("Aa", "Ab", "Ac"):
            await _post(client, name)
        response = await client.get("/api/search", params={"q": "a", "limit": 2})
        assert len(response.json()["hits"]) == 2

    async def test_symbols_only_query(self, client: httpx.AsyncClient) -> None:
        await _post(client, "Abandon")
        response = await client.get("/api/search", params={"q": "!!!"})
        assert response.json()["hits"] == []

    @pytest.mark.parametrize(
        "params", [{}, {"q": ""}, {"q": "x" * 256}, {"q": "a", "limit": 0}, {"q": "a", "limit": 51}]
    )
    async def test_invalid_params_returns_422(self, client: httpx.AsyncClient, params: dict[str, t.Any]) -> None:
        response = await client.get("/api/search", params=params)
        assert response.status_code == 422


class TestDefinitionSearch:
    async def test_definition_hit_shape(self, client: httpx.AsyncClient) -> None:
        word_id = await _post(client, "Bursar", lexemes=_defined(BURSAR_DEFINITION))
        hits = await _hits(client, q="funds")
        assert len(hits) == 1
        hit = hits[0]
        assert hit["kind"] == "definition"
        assert hit["matched"] == BURSAR_DEFINITION
        assert hit["score"] == pytest.approx(100.0)
        assert hit["word"]["id"] == word_id
        assert hit["word"]["spellings"][0]["text"] == "Bursar"
        assert hit["word"]["definition_count"] == 1

    async def test_spelling_hit_ranks_before_definition_hit(self, client: httpx.AsyncClient) -> None:
        bursar = await _post(client, "Bursar", lexemes=_defined(BURSAR_DEFINITION))
        treasurer = await _post(client, "Treasurer", lexemes=_defined("a person who manages the money"))
        hits = await _hits(client, q="treasurer")
        assert [(h["word"]["id"], h["kind"]) for h in hits] == [(treasurer, "spelling"), (bursar, "definition")]
        assert hits[0]["matched"] == "Treasurer"
        assert hits[1]["matched"] == BURSAR_DEFINITION

    async def test_fuzzy_query_reaches_definitions(self, client: httpx.AsyncClient) -> None:
        bursar = await _post(client, "Bursar", lexemes=_defined(BURSAR_DEFINITION))
        hits = await _hits(client, q="tresurer")
        assert [(h["word"]["id"], h["kind"]) for h in hits] == [(bursar, "definition")]
        assert 85 <= hits[0]["score"] < 100

    async def test_word_matched_by_spelling_listed_once(self, client: httpx.AsyncClient) -> None:
        ring = await _post(client, "Ring", lexemes=_defined("to make the bells ring"))
        hits = await _hits(client, q="ring")
        assert [(h["word"]["id"], h["kind"], h["matched"]) for h in hits] == [(ring, "spelling", "Ring")]

    @pytest.mark.parametrize(("q", "kinds"), [("rin", []), ("ring", ["definition"])])
    async def test_short_query_skips_definitions(self, client: httpx.AsyncClient, q: str, kinds: list[str]) -> None:
        await _post(client, "Bell", lexemes=_defined("ring the bell"))
        assert [h["kind"] for h in await _hits(client, q=q)] == kinds

    async def test_definition_must_start_a_word(self, client: httpx.AsyncClient) -> None:
        await _post(client, "Whilst", lexemes=_defined("during the time that"))
        await _post(client, "Fetch", lexemes=_defined("to bring something back"))
        assert await _hits(client, q="ring") == []
        assert [h["kind"] for h in await _hits(client, q="bring")] == ["definition"]

    async def test_limit_counts_both_kinds(self, client: httpx.AsyncClient) -> None:
        await _post(client, "Bursar", lexemes=_defined(BURSAR_DEFINITION))
        treasurer = await _post(client, "Treasurer")
        hits = await _hits(client, q="treasurer", limit=1)
        assert [(h["word"]["id"], h["kind"]) for h in hits] == [(treasurer, "spelling")]

    async def test_definitions_across_lexemes_dedupe_to_one_hit(self, client: httpx.AsyncClient) -> None:
        lexemes = [
            lexeme_payload(definitions=[{"text": "to hold money in trust", "source": "user", "examples": []}]),
            lexeme_payload(
                part_of_speech="noun",
                definitions=[{"text": "money held in trust", "source": "user", "examples": []}],
            ),
        ]
        word_id = await _post(client, "Escrow", lexemes=lexemes)
        hits = await _hits(client, q="money")
        assert [(h["word"]["id"], h["kind"]) for h in hits] == [(word_id, "definition")]
        assert hits[0]["word"]["definition_count"] == 2

    async def test_definition_edit_is_reindexed(self, client: httpx.AsyncClient, app: fastapi.FastAPI) -> None:
        word_id = await _post(client, "Bursar", lexemes=_defined("keeper of the funds"))
        assert [h["matched"] for h in await _hits(client, q="funds")] == ["keeper of the funds"]
        payload = word_payload(spellings=[{"text": "Bursar"}], lexemes=_defined("keeper of the money"))
        response = await client.put(f"/api/words/{word_id}", json=payload)
        assert response.status_code == 200
        assert app.state.search.dirty is True
        assert await _hits(client, q="funds") == []
        hits = await _hits(client, q="money")
        assert [(h["word"]["id"], h["kind"], h["matched"]) for h in hits] == [
            (word_id, "definition", "keeper of the money")
        ]

    async def test_delete_removes_definition_hits(self, client: httpx.AsyncClient) -> None:
        word_id = await _post(client, "Bursar", lexemes=_defined(BURSAR_DEFINITION))
        assert len(await _hits(client, q="funds")) == 1
        assert (await client.delete(f"/api/words/{word_id}")).status_code == 204
        assert await _hits(client, q="funds") == []

    async def test_create_makes_definition_searchable(self, client: httpx.AsyncClient, app: fastapi.FastAPI) -> None:
        assert await _hits(client, q="funds") == []
        assert app.state.search.dirty is False
        await _post(client, "Bursar", lexemes=_defined(BURSAR_DEFINITION))
        assert app.state.search.dirty is True
        assert [h["kind"] for h in await _hits(client, q="funds")] == ["definition"]

    async def test_import_makes_definition_searchable(self, client: httpx.AsyncClient) -> None:
        body = {"version": 2, "words": [word_payload(spellings=[{"text": "Bursar"}], lexemes=_defined("funds"))]}
        assert (await client.post("/api/import", json=body)).status_code == 200
        hits = await _hits(client, q="funds")
        assert [(h["kind"], h["matched"]) for h in hits] == [("definition", "funds")]

    async def test_both_loaders_called_once_per_rebuild(self, client: httpx.AsyncClient, mocker: MockerFixture) -> None:
        await _post(client, "Bursar", lexemes=_defined(BURSAR_DEFINITION))
        spellings = mocker.spy(word_controller, "spelling_index_rows")
        definitions = mocker.spy(word_controller, "definition_index_rows")
        await _hits(client, q="funds")
        await _hits(client, q="funds")
        assert spellings.call_count == 1
        assert definitions.call_count == 1


class TestIndexInvalidation:
    async def test_create_invalidates(self, client: httpx.AsyncClient, app: fastapi.FastAPI) -> None:
        await client.get("/api/search", params={"q": "x"})
        assert app.state.search.dirty is False
        await _post(client, "Abandon")
        assert app.state.search.dirty is True
        response = await client.get("/api/search", params={"q": "aban"})
        assert len(response.json()["hits"]) == 1

    async def test_put_invalidates(self, client: httpx.AsyncClient, app: fastapi.FastAPI) -> None:
        word_id = await _post(client, "Abandon")
        await client.get("/api/search", params={"q": "aban"})
        response = await client.put(f"/api/words/{word_id}", json=word_payload(spellings=[{"text": "Betray"}]))
        assert response.status_code == 200
        assert app.state.search.dirty is True
        hits = (await client.get("/api/search", params={"q": "betr"})).json()["hits"]
        assert hits[0]["matched"] == "Betray"
        assert (await client.get("/api/search", params={"q": "aban"})).json()["hits"] == []

    async def test_delete_invalidates(self, client: httpx.AsyncClient, app: fastapi.FastAPI) -> None:
        word_id = await _post(client, "Abandon")
        await client.get("/api/search", params={"q": "aban"})
        await client.delete(f"/api/words/{word_id}")
        assert app.state.search.dirty is True
        assert (await client.get("/api/search", params={"q": "aban"})).json()["hits"] == []

    async def test_patch_does_not_invalidate(self, client: httpx.AsyncClient, app: fastapi.FastAPI) -> None:
        word_id = await _post(client, "Abandon")
        await client.get("/api/search", params={"q": "aban"})
        await client.patch(f"/api/words/{word_id}", json={"status": "inbox"})
        assert app.state.search.dirty is False
        hit = (await client.get("/api/search", params={"q": "aban"})).json()["hits"][0]
        assert hit["word"]["status"] == "inbox"

    async def test_failed_write_does_not_invalidate(self, client: httpx.AsyncClient, app: fastapi.FastAPI) -> None:
        await _post(client, "Abandon")
        await client.get("/api/search", params={"q": "aban"})
        assert app.state.search.dirty is False
        response = await client.post("/api/words", json=word_payload(spellings=[{"text": "ABANDON"}]))
        assert response.status_code == 409
        assert app.state.search.dirty is False
        response = await client.put("/api/words/999", json=word_payload(spellings=[{"text": "Betray"}]))
        assert response.status_code == 404
        assert app.state.search.dirty is False

    async def test_import_invalidates_after_commit(self, client: httpx.AsyncClient, app: fastapi.FastAPI) -> None:
        await client.get("/api/search", params={"q": "x"})
        body = {"version": 2, "words": [word_payload(spellings=[{"text": "Betray"}])]}
        response = await client.post("/api/import", json=body)
        assert response.status_code == 200
        assert app.state.search.dirty is True
        hits = (await client.get("/api/search", params={"q": "betr"})).json()["hits"]
        assert hits[0]["matched"] == "Betray"

    async def test_legacy_import_invalidates_after_commit(
        self, client: httpx.AsyncClient, app: fastapi.FastAPI
    ) -> None:
        await client.get("/api/search", params={"q": "x"})
        body = {"version": 1, "words": [legacy_word_payload(spellings=[{"text": "Betray"}])]}
        response = await client.post("/api/import", json=body)
        assert response.status_code == 200
        assert app.state.search.dirty is True
        hits = (await client.get("/api/search", params={"q": "betr"})).json()["hits"]
        assert hits[0]["matched"] == "Betray"
        assert hits[0]["word"]["parts_of_speech"] == ["verb"]

    async def test_search_hydrates_in_one_batch(
        self, client: httpx.AsyncClient, app: fastapi.FastAPI, mocker: MockerFixture
    ) -> None:
        for name in ("Aa", "Ab", "Ac"):
            await _post(client, name)
        get_many = mocker.spy(word_controller, "get_many")
        get_one = mocker.spy(word_controller, "get")
        response = await client.get("/api/search", params={"q": "a"})
        assert len(response.json()["hits"]) == 3
        assert get_many.call_count == 1
        assert get_one.call_count == 0
