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

import re
from pathlib import Path, PureWindowsPath
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
    """The bare name of a client-supplied file name: any path in it is
    dropped, in either style (a Windows path splits on both "/" and "\\"),
    so it names a file in the directory it is joined onto and nowhere else.
    Refused when nothing usable is left."""
    if not filename:
        raise HTTPException(status_code=400, detail="Filename is required.")
    name = PureWindowsPath(filename).name
    if name in {"", ".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    if len(name) > 255:
        raise HTTPException(status_code=400, detail="Filename is too long.")
    if "\x00" in name:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    return name


#: An extension worth keeping on a stored file whose name the server picks.
_SAFE_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,10}")


def safe_suffix(filename: str | None) -> str:
    """The extension of a client-supplied file name, or "" when it has none
    or it is anything but a short alphanumeric one.

    For files stored under a name the server generates: the extension is all
    that is kept of the client's name, so it must not carry a path either.
    """
    if not filename:
        return ""
    suffix = Path(filename.replace("\\", "/")).suffix
    return suffix if _SAFE_SUFFIX.fullmatch(suffix) else ""
