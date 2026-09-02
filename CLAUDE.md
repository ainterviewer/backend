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
  - `AnalysisRepository`: Annotations, categories, vector search
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
- **Foreign keys are not enforced.** `foreign_keys` is deliberately left out of
  `_SQLITE_PRAGMAS`: the database holds orphaned rows from the period when the
  pragma never reached a live connection, and the Core `delete()` statements in
  `TestRepository.delete_test_setup` / `delete_experiment` raise IntegrityError
  once it is on (their child FKs have no `ondelete`). Until that is fixed,
  `ON DELETE CASCADE` does nothing at runtime -- delete child rows explicitly,
  as `InterviewRepository.delete_interviews` does. Note PostgreSQL enforces
  foreign keys unconditionally, so this must be resolved before migrating.
- Storage location: `storage/db.sqlite`

**Alternative: PostgreSQL**

- Connection pooling: 20 pool size, 40 max overflow
- Configured via `DATABASE_URL` environment variable
- Use `db = "postgres"` in config.toml

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
- Prefixes: `APP_SECRET__`, `APP_SERVICE__`, `APP_DATABASE__`
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

`ProjectLocalizationTable.prompt_overrides` holds only per-project overrides,
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
# Override config values via environment
APP_DATABASE__DB=postgres DATABASE_URL=postgresql://... just dev

# Use custom config file
CONFIG_FILE=config.production.toml just dev
```

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

### Project languages and the default localization

A project's languages are its `projectlocalization` rows. Exactly one of them
carries `is_default = True`, enforced by the partial unique index
`uq_project_default_language`. That row is the project's fallback language: it
is seeded at project creation, used when a requested language has no
localization (`app/api/interview.py`, `app/api/websockets/interviews/ai.py`),
used as the translation source when a language is added, and sorted first in
the participant email templates.

Read it through `ProjectRepository.get_default_language` /
`_get_default_localization`, and change it only through `set_default_language`,
which clears the old flag before setting the new one. `remove_project_language`
refuses to delete the default localization or the last remaining one, raising
`ProjectLanguageError`, which the API maps to a 409.

Do **not** reintroduce a `default_language` field on `InterviewConfig`. That was
the old design: a bare language string in the project's JSON config with nothing
tying it to the rows it named. Deleting the localization it pointed at left it
dangling, which made localization lookup raise, broke interview creation and the
translation source, and sent the dashboard's per-language routes to a language
with no data. Nothing validated writes to it either. See revision
`a1c4e9f30b57`, which moved the flag onto the row and stripped the key from
every stored config.

The public language lists use `ProjectLanguage` (`app/db/models.py`), which is
`LanguageDict` plus `is_default`. The library's `LanguageDict` deliberately
stays free of the flag: it comes straight out of the shared `LANGUAGES`
constant and knows nothing about projects.

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

- No general test suite: `tests/test_clustering.py` covers the embedding
  clustering logic (pure, synthetic data, no database or network) and is the
  only one so far. The `ainterviewer` library has its own suite under
  `../lib/tests`.
- OpenAPI SDK generation pattern needs full implementation (see the TODO at the top of `app/main.py`)
- `create_interview` falls back to the project's default language when a
  respondent requests one the project has no localization for, instead of
  surfacing the choice (see the `FIXME` in `app/api/interview.py`)

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

# Database (if using PostgreSQL)
DATABASE_URL=postgresql://user:pass@host:port/dbname
```

## Package Manager: uv

This project uses `uv` (fast Python package installer/resolver):

- Always use `uv run` to execute commands with project dependencies
- `uv sync` installs/updates all dependencies from `uv.lock`
- `uv add <package>` to add new dependencies
- `uv version --bump <type>` for version management
