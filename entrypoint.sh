#!/bin/sh
set -e
# Sets up or migrates the database, then serves, in one Python process, so
# the app's imports are paid for once (see app/boot.py).
exec python -m app.boot
