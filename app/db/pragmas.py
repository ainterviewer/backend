"""Per-connection SQLite pragmas.

Every pragma here except `journal_mode` is *connection* state, so setting it
once when the database is created does nothing for the pooled connections the
app actually serves requests on. It has to be reapplied on every new connection,
which is what `register_pragmas` does via SQLAlchemy's `connect` event.
"""

import logging

from sqlalchemy import event
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

_SQLITE_PRAGMAS: tuple[tuple[str, str], ...] = (
    # First, so anything below waits rather than failing on a busy database.
    # Not the SQLite default of 0: pysqlite passes `timeout=5.0`, so the
    # baseline is 5s. Higher trades a fast "database is locked" error for a
    # longer stall, which is the better failure mode for short transactions.
    ("busy_timeout", "20000"),
    # Persistent (stored in the database header); reasserted so a freshly
    # created file gets it too.
    ("journal_mode", "WAL"),
    # Negative values are KiB rather than pages. This is per connection, and
    # the pool holds up to pool_size + max_overflow of them, so the ceiling is
    # this value times that count -- keep it well under what the host can
    # afford to hand out that many times over.
    ("cache_size", "-16000"),
    ("temp_store", "MEMORY"),
)


def register_pragmas(engine: Engine) -> None:
    """Apply the SQLite pragmas to every new connection of `engine`.

    A no-op on other dialects.
    """
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        try:
            for pragma, value in _SQLITE_PRAGMAS:
                cursor.execute(f"PRAGMA {pragma}={value}")
        except Exception:
            logger.exception("Failed to apply SQLite pragmas to a new connection")
        finally:
            cursor.close()
