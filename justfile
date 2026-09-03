set dotenv-load

[private]
default:
    @just --list

dev:
    bash -c 'uv run -m fastapi dev app/main.py --port 8666 \
      --reload-dir app/ \
      --reload-dir .venv/ \
      --reload-dir ../lib/src'

[group("Database")]
setup-db:
    python -m app.db --recreate-db
    python -m app.db --create-users

[group("Database")]
update-projects:
    python -m app.db --upgrade-projects

[group("Database")]
update-users:
    python -m app.db --create-users

[doc("Fetches the latest daily backup db from the remote AWS server.
Pass \"migrate\" to run `alembic upgrade head` on the fetched database.")]
[group("Database")]
fetch-db MIGRATE="":
    #!/usr/bin/env bash
    set -euo pipefail
    case "{{ MIGRATE }}" in
      ""|migrate) ;;
      *) echo "expected 'migrate' or nothing, got '{{ MIGRATE }}'" >&2; exit 1 ;;
    esac
    rm -f storage/db.sqlite*
    scp aws-1:/var/backups/sqlite/app-daily-latest.db storage/db.sqlite
    if [ -n "{{ MIGRATE }}" ]; then
      uv run alembic upgrade head
    fi

[doc("Run a backup on the remote out of band, exactly as cron does.
WAL-safe (sqlite3 \".backup\"), so the backend keeps running.")]
[group("Database")]
backup-db KIND="daily" HOST="aws-1":
    #!/usr/bin/env bash
    set -euo pipefail
    case "{{ KIND }}" in
      daily|weekly) ;;
      *) echo "KIND must be 'daily' or 'weekly', got '{{ KIND }}'" >&2; exit 1 ;;
    esac
    # sudo: the scripts live in root's crontab and write to root-owned
    # /var/backups/sqlite; they export the ubuntu AWS creds themselves.
    ssh {{ HOST }} "/home/ubuntu/deploy/scripts/backups/sqlite_{{ KIND }}_backup.sh"
    aws s3 ls s3://ainterviewer-sodas/data/backups/{{ KIND }}/ | tail -3

[doc(" ENV/VERSION pick which archived manifest to copy in (VERSION defaults to newest).
Copy a release — versions, notes and highlights — into the local dev database.")]
[group("Database")]
seed-release ENV="prod" VERSION="":
    #!/usr/bin/env bash
    set -euo pipefail
    VERSION="{{ VERSION }}"
    [ -n "$VERSION" ] || VERSION="$(python3 ../deploy/scripts/latest_version.py {{ ENV }})"
    MANIFEST="../deploy/manifests/{{ ENV }}/${VERSION}.json"
    uv run python -m app.db.cli add-release-manifest "$(cat "$MANIFEST")"
    echo "Seeded local dev database from $MANIFEST"

# Embedding server reachability and stored vector counts.
[group("Embeddings")]
embed-status:
    uv run python -m app.embed.cli status

# Embed every chunk that has no current vector. Idempotent; safe to re-run.
# Pass --dry-run to count chunks without calling the model.
[group("Embeddings")]
embed-backfill *ARGS:
    uv run python -m app.embed.cli backfill {{ ARGS }}

[group("Release & Publish")]
bump TYPE: && publish
    #!/usr/bin/env bash
    set -euo pipefail
    uv run prek -a
    uv version --bump {{ TYPE }}

# Install this clone's git hooks (pre-commit + commit-msg).
[group("Release & Publish")]
install-hooks:
    uv run prek install

[group("Release & Publish")]
publish:
    #!/usr/bin/env bash
    set -euo pipefail
    VERSION="$(uv version --short)"

    uv sync
    # Prepend this release's section; --prepend needs the file to exist.
    touch CHANGELOG.md
    uvx git-cliff@2.13.1 --unreleased --tag "v${VERSION}" --prepend CHANGELOG.md

    git commit --only uv.lock pyproject.toml CHANGELOG.md -m "chore(release): v${VERSION}"
    git tag -a "v${VERSION}" -m "v${VERSION}"
    git push --follow-tags

# Manually build & push the Docker image to ghcr.io (fallback for when CI is down).
# Reads GHCR_TOKEN, GITHUB_TOKEN and GITHUB_USERNAME from .env (auto-loaded).
[group("Release & Publish")]
publish-docker:
    #!/usr/bin/env bash
    set -euo pipefail
    : "${GHCR_TOKEN:?set GHCR_TOKEN in .env (PAT with write:packages)}"
    : "${GITHUB_TOKEN:?set GITHUB_TOKEN in .env (PAT with repo, for the github_token build secret)}"
    : "${GITHUB_USERNAME:?set GITHUB_USERNAME in .env}"

    IMAGE="ghcr.io/ainterviewer/backend"
    VERSION="$(uv version --short)"

    TAGS=(-t "${IMAGE}:v${VERSION}")
    case "${VERSION}" in
      *rc*) ;;                          # pre-release: skip 'latest'
      *) TAGS+=(-t "${IMAGE}:latest") ;;
    esac

    echo "${GHCR_TOKEN}" | docker login ghcr.io -u "${GITHUB_USERNAME}" --password-stdin

    TOKEN_FILE="$(mktemp)"
    trap 'rm -f "${TOKEN_FILE}"' EXIT
    printf '%s' "${GITHUB_TOKEN}" > "${TOKEN_FILE}"

    docker buildx build \
      --secret id=github_token,src="${TOKEN_FILE}" \
      "${TAGS[@]}" \
      --push \
      .
