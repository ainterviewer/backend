"""one multilingual content row per project

Revision ID: 916e657898af
Revises: 8bc0617dbbd2
Create Date: 2026-10-14 15:00:00.000000

"""

# NOTE: Don't manage the touch-last_updated triggers here. alembic/env.py
# uninstalls them before every run and reinstalls the full set from
# app/db/triggers.py after, so batch_alter_table on `project`, `testrun` etc.
# cannot lose or break them.
import json
import uuid
from collections import defaultdict
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.types

# revision identifiers, used by Alembic.
revision: str = "916e657898af"
down_revision: str | None = "8bc0617dbbd2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# A project's guide, consent and welcome were stored once per language, each a
# full copy of the structure with that language's text, on `projectlocalization`
# rows. They move to one `projectcontent` row per project in the lib's localized
# form (`ainterviewer.interview_guides.localized`): the structure once, every
# text leaf a `{language: text}` map. The participant emails become
# `{language: text}` maps on the same row; `agent_configs` and
# `prompt_overrides` move with them, per language only by accident of where
# they lived. What remained of a localization -- the language and the default
# flag -- becomes `languages` and `default_language` on the row, and
# `projectlocalization` is dropped.
#
# Deliberately plain JSON, not the lib's models: this must keep doing the same
# thing however the models change later. It mirrors
# `LocalizedInterviewGuide.from_rendered` as of lib 0.6:
#
# - The default localization supplies everything shared: sections, questions
#   and their order, settings, survey item types, conditions, images, timings.
#   Every other language contributes only text, matched by section and question
#   id (shared since revision 8bc0617dbbd2), and within a question by position
#   for probes, alternative wordings and options. Where a language disagrees,
#   its text is left missing -- the editor marks it untranslated -- and the
#   mismatch is printed. Nothing is invented.
# - Choice options get ids. A `match` condition whose trigger is one of the read
#   question's options (in the default language) names it by `option_id`;
#   rendering writes the option's text in the interview's language back into
#   `trigger_value`. Any other trigger stays a shared `trigger_value`.
# - `extra_variables` is the union over languages.
# - Interview rows keep their own rendered copy of the guide and are untouched.
#
# Downgrade recreates `projectlocalization` and renders every language back into
# it, untranslated leaves taking the default's text. Localization ids and
# per-language timestamps are not kept; nothing referenced them.

content = sa.table(
    "projectcontent",
    sa.column("id"),
    sa.column("project_id"),
    sa.column("languages", sa.Text()),
    sa.column("default_language"),
    sa.column("emails", sa.Text()),
    sa.column("guide", sa.Text()),
    sa.column("consent", sa.Text()),
    sa.column("welcome", sa.Text()),
    sa.column("prompt_overrides", sa.Text()),
    sa.column("agent_configs", sa.Text()),
    sa.column("created_at", app.db.types.AppDateTime()),
    sa.column("last_updated", app.db.types.AppDateTime()),
)
localization = sa.table(
    "projectlocalization",
    sa.column("id"),
    sa.column("project_id"),
    sa.column("language"),
    sa.column("is_default", sa.Boolean()),
    sa.column("created_at", app.db.types.AppDateTime()),
    sa.column("last_updated", app.db.types.AppDateTime()),
    sa.column("participant_email_subject"),
    sa.column("participant_email_template"),
    sa.column("participant_reminder_email_subject"),
    sa.column("participant_reminder_email_template"),
    sa.column("interview_guide", sa.Text()),
    sa.column("consent", sa.Text()),
    sa.column("welcome", sa.Text()),
    sa.column("prompt_overrides", sa.Text()),
    sa.column("agent_configs", sa.Text()),
)

CHOICE_ITEMS = ("radio", "checkbox", "likert")

# Question fields that carry text or are rebuilt; every other key is shared and
# copied from the default language as stored (absent stays absent, so the
# model's survey-item defaults still apply).
QUESTION_TEXT = {
    "main_question",
    "description",
    "probes",
    "alternative_main_questions",
    "survey_item",
    "image",
    "conditions",
    "index",
}


def _load(raw: str | None):
    return json.loads(raw) if raw else None


def _message(value):
    """`introduction` and `outro` may be stored as an `InterviewMessage`."""
    return value.get("message") if isinstance(value, dict) else value


class Merge:
    def __init__(self, structure: str, where: str) -> None:
        self.structure = structure
        self.where = where
        self.report: list[str] = []

    def note(self, line: str) -> None:
        self.report.append(f"{self.where}: {line}")

    def text(self, values: dict[str, str | None]) -> dict[str, str] | None:
        if values.get(self.structure) is None:
            return None
        return {language: value for language, value in values.items() if value}

    def texts(self, values: dict[str, list | None], where: str) -> list | None:
        base = values.get(self.structure)
        if base is None:
            return None
        for language, items in values.items():
            if language != self.structure and len(items or []) != len(base):
                self.note(
                    f"{language}: {where} has {len(items or [])} entries against "
                    f"{len(base)}; the rest are left untranslated"
                )
        return [
            {
                language: items[n]
                for language, items in values.items()
                if items and n < len(items) and items[n]
            }
            for n in range(len(base))
        ]


def _survey_item(items: dict[str, dict | None], merge: Merge, where: str):
    base = items.get(merge.structure)
    if base is None:
        return None
    same = {
        language: item
        for language, item in items.items()
        if item is not None and item.get("type") == base.get("type")
    }
    for language in items.keys() - same.keys():
        merge.note(
            f"{language}: {where} has no {base.get('type')} survey item like "
            f"{merge.structure}'s; its text is left untranslated"
        )

    if base.get("type") in CHOICE_ITEMS:
        options = merge.texts(
            {language: item.get("options") for language, item in same.items()},
            f"{where}.survey_item.options",
        )
        return {
            **base,
            "options": [{"id": str(uuid.uuid4()), "text": t} for t in options or []],
        }
    if base.get("type") == "slider":
        return {
            **base,
            "min_label": merge.text(
                {language: item.get("min_label") for language, item in same.items()}
            ),
            "max_label": merge.text(
                {language: item.get("max_label") for language, item in same.items()}
            ),
        }
    return base


def _image(images: dict[str, dict | None], merge: Merge):
    base = images.get(merge.structure)
    if base is None:
        return None
    present = {language: image for language, image in images.items() if image}

    def leaf(key: str):
        return merge.text(
            {language: image.get(key) for language, image in present.items()}
        )

    return {
        "type": base.get("type", "image"),
        "name": base["name"],
        "data": base.get("data"),
        "primer": leaf("primer"),
        "description": leaf("description") or {},
        "alt": leaf("alt") or {},
    }


def _question(questions: dict[str, dict], merge: Merge, where: str) -> dict:
    base = questions[merge.structure]

    def leaf(key: str):
        return merge.text({language: q.get(key) for language, q in questions.items()})

    def leaves(key: str):
        return merge.texts(
            {language: q.get(key) for language, q in questions.items()},
            f"{where}.{key}",
        )

    out = {key: value for key, value in base.items() if key not in QUESTION_TEXT}
    out["main_question"] = leaf("main_question") or {}
    out["description"] = leaf("description")
    out["probes"] = leaves("probes")
    out["alternative_main_questions"] = leaves("alternative_main_questions")
    out["survey_item"] = _survey_item(
        {language: q.get("survey_item") for language, q in questions.items()},
        merge,
        where,
    )
    out["image"] = _image(
        {language: q.get("image") for language, q in questions.items()}, merge
    )
    out["conditions"] = None  # converted once every option has an id
    return out


def _conditions(conditions: dict, options: dict[str, list], merge: Merge, where: str):
    def evaluation(condition: dict, evaluation: dict) -> dict:
        read = options.get(condition["question_context"]["question_id"])
        operator = evaluation.get("comparison_operator", "==")
        value = evaluation["trigger_value"]
        rest = {
            "comparison_operator": operator,
            "combine_next": evaluation.get("combine_next"),
        }
        if (
            read
            and condition.get("trigger_type", "match") == "match"
            and operator == "=="
        ):
            key = str(value).strip().casefold()
            for option in read:
                text = option["text"].get(merge.structure, "")
                if text.strip().casefold() == key:
                    return {"option_id": option["id"], **rest}
            merge.note(
                f"{where}: trigger {value!r} is none of the read question's "
                "options; kept as a pattern shared by every language"
            )
        return {"trigger_value": value, **rest}

    return {
        "action": conditions["action"],
        "conditions": [
            {
                **{k: v for k, v in condition.items() if k != "evaluation"},
                "evaluation": [
                    evaluation(condition, e) for e in condition["evaluation"]
                ],
            }
            for condition in conditions["conditions"]
        ],
    }


def _guide(guides: dict[str, dict], merge: Merge) -> dict:
    base = guides[merge.structure]
    sections_by_id = {
        language: {s["id"]: s for s in guide.get("question_sections") or []}
        for language, guide in guides.items()
    }

    sections = []
    for n_section, section in enumerate(base.get("question_sections") or []):
        where = f"question_sections[{n_section}]"
        renderings = {
            language: found[section["id"]]
            for language, found in sections_by_id.items()
            if section["id"] in found
        }
        for language in guides.keys() - renderings.keys():
            merge.note(f"{language}: {where} is missing; its text is left untranslated")

        questions_by_id = {
            language: {q["id"]: q for q in s.get("questions") or []}
            for language, s in renderings.items()
        }
        questions = []
        for n_question, question in enumerate(section.get("questions") or []):
            q_where = f"{where}.questions[{n_question}]"
            found = {
                language: by_id[question["id"]]
                for language, by_id in questions_by_id.items()
                if question["id"] in by_id
            }
            for language in renderings.keys() - found.keys():
                merge.note(
                    f"{language}: {q_where} is missing; its text is left untranslated"
                )
            questions.append(_question(found, merge, q_where))

        sections.append(
            {
                **{
                    k: v
                    for k, v in section.items()
                    if k not in ("description", "questions")
                },
                "description": merge.text(
                    {
                        language: s.get("description")
                        for language, s in renderings.items()
                    }
                )
                or {},
                "questions": questions,
            }
        )

    options = {
        question["id"]: question["survey_item"]["options"]
        for section in sections
        for question in section["questions"]
        if (question["survey_item"] or {}).get("type") in CHOICE_ITEMS
    }
    for n_section, (section, source) in enumerate(
        zip(sections, base.get("question_sections") or [])
    ):
        for n_question, (question, original) in enumerate(
            zip(section["questions"], source.get("questions") or [])
        ):
            if original.get("conditions"):
                question["conditions"] = _conditions(
                    original["conditions"],
                    options,
                    merge,
                    f"question_sections[{n_section}].questions[{n_question}]",
                )

    def top(key: str, default=None):
        values = {
            language: _message(g.get(key, default)) for language, g in guides.items()
        }
        return merge.text(values)

    timed = merge.texts(
        {
            language: [m["message"] for m in g["timed_messages"]]
            if g.get("timed_messages") is not None
            else None
            for language, g in guides.items()
        },
        "timed_messages",
    )
    return {
        "framing": top("framing", "") or {},
        "introduction": top("introduction", "") or {},
        "question_sections": sections,
        "outro": top("outro"),
        "alt_outro": top("alt_outro"),
        "extra_variables": list(
            dict.fromkeys(
                variable
                for g in (base, *guides.values())
                for variable in g.get("extra_variables") or []
            )
        ),
        "timed_messages": None
        if base.get("timed_messages") is None or timed is None
        else [
            {**{k: v for k, v in m.items() if k != "message"}, "message": text}
            for m, text in zip(base["timed_messages"], timed)
        ],
        "ai_generated_sections": base.get("ai_generated_sections", 0),
    }


def _consent(consents: dict[str, dict | None]) -> dict | None:
    present = {language: c for language, c in consents.items() if c}
    if not present:
        return None
    return {
        "title": {lang: c["title"] for lang, c in present.items() if c.get("title")},
        "text": {lang: c["text"] for lang, c in present.items() if c.get("text")},
    }


def _welcome(welcomes: dict[str, dict | None], structure: str, merge: Merge):
    present = {language: w for language, w in welcomes.items() if w}
    if not present:
        return None
    if structure not in present:
        structure = min(present)
        merge.note(f"welcome: the default has none; shared fields from {structure}")
    base = present[structure]
    return {
        **{k: v for k, v in base.items() if k not in ("title", "text")},
        "title": {lang: w["title"] for lang, w in present.items() if w.get("title")},
        "text": {lang: w["text"] for lang, w in present.items() if w.get("text")},
    }


EMAIL_COLUMNS = {
    ("invite", "subject"): "participant_email_subject",
    ("invite", "template"): "participant_email_template",
    ("reminder", "subject"): "participant_reminder_email_subject",
    ("reminder", "template"): "participant_reminder_email_template",
}


def _emails(rows) -> dict:
    emails: dict = {"invite": {"subject": {}, "template": {}}}
    emails["reminder"] = {"subject": {}, "template": {}}
    for row in rows:
        for (email, part), column in EMAIL_COLUMNS.items():
            if value := getattr(row, column):
                emails[email][part][row.language] = value
    return emails


def upgrade() -> None:
    op.create_table(
        "projectcontent",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("languages", sa.JSON(), nullable=False),
        sa.Column("default_language", sa.VARCHAR(length=2), nullable=False),
        sa.Column("guide", sa.JSON(), nullable=False),
        sa.Column("consent", sa.JSON(), nullable=True),
        sa.Column("welcome", sa.JSON(), nullable=True),
        sa.Column(
            "prompt_overrides",
            sa.JSON(),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("agent_configs", sa.JSON(), nullable=False),
        sa.Column("emails", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_updated", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            """instr(languages, '"' || default_language || '"') > 0""",
            name=op.f("ck_projectcontent_default_language_offered"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_projectcontent_project_id_project"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_projectcontent")),
        sa.UniqueConstraint("id", name=op.f("uq_projectcontent_id")),
    )
    op.create_index(
        op.f("ix_projectcontent_project_id"),
        "projectcontent",
        ["project_id"],
        unique=True,
    )

    bind = op.get_bind()
    by_project = defaultdict(list)
    for row in bind.execute(sa.select(localization)).all():
        by_project[row.project_id].append(row)

    for project_id, rows in by_project.items():
        default = next((row for row in rows if row.is_default), None)
        if default is None:
            raise RuntimeError(
                f"[{revision}] project {project_id} has no default localization"
            )
        merge = Merge(default.language, f"project {project_id}")
        # The default first, the rest in the order they were added.
        rows.sort(key=lambda row: (not row.is_default, row.created_at))

        guides = {
            row.language: guide for row in rows if (guide := _load(row.interview_guide))
        }
        if default.language not in guides:
            guides[default.language] = {}

        for row in rows:
            if row.language != default.language and (
                _load(row.agent_configs) != _load(default.agent_configs)
                or _load(row.prompt_overrides) != _load(default.prompt_overrides)
            ):
                merge.note(
                    f"{row.language}: agent configs or prompt overrides differ from "
                    f"{default.language}'s; {default.language}'s are kept"
                )

        welcome = _welcome(
            {row.language: _load(row.welcome) for row in rows}, default.language, merge
        )
        consent = _consent({row.language: _load(row.consent) for row in rows})
        updated = [row.last_updated for row in rows if row.last_updated is not None]
        bind.execute(
            content.insert().values(
                id=uuid.uuid4().hex,
                project_id=project_id,
                languages=json.dumps([row.language for row in rows]),
                default_language=default.language,
                guide=json.dumps(_guide(guides, merge)),
                consent=None if consent is None else json.dumps(consent),
                welcome=None if welcome is None else json.dumps(welcome),
                prompt_overrides=default.prompt_overrides or "{}",
                agent_configs=default.agent_configs,
                emails=json.dumps(_emails(rows)),
                created_at=default.created_at,
                last_updated=max(updated) if updated else default.created_at,
            )
        )
        for line in merge.report:
            print(f"[{revision}] {line}")

    # Its indexes go with it.
    op.drop_table("projectlocalization")


# ----- downgrade: render every language back ----------------------------------


def _pick(text: dict | None, languages: list[str]) -> str | None:
    if text is None:
        return None
    for language in languages:
        if value := text.get(language):
            return value
    return ""


def _render_guide(guide: dict, languages: list[str]) -> dict:
    options = {
        option["id"]: option["text"]
        for section in guide["question_sections"]
        for question in section["questions"]
        if (question.get("survey_item") or {}).get("type") in CHOICE_ITEMS
        for option in question["survey_item"]["options"]
    }

    def survey_item(item):
        if item is None:
            return None
        if item["type"] in CHOICE_ITEMS:
            return {
                **item,
                "options": [_pick(o["text"], languages) for o in item["options"]],
            }
        if item["type"] == "slider":
            return {
                **item,
                "min_label": _pick(item.get("min_label"), languages),
                "max_label": _pick(item.get("max_label"), languages),
            }
        return item

    def conditions(conditions):
        if conditions is None:
            return None

        def evaluation(e):
            value = (
                _pick(options[e["option_id"]], languages)
                if e.get("option_id")
                else e["trigger_value"]
            )
            return {
                "trigger_value": value,
                "comparison_operator": e.get("comparison_operator", "=="),
                "combine_next": e.get("combine_next"),
            }

        return {
            "action": conditions["action"],
            "conditions": [
                {**c, "evaluation": [evaluation(e) for e in c["evaluation"]]}
                for c in conditions["conditions"]
            ],
        }

    def image(image):
        if image is None:
            return None
        return {
            **image,
            "primer": _pick(image.get("primer"), languages),
            "description": _pick(image["description"], languages),
            "alt": _pick(image["alt"], languages),
        }

    def question(q):
        return {
            **q,
            "main_question": _pick(q["main_question"], languages),
            "description": _pick(q.get("description"), languages),
            "probes": None
            if q.get("probes") is None
            else [_pick(p, languages) for p in q["probes"]],
            "alternative_main_questions": None
            if q.get("alternative_main_questions") is None
            else [_pick(a, languages) for a in q["alternative_main_questions"]],
            "survey_item": survey_item(q.get("survey_item")),
            "image": image(q.get("image")),
            "conditions": conditions(q.get("conditions")),
        }

    return {
        **guide,
        "framing": _pick(guide["framing"], languages),
        "introduction": _pick(guide["introduction"], languages),
        "outro": _pick(guide.get("outro"), languages),
        "alt_outro": _pick(guide.get("alt_outro"), languages),
        "question_sections": [
            {
                **s,
                "description": _pick(s["description"], languages),
                "questions": [question(q) for q in s["questions"]],
            }
            for s in guide["question_sections"]
        ],
        "timed_messages": None
        if guide.get("timed_messages") is None
        else [
            {**m, "message": _pick(m["message"], languages)}
            for m in guide["timed_messages"]
        ],
    }


def _render_texts(value: dict | None, languages: list[str]) -> dict | None:
    if value is None:
        return None
    return {
        **value,
        "title": _pick(value["title"], languages),
        "text": _pick(value["text"], languages),
    }


def downgrade() -> None:
    op.create_table(
        "projectlocalization",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("language", sa.VARCHAR(length=2), nullable=False),
        sa.Column("consent", sa.JSON(), nullable=True),
        sa.Column("welcome", sa.JSON(), nullable=True),
        sa.Column("interview_guide", sa.JSON(), nullable=False),
        sa.Column("agent_configs", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_updated", sa.DateTime(), nullable=True),
        sa.Column("participant_email_template", sa.String(), nullable=True),
        sa.Column("participant_email_subject", sa.String(), nullable=True),
        sa.Column("participant_reminder_email_subject", sa.String(), nullable=True),
        sa.Column("participant_reminder_email_template", sa.String(), nullable=True),
        sa.Column(
            "prompt_overrides",
            sa.JSON(),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "is_default", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_projectlocalization_project_id_project"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_projectlocalization")),
        sa.UniqueConstraint("id", name=op.f("uq_projectlocalization_id")),
        sa.UniqueConstraint(
            "project_id", "language", name=op.f("unique_project_language")
        ),
    )
    op.create_index(
        op.f("ix_projectlocalization_language"),
        "projectlocalization",
        ["language"],
        unique=False,
    )
    op.create_index(
        "uq_project_default_language",
        "projectlocalization",
        ["project_id"],
        unique=True,
        sqlite_where=sa.text("is_default"),
        postgresql_where=sa.text("is_default"),
    )

    bind = op.get_bind()
    for stored in bind.execute(sa.select(content)).all():
        emails = json.loads(stored.emails)
        for language in json.loads(stored.languages):
            languages = [language, stored.default_language]
            bind.execute(
                localization.insert().values(
                    id=uuid.uuid4().hex,
                    project_id=stored.project_id,
                    language=language,
                    is_default=language == stored.default_language,
                    interview_guide=json.dumps(
                        _render_guide(json.loads(stored.guide), languages)
                    ),
                    consent=None
                    if (c := _load(stored.consent)) is None
                    else json.dumps(_render_texts(c, languages)),
                    welcome=None
                    if (w := _load(stored.welcome)) is None
                    else json.dumps(_render_texts(w, languages)),
                    prompt_overrides=stored.prompt_overrides,
                    agent_configs=stored.agent_configs,
                    created_at=stored.created_at,
                    last_updated=stored.last_updated,
                    **{
                        column: emails[email][part].get(language)
                        for (email, part), column in EMAIL_COLUMNS.items()
                    },
                )
            )

    op.drop_table("projectcontent")
