"""Helpers for routes that store files on disk or answer with one.

`file_response` is the `responses=` of a route that answers a file rather
than JSON: declared as a binary string per media type, which the generated
client types as a `Blob`. Without it FastAPI documents an empty JSON body,
which the client types as `unknown`; with a media type but no schema, a
`text/*` one (a CSV) still comes out `unknown`. Pair it with
`response_class=Response`, so the JSON body is not documented alongside.

The rest keep what a client sends from turning into a path it chose: a file
name, or a language used as a directory name.
"""

from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import HTTPException

from ainterviewer.constants import LANGUAGE_CODES
from ainterviewer.types import LanguageCode

_BINARY = {"schema": {"type": "string", "format": "binary"}}


def file_response(*media_types: str) -> dict[int | str, dict[str, Any]]:
    return {200: {"content": {media_type: _BINARY for media_type in media_types}}}


def checked_language(language: LanguageCode) -> LanguageCode:
    """Reject language codes that aren't real, before they become a path.

    ``LanguageCode`` only constrains the string to two characters, which ".."
    satisfies, so every use of a language as a directory name has to check it
    against the known codes first.
    """
    if language not in LANGUAGE_CODES:
        raise HTTPException(status_code=400, detail=f"Unknown language '{language}'.")
    return language


def content_disposition(filename: str, disposition: str = "attachment") -> str:
    """Build a Content-Disposition value that survives non-ASCII filenames.

    Header values are latin-1, so the raw name is sent RFC 5987 style and the
    quoted fallback is stripped to ASCII.
    """
    ascii_name = filename.encode("ascii", "replace").decode("ascii").replace('"', "_")
    return (
        f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
    )


def safe_filename(filename: str | None) -> str:
    """A client-supplied file name, refused unless it is a bare name."""
    if not filename:
        raise HTTPException(status_code=400, detail="Filename is required.")
    name = Path(filename).name
    if (
        not name
        or name in {".", ".."}
        or name != filename.replace("\\", "/").split("/")[-1]
    ):
        raise HTTPException(status_code=400, detail="Invalid filename.")
    if len(name) > 255:
        raise HTTPException(status_code=400, detail="Filename is too long.")
    if "\x00" in name:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    return name
