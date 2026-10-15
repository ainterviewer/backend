"""The guide as a Yjs document: `LocalizedInterviewGuide` <-> `pycrdt.Doc`.

The only place the mapping lives server-side; the editor's
(`frontend/src/lib/collab/guideDoc.ts`) mirrors it, and the two must agree
key for key.

The shape keeps concurrent edits apart wherever they can be:

- Every piece of text is a `Y.Map` from language to `Y.Text`, so two people
  typing in the same field merge character by character, and translators of
  different languages never touch the same item.
- Sections, questions and survey options are `Y.Map`s keyed by id, each
  entry carrying an `order` key (fractional indexing, sorted as strings).
  Questions are flat, with a `section_id`. Moving one rewrites two plain
  values, so an edit to its text is never lost to a concurrent move -- a
  `Y.Array` would need delete-and-insert, and two concurrent moves would
  duplicate the item.
- Probes, alternative wordings and timed messages are `Y.Array`s of maps,
  matched by content when a write changes them (`align`): inserted and
  removed, never moved.
- Everything else -- flags, settings, conditions, references -- is a plain
  value holding JSON text. Last writer wins per field. Always JSON text, never
  a bare number: Yjs has only doubles, and an `int` coming back a `float`
  would read as a changed scale to versioning.

Root keys: `meta` (`doc_id`, see `DOC_ID`), `guide` (the top-level fields),
`sections`, `questions`.

`apply_guide` writes a guide into a doc as the smallest set of changes it
can, which is how a doc is seeded and how every server-side write (restore,
translate, generate, the PUT) reaches open editors without replacing what
they are typing in. `read_guide` projects a doc back; it can fail
validation, since the doc accepts whatever editors send.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterable
from contextvars import ContextVar
from typing import Any
from uuid import uuid4

from pycrdt import Array, Doc, Map, Text

from ainterviewer.interview_guides.localized import LocalizedInterviewGuide

#: `meta[DOC_ID]` names a doc's lineage. Editors check it on connecting and
#: start over on a mismatch: merging a tab's copy of a different lineage
#: (a doc dropped and re-seeded) would add every item a second time.
DOC_ID = "doc_id"

ORDER = "order"
SECTION_ID = "section_id"

#: Fields that are text, per kind of entity. Anything not named here is a
#: plain JSON value.
_GUIDE_TEXTS = ("framing", "introduction", "outro", "alt_outro")
_SECTION_TEXTS = ("description",)
_QUESTION_TEXTS = ("main_question", "description")
_QUESTION_TEXT_LISTS = ("probes", "alternative_main_questions")
_SURVEY_TEXTS = ("min_label", "max_label")
_IMAGE_TEXTS = ("primer", "description", "alt")
_TIMED_TEXTS = ("message",)

type Json = dict[str, Any]


# -- Writing -------------------------------------------------------------------


def _byte_offset(text: str, index: int) -> int:
    """pycrdt indexes a `Text` in UTF-8 bytes (Yjs in the browser uses UTF-16
    code units); cutting inside a character panics."""
    return len(text[:index].encode())


def _set_text(target: Text, value: str) -> None:
    """Make `target` read `value`, touching only what differs between them:
    a common prefix and suffix are kept, so a caret elsewhere stays put."""
    current = str(target)
    if current == value:
        return
    start = 0
    limit = min(len(current), len(value))
    while start < limit and current[start] == value[start]:
        start += 1
    end = 0
    while end < limit - start and current[-1 - end] == value[-1 - end]:
        end += 1
    old_end = len(current) - end
    new_end = len(value) - end
    begin = _byte_offset(current, start)
    if old_end > start:
        del target[begin : _byte_offset(current, old_end)]
    if new_end > start:
        target.insert(begin, value[start:new_end])


#: The project's languages while `apply_guide_data` runs: every text gets one
#: per language, empty if untranslated. An editor typing into a language the
#: text lacks would otherwise create it, and two creating it at once would
#: each make their own, one of which -- with what was typed into it -- loses.
_LANGUAGES: ContextVar[tuple[str, ...]] = ContextVar("languages", default=())


def _new_texts(texts: dict[str, str]) -> Map:
    return Map(
        {
            language: Text(texts.get(language, ""))
            for language in [
                *texts,
                *(lang for lang in _LANGUAGES.get() if lang not in texts),
            ]
        }
    )


def _fill_texts(current: Map, texts: dict[str, str]) -> None:
    languages = _LANGUAGES.get()
    for language in list(current.keys()):
        if language not in texts and language not in languages:
            del current[language]
    for language in [*texts, *(lang for lang in languages if lang not in texts)]:
        text = texts.get(language, "")
        existing = current.get(language)
        if isinstance(existing, Text):
            _set_text(existing, text)
        else:
            current[language] = Text(text)


def _set_texts(parent: Map, key: str, texts: dict[str, str] | None) -> None:
    """`parent[key]` as a language -> text map; removed when None."""
    if texts is None:
        if key in parent:
            del parent[key]
        return
    current = parent.get(key)
    if isinstance(current, Map):
        _fill_texts(current, texts)
    else:
        parent[key] = _new_texts(texts)


def _similarity(a: str, b: str) -> float:
    """How much of `a` and `b` is one common prefix and suffix, 0 to 1: what
    `_set_text` would keep of one when turning it into the other."""
    if a == b:
        return 1.0
    limit = min(len(a), len(b))
    start = 0
    while start < limit and a[start] == b[start]:
        start += 1
    end = 0
    while end < limit - start and a[-1 - end] == b[-1 - end]:
        end += 1
    return (start + end) / max(len(a), len(b))


def align(old: list[str], new: list[str]) -> list[int | None]:
    """For each of `new`, the entry of `old` it continues, or None for one to
    insert; entries of `old` no one continues are to be deleted. Entries are
    compared as text (canonical JSON).

    The order is kept and the pairs are those with the most text in common,
    so an unchanged entry keeps its item and an edited one is edited in
    place: whoever is typing in a probe keeps typing in it when another one
    is removed or added above it. Any pairing scores a little over none, so
    a rewritten entry is still edited rather than replaced. Lists are a
    handful of entries, so the quadratic table is nothing."""
    n, m = len(old), len(new)
    score = [[_similarity(a, b) + _PAIRED for b in new] for a in old]
    best = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            best[i][j] = max(
                best[i - 1][j - 1] + score[i - 1][j - 1],
                best[i - 1][j],
                best[i][j - 1],
            )
    pairs: list[int | None] = [None] * m
    i, j = n, m
    while i and j:
        if best[i][j] == best[i - 1][j - 1] + score[i - 1][j - 1]:
            pairs[j - 1] = i - 1
            i, j = i - 1, j - 1
        elif best[i][j] == best[i - 1][j]:
            i -= 1
        else:
            j -= 1
    return pairs


#: What pairing two entries scores on top of their similarity; see `align`.
_PAIRED = 0.01


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _set_list(
    parent: Map,
    key: str,
    items: list[Any],
    read: Callable[[Map], Any],
    normalize: Callable[[Any], Any],
    create: Callable[[Any], Map],
    fill: Callable[[Map, Any], None],
) -> None:
    """`parent[key]` as a `Y.Array` of maps reading `items`, changed as
    little as `align` can: entries are matched by content, not position.
    Each is filled once it is in the array, a new one included (`create` may
    make it empty)."""
    current = parent.get(key)
    if not isinstance(current, Array):
        parent[key] = Array()
        current = parent[key]
    entries = list(current)
    pairs = align(
        [_canonical(read(e)) if isinstance(e, Map) else "" for e in entries],
        [_canonical(normalize(item)) for item in items],
    )
    kept = {i for i in pairs if i is not None and isinstance(entries[i], Map)}
    for index in reversed(range(len(entries))):
        if index not in kept:
            del current[index]
    # What is left is the kept entries in order, each at the position of the
    # item it continues once the new ones before it are inserted.
    for index, (item, paired) in enumerate(zip(items, pairs, strict=True)):
        if paired not in kept:
            current.insert(index, create(item))
        fill(current[index], item)


def _nonempty(texts: dict[str, str]) -> dict[str, str]:
    return {language: text for language, text in texts.items() if text}


def _set_text_list(parent: Map, key: str, items: list[dict[str, str]] | None) -> None:
    if items is None:
        if key in parent:
            del parent[key]
        return
    _set_list(
        parent,
        key,
        items,
        read=_read_texts,
        normalize=_nonempty,
        create=_new_texts,
        fill=_fill_texts,
    )


def _set_value(parent: Map, key: str, value: Any) -> None:
    # Compared as values, not as text: the editor writes `2` where this
    # writes `2.0`, and rewriting either would only make the two flap.
    current = parent.get(key)
    if isinstance(current, str) and json.loads(current) == value:
        return
    parent[key] = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def _child(parent: Map, key: str) -> Map:
    current = parent.get(key)
    if isinstance(current, Map):
        return current
    parent[key] = Map()
    return parent[key]


def _set_fields(
    target: Map,
    data: Json,
    texts: Iterable[str] = (),
    text_lists: Iterable[str] = (),
    nested: dict[str, Callable[[Map, Json], None]] | None = None,
    skip: Iterable[str] = (),
) -> None:
    """Write `data`'s fields into `target`: texts as texts, `nested` through
    their own writers, the rest as JSON values. Fields `data` lacks are
    removed, except `skip` (written by the caller)."""
    texts, text_lists, nested = set(texts), set(text_lists), nested or {}
    skip = set(skip)
    for key in list(target.keys()):
        if key not in data and key not in skip:
            del target[key]
    for key, value in data.items():
        if key in skip:
            continue
        if key in texts:
            _set_texts(target, key, value)
        elif key in text_lists:
            _set_text_list(target, key, value)
        elif key in nested:
            if value is None:
                if key in target:
                    del target[key]
            else:
                nested[key](_child(target, key), value)
        else:
            _set_value(target, key, value)


_DIGITS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def order_keys(n: int) -> list[str]:
    """`n` consecutive order keys from the start, as the editor's
    `fractional-indexing` writes them (`generateNKeysBetween(null, null, n)`):
    "a0" ... "az", "b00" ... "bzz", "c000", ...

    The server only ever numbers a whole collection afresh, so this is all of
    the format it needs; keys *between* two others are made in the browser,
    which rejects any key not in this format. A head letter, from "a", gives
    the number of digits after it (one more per letter), and the digits are
    base 62.
    """
    keys = []
    for index in range(n):
        width, start = 1, 0
        while index - start >= len(_DIGITS) ** width:
            start += len(_DIGITS) ** width
            width += 1
        value, digits = index - start, ""
        for _ in range(width):
            value, digit = divmod(value, len(_DIGITS))
            digits = _DIGITS[digit] + digits
        keys.append(chr(ord("a") + width - 1) + digits)
    return keys


def _orders(current: Map, ids: list[str]) -> dict[str, str]:
    """The order keys `ids` should carry: kept where the entries already sit
    in that order, otherwise new ones for all of them."""
    existing = [
        (json.loads(current[id_][ORDER]), id_)
        for id_ in ids
        if isinstance(current.get(id_), Map) and ORDER in current[id_]
    ]
    if len(existing) == len(ids) and [id_ for _, id_ in sorted(existing)] == ids:
        return {id_: order for order, id_ in existing}
    return dict(zip(ids, order_keys(len(ids)), strict=True))


def _set_keyed(
    collection: Map,
    entries: list[tuple[str, Json]],
    write: Callable[[Map, Json], None],
    extra: Callable[[str], dict[str, Any]] | None = None,
    keep: Callable[[str], bool] | None = None,
) -> None:
    """Write `entries` (id, data) into a keyed collection, in their order.
    Ids not among them are removed, unless `keep` says otherwise (questions
    are one collection across sections)."""
    ids = [id_ for id_, _ in entries]
    wanted = set(ids)
    for id_ in list(collection.keys()):
        if id_ not in wanted and not (keep and keep(id_)):
            del collection[id_]
    orders = _orders(collection, ids)
    for id_, data in entries:
        entry = _child(collection, id_)
        write(entry, data)
        _set_value(entry, ORDER, orders[id_])
        for key, value in (extra(id_) if extra else {}).items():
            _set_value(entry, key, value)


def _write_option(entry: Map, data: Json) -> None:
    _set_fields(entry, data, texts=("text",), skip=("id", ORDER))


def _write_survey_item(target: Map, data: Json) -> None:
    options = data.get("options")
    _set_fields(target, data, texts=_SURVEY_TEXTS, skip=("options",))
    if options is None:
        if "options" in target:
            del target["options"]
        return
    _set_keyed(
        _child(target, "options"),
        [(str(option["id"]), option) for option in options],
        _write_option,
    )


def _write_image(target: Map, data: Json) -> None:
    _set_fields(target, data, texts=_IMAGE_TEXTS)


def _write_timed(target: Map, items: list[Json] | None) -> None:
    if items is None:
        if "timed_messages" in target:
            del target["timed_messages"]
        return
    _set_list(
        target,
        "timed_messages",
        items,
        read=lambda entry: _read_entity(entry, _TIMED_TEXTS),
        normalize=_normalize_timed,
        create=lambda _item: Map(),
        fill=lambda entry, item: _set_fields(entry, item, texts=_TIMED_TEXTS),
    )


def _normalize_timed(item: Json) -> Json:
    return {
        key: _nonempty(value) if key in _TIMED_TEXTS and value else value
        for key, value in item.items()
    }


def _write_question(entry: Map, data: Json) -> None:
    _set_fields(
        entry,
        data,
        texts=_QUESTION_TEXTS,
        text_lists=_QUESTION_TEXT_LISTS,
        nested={"survey_item": _write_survey_item, "image": _write_image},
        skip=("id", ORDER, SECTION_ID),
    )


def _write_section(entry: Map, data: Json) -> None:
    _set_fields(entry, data, texts=_SECTION_TEXTS, skip=("id", ORDER, "questions"))


def roots(doc: Doc) -> tuple[Map, Map, Map, Map]:
    """The doc's root maps: meta, guide, sections, questions."""
    return (
        doc.get("meta", type=Map),
        doc.get("guide", type=Map),
        doc.get("sections", type=Map),
        doc.get("questions", type=Map),
    )


def apply_guide(
    doc: Doc,
    guide: LocalizedInterviewGuide,
    languages: Iterable[str] = (),
    origin: Any = None,
) -> None:
    """Write `guide` into `doc` as the smallest set of changes, in one
    transaction. Seeds an empty doc, giving it a `doc_id`. Every text gets a
    (possibly empty) one in each of `languages`, the project's."""
    apply_guide_data(doc, guide.model_dump(mode="json"), languages, origin)


def apply_guide_data(
    doc: Doc, data: Json, languages: Iterable[str] = (), origin: Any = None
) -> None:
    """`apply_guide` for a guide's JSON, which need not validate: what
    `merge_guide_data` makes of a doc that does not."""
    token = _LANGUAGES.set(tuple(languages))
    try:
        _apply(doc, copy.deepcopy(data), origin)
    finally:
        _LANGUAGES.reset(token)


def _apply(doc: Doc, data: Json, origin: Any) -> None:
    meta, top, sections, questions = roots(doc)
    with doc.transaction(origin=origin):
        if DOC_ID not in meta:
            meta[DOC_ID] = str(uuid4())
        timed = data.pop("timed_messages", None)
        section_list = data.pop("question_sections")
        _set_fields(top, data, texts=_GUIDE_TEXTS, skip=("timed_messages",))
        _write_timed(top, timed)

        _set_keyed(
            sections,
            [(section["id"], section) for section in section_list],
            _write_section,
        )
        # Ordered within their section; one collection for all of them.
        for section in section_list:
            in_section = [(q["id"], q) for q in section["questions"]]
            _set_keyed(
                questions,
                in_section,
                _write_question,
                extra=lambda _id, section_id=section["id"]: {SECTION_ID: section_id},
                keep=lambda _id: True,
            )
        wanted = {q["id"] for s in section_list for q in s["questions"]}
        for id_ in list(questions.keys()):
            if id_ not in wanted:
                del questions[id_]


def new_doc(guide: LocalizedInterviewGuide, languages: Iterable[str] = ()) -> Doc:
    doc = Doc()
    apply_guide(doc, guide, languages)
    return doc


# -- Merging -------------------------------------------------------------------

_ABSENT: Any = object()


def _is_entities(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(entry, dict) and "id" in entry for entry in value)
    )


def _merge(base: Any, theirs: Any, ours: Any) -> Any:
    if theirs == ours:
        return theirs
    if theirs == base:
        return ours
    if ours == base:
        return theirs
    if isinstance(theirs, dict) and isinstance(ours, dict):
        base = base if isinstance(base, dict) else {}
        merged = {}
        for key in [*ours, *(k for k in theirs if k not in ours)]:
            value = _merge(
                base.get(key, _ABSENT), theirs.get(key, _ABSENT), ours.get(key, _ABSENT)
            )
            if value is not _ABSENT:
                merged[key] = value
        return merged
    if _is_entities(theirs) or _is_entities(ours):
        return _merge_entities(
            base if isinstance(base, list) else [],
            theirs if isinstance(theirs, list) else [],
            ours if isinstance(ours, list) else [],
        )
    # Both changed one plain value: the write being merged in wins.
    return theirs


def _merge_entities(
    base: list[Json], theirs: list[Json], ours: list[Json]
) -> list[Json]:
    """Lists of things with ids (sections, questions, options), merged by id:
    an entry either side added is kept, one either side removed is gone, and
    the order is whichever side changed it."""
    by_id = [{entry["id"]: entry for entry in side} for side in (base, theirs, ours)]
    in_base, in_theirs, in_ours = by_id
    order = (
        [entry["id"] for entry in theirs]
        if [entry["id"] for entry in theirs] != [entry["id"] for entry in base]
        else [entry["id"] for entry in ours]
    )
    order += [id_ for id_ in [*in_ours, *in_theirs] if id_ not in order]
    merged = []
    for id_ in order:
        if id_ in in_theirs and id_ in in_ours:
            merged.append(_merge(in_base.get(id_), in_theirs[id_], in_ours[id_]))
        elif id_ not in in_base:
            merged.append(in_theirs.get(id_) or in_ours[id_])
        # In the base and missing from one side: that side removed it.
    return merged


def merge_guide_data(base: Json, theirs: Json, ours: Json) -> Json:
    """Three-way merge of a guide's JSON: what `theirs` changed since `base`,
    applied to `ours`.

    For a write to the column the doc did not make: `base` is what the doc
    last agreed with the column, `theirs` the column now, `ours` the doc now
    -- which may hold edits not yet saved. Where both changed the same piece
    of text or value, `theirs` wins: it is a deliberate whole write (a
    restore, a translation).
    """
    return _merge(base, theirs, ours)


# -- Reading -------------------------------------------------------------------


def _read_texts(value: Any) -> dict[str, str]:
    # An emptied language is untranslated, as if it had never been written:
    # otherwise `{"EN": "x", "DA": ""}` and `{"EN": "x"}` would hash apart.
    return {
        language: str(text)
        for language, text in value.items()
        if isinstance(text, Text) and str(text)
    }


def _read_fields(source: Map) -> Json:
    """`source` as plain data: texts as language maps, arrays of texts as
    lists of them, JSON values decoded. Nested maps are left to the caller."""
    data: Json = {}
    for key, value in source.items():
        if isinstance(value, Map):
            data[key] = value
        elif isinstance(value, Array):
            data[key] = [
                _read_texts(entry) if isinstance(entry, Map) else entry
                for entry in value
            ]
        elif isinstance(value, Text):
            data[key] = str(value)
        else:
            data[key] = json.loads(value)
    return data


def _read_entity(source: Map, texts: Iterable[str]) -> Json:
    data = _read_fields(source)
    for key in texts:
        if isinstance(data.get(key), Map):
            data[key] = _read_texts(data[key])
    return data


def _sorted(collection: Map) -> list[tuple[str, Map]]:
    def key(item: tuple[str, Any]) -> tuple[str, str]:
        id_, entry = item
        order = entry.get(ORDER) if isinstance(entry, Map) else None
        return (json.loads(order) if order else "", id_)

    return sorted(
        ((id_, entry) for id_, entry in collection.items() if isinstance(entry, Map)),
        key=key,
    )


def _read_question(id_: str, entry: Map) -> Json:
    data = _read_entity(entry, _QUESTION_TEXTS)
    data.pop(ORDER, None)
    data.pop(SECTION_ID, None)
    item = data.get("survey_item")
    if isinstance(item, Map):
        survey = _read_entity(item, _SURVEY_TEXTS)
        options = survey.get("options")
        if isinstance(options, Map):
            survey["options"] = [
                {"id": option_id, "text": _read_texts(option.get("text") or Map())}
                for option_id, option in _sorted(options)
            ]
        data["survey_item"] = survey
    image = data.get("image")
    if isinstance(image, Map):
        data["image"] = _read_entity(image, _IMAGE_TEXTS)
    return {"id": id_, **data}


def read_guide_data(doc: Doc) -> Json:
    """The doc as the JSON of a `LocalizedInterviewGuide`, unvalidated.

    A question whose section is gone (deleted while someone edited it) is
    left out: it has nowhere to be asked."""
    _meta, top, sections, questions = roots(doc)
    data = _read_entity(top, _GUIDE_TEXTS)
    timed = top.get("timed_messages")
    if isinstance(timed, Array):
        data["timed_messages"] = [
            _read_entity(entry, _TIMED_TEXTS)
            for entry in timed
            if isinstance(entry, Map)
        ]
    by_section: dict[str, list[tuple[str, Map]]] = {}
    for id_, entry in _sorted(questions):
        section_id = entry.get(SECTION_ID)
        if section_id is not None:
            by_section.setdefault(json.loads(section_id), []).append((id_, entry))
    data["question_sections"] = [
        {
            "id": id_,
            **{
                k: v
                for k, v in _read_entity(entry, _SECTION_TEXTS).items()
                if k != ORDER
            },
            "questions": [
                _read_question(q_id, q) for q_id, q in by_section.get(id_, [])
            ],
        }
        for id_, entry in _sorted(sections)
    ]
    return data


def read_guide(doc: Doc) -> LocalizedInterviewGuide:
    """The doc as a guide. Raises `pydantic.ValidationError` if what editors
    wrote does not make one."""
    return LocalizedInterviewGuide.model_validate(read_guide_data(doc))


def doc_id(doc: Doc) -> str | None:
    meta = doc.get("meta", type=Map)
    value = meta.get(DOC_ID)
    return value if isinstance(value, str) else None
