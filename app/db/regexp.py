"""A `REGEXP` implementation for SQLite.

Postgres has regular expressions; SQLite has the `REGEXP` *operator* but ships no
function behind it, so `x REGEXP y` raises "no such function: regexp" until one
is registered. Keyword search compiles to a regular expression -- it is how word
boundaries are expressed in a way both engines read the same -- so the function
has to exist on every connection, exactly as the pragmas do.

Case-insensitivity lives here rather than in the pattern. SQLAlchemy's
`regexp_match(..., flags="i")` renders as `~*` on Postgres but drops the flag
silently on SQLite, so this side has to supply it, and does: every pattern is
compiled `IGNORECASE`. Both halves of that promise are one call apart on
purpose -- see `app.db.keyword_query.compile_condition`.
"""

import logging
import re
import sqlite3
from functools import lru_cache

from sqlalchemy import event
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


@lru_cache(maxsize=512)
def _compiled(pattern: str) -> re.Pattern[str] | None:
    """The pattern, compiled once per process.

    A query repeats its pattern across every row of a scan, so compiling per
    call would dominate the cost of the scan itself.

    None where the pattern will not compile. That can only happen if something
    built a pattern by hand -- `keyword_query` escapes everything it puts in one
    -- and matching nothing is the safe answer for a predicate: a bad pattern
    should return no rows, not every row.
    """
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        logger.warning("Uncompilable REGEXP pattern from SQL: %r", pattern)
        return None


def _regexp(pattern: str | None, value: str | None) -> int | None:
    """SQLite's `REGEXP` argument order: `value REGEXP pattern` calls
    `regexp(pattern, value)`, pattern first.

    NULL in, NULL out, so `NULL REGEXP '...'` is unknown rather than false and
    behaves as SQL expects under `NOT`.
    """
    if pattern is None or value is None:
        return None
    compiled = _compiled(pattern)
    if compiled is None:
        return 0
    return 1 if compiled.search(value) else 0


def register_regexp(engine: Engine) -> None:
    """Make `REGEXP` work on every new connection of `engine`.

    A no-op on other dialects, which have it already.
    """
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def _register(dbapi_connection, _connection_record):
        try:
            # Deterministic so SQLite may use it in an index or a partial
            # index; it is a pure function of its arguments.
            dbapi_connection.create_function("regexp", 2, _regexp, deterministic=True)
        except sqlite3.NotSupportedError:  # pragma: no cover - very old SQLite
            dbapi_connection.create_function("regexp", 2, _regexp)
        except Exception:
            logger.exception("Failed to register REGEXP on a new connection")
