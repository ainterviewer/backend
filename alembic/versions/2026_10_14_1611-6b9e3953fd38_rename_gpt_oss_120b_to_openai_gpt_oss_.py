"""rename gpt-oss-120b to openai/gpt-oss-120b

The self-hosted gpt-oss model is named `openai/gpt-oss-120b` now (a name
without a provider prefix goes to the vLLM server as-is; see
`ainterviewer.lpm.clients.chat`), but most projects still name it
`gpt-oss-120b`, which is not in `available_models`: the agents page cannot
show it, and it only reaches the model while the server keeps the old alias.

Renamed where a model is *configured*, so every future interview and test
uses the new name:

- `agent_configs` on the draft (`projectcontent`) and on every version
  (`projectcontentversion`), published ones included. Versions are otherwise
  immutable, but this is the same model under its new name, so what a version
  does is unchanged -- and leaving the published ones would leave respondents
  on the old name until the next publish.
- `testsetup.answering_model`.

Not where a model is *recorded* -- `task.model`, `testrun.answering_model`:
those say what ran at the time.

Only an exact match is renamed. `llava` stays: it is `VisualConfig`'s own
default, for an agent that is off and hidden in the UI. The stored hash of a
test snapshot no longer matches its content; that only costs the next
identical snapshot its dedupe. The draft's `revision` is bumped where it
changed, as an ORM write would.

The downgrade does nothing: after the upgrade, a model reading
`openai/gpt-oss-120b` may have been chosen that way on purpose, and renaming
it back would be a guess.

Revision ID: 6b9e3953fd38
Revises: 1a7c2e94b5d3
Create Date: 2026-10-14 16:11:30.903826

"""

# NOTE: Don't manage the touch-last_updated triggers here. alembic/env.py
# uninstalls them before every run and reinstalls the full set from
# app/db/triggers.py after, so batch_alter_table on `project`, `testrun` etc.
# cannot lose or break them.
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6b9e3953fd38"
down_revision: str | None = "1a7c2e94b5d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Old name -> new name. Frozen here: what this revision renamed, whatever
#: the settings say later.
RENAMES = {"gpt-oss-120b": "openai/gpt-oss-120b"}


def _rename_agent_configs(connection, table: str) -> int:
    """Rename the model of every agent in `table`'s `agent_configs`.
    Returns how many agent configs changed."""
    changed = 0
    rows = connection.execute(sa.text(f"SELECT id, agent_configs FROM {table}")).all()
    for row_id, raw in rows:
        configs = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(configs, dict):
            continue
        touched = False
        for config in configs.values():
            if isinstance(config, dict) and config.get("model") in RENAMES:
                config["model"] = RENAMES[config["model"]]
                touched = True
                changed += 1
        if touched:
            # The draft's revision too, as an ORM write would bump it: an editor
            # open across the deploy must not save the old name back unasked.
            bump = ", revision = revision + 1" if table == "projectcontent" else ""
            connection.execute(
                sa.text(
                    f"UPDATE {table} SET agent_configs = :configs{bump} WHERE id = :id"
                ),
                {"configs": json.dumps(configs), "id": row_id},
            )
    return changed


def upgrade() -> None:
    connection = op.get_bind()
    for table in ("projectcontent", "projectcontentversion"):
        _rename_agent_configs(connection, table)
    for old, new in RENAMES.items():
        connection.execute(
            sa.text(
                "UPDATE testsetup SET answering_model = :new "
                "WHERE answering_model = :old"
            ),
            {"old": old, "new": new},
        )


def downgrade() -> None:
    pass
