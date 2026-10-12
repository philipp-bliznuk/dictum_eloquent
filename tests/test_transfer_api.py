import io
import typing as t
import zipfile

import pytest

from tests.conftest import LOOSE_EXAMPLE
from tests.conftest import NESTED_EXAMPLE
from tests.conftest import legacy_word_payload
from tests.conftest import lexeme_payload
from tests.conftest import word_payload

if t.TYPE_CHECKING:
    import fastapi
    import httpx

Json = dict[str, t.Any]

CREATED_AT: t.Final = "2020-02-02T10:00:00Z"


def _file(*words: Json) -> Json:
    return {"version": 2, "words": list(words)}


def _legacy_file(*words: Json) -> Json:
    return {"version": 1, "words": list(words)}


def _definition(text: str, *examples: str) -> Json:
    return {"text": text, "examples": [{"text": e} for e in examples]}


class TestImport:
    async def test_creates(self, client: httpx.AsyncClient, app: fastapi.FastAPI) -> None:
        await client.get("/api/search", params={"q": "x"})
        response = await client.post(
            "/api/import", json=_file(word_payload(), word_payload(spellings=[{"text": "Betray"}]))
        )
        assert response.status_code == 200
        assert response.json() == {"created": 2, "replaced": 0, "skipped": 0, "failures": []}
        assert app.state.search.dirty is True
        assert (await client.get("/api/words/stats")).json() == {
            "words": 2,
            "inbox": 0,
            "definitions": 2,
            "examples": 4,
        }

    async def test_imported_word_shape(self, client: httpx.AsyncClient) -> None:
        payload = word_payload(
            lexemes=[
                lexeme_payload(part_of_speech=" Noun ", definitions=[_definition("a", "a1"), _definition("b")]),
                lexeme_payload(part_of_speech="verb", definitions=[_definition("c", "c1")]),
            ],
            examples=[{"text": "l1"}],
        )
        response = await client.post("/api/import", json=_file(payload))
        assert response.json()["created"] == 1
        word = (await client.get("/api/words/by-spelling", params={"text": "abandon"})).json()
        assert [x["part_of_speech"] for x in word["lexemes"]] == ["noun", "verb"]
        assert [[d["text"] for d in x["definitions"]] for x in word["lexemes"]] == [["a", "b"], ["c"]]
        assert [[[e["text"] for e in d["examples"]] for d in x["definitions"]] for x in word["lexemes"]] == [
            [["a1"], []],
            [["c1"]],
        ]
        assert [(e["text"], e["position"]) for e in word["examples"]] == [("l1", 2)]

    async def test_honours_created_at(self, client: httpx.AsyncClient) -> None:
        response = await client.post("/api/import", json=_file({**word_payload(), "created_at": CREATED_AT}))
        assert response.json()["created"] == 1
        word = (await client.get("/api/words/by-spelling", params={"text": "abandon"})).json()
        assert word["created_at"] == CREATED_AT

    async def test_created_at_optional(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            "/api/import", json=_file(word_payload(), {**word_payload(spellings=[{"text": "B"}]), "created_at": None})
        )
        assert response.json()["created"] == 2
        for text in ("abandon", "b"):
            word = (await client.get("/api/words/by-spelling", params={"text": text})).json()
            assert word["created_at"].startswith("20")

    @pytest.mark.parametrize(
        ("policy", "expected"),
        [
            ("skip", {"created": 0, "replaced": 0, "skipped": 1, "notes": None}),
            ("replace", {"created": 0, "replaced": 1, "skipped": 0, "notes": "new"}),
        ],
    )
    async def test_conflict_policy(
        self, *, client: httpx.AsyncClient, word_id: int, policy: str, expected: dict[str, t.Any]
    ) -> None:
        body = _file(word_payload(spellings=[{"text": "abandon"}], notes="new"))
        response = await client.post("/api/import", params={"on_conflict": policy}, json=body)
        assert response.status_code == 200
        notes = (await client.get(f"/api/words/{word_id}")).json()["notes"]
        assert {**response.json(), "notes": notes} == {**expected, "failures": []}

    async def test_replace_keeps_created_at_and_id(self, client: httpx.AsyncClient, word_id: int) -> None:
        before = (await client.get(f"/api/words/{word_id}")).json()
        body = _file({**word_payload(notes="new", lexemes=[]), "created_at": CREATED_AT})
        response = await client.post("/api/import", params={"on_conflict": "replace"}, json=body)
        assert response.json()["replaced"] == 1
        after = (await client.get(f"/api/words/{word_id}")).json()
        assert after["created_at"] == before["created_at"]
        assert after["notes"] == "new"
        assert after["lexemes"] == []
        assert (await client.get("/api/words/stats")).json()["words"] == 1

    async def test_default_policy_is_skip(self, client: httpx.AsyncClient, word_id: int) -> None:
        response = await client.post("/api/import", json=_file(word_payload()))
        assert response.json()["skipped"] == 1

    async def test_fail_policy_returns_422(self, client: httpx.AsyncClient, word_id: int) -> None:
        response = await client.post("/api/import", params={"on_conflict": "fail"}, json=_file(word_payload()))
        assert response.status_code == 422
        assert "already belongs to word" in response.json()["detail"]

    async def test_fail_policy_rolls_back_batch(self, client: httpx.AsyncClient, word_id: int) -> None:
        body = _file(word_payload(spellings=[{"text": "Fresh"}]), word_payload())
        response = await client.post("/api/import", params={"on_conflict": "fail"}, json=body)
        assert response.status_code == 422
        assert (await client.get("/api/words/stats")).json()["words"] == 1

    @pytest.mark.parametrize(
        ("params", "body"),
        [
            ({"on_conflict": "bogus"}, _file()),
            ({}, {"version": 3, "words": []}),
            ({}, {"version": "2", "words": []}),
            ({}, {"words": []}),
            ({}, {"version": 2}),
            ({}, _file(word_payload(spellings=[]))),
            (
                {},
                _file(
                    word_payload(lexemes=[lexeme_payload(part_of_speech="Noun"), lexeme_payload(part_of_speech="noun")])
                ),
            ),
            ({}, _file(word_payload(lexemes=[lexeme_payload(part_of_speech="  ")]))),
            ({}, _file({**word_payload(), "created_at": "yesterday"})),
            ({}, _legacy_file(legacy_word_payload(examples=[{"text": "x", "definition_index": 5}]))),
            ({}, _legacy_file(legacy_word_payload(part_of_speech=""))),
            ({}, _legacy_file(legacy_word_payload(spellings=[]))),
            ({}, _legacy_file(word_payload())),
        ],
    )
    async def test_invalid_returns_422(
        self, client: httpx.AsyncClient, params: dict[str, str], body: dict[str, t.Any]
    ) -> None:
        response = await client.post("/api/import", params=params, json=body)
        assert response.status_code == 422
        assert (await client.get("/api/words/stats")).json()["words"] == 0

    async def test_too_many_words_returns_422(self, client: httpx.AsyncClient) -> None:
        words = [{"spellings": [{"text": f"w{i}"}]} for i in range(20_001)]
        response = await client.post("/api/import", json=_file(*words))
        assert response.status_code == 422
        assert "at most 20000" in response.text


class TestImportLegacy:
    async def test_upgraded_on_the_fly(self, client: httpx.AsyncClient, app: fastapi.FastAPI) -> None:
        await client.get("/api/search", params={"q": "x"})
        legacy = legacy_word_payload(
            part_of_speech=" Verb / Noun ",
            audio_path="/audio/abando01.mp3",
            notes="n",
            status="inbox",
            definitions=[{"text": "to give up"}, {"text": "freedom", "source": "mw"}],
            examples=[
                {"text": "He *abandoned* ship.", "source": "ai", "definition_index": 0},
                {"text": LOOSE_EXAMPLE, "source": "vault", "definition_index": None},
                {"text": "with *abandon*", "definition_index": 1},
                {"text": "Also first.", "definition_index": 0},
                {"text": "Loose too."},
            ],
        )
        response = await client.post("/api/import", params={"on_conflict": "fail"}, json=_legacy_file(legacy))
        assert response.status_code == 200, response.text
        assert response.json() == {"created": 1, "replaced": 0, "skipped": 0, "failures": []}
        assert app.state.search.dirty is True
        word = (await client.get("/api/words/by-spelling", params={"text": "abandon"})).json()
        assert (word["notes"], word["status"]) == ("n", "inbox")
        [lexeme] = word["lexemes"]
        assert lexeme["part_of_speech"] == "verb / noun"
        assert lexeme["pronunciation"] == "uh-BAN-dun"
        assert lexeme["audio_path"] == "/audio/abando01.mp3"
        assert [(d["text"], d["source"]) for d in lexeme["definitions"]] == [("to give up", "user"), ("freedom", "mw")]
        assert [[(e["text"], e["source"]) for e in d["examples"]] for d in lexeme["definitions"]] == [
            [("He *abandoned* ship.", "ai"), ("Also first.", "user")],
            [("with *abandon*", "user")],
        ]
        assert [(e["text"], e["source"]) for e in word["examples"]] == [
            (LOOSE_EXAMPLE, "vault"),
            ("Loose too.", "user"),
        ]
        positions = [e["position"] for d in lexeme["definitions"] for e in d["examples"]]
        positions += [e["position"] for e in word["examples"]]
        assert positions == [0, 1, 2, 3, 4]

    async def test_legacy_defaults(self, client: httpx.AsyncClient) -> None:
        legacy = {"spellings": [{"text": "Zeal"}], "part_of_speech": "noun"}
        response = await client.post("/api/import", json=_legacy_file(legacy))
        assert response.json()["created"] == 1
        word = (await client.get("/api/words/by-spelling", params={"text": "zeal"})).json()
        assert (word["notes"], word["status"], word["examples"]) == (None, "learned", [])
        assert word["lexemes"] == [
            {
                "id": word["lexemes"][0]["id"],
                "position": 0,
                "part_of_speech": "noun",
                "pronunciation": None,
                "audio_path": None,
                "definitions": [],
            }
        ]

    async def test_legacy_conflict_policies(self, client: httpx.AsyncClient, word_id: int) -> None:
        body = _legacy_file(legacy_word_payload(notes="legacy"))
        assert (await client.post("/api/import", json=body)).json()["skipped"] == 1
        assert (await client.get(f"/api/words/{word_id}")).json()["notes"] is None
        response = await client.post("/api/import", params={"on_conflict": "replace"}, json=body)
        assert response.json()["replaced"] == 1
        assert (await client.get(f"/api/words/{word_id}")).json()["notes"] == "legacy"
        response = await client.post("/api/import", params={"on_conflict": "fail"}, json=body)
        assert response.status_code == 422

    @pytest.mark.parametrize(
        ("overrides", "fragment"),
        [
            (
                {"spellings": [{"text": "Abandon"}, {"text": "abandon"}]},
                "words.0.spellings: Value error, duplicate spelling: abandon",
            ),
            (
                {"part_of_speech": "   "},
                "words.0.lexemes.0.part_of_speech: Value error, part of speech must contain visible characters",
            ),
        ],
    )
    async def test_upgrade_failure_returns_422(
        self, client: httpx.AsyncClient, *, overrides: Json, fragment: str
    ) -> None:
        response = await client.post("/api/import", json=_legacy_file(legacy_word_payload(**overrides)))
        assert response.status_code == 422
        assert response.json()["detail"] == f"version 1 payload cannot be upgraded: {fragment}"
        assert (await client.get("/api/words/stats")).json()["words"] == 0

    async def test_mislabelled_versions_are_rejected(self, client: httpx.AsyncClient) -> None:
        v1_as_v2 = {"version": 2, "words": [legacy_word_payload()]}
        response = await client.post("/api/import", json=v1_as_v2)
        assert response.status_code == 422
        stray = sorted(err["loc"][-1] for err in response.json()["detail"] if err["type"] == "extra_forbidden")
        assert stray == [
            "audio_path",
            "definition_index",
            "definition_index",
            "definitions",
            "part_of_speech",
            "pronunciation",
        ]
        v2_as_v1 = {"version": 1, "words": [word_payload()]}
        response = await client.post("/api/import", json=v2_as_v1)
        assert response.status_code == 422
        stray = sorted(err["loc"][-1] for err in response.json()["detail"] if err["type"] == "extra_forbidden")
        assert stray == ["lexemes"]
        assert (await client.get("/api/words/stats")).json()["words"] == 0


class TestExportJson:
    async def test_empty(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/export.json")
        assert response.status_code == 200
        assert response.json() == {"version": 2, "words": []}

    async def test_shape(self, client: httpx.AsyncClient, word_id: int) -> None:
        created = (await client.get(f"/api/words/{word_id}")).json()
        exported = (await client.get("/api/export.json")).json()
        assert exported == {
            "version": 2,
            "words": [
                {
                    "spellings": [{"text": "Abandon", "kind": "primary"}],
                    "notes": None,
                    "status": "learned",
                    "created_at": created["created_at"],
                    "lexemes": [
                        {
                            "part_of_speech": "verb",
                            "pronunciation": "uh-BAN-dun",
                            "audio_path": None,
                            "definitions": [
                                {
                                    "text": "to give up",
                                    "source": "user",
                                    "examples": [{"text": NESTED_EXAMPLE, "source": "user"}],
                                }
                            ],
                        }
                    ],
                    "examples": [{"text": LOOSE_EXAMPLE, "source": "vault"}],
                }
            ],
        }

    async def test_roundtrip(self, client: httpx.AsyncClient) -> None:
        payloads = [
            word_payload(),
            word_payload(
                spellings=[{"text": "Color"}, {"text": "Colour", "kind": "variant"}],
                notes="n",
                status="inbox",
                lexemes=[
                    lexeme_payload(part_of_speech="noun", definitions=[_definition("d1"), _definition("d2", "e2")]),
                    lexeme_payload(
                        part_of_speech="verb", pronunciation=None, audio_path="/audio/color01.mp3", definitions=[]
                    ),
                ],
                examples=[{"text": "e"}],
            ),
        ]
        for payload in payloads:
            assert (await client.post("/api/words", json=payload)).status_code == 201
        exported = (await client.get("/api/export.json")).json()
        assert exported["version"] == 2
        assert [w["spellings"][0]["text"] for w in exported["words"]] == ["Abandon", "Color"]
        color = exported["words"][1]
        assert [x["part_of_speech"] for x in color["lexemes"]] == ["noun", "verb"]
        assert color["lexemes"][0]["definitions"][1]["examples"] == [{"text": "e2", "source": "user"}]
        assert color["lexemes"][1]["audio_path"] == "/audio/color01.mp3"
        assert color["examples"] == [{"text": "e", "source": "user"}]
        assert all(w["created_at"] for w in exported["words"])
        for item in (await client.get("/api/words")).json()["items"]:
            assert (await client.delete(f"/api/words/{item['id']}")).status_code == 204
        response = await client.post("/api/import", params={"on_conflict": "fail"}, json=exported)
        assert response.json()["created"] == 2
        assert (await client.get("/api/export.json")).json() == exported

    async def test_roundtrip_via_replace(self, client: httpx.AsyncClient) -> None:
        for payload in (word_payload(), word_payload(spellings=[{"text": "Betray"}], status="inbox")):
            assert (await client.post("/api/words", json=payload)).status_code == 201
        exported = (await client.get("/api/export.json")).json()
        response = await client.post("/api/import", params={"on_conflict": "replace"}, json=exported)
        assert response.json() == {"created": 0, "replaced": 2, "skipped": 0, "failures": []}
        assert (await client.get("/api/export.json")).json() == exported
        assert (await client.get("/api/words/stats")).json() == {
            "words": 2,
            "inbox": 1,
            "definitions": 2,
            "examples": 4,
        }


class TestExportMarkdown:
    async def test_zip_contents(self, client: httpx.AsyncClient) -> None:
        await client.post("/api/words", json=word_payload(notes="see also: forsake"))
        await client.post(
            "/api/words",
            json=word_payload(spellings=[{"text": "zeal"}], status="inbox", lexemes=[], examples=[]),
        )
        response = await client.get("/api/export.md")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert response.headers["content-disposition"] == 'attachment; filename="dictum-vault.zip"'
        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            assert zf.namelist() == ["A.md", "_Inbox.md"]
            assert zf.read("A.md").decode() == (
                "##### Abandon\n"
                "###### verb\n"
                "*uh-BAN-dun*\n"
                "- **to give up**\n"
                f"\t{NESTED_EXAMPLE}\n"
                "###### examples\n"
                f"\t{LOOSE_EXAMPLE}\n"
                "> see also: forsake\n"
            )
            assert zf.read("_Inbox.md").decode() == "##### zeal\n"

    async def test_lexeme_sections(self, client: httpx.AsyncClient) -> None:
        payload = word_payload(
            spellings=[{"text": "Record"}],
            lexemes=[
                lexeme_payload(
                    part_of_speech="noun",
                    pronunciation="REK-erd",
                    definitions=[_definition("a disc"), _definition("a log", "Keep a *record*.")],
                ),
                lexeme_payload(part_of_speech="verb", pronunciation=None, definitions=[_definition("to log")]),
            ],
            examples=[],
        )
        await client.post("/api/words", json=payload)
        response = await client.get("/api/export.md")
        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            assert zf.read("R.md").decode() == (
                "##### Record\n"
                "###### noun\n"
                "*REK-erd*\n"
                "- **a disc**\n"
                "- **a log**\n"
                "\tKeep a *record*.\n"
                "###### verb\n"
                "- **to log**\n"
            )

    async def test_empty_zip(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/export.md")
        assert response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            assert zf.namelist() == []
