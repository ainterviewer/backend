dev:
    bash -c 'uv run -m fastapi dev app/main.py --port 8666'

[group("Database")]
setup-db: 
    python -m app.db --recreate-db
    python -m app.db --create-users

[group("Backend")]
generate-openapi:
    python -m app.cli generate-openapi-scheme

[group("Backend")]
update-projects:
    python -m app.db --upgrade-projects

[group("Backend")]
update-users:
    python -m app.db --create-users

[group("Backend")]
[group("Release & Publish")]
bump TYPE: && publish
    uv version --bump {{ TYPE }} 

[group("Backend")]
[group("Release & Publish")]
publish:
    #!/usr/bin/env bash
    VERSION="$(uv version --short)"

    uv sync
    git add uv.lock pyproject.toml
    git commit -m "Release v${VERSION}"
    git tag -a "v${VERSION}" -m "Release v${VERSION}"
    git push --follow-tags
