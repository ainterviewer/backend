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

# Generate OpenAPI schema (for SDK generation)
just generate-openapi
# or
python -m app.cli generate-openapi-scheme
```

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
- Specialized repositories:
  - `UserRepository`: Users, invites, access requests with email notifications
  - `ProjectRepository`: Projects, folders, collaborators, multi-language support
  - `InterviewRepository`: Interview records, messages, feedback tracking
  - `AnalysisRepository`: Annotations, categories, vector search
  - `TestRepository`: Experiment management

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

**5. WebSocket Management (`app/websockets.py`)**

- `WebSocketConnectionManager`: Tracks active connections per project/interview
- `WebsocketMessageHandler`: Implements `IOProtocol` to bridge WebSocket ↔ ainterviewer library
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
   - Regenerate OpenAPI schema: `just generate-openapi`
   - Commit `openapi.json` for SDK client generation

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

- Template-based routes in `app/routes/` are legacy (migrating to SvelteKit frontend)
- Some exception handlers are marked as not working with new frontend (see `app/main.py:86-101`)
- No test suite currently exists (pytest configured but no tests written)
- OpenAPI SDK generation pattern needs full implementation (see `app/main.py:1-3`)

## Release Process

```bash
# Bump version (patch/minor/major)
just bump patch  # or minor, major

# This will:
# 1. Update version in pyproject.toml
# 2. Run uv sync to update uv.lock
# 3. Stage changes
# 4. Commit with "Release vX.Y.Z"
# 5. Create git tag
# 6. Push with tags
```

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
