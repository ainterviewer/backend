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
    uv run python -m app.db.cli setup-db

# A running `just dev` keeps pooled connections to the deleted database files;
# touching a watched file makes its --reload restart it onto the new one.
[doc("Replace the local database with realistic, image-free development fixtures.")]
[group("Database")]
seed-dev *ARGS:
    uv run python -m app.db.cli seed-dev --yes {{ ARGS }}
    touch app/main.py

[doc("Fetches the latest daily backup db from the remote AWS server.
Pass \"migrate\" to run `alembic upgrade head` on the fetched database.
ENV picks which environment's backups to pull from.")]
[group("Database")]
fetch-db MIGRATE="" ENV="production":
    #!/usr/bin/env bash
    set -euo pipefail
    case "{{ MIGRATE }}" in
      ""|migrate) ;;
      *) echo "expected 'migrate' or nothing, got '{{ MIGRATE }}'" >&2; exit 1 ;;
    esac
    case "{{ ENV }}" in
      production|staging) ;;
      *) echo "ENV must be 'production' or 'staging', got '{{ ENV }}'" >&2; exit 1 ;;
    esac
    rm -f storage/db.sqlite*
    scp aws-1:/var/backups/sqlite/{{ ENV }}/app-daily-latest.db storage/db.sqlite
    if [ -n "{{ MIGRATE }}" ]; then
      uv run alembic upgrade head
    fi
    # Local copies skip the emailed login code. Backups older than the
    # column don't have it until migrated.
    if [ "$(sqlite3 storage/db.sqlite "SELECT count(*) FROM pragma_table_info('user') WHERE name = 'two_factor_enabled'")" = 1 ]; then
      sqlite3 storage/db.sqlite 'UPDATE "user" SET two_factor_enabled = 0'
      echo "Disabled two-factor for all users"
    else
      echo "No two_factor_enabled column; run with 'migrate' to add it" >&2
    fi

[doc("Run a backup on the remote out of band, exactly as cron does.
WAL-safe (sqlite3 \".backup\"), so the backend keeps running.
ENV picks which environment's database to back up.")]
[group("Database")]
backup-db KIND="daily" ENV="production" HOST="aws-1":
    #!/usr/bin/env bash
    set -euo pipefail
    case "{{ KIND }}" in
      daily|weekly) ;;
      *) echo "KIND must be 'daily' or 'weekly', got '{{ KIND }}'" >&2; exit 1 ;;
    esac
    case "{{ ENV }}" in
      production|staging) ;;
      *) echo "ENV must be 'production' or 'staging', got '{{ ENV }}'" >&2; exit 1 ;;
    esac
    # sudo: the scripts live in root's crontab and write to root-owned
    # /var/backups/sqlite; they export the ubuntu AWS creds themselves.
    ssh {{ HOST }} "sudo /home/ubuntu/deploy/scripts/backups/sqlite_{{ KIND }}_backup.sh {{ ENV }}"
    aws s3 ls s3://ainterviewer-sodas/data/backups/{{ ENV }}/{{ KIND }}/ | tail -3

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
bump TYPE: confirm-clean && publish
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

# uv fetches the tag from GitHub, so "latest" is read from there, not ../lib:
# a tag that only exists locally cannot be pinned. Pinning also switches off the
# editable ../lib source, so the editable line is never what gets committed.
[doc("Pin ainterviewer to a lib release tag (default: newest on GitHub), lock, sync and commit.")]
[group("Library")]
pin-lib VERSION="":
    #!/usr/bin/env bash
    set -euo pipefail
    REPO="https://github.com/ainterviewer/lib.git"
    VERSION="{{ VERSION }}"
    if [ -z "$VERSION" ]; then
      VERSION="$(git ls-remote --tags --refs --sort=-v:refname "$REPO" 'v*' \
        | sed 's|.*refs/tags/||' | grep -v rc | head -1)"
    fi
    case "$VERSION" in v*) ;; *) VERSION="v$VERSION" ;; esac
    git ls-remote --exit-code --tags "$REPO" "refs/tags/$VERSION" >/dev/null \
      || { echo "no tag $VERSION on $REPO (pushed?)" >&2; exit 1; }

    # Make the tag line the active source; comment out the path and rev lines.
    sed -i -E \
      -e 's|^(# )?ainterviewer = \{ git = "([^"]+)", tag = "[^"]+" \}|ainterviewer = { git = "\2", tag = "'"$VERSION"'" }|' \
      -e 's|^ainterviewer = \{ path|# &|' \
      -e 's|^ainterviewer = \{ git = "[^"]+", rev|# &|' \
      pyproject.toml

    # `git commit --only` takes the whole file, so refuse to sweep other edits in.
    if git diff -U0 pyproject.toml | grep -E '^[+-][^+-]' | grep -vq 'ainterviewer = {'; then
      echo "pyproject.toml has changes besides the ainterviewer source; commit or stash them first" >&2
      exit 1
    fi

    uv lock
    uv sync
    if git diff --quiet pyproject.toml uv.lock; then
      echo "Already pinned to $VERSION"
      exit 0
    fi
    git commit --only pyproject.toml uv.lock -m "build(deps): bump ainterviewer to $VERSION"

# Leaves pyproject.toml and uv.lock modified: never commit them in this state,
# `just pin-lib` switches back. The sync is what makes ../lib edits visible; a
# venv last synced against the tag imports a site-packages copy instead.
[doc("Use the editable ../lib checkout as the ainterviewer source and sync the venv.")]
[group("Library")]
lib-local:
    #!/usr/bin/env bash
    set -euo pipefail
    [ -d ../lib/src/ainterviewer ] || { echo "no lib checkout at ../lib" >&2; exit 1; }
    sed -i -E \
      -e 's|^ainterviewer = \{ git|# &|' \
      -e 's|^# ?(ainterviewer = \{ path = "\.\./lib")|\1|' \
      pyproject.toml
    uv sync
    uv run --no-sync python -c "import ainterviewer; print('ainterviewer from', ainterviewer.__file__)"

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

# Prompt before releasing over uncommitted (or untracked) changes.
[private]
confirm-clean:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -n "$(git status --porcelain)" ]; then
        git status --short
        read -rp "There are uncommitted changes. Continue? [y/N] " answer
        [[ "$answer" =~ ^[Yy]$ ]] || { echo "Aborted." >&2; exit 1; }
    fi
