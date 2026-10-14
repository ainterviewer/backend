# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AInterviewer is a FastAPI backend for conducting automated AI-powered interviews at scale. The system integrates with the `ainterviewer` library (a sibling package) to provide real-time interview capabilities via WebSocket, managing interviews, analysis, and synthetic interview generation.

## Essential Commands

### Development

```bash
# Start development server (port 8666)
just dev
# or
uv run -m fastapi dev app/main.py --port 8666

# Install dependencies
uv sync
```

There is no SDK or OpenAPI recipe in this repo. The TypeScript client is
generated from the **frontend** repo against this backend's live
`/api/openapi.json`; see "Making Changes" below.

### Database Migrations (Alembic)

```bash
# Create a new migration
uv run alembic revision --autogenerate -m "description"

# Apply migrations
uv run alembic upgrade head

# Downgrade one revision
uv run alembic downgrade -1

# View migration history
uv run alembic history
```

### Code Quality

```bash
# Run ruff linter and formatter
uv run ruff check .
uv run ruff format .
uv run ty check app

```

### Testing

```bash
# Run tests (pytest)
uv run pytest

# Run load tests (Locust)
uv run locust
```

## Architecture

### Layered Structure

The codebase follows a clean layered architecture with clear separation of concerns:

```
API Layer (app/api) → Services (app/services) → Repository Pattern (app/db) → ORM (tables.py) → Database
```

### Core Components

**1. API Layer (`app/api/`)**

- Organized by feature domain with sub-routers
- Main aggregator: `api/main.py` combines all routes under `/api` prefix
- Key modules:
  - `auth.py`: Login, registration, JWT token management
  - `dashboard/`: Project CRUD, analysis, folders, collaborators, experiments
  - `ws.py`: WebSocket endpoint for real-time interviews
  - `admin/`: Access requests, user management, cloud operations
- Custom operation ID generation for clean SDK client generation
- Generic `PaginatedResponse[T]` pattern for list endpoints

**2. Repository Pattern (`app/db/repositories/`)**

- `InterviewDataBase` facade implements `PersistenceProtocol` from ainterviewer library
- All repositories share a single SQLAlchemy session (transactional consistency)
- Specialized repositories (`app/db/repositories/`):
  - `UserRepository`, `AuthRepository`, `VerificationRepository`: users, invites,
    access requests, credentials and email verification
  - `ProjectRepository`: Projects, folders, collaborators, multi-language support
  - `InterviewRepository`: Interview records, messages, feedback tracking
  - `AnalysisRepository`: the codebook, the codings made with it, comments,
    and the filtered-message queries
  - `TestRepository`: Experiment management
  - `ParticipantRepository`, `AssistanceRepository`, `NewsletterRepository`
- `errors.py` holds the domain exceptions repositories raise (e.g.
  `ProjectLanguageError`); the API layer maps them to HTTP status codes rather
  than repositories raising `HTTPException` themselves

**3. ORM Layer (`app/db/tables.py`)**

- SQLAlchemy 2.0+ with typed mapped columns and relationships
- UUID primary keys throughout
- JSON/JSONB columns for complex data (interview guides, configs, prompts)
- Automatic timestamps (created_at, updated_at)
- Foreign key constraints with cascade options

**4. Authentication & Authorization (`app/auth.py`)**

- Two token types:
  - `AuthToken`: API access (JWT in secure httponly cookies)
  - `InterviewToken`: Interview participation (includes project/interview IDs)
- Hierarchical scopes: `ADMIN` → `USER` → `GUEST`
- `ScopeChecker` class for dependency injection-based authorization
- Pre-configured aliases: `AdminToken`, `UserToken`, `GuestToken`

**5. WebSocket Management (`app/api/websockets/`)**

- `manager.py` — `InterviewSessionManager`: tracks active sessions per project/interview
- `handler.py` — `WebsocketMessageHandler`: implements `IOProtocol` to bridge WebSocket ↔ ainterviewer library
- `interviews/` — the interview loop itself, including agent/template wiring (`interviews/ai.py`)
- Automatic message queueing for embedding generation after send/receive
- Image upload support (path over WS, full file over HTTP)
- System messages broadcast when users disconnect

### Critical Integration: ainterviewer Library

The backend is tightly coupled with the `ainterviewer` library (sibling package at `../lib`). Key imports:

- `ainterviewer.interview.AInterviewer`: Main interview orchestration engine
- `ainterviewer.agents`: AnsweringAgent, probing agents
- `ainterviewer.types`: Core enums (Interviewer, MessageRole, MessageType, LanguageCode, Feedback)
- `ainterviewer.interview_guides`: InterviewGuide, SurveyItem, Image, Consent, Welcome
- `ainterviewer.config`: AgentConfigs, InterviewConfig
- `ainterviewer.interfaces`: Protocol classes (`IOProtocol`, `PersistenceProtocol`)

**Important**: Changes to the ainterviewer library may require updates to the backend's protocol implementations.

### Database Support

**Default: SQLite**

- WAL mode enabled for concurrency
- SQLiteAI vector extension for embeddings
- Pragmas live in `app/db/pragmas.py` and are applied to **every** connection
  via a `connect` event listener registered in `app/dependencies.py`. They are
  per-connection state, so setting them anywhere else (as
  `create_db_and_tables` used to) leaves the pooled connections that actually
  serve requests on SQLite's defaults. Add new pragmas to `_SQLITE_PRAGMAS`,
  never to a one-off `session.execute`.
- **Foreign keys are enforced** (`enforce_foreign_keys`, default `true`):
  `register_pragmas` adds `foreign_keys=ON` to every connection, so the
  declared `ON DELETE CASCADE` / `SET NULL` fire and a write pointing at a
  missing row raises `IntegrityError`. `DATABASE__ENFORCE_FOREIGN_KEYS=false`
  and a restart switch it off without a deploy if it ever surfaces a violation
  in production.
  - Keep deleting child rows explicitly in Core `delete()` paths
    (`InterviewRepository.delete_interviews`, `TestRepository.delete_test_setup`,
    `BaseRepository._delete_message_children`): they must stay correct with
    enforcement switched off, which is how orphans accumulated before.
  - Every endpoint that deletes interviews calls `session_manager.end(...)`
    first, so live interview sessions stop before their rows go. A session
    left running writes messages and tasks for a deleted interview -- that is
    what orphaned 1,097 rows in production on 2026-10-06. Any new endpoint that
    deletes interviews must do the same. If a write still lands on a deleted
    interview, `insert_message` / `insert_task` raise `InterviewGoneError` and
    the websocket sends the restart frame. The session registry is
    per-process: with more than one worker, that backstop is all that ends
    sessions on other workers (see `InterviewSessionManager`).
  - Validate ids a client supplies (test run, participant, interview) against
    the project and answer 404; don't leave it to the foreign key, which turns
    a stale id into a 500. `is_foreign_key_violation` (`app/db/utils.py`) tells
    a foreign-key failure from a unique one where code expects the latter.
  - Every foreign-key column is indexed (revision `0fa4a271248f`); index new
    ones too, or deleting a parent scans the child table once per row.
  - Alembic runs with foreign keys **off**: `alembic/env.py` builds its own
    engine without the pragmas. Keep it that way -- `batch_alter_table`
    rebuilds a table by dropping it, and with enforcement on that drop deletes
    every row that cascades from it. Point Alembic at another database with
    `-x db_url=sqlite:///path`; it does not read the app settings.
  - `PRAGMA foreign_key_check` should return nothing. Revision `4f8273d9e6f2`
    derives its orphan sweep from the schema and can be re-run safely.
- Storage location: `storage/db.sqlite`
- The triggers in `app/db/triggers.py` that bump a parent's `last_updated`
  stamp `app_now()`, a Python function registered on every connection
  (`register_trigger_functions`, `app/dependencies.py`) so they write the same
  clock and format as the app's `now()`. Never use `CURRENT_TIMESTAMP` there:
  it is UTC with whole seconds, which is how projects came to show a
  `last_updated` hours before their `created_at` (fixed by revision
  `6b1d0e7a9c35`). The cost is that a connection without the function -- the
  `sqlite3` shell, a new engine -- fails writes to `projectcontent`,
  `testsetup` and `testrun` with "no such function: app_now"; register it on
  any engine you build. That includes Alembic's (`alembic/env.py`): older
  migrations call `install_triggers()` mid-run, so the triggers can fire
  during later ones even though env.py takes them down first.

**PostgreSQL: not implemented.** `DatabaseType.POSTGRESQL` exists and the
engine is created with a 20/40 pool, but `DatabaseSettings.connection_string`
builds no URL for it (the branch is a `pass`), so `db = "postgresql"` fails at
startup. Nothing reads a `DATABASE_URL` variable.

### Embeddings

Interview text is vectorised by a
[text-embeddings-inference](https://github.com/huggingface/text-embeddings-inference)
server (`microsoft/harrier-oss-v1-0.6b`, 1024d), configured under
`[services.embedding]` / `APP_SERVICE__EMBEDDING__*` and **disabled by
default**: with `enabled = false` nothing is embedded and an unreachable server
changes nothing about how interviews run.

**One vector per chunk, not one per task.** The model is asymmetric on the query
side only -- its card specifies `"Instruct: {task_description}\nQuery: {query}"`
for queries and encodes passages with no prefix at all. So a passage is embedded
once, task-free, and the downstream task lives entirely in how the *query* is
templated (`app/embed/templates.py`). `EmbeddingTable.task` exists so a
symmetric-task variant (clustering, STS) could be added as a second vector per
chunk without a migration, but only `DOCUMENT` is ever written. Do not
reintroduce a per-task fan-out on the document side; it multiplies storage and
inference for no measurable gain against this model.

**What gets embedded is decided in the library**, in `ainterviewer.embedding`:

- `MESSAGE` -- respondent free text only. Interviewer turns are recoverable
  through their QA pair, and closed-ended survey answers are excluded outright:
  a likert value drawn from a handful of option strings produces near-identical
  vectors in bulk that crowd out real answers and drag clustering toward the
  survey scaffolding. Count those, don't embed them.
- `QA_PAIR` -- the primary analytic unit: a main question, its answer and every
  probe, but only when the group drew at least one free-text answer. A survey
  answer still appears *inside* a qualifying pair as context.
- `INTERVIEW` -- the whole transcript, for interview-level retrieval.

*Assembly* lives in the library because both producers need it and one of them
is inside the library: the interview loop emitting chunks live, and
`app/embed/backfill.py` re-deriving them from stored messages through
`InterviewHistory.process_history`. Two implementations would drift, and
`content_hash` -- which is what stops the backfill re-embedding everything on
every run -- is only stable if the rendered text is byte-identical between them.

*Policy* is separate and replaceable. The list above is `DefaultChunkPolicy`,
not a law: whether a closed-ended survey answer deserves a vector is a
research-methodology judgement, so every such decision sits on the `ChunkPolicy`
protocol and can be swapped by passing `chunk_policy=` to `AInterviewer` and
`policy=` to the backfill. **A consumer must pass the same policy to both**, or
the live path and the backfill produce different text for the same chunk.

`ChunkPolicy.format_version` rides on every chunk and is stored in
`EmbeddingTable.format_version`. A stored vector is a function of the model
*and* of the rules that chose and rendered its text, so `needs_embedding`
compares both the content hash and the format version -- the hash alone cannot
see a policy change that alters which chunks are included without changing how
the survivors render. Bump `CHUNK_FORMAT_VERSION` whenever the default policy's
output changes, and the next backfill re-embeds exactly what went stale.

**Synthetic test interviews are never embedded.** `app/synthesize/core.py`
simply passes no `embedder` to `AInterviewer`, so the synthetic path
structurally cannot emit chunks, and the backfill filters
`InterviewType.SYNTHETIC_TEST`. This is not a nicety: they are 64% of the
message corpus, and mixing model output into an analysis corpus would let
generated answers surface as if a respondent had said them.

**Search is an exact brute-force scan** (`EmbeddingRepository.search`), not the
`sqlite-vector` ANN index. `vector_quantize_scan` returns a *global* top-k that
cannot be filtered -- there is no way to scope it to a project, let alone a date
range -- so over-fetching and filtering afterwards would give no guarantee of
returning k results. Stored vectors are L2-normalised raw float32, so scoring is
one matrix product and cosine is a plain dot product. At the current corpus size
this is single-digit milliseconds with every SQL filter available; `search` is
the seam to put an ANN index behind if one project ever passes ~50k chunks.

**The live path is deliberately lossy.** `AInterviewer` emits chunks through
`QueueEmbedder` into an in-memory `ChunkQueue`; `EmbeddingWorker` (started in
the `lifespan` in `app/main.py`) batches them and stores them. `embed_chunk`
never blocks and never raises -- if the queue is full or the server is down the
chunk is dropped and counted in `queue_dropped`. That is a freshness cost, not
data loss, because the backfill re-derives every chunk from the stored messages.
Blocking there would cost a respondent their session. The same reasoning covers
the gaps the live path cannot see: an abandoned interview never completes its
last question group, and a resumed one replays without re-emitting. **The
backfill is not a one-off migration tool** -- run it on a schedule.

`live = false` (`APP_SERVICE__EMBEDDING__LIVE`) turns the live path off to save
inference cost: interviews get no embedder, and the server is only called for
search and for a backfill someone requested from the dashboard or the CLI. The
worker keeps running regardless, because the dashboard backfill goes through
it. There is no automatic backfill to switch off -- a schedule is a cron
running the CLI, outside this app. The status endpoint reports `live`, so a
corpus that lags behind its interviews reads as configuration, not a fault.

Two ways to run it, for two different situations:

```bash
uv run python -m app.embed.cli status              # reachability + vector counts
uv run python -m app.embed.cli backfill --dry-run  # count chunks, call nothing
uv run python -m app.embed.cli backfill            # embed inline, blocking
```

`POST /projects/{id}/analysis/embeddings/backfill` is the in-app equivalent, but
it only *derives* the chunks (database work, seconds) and queues them for the
worker; embedding them is minutes of network work against a CPU box. Do not
reach for `BackgroundTasks` here -- it holds a threadpool worker for the whole
job, which for a full project means half an hour. Poll
`GET /projects/{id}/analysis/embeddings/status`; `queue_depth` reaching zero is
what "done" looks like.

Search is `GET /projects/{id}/analysis/embeddings/search`, `ProjectViewer`-gated
like the rest of the analysis surface. `task` picks the query instruction and
`kind` the unit searched; results carry the cosine score so a client can cut off
weak matches. It pages with `limit`/`offset` like the rest of the dashboard's
lists, capped at `offset + limit <= MAX_SEARCH_DEPTH` (1000) -- a ranked scan
scores every chunk in scope, so `total` is the length of the ranking and not a
count of matches, and without the cap the endpoint is a way to page a whole
project out one screen at a time. Each page re-embeds the query and re-scores
the candidates rather than caching a ranking: it is the same millisecond-scale
matrix product the first page does, and it keeps a page a function of the query
and the corpus alone. A 409 means the query's dimension no longer matches the stored
vectors -- the model changed and the corpus needs re-embedding.

`GET …/embeddings/{embedding_id}/similar` is "more like this". It reuses the
stored vector, so it costs no inference and works while the embedding server is
down.

**Filters are applied in SQL, before scoring** (`EmbeddingFilters`), which is
the advantage an exact scan has over an ANN index: asking for 10 results from
one participant returns 10 of theirs, not whichever of the global top 10 happen
to be theirs. `candidates` in the response reports how many chunks were scored,
so a client can tell "nothing matched" from "the filters left nothing to match".

The same ordering matters more for clustering than for search: a filtered-out
chunk is never *fitted*, so scoping to one language removes that dimension
outright rather than subtracting its mean. On the Danish/English project,
`language=DA` + `center_by_question` reached 0.47 question purity against 0.50
for centering both axes over the whole corpus -- filtering is the better
analysis whenever the corpus can afford it. What it costs is the corpus:
`language=EN` there is 246 message chunks and collapses to two clusters, and no
filter can answer whether the two languages talk about a thing the same way.
Filter to analyse a language; centre to compare across them.

`language` is repeatable (`?language=DA&language=EN`) and validated against the
`LANGUAGES` constant via `LanguageFilter` (`app/api/request_models.py`). Both
halves of that matter: a multilingual project is usually analysed over the
languages with enough respondents to say anything, and a bare `LanguageCode`
only checks the shape -- "DK" is Denmark's country code, passes, and matches
nothing, which reads as "no Danish data" rather than as a typo. A malformed code
used to reach the column type and surface as a `StatementError` 500.

**Clustering** is `GET …/embeddings/clusters` (`app/embed/clustering.py`):
HDBSCAN in a reduced space, with the scatter taken from the first two dimensions
of that *same* space rather than a separate fit, so the picture is always a
sub-projection of where the clusters were found. HDBSCAN rather than k-means
because exploratory work does not know `k` up front and because unplaceable
points come back as outliers instead of being forced into the nearest blob.
Nothing is stored.

`projection` chooses how the dimensions come down, and the invariant above holds
either way:

- `pca` reduces linearly to 50 components and plots the first two of them.
  PCA's components are nested, so those two columns *are* a 2-component PCA. A
  full recompute is ~200ms, which keeps `min_cluster_size` genuinely
  interactive. Its weakness is the reason UMAP was added: 50 dimensions is
  still high enough for distances to concentrate, and HDBSCAN returned few
  large blobs.
- `umap` (the default) reduces non-linearly to 2 dimensions -- through a PCA to
  50 first, which denoises and makes the neighbour search affordable -- and
  clusters in those two. Neighbourhoods separate far more sharply, which is what
  makes the picture readable. Three costs, all visible in the response: seconds
  rather than milliseconds (plus a one-off numba compile the first time a
  process runs one, which is why `umap` is imported inside `_project` and not at
  module scope), `explained_variance_2d` comes back NULL, and clustering in two
  dimensions can manufacture a split between neighbourhoods that are not really
  apart. **Distances on a UMAP scatter mean nothing** -- read which points sit
  together, never how far apart two clusters are or how big one looks. Reach for
  `pca` when the geometry has to mean something.

`n_neighbors` and `min_dist` are UMAP's own knobs and are ignored under PCA.
`min_dist` defaults to 0.0 rather than UMAP's 0.1: HDBSCAN separates on density,
and slack between points blurs exactly the gaps it needs. The UMAP seed is fixed
(`UMAP_RANDOM_STATE`) so the same corpus gives the same picture twice, which
costs single-threading -- comparability is worth more here than the seconds.

The route runs `cluster_vectors` through `run_in_threadpool`. A seconds-long
UMAP fit inside an `async def` would stall every other request in flight.

**Some of what these vectors encode is scaffolding, not content**, and left
alone it is what clustering finds. Two confounds, both measured on the real
corpus, both handled the same way -- named as a `GroupAxis`, always *reported*
as a per-cluster purity, and optionally *removed* by centering:

- **Question.** A QA-pair chunk repeats its interview question verbatim, and
  every respondent was asked the same one. Uncentred, clusters came out 79-100%
  pure by question, against 28-77% for message chunks. `center_by_question=true`
  takes that to 55%.
- **Language.** A multilingual project embeds every language into one space, and
  the model separates languages before it separates topics. On the Danish/
  English project, `center_by_question` alone gave six clusters of which the two
  largest (628 and 241 chunks) were simply Danish and English -- mean
  `language_purity` 0.99. `center_by_language=true` as well: 52 clusters, mean
  language purity 0.76, question purity 0.50.

Setting both centres on the **composite** key, subtracting the mean of each
language-within-question cell. That is stronger than either alone -- centering
by question does nothing about language, because a question group spans both
languages and the language axis survives inside it. The cost is thinner cells:
outliers went 1/915 to 255/915 on that corpus, because a cell of one chunk
becomes the zero vector. Filtering to a single `language` is the blunter
alternative and analyses one language properly instead of comparing across them.

Do not remove the purity figures to tidy the response -- they are the only thing
that makes these failures visible rather than something an analyst discovers a
month later. The language confound went unnoticed until someone read the
clusters by eye. `groups` now also carries a `language` row per language in
scope, so the scatter can be coloured by language directly; that is the fastest
way to see whether a split is real.

`explained_variance_2d` is the companion honesty signal **under `pca`**: for text
embeddings it runs around 30%, so the plot is a navigation aid, not evidence.
Points far apart on screen really are far apart; points close together may not
be. It is NULL under `umap`, which has no such quantity -- and there, not even
"far apart on screen means far apart" holds.

`EmbeddingTable.text` holds the embedded text in full rather than a preview. It
has to: a QA-pair chunk spans several messages and carries no `message_id`, so
there is nothing a client could resolve it to, and a truncated preview cuts the
average pair mid-sentence. Search hits also carry their interview's timestamp,
status, type and participant, so a result list costs one request rather than one
per row. `uv run python -m app.embed.cli rehydrate-text` fills the column in for
rows stored before it existed, matching on content hash so no vector is
recomputed and no text can land beside a vector made from something else.

### Configuration Management

**Multi-source configuration** (`app/settings.py`):

- Sources (priority order): Environment variables → `.env` → `pyproject.toml` → `config.toml`
- Pydantic BaseSettings with validation
- Prefixes: `APP_SECRET__` and `APP_SERVICE__` for those two sections; every
  other section is read with no prefix and `__` as the nesting delimiter, so
  `[database] enforce_foreign_keys` is `DATABASE__ENFORCE_FOREIGN_KEYS`, not
  `APP_DATABASE__…` (which is silently ignored)
- Example: `APP_SECRET__JWT_SECRET_KEY` overrides default JWT secret

## Development Workflow

### Making Changes

1. **API Endpoint Changes**:
   - Update routes in `app/api/`
   - Update request models in `app/api/request_models.py`
   - Update response models in `app/api/response_models.py`
   - Regenerate the frontend SDK from the **frontend** repo: `just generate-sdk`
     there, with this backend's dev server running. It reads the live
     `/api/openapi.json` (see `app/openapi.py`) using the generator version
     pinned in the frontend's `package.json` — never generate it via `bunx`,
     which resolves to an unpinned latest and rewrites the vendored client
     runtime.

2. **Database Schema Changes**:
   - Modify ORM models in `app/db/tables.py`
   - Create migration: `uv run alembic revision --autogenerate -m "description"`
   - Review generated migration in `alembic/versions/`
   - Apply: `uv run alembic upgrade head`

3. **Adding New Repositories**:
   - Extend `BaseRepository` class
   - Add to `InterviewDataBase` facade
   - Ensure session sharing for transactional consistency

4. **WebSocket Protocol Changes**:
   - Implement changes in `WebsocketMessageHandler`
   - Ensure compatibility with ainterviewer library's `IOProtocol`
   - Test message serialization/deserialization

### Working with ainterviewer Library

The library is in editable mode from `../lib`. Changes to the library are immediately reflected:

```bash
# Library location
cd ../lib

# Backend uses local version
# See pyproject.toml: ainterviewer = { path = "../lib", editable = true }
```

#### Agent prompt templates

The agents' Jinja prompt templates live in the library
(`../lib/src/ainterviewer/agents/prompts/templates/EN/`) and are resolved at
interview time via `PackageLoader`, so **editing a template there is enough --
no migration is needed and every project picks it up on the next deploy.**

`ProjectContentTable.prompt_overrides` holds only per-project overrides,
keyed by template name (`"probing_agent/system_prompt.jinja"`). It is empty for
virtually every project. The interview loader is a `ChoiceLoader` that tries the
overrides first and falls through to the package
(`app/api/websockets/interviews/ai.py`), which is what makes a newly added
template impossible to "miss" for an existing project.

Do **not** reintroduce snapshotting whole prompt sets into the database. That
was the old design: it froze each project on the templates that existed when it
was created, raised `TemplateNotFound` mid-interview whenever the library added
one, and required a hand-written data migration per template change (see
revisions `f7aaeeea0a76` and `b4e91c07d3a2`). It also looked like it pinned
prompt behaviour but did not -- it captured no agent code, model IDs or schemas,
and the resync migrations overwrote it wholesale anyway. Real reproducibility
needs a pinned library version plus workers running it; that is a separate,
unbuilt concern.

User-facing prompt customisation is exposed through
`agent_configs.probing.prompt_slots`, not through this column.

### Running with Different Configurations

```bash
# Override config values via environment (no APP_ prefix outside the
# secret and service sections)
DATABASE__DB_PATH=/tmp/scratch-db just dev
```

The config file is always `config.toml` in the working directory; there is no
variable to point at another one.

## Important Patterns

### Dependency Injection for Auth

Always use typed annotations for automatic scope checking:

```python
from app.dependencies import AdminToken, UserToken


@router.get("/admin-only")
async def admin_endpoint(token: AdminToken):  # Only ADMIN scope
    # token.user_id is UUID of authenticated user
    pass


@router.get("/user-endpoint")
async def user_endpoint(token: UserToken):  # USER and ADMIN allowed
    pass
```

### Pagination Pattern

Use `PaginatedQueryParams` and `PaginatedResponse[T]`:

```python
from app.api.request_models import PaginatedQueryParams
from app.api.response_models import PaginatedResponse


@router.get("/items")
async def list_items(
    params: Annotated[PaginatedQueryParams, Depends()],
) -> PaginatedResponse[ItemModel]:
    items = db.get_items(limit=params.limit, offset=params.offset)
    total = db.count_items()
    return PaginatedResponse(results=items, total=total)
```

### Project languages and content

**Everything a project says is stored once, not per language.** One
`projectcontent` row (`ProjectContentTable`) per project holds the guide,
consent, welcome and participant emails, plus `agent_configs` and
`prompt_overrides`. Text is stored in the library's localized form
(`ainterviewer.interview_guides.localized`): the structure once -- sections,
questions and their order, settings, survey items, conditions -- and every
piece of text a `{language: text}` map. Every language asks the same questions
in the same order; only the wording differs.

The same row says which languages the project offers (`languages`) and which is
the default (`default_language`). The default is the project's fallback: seeded
at project creation, used when a respondent requests a language the project
does not offer (`app/api/interview.py`, `app/api/websockets/interviews/ai.py`),
the text of anything not yet translated, the translation source, and first in
the participant email templates. A CHECK constraint holds it to a language in
`languages`; a scalar column cannot name two defaults.

- Read one language through `ProjectRepository.get_project_localization`, which
  renders the content in it with the default as fallback and raises
  `NoResultFound` for a language the project does not offer. The interview,
  the websocket and the public consent/welcome routes use it; the analysis
  views use `render_default_guide`. The editor reads and writes the localized
  form: `GET`/`PUT /projects/{id}/guide`, `/consent`, `/welcome`, `/agents`.
- A condition on a choice question names the option by `option_id`; rendering
  writes that option's text in the interview's language into `trigger_value`.
  There is nothing to remap when a language is added or reworded.
- Change the languages only through `add_project_language` (which can
  machine-translate the missing text, as can `POST /projects/{id}/{lang}/translate`),
  `remove_project_language` and `set_default_language`. Removing a language
  deletes its text in the same write, so no translation outlives its language;
  it refuses the default and the last language with `ProjectLanguageError`,
  which the API maps to a 409. Keep the language list out of the Phase 4
  collaborative document: a removal racing someone's typing would bring the
  removed language's text back.
- The participant emails are HTML with Jinja placeholders, so
  `translate_missing` leaves them alone.
- Interviews still store their own rendered, shuffled copy of the guide.

Do **not** go back to content per language. That was the old design: a
`projectlocalization` row per language held a full copy of the guide, consent,
welcome and emails. The copies drifted -- an edit to one language's structure
never reached the others, so the same project asked different questions in
different orders, and every condition on a choice had to be remapped onto each
translation's option text. Revision `916e657898af` merged them, the default
supplying the structure, and dropped the table.

Do **not** reintroduce a `default_language` field on `InterviewConfig` either.
A bare language string in the project's JSON config, with nothing tying it to
the languages it named, went dangling when one was deleted: localization lookup
raised, interview creation and the translation source broke, and the
dashboard's per-language routes went to a language with no data (revision
`a1c4e9f30b57`). `projectcontent.default_language` avoids that by sitting on
the same row as `languages` and the text, under a CHECK.

The public language lists use `ProjectLanguage` (`app/db/models.py`), which is
`LanguageDict` plus `is_default`. The library's `LanguageDict` deliberately
stays free of the flag: it comes straight out of the shared `LANGUAGES`
constant and knows nothing about projects.

### Content versions: draft, published, revision

**`projectcontent` is the draft; respondents never read it.** They get the
version `projectcontent.published_version_id` points at, an immutable copy of
the content columns in `projectcontentversion` (`VersionRepository`,
`app/db/repositories/version.py`). The columns a version captures are the
`ContentColumns` mixin's, shared by both tables, so a column added there is
versioned automatically; anything that changes what respondents get belongs on
it. The participant emails are versioned too; attachments (files on disk) are
not.

- **Who reads what.** Distributed interviews, the public `available_languages`,
  `guide/consent|welcome/{lang}` and `models` routes, and participant email
  sending (invites, reminders, the bundle) read the **published** version, and
  answer `NotPublishedError` (409, `code: not_published`) until there is one --
  never a fallback to the draft. A new project is unpublished. Test interviews
  (manual, synthetic) run the **draft** through a `test` snapshot, deduped by
  content hash. The public routes serve the draft with `?draft=true` to a
  viewer of the project (`RespondentContent`, `app/dependencies.py`), because
  manual tests go through the respondent interview page. The editor and the
  analysis views read the draft.
- **Render through `get_project_localization(..., source=...)`** (`"draft"`,
  `"published"` or a version id), never a second renderer.
  `InterviewTable.content_version_id` records what an interview ran; the
  websocket takes its agent configs and prompt overrides from there, so
  republishing mid-interview changes nothing for it.
- **Publishing** (`POST /projects/{id}/versions` with `publish: true`) is
  refused while any offered language has untranslated text in the guide,
  consent, welcome or emails (`PublishError`, 409, `code: cannot_publish`,
  `missing` per language).
- **Languages in progress** (`languages_in_progress`, versioned like the rest)
  are in the draft but not *offered*: `ContentColumns.offered` is
  `languages` minus them, and the `"published"` source -- respondents'
  language list, interview creation, consent/welcome, sent emails -- uses
  only that, while the draft and test snapshots keep them so they can be
  tested. Their missing text does not block publishing. A newly added
  language starts in progress until someone marks it ready
  (`PUT …/available_languages/{lang}/in_progress`); marking one ready is a
  minor bump. The default is never in progress (a CHECK, and a 409).
- **Semantic versions.** A publish is numbered by how far it is from the
  published version, by whether interviews stay comparable
  (`app/db/versioning.py`): major when something answered is removed or
  reshaped (a section, question or option removed; a survey item's type or
  scale; conditions), minor for additions and settings (questions added,
  reordering, probing, agent configs, languages), patch for text only. So
  every interview within one major answered the same questions. The diff
  cannot read meaning, so the publisher may raise the bump, never lower it.
  Only `publish` versions carry one; numbering continues from the highest
  published, so republishing restored content still moves forward.
- **Retention.** `publish` and `manual` versions are never deleted. Automatic
  ones (`test`, `restore`, `auto`) beyond the newest
  `KEEP_AUTOMATIC_VERSIONS` are pruned, except those an interview references.
- **Restore** copies a version, languages and all, into the draft after keeping
  the draft it replaces as a `restore` version. It publishes nothing.

**Revisions stop concurrent editors overwriting each other.**
`projectcontent.revision` is bumped by a `before_update` listener whenever a
content column changes (not when publishing). The editor GETs return it as
`ETag`; every content write (`PUT /guide`, `/consent`, `/welcome`,
`POST /agents`, the email template PUTs, publish and restore) requires it back
as `If-Match` (`IfMatch`): missing is 428, stale is `ContentRevisionError`
(412, `code: stale_revision`, `current_revision`). New writes go through
`ProjectRepository._content(project_id, revision)`, which does the check. The
three errors are mapped once in `app/main.py`. Server-side writes that are
whole replacements (guide generation, translation, language changes) take no
`If-Match` but still bump the revision and return the new `ETag`.

### Repository Session Management

Never create new sessions within repository methods. Always use `self.session`:

```python
class MyRepository(BaseRepository):
    def get_item(self, item_id: UUID) -> Item:
        # Good: uses shared session
        return self.session.get(Item, item_id)

        # Bad: creates new session (breaks transactions)
        # with Session(engine) as session:
        #     return session.get(Item, item_id)
```

### WebSocket Message Flow

Messages are automatically queued for embedding:

```python
# In WebsocketMessageHandler.send_data()
await self.message_queue.put(
    EmbedTask(
        message_id=message.id,
        content=message.content,
        priority=0,  # AI message
        retry_count=0,
    )
)
```

## Known Issues & TODOs

- The interview session registry is per-process, so ending sessions on delete
  and `claim`'s takeover only work within one worker; see
  `InterviewSessionManager` for what degrades with several.
- The test suite (`uv run pytest`) covers specific behaviour rather than every
  endpoint: each module builds its own in-memory SQLite database or none, with
  no shared fixtures beyond the throwaway secrets in `tests/conftest.py`. The
  `ainterviewer` library has its own suite under `../lib/tests`.
- OpenAPI SDK generation pattern needs full implementation (see the TODO at the top of `app/main.py`)
- `create_interview` falls back to the project's default language when a
  respondent requests one the project does not offer, instead of
  surfacing the choice (see the `FIXME` in `app/api/interview.py`)

## Branching & Merging

History on `main` is linear: no merge commits.

- Work on a branch (`feat/…`, `fix/…`, `refactor/…`), never directly on `main`.
  The exception is the release commit `just publish` makes, which lands on
  `main` directly.
- To land it, rebase onto `main` and fast-forward:
  `git rebase main <branch> && git switch main && git merge --ff-only <branch>`.
  Several branches land one after another, each rebased onto the result of
  the last.
- Run the checks again after a rebase that touched the same files as `main`;
  a clean rebase is not proof the two changes compose.
- On GitHub, PRs merge by rebase or squash; merge commits are disabled.
- Clones should set `git config pull.rebase true` and
  `git config merge.ff only`, so a stray `git pull` or `git merge` cannot
  create a merge commit.

## Release Process

```bash
# Bump version (patch/minor/major); chains into `just publish`
just bump patch  # or minor, major
```

`just bump` runs `prek` over the tree and bumps the version in
`pyproject.toml`. `just publish` then syncs `uv.lock`, prepends this release's
section to `CHANGELOG.md` via `git-cliff`, commits as `chore(release): vX.Y.Z`,
tags, and pushes with `--follow-tags`.

`just install-hooks` installs this clone's pre-commit and commit-msg hooks.

## Environment Variables

Required secrets (set in `.env` or environment):

```bash
# JWT Authentication
APP_SECRET__JWT_SECRET_KEY=your-secret-key

# Session Management
APP_SECRET__SESSION_SECRET_KEY=your-session-key

# Email Service (optional)
APP_SERVICE__EMAIL__SMTP_PASSWORD=your-smtp-password
```

## Package Manager: uv

This project uses `uv` (fast Python package installer/resolver):

- Always use `uv run` to execute commands with project dependencies
- `uv sync` installs/updates all dependencies from `uv.lock`
- `uv add <package>` to add new dependencies
- `uv version --bump <type>` for version management
