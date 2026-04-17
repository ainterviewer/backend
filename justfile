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
fetch-backup:
    rm -f storage/db.sqlite*
    latest=$(ssh aws-1 'ls -t /var/backups/sqlite/app-daily-*.db | head -n 1'); \
    scp aws-1:"$latest" storage/db.sqlite

[group("Release & Publish")]
bump TYPE: && publish
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
