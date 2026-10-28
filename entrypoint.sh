#!/bin/sh
set -e
python -m app.db.cli setup-db
alembic upgrade head
exec fastapi run app/main.py --port=8666
