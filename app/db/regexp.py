"""`REGEXP` and `regexp_replace` for SQLite.

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

`regexp_replace` is here for the same reason and with the same shape: Postgres
has it, SQLite does not, and the keyword condition needs it to take the markup
out of guide text before matching. The argument order and the `g` flag are
Postgres's, so one SQLAlchemy expression renders on both.
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


def _regexp_replace(
    value: str | None, pattern: str | None, replacement: str | None, flags: str = ""
) -> str | None:
    """Postgres's `regexp_replace(source, pattern, replacement, flags)`.

    Source first here, unlike `regexp` above -- these are two different
    conventions and both are somebody else's: the operator's, and Postgres's.

    Only the `g` flag is honoured, which is the only one anything here passes.
    An uncompilable pattern leaves the value alone: the caller is stripping
    something out, and returning the string unstripped is the answer that
    changes least.
    """
    if value is None or pattern is None or replacement is None:
        return None
    compiled = _compiled(pattern)
    if compiled is None:
        return value
    return compiled.sub(replacement, value, count=0 if "g" in (flags or "") else 1)


def register_regexp(engine: Engine) -> None:
    """Make `REGEXP` and `regexp_replace` work on every new connection.

    A no-op on other dialects, which have both already.
    """
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def _register(dbapi_connection, _connection_record):
        for name, arity, function in (
            ("regexp", 2, _regexp),
            ("regexp_replace", 4, _regexp_replace),
        ):
            try:
                # Deterministic so SQLite may use it in an index or a partial
                # index; they are pure functions of their arguments.
                dbapi_connection.create_function(
                    name, arity, function, deterministic=True
                )
            except sqlite3.NotSupportedError:  # pragma: no cover - old SQLite
                dbapi_connection.create_function(name, arity, function)
            except Exception:
                logger.exception("Failed to register %s on a new connection", name)
