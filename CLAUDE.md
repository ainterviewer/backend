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
- Pragmas: `foreign_keys=ON`, `busy_timeout=60000`, `cache_size=-65536`
- Storage location: `storage/db.sqlite`

**Alternative: PostgreSQL**

- Connection pooling: 20 pool size, 40 max overflow
- Configured via `DATABASE_URL` environment variable
- Use `db = "postgres"` in config.toml

### Async Task Queue Pattern

**Embedding Queue (`app/embed/main.py`)**

- Priority queue for message embeddings (higher priority first, FIFO within same priority)
- User messages: priority=1, AI messages: priority=0
- Decouples message delivery from embedding generation
- Task structure: `message_id`, `content`, `priority`, `retry_count`
- Ready for scaling with Redis/RabbitMQ

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

- No test suite currently exists (pytest is a dev dependency but no tests are written)
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
