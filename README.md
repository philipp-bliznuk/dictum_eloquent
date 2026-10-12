# Dictum Eloquent

Personal vocabulary notebook. Self-hosted, LAN only, one container, SQLite on disk.

- FastAPI + SQLModel + Alembic backend (`app.py`, `dictum/`)
- Svelte 5 + Vite single-page UI (`ui/`), served by the API with SPA fallback
- Headwords are single words or phrases (`abandon`, `in extenso`, `put paid to`, `By my troth!`)
- One entry per word, split by part of speech like a dictionary: each part of speech carries its
  own pronunciation, audio and definitions; examples sit under a definition or loose on the word
- Fuzzy search over every spelling, then over definition texts (`rapidfuzz`, in-memory index):
  `treasurer` lists the word itself first and `bursar` after it, via its definition
- JSON import/export (the backup format), plus a one-way markdown export in the Obsidian
  `A.md`..`Z.md` layout
- UI: Catppuccin Mocha, search bar on every page (`/` focuses it, arrow keys + Enter pick a hit,
  no hit offers "Add"), bottom tab bar on phones, top bar on wider screens

## Layout

```
app.py                 create_app() factory
dictum/
  settings.py          Settings(BaseSettings) - env / .env
  enums.py exceptions.py database.py
  models/              SQLModel tables: word, spelling, lexeme, definition, example
  schemas/             request / response models (+ partial_model factory)
  controllers/         generic Controller[Model] + WordController
  services/            search index, import/export, markdown vault renderer, text filters, AI
  routers/             registry: health, words, search, lookup, generate, transfer
migrations/            Alembic (async, sqlite batch mode)
ui/                    Svelte app (bun)
docker/                Containerfile + entrypoint (runs alembic upgrade head)
compose.yaml           bind mounts ./data -> /data, port 3000
```

## Develop

Requirements: `uv`, `bun`, `podman` (optional).

```bash
make install          # uv sync, bun install, pre-commit hooks
make dev              # api on :3000 with reload, sqlite in ./data (PORT=... to change)
make ui-dev           # vite on :5173, proxies /api to :3000
make check            # ruff format+lint, bandit, pytest (95% coverage gate), ui check
make ui-build         # writes ui/dist, picked up by `make dev`
```

API docs: `http://localhost:3000/api/docs` (disabled in test env).

## Configuration

Copy `.env.example` to `.env`. Everything optional except the DB path in prod.

| Var | Default | Purpose |
| --- | --- | --- |
| `ENVIRONMENT` | `prod` | `local` enables debug + SQL echo |
| `LOG_LEVEL` | `INFO` | `DEBUG` .. `CRITICAL`, case-insensitive |
| `ALLOWED_HOSTS` | `*` | `Host` allow-list (`,` `;` or space separated); set it on a LAN box (see below) |
| `DATA_DIR` | `/data` | sqlite file + cached audio |
| `DATABASE_URL` | `sqlite+aiosqlite:////data/dictum.db` | async SQLAlchemy URL |
| `BIND` | `0.0.0.0:3000` | gunicorn bind (container only) |
| `PUID`, `PGID` | `1000`, `1000` | uid/gid the container drops to; owner of `/data` (container only) |
| `HTTP_TIMEOUT` | `10` | seconds per upstream request (dictionary APIs, audio) |
| `MW_API_KEY` | empty | enables Merriam-Webster Collegiate as first lookup provider |
| `MW_BASE_URL` | collegiate json endpoint | override for tests / proxies |
| `MW_AUDIO_BASE_URL` | `media.merriam-webster.com/...` | where pronunciation mp3s come from |
| `FREE_DICTIONARY_BASE_URL` | `api.dictionaryapi.dev/...` | keyless fallback provider |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | empty | both set = AI drafting on (Bedrock); see below |
| `AWS_REGION` | `us-east-1` | Bedrock region |
| `BEDROCK_MODEL_ID` | Claude Haiku 4.5 inference profile | any Converse model with forced tool use (Claude 3+, Nova) |

Lookup tries providers in order and stops at the first hit: Merriam-Webster (only with a key,
free tier is 1000 queries/day), then dictionaryapi.dev. A provider error is reported in the
response and the chain moves on; only when every provider fails does the request return `502`.
Provider examples are kept only when they read as clean sentences (capital start or terminal
punctuation, no `/` line breaks, ellipses or editorial brackets); dictionary fragments such as
`with reckless *abandon*` are dropped, and with AI configured the add form fills the gaps itself.
Pronunciation audio is downloaded once into `DATA_DIR/audio` and served from `/audio/<file>.mp3`;
the UI falls back to the browser's speech synthesis when there is none.

There is no auth. The one thing that turns "LAN only" into "any web page you visit" is DNS
rebinding, and `ALLOWED_HOSTS` closes it: list the names and IPs you actually type into the
browser, e.g. `ALLOWED_HOSTS=dictum.local,192.168.1.20`. Entries are forgiving: `,` `;` or spaces
separate them, and `http://Dictum.LOCAL:3000/` counts as `dictum.local`; `*.lan` matches any host
under that suffix. `localhost`, `127.0.0.1` and `::1` are always accepted (the container
healthcheck needs them, and a rebinding page cannot make a browser send them). Anything else gets
a JSON `400` naming the host, and the server log shows both the rejected host and the parsed list
at startup (`allowed_hosts=...`), so a mismatch is visible in `make logs`. An empty value refuses
to start instead of silently blocking everything.

### AI drafting (optional)

The AI never sits in the lookup chain. It only answers requests from the add/edit form: "Generate
examples" per definition, "Generate missing examples" for every definition still short of two
examples (the form runs this on its own right after a successful lookup, so dictionary entries whose
examples were filtered out as fragments arrive complete; it only tops up to two, never past),
"Draft definitions with AI" inside one
part of speech (restricted to it, with the definitions already typed there passed as hints), and
"Ask AI to draft it" after every dictionary came back empty. Every AI item is tagged `ai` and
editable before saving; a misspelling is reported as not recognised rather than silently corrected.
Calls go through the Bedrock Converse API with a forced tool call, so the model can only answer in
the exact JSON shape the app validates; a truncated or refused answer is a `502`, never a
half-filled form.

Drafting follows one sense policy: every distinct sense a well-read speaker may meet (common,
literary, archaic), closely related senses merged into one definition, jargon only when it is the
main use, most common first, at most 8 per part of speech. `POST /api/generate/definitions` takes
optional `hints` (senses you noted earlier; the model covers them first, rewritten cleanly, then adds
what is missing), `parts_of_speech` (restrict drafting, e.g. `["noun"]` when a dictionary lists the
word only as a verb) and `per_definition` (examples per definition, 1 to 5).

Setting `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` enables it. Because the app has no auth,
anyone who can reach port 3000 can spend your Bedrock budget, so when those keys are set:

- set `ALLOWED_HOSTS` (above); it is mandatory, not a recommendation, in this mode
- scope the IAM policy to `bedrock:InvokeModel` on the inference profile ARN and the foundation
  model ARNs it routes to, nothing else, so a leaked key cannot run bigger models
- put an AWS Budgets alert on the account (a few dollars a month is plenty for one user)

The app caps itself at two concurrent Bedrock calls, one retry, 2048 output tokens for examples and
4096 for drafting whole entries, and shows clients only the AWS error code (`AccessDeniedException`);
the full message stays in the log.
A Pi has no RTC: after a power cut the first calls can fail with a signature/clock error until
NTP catches up, and botocore retries with the corrected clock on its own.

## Run as a container

```bash
cp .env.example .env && mkdir -p data
make build up         # podman compose; `make logs` / `make down`
```

The container starts as root, `chown`s `/data` to `PUID`/`PGID` (default `1000:1000`), then
drops to that user before running migrations and gunicorn (linuxserver.io style). Set `PUID`
and `PGID` to the owner of the data folder on the host. Rootless podman with
`--userns=keep-id:uid=1000,gid=1000` skips the switch and simply needs a writable `/data`;
the entrypoint refuses to start with a clear message otherwise.

`./data` holds everything worth backing up (sqlite + WAL + cached audio). Do not copy
`dictum.db` alone while the app runs; the `-wal` file carries recent writes. Either snapshot
the dataset (ZFS/btrfs, atomic) or ask SQLite for a consistent copy:

```bash
sqlite3 data/dictum.db ".backup backup/dictum-$(date +%F).db"
```

## Data model

```
word         status (inbox | learned), notes
  spelling   text, kind (primary | variant)        unique across all words, case-insensitive
  lexeme     part_of_speech, pronunciation, audio_path   one per part of speech, ordered
    definition   text, source (user | mw | free_dict | ai), ordered
  example    text, source (user | vault | mw | free_dict | ai), ordered
             owned by the word; optionally linked to one definition
```

A lexeme is the word used as one part of speech (`record` the noun, `record` the verb) so stress,
audio and senses stay apart. Parts of speech are free text, normalised (whitespace collapsed,
case-folded) and unique within a word; dictionary homographs such as `bank:1` / `bank:3` fold into
one lexeme per part of speech. Examples that illustrate a definition are nested under it in every
JSON shape; the rest stay on the word and show up as "More examples". Deleting a definition keeps
its examples as loose ones.

## API

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/health` | liveness + `SELECT 1`; also `providers` list and `ai` flag |
| GET | `/api/words?status=&offset=&limit=` | paged summaries (`parts_of_speech`, `definition_count`) |
| GET | `/api/words/stats` | counters |
| GET | `/api/words/by-spelling?text=` | case-insensitive, whitespace-normalised, phrases ok |
| GET/PUT/PATCH/DELETE | `/api/words/{id}` | PUT replaces spellings, lexemes and examples (`created_at` kept); PATCH is for `status` / `notes` |
| POST | `/api/words` | 409 + `Location` header when a spelling exists |
| POST | `/api/words/{id}/audio` | fetch + cache pronunciation audio for every lexeme still without it, matched by part of speech; 404 if none |
| POST | `/api/lookup` | `{"text"}` -> draft entries, suggestions, provider attempts, `existing_word_id` |
| POST | `/api/generate/examples` | `{"word", "part_of_speech", "definitions": [..], "per_definition"}` -> example groups aligned with `definitions`; 503 when AI is off |
| POST | `/api/generate/definitions` | `{"text", "hints": [..], "parts_of_speech": [..], "per_definition"}` -> `found` + draft entries, same shape as lookup entries; only `text` is required; 503 when AI is off |
| GET | `/api/search?q=&limit=` | one hit per word; spelling hits (exact, prefix, fuzzy, inside phrases) first, then definition-text hits (`kind: "definition"`, `matched` = the definition) for queries of 4+ characters |
| POST | `/api/import?on_conflict=skip\|replace\|fail` | `{"version": 2, "words": [...]}`; a `"version": 1` file (flat `part_of_speech` + `definitions` + `examples` with `definition_index`) is upgraded on the fly |
| GET | `/api/export.json` | version 2, same shape as import, plus `created_at` per word |
| GET | `/api/export.md` | zip of `A.md`..`Z.md` + `_Inbox.md`; one-way, JSON is the backup |

Word shape (import, export and `GET /api/words/{id}` alike):

```json
{
  "spellings": [{"text": "record", "kind": "primary"}],
  "status": "learned",
  "notes": null,
  "lexemes": [
    {
      "part_of_speech": "noun",
      "pronunciation": "ˈre-kərd",
      "audio_path": "/audio/record03.mp3",
      "definitions": [
        {"text": "a written account", "source": "mw",
         "examples": [{"text": "Keep a *record* of every call.", "source": "ai"}]}
      ]
    },
    {"part_of_speech": "verb", "pronunciation": "ri-ˈkȯrd", "audio_path": "/audio/record01.mp3",
     "definitions": [{"text": "to set down in writing", "source": "user", "examples": []}]}
  ],
  "examples": [{"text": "with no *record* of the meeting", "source": "vault"}]
}
```

Text fields accept a markdown subset: `**bold**`, `*em*`, `_em_`. Lexemes, definitions and
examples keep the order of the arrays you send. Write payloads are strict: unknown keys are
rejected with 422 (`Extra inputs are not permitted`), so derived fields such as `position`,
`id`, `created_at` (except in import files) must not be sent back, and a version 1 word
mislabelled as version 2 fails loudly instead of importing half of its content. Markdown export
writes `##### Headword / Variant`, then `###### <part of speech>` with
its `*pronunciation*` and `- **definition**` lines (tab-indented examples below each), loose
examples under `###### examples`, notes as `> ` quote lines.

`POST /api/lookup` persists nothing. It returns one draft entry per part of speech with the
provider's definitions and example sentences (target word wrapped in `*..*`), spelling variants
(`aggrandise` -> `aggrandize` primary + `aggrandise` variant), pronunciation and cached audio
path. Merriam-Webster run-ons with the same spelling (`nitpick` listed as a verb with an undefined
noun) come back as an entry with no definitions, which the form fills with "Draft definitions with
AI" restricted to that part of speech. The UI merges entries into the add form by part of speech,
capitalising the first letter of every spelling on the way (`galore` -> `Galore`, `élan` -> `Élan`),
where every item can be edited or dropped before `POST /api/words`. Pronunciations keep the
Merriam-Webster respelling as delivered; the UI renders the voiced `th` tie (`t͟h`) as an underlined
`th` instead of a stray combining mark.

The container healthcheck polls `GET /api/health` every 30 seconds; those requests are filtered out
of the access log (`uvicorn.access`) so `make logs` shows only real traffic.

Spelling rules: 1..255 chars, inner whitespace collapsed, ` / ` not allowed (it separates
variants in the vault headline `##### Aggrandize / Aggrandise`). Uniqueness and lookup
are case-insensitive and Unicode aware (NFKC + casefold): `Éclair` = `éclair`, `Straße` = `STRASSE`.
