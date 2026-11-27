"""Start the backend in one process: set up or migrate the database, then serve.

This is what entrypoint.sh runs. It used to run three interpreters in turn
(`python -m app.db.cli setup-db`, `alembic upgrade head`, `fastapi run`), and
each imported the app's dependency tree again. On the deploy host that import
costs ~6s even with bytecode compiled, so doing it once takes a large share
off every deploy's downtime. The steps and their order are unchanged.
"""

import logging
import sys
from collections.abc import Generator
from contextlib import contextmanager

import uvicorn
from alembic import command

from .db.cli import setup_db
from .db.migrations import alembic_config
from .dependencies import engine

PORT = 8666


@contextmanager
def alembic_logging() -> Generator[None]:
    """Print the `alembic` logger's INFO lines while the database is set up.

    `alembic_config` skips alembic.ini, and with it the logging config that
    would have printed "Running upgrade ..." (see app/db/migrations.py). This
    puts back just that, on the `alembic` logger alone, and only for the
    duration.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)-5.5s [%(name)s] %(message)s"))
    logger = logging.getLogger("alembic")
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def main() -> None:
    with alembic_logging():
        # Passed explicitly: setup_db is a typer command, so its defaults are
        # typer.Option objects, and an OptionInfo for `fresh` is truthy --
        # relying on the default would offer to delete the database.
        setup_db(fresh=False, users_file=None)
        # Close setup_db's pooled connections before alembic, which uses an
        # engine of its own, rebuilds tables underneath them.
        engine.dispose()
        # `alembic upgrade head`. env.py treats a programmatic call as
        # schema-changing, so it reinstalls the triggers as the CLI does.
        command.upgrade(alembic_config(), "head")

    # What `fastapi run app/main.py --port=8666` ran: uvicorn on all
    # interfaces, one worker, proxy headers on. The trusted proxies still come
    # from FORWARDED_ALLOW_IPS, which uvicorn reads itself.
    uvicorn.run("app.main:app", host="0.0.0.0", port=PORT, proxy_headers=True)


if __name__ == "__main__":
    main()
