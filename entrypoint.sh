#!/bin/sh
set -e
alembic upgrade head
exec fastapi run app/main.py --port=8666
