set dotenv-load := true

[private]
default:
    @just --list

dev:
    bash -c 'uv run -m fastapi dev app/main.py --port 8666 \
      --reload-dir app/ \
      --reload-dir .venv/ \
      --reload-dir ../lib/src'

generate-sdk:
    uv run -m app.cli generate-openapi-scheme
    bunx @hey-api/openapi-ts --input "openapi.json" --output "../frontend/src/lib/api" --file "../frontend/openapi-ts.config.ts"

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

[group("Database")]
fetch-db:
    rm -f storage/db.sqlite*
    scp aws-1:/var/backups/sqlite/app-daily-latest.db storage/db.sqlite

[group("Release & Publish")]
bump TYPE: && publish
    #!/usr/bin/env bash
    set -euo pipefail
    uv run prek -a
    uv version --bump {{ TYPE }}

[group("Release & Publish")]
publish:
    #!/usr/bin/env bash
    VERSION="$(uv version --short)"

    uv sync
    git add uv.lock pyproject.toml
    git commit -m "Release v${VERSION}"
    git tag -a "v${VERSION}" -m "Release v${VERSION}"
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
