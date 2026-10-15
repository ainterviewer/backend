"""A flat part of the content -- the consent, the welcome screen -- as a Yjs
document.

The same building blocks as the guide's (`guide_doc.py`), without its
structure: one root map, `content`, whose text fields are a `Y.Map` of
language to `Y.Text` (an empty one for each of the project's languages) and
whose other fields are JSON text. The frontend's `src/lib/collab/flatDoc.ts`
mirrors it key for key.

Root keys: `meta` (`doc_id`) and `content`.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable
from typing import Any
from uuid import uuid4

from pycrdt import Doc, Map

from .guide_doc import _LANGUAGES, DOC_ID, Json, _read_entity, _set_fields

CONTENT = "content"


def roots(doc: Doc) -> tuple[Map, Map]:
    """The doc's root maps: meta, content."""
    return doc.get("meta", type=Map), doc.get(CONTENT, type=Map)


def apply_flat_data(
    doc: Doc,
    data: Json,
    texts: tuple[str, ...],
    languages: Iterable[str] = (),
    origin: Any = None,
) -> None:
    """Write `data` into `doc` as the smallest set of changes, in one
    transaction. Every one of `texts` is written, empty if `data` lacks it,
    so it has an entry for each of `languages` nobody has to create by
    typing. Seeds an empty doc, giving it a `doc_id`."""
    data = copy.deepcopy(data)
    for key in texts:
        data[key] = data.get(key) or {}
    token = _LANGUAGES.set(tuple(languages))
    try:
        meta, content = roots(doc)
        with doc.transaction(origin=origin):
            if DOC_ID not in meta:
                meta[DOC_ID] = str(uuid4())
            _set_fields(content, data, texts=texts)
    finally:
        _LANGUAGES.reset(token)


def read_flat_data(doc: Doc, texts: tuple[str, ...]) -> Json:
    """The doc's content as plain data, unvalidated."""
    _meta, content = roots(doc)
    return _read_entity(content, texts)
