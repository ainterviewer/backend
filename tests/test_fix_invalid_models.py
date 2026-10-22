"""`fix_invalid_models` repoints configured models that are not available,
and nothing else.

It replaces every unknown model with one, so what it must not touch matters as
much as what it does: an agent's own default, the published versions it has to
reach, and the records of what ran.
"""

import json

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session

from ainterviewer.agents.config import AgentConfigs
from ainterviewer.settings import settings as lib_settings
from ainterviewer.types import TestType
from app.db.cli import invalid_model_changes
from app.db.crud import InterviewDataBase
from app.db.tables import (
    Base,
    ProjectContentTable,
    ProjectFolderTable,
    TestRunTable,
    TestSetupTable,
    UserTable,
)
from app.db.triggers import register_trigger_functions
from app.types import TestRunStatus

OLD = "an-old-model"


@pytest.fixture
def db():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _enforce(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    register_trigger_functions(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        user = UserTable(email="a@example.com", password="x", first_name="A")
        folder = ProjectFolderTable(title="f")
        session.add_all([user, folder])
        session.commit()
        database = InterviewDataBase(session)
        project_id = database.projects.create_project(
            folder_id=folder.id, title="p", owner_id=user.id, default_language="EN"
        )
        configs = AgentConfigs()
        assert configs.probing is not None
        configs.probing.model = OLD
        database.projects.update_agent_configs(project_id, configs)
        database.versions.publish(project_id, user.id, None)
        setup = TestSetupTable(
            project_id=project_id,
            name="s",
            type=TestType.FIXED_AI,
            language="EN",
            n_interviews=1,
            answering_model=OLD,
        )
        session.add(setup)
        session.flush()
        session.add(
            TestRunTable(
                test_setup_id=setup.id,
                n_interviews=1,
                status=TestRunStatus.COMPLETED,
                answering_model=OLD,
            )
        )
        session.commit()
        yield session, project_id


def _models(session, table):
    return [
        {name: config.get("model") for name, config in json.loads(raw).items()}
        for (raw,) in session.execute(text(f"SELECT agent_configs FROM {table}"))
    ]


def test_configured_models_are_repointed_in_the_draft_and_every_version(db):
    session, _ = db
    to = lib_settings.llm.default_model

    changes = invalid_model_changes(session, to)

    for table in ("projectcontent", "projectcontentversion"):
        (models,) = _models(session, table)
        assert models["probing"] == to
    assert session.execute(text("SELECT answering_model FROM testsetup")).scalar() == to
    assert len(changes) == 3


def test_an_agents_own_default_is_left_alone(db):
    session, _ = db
    visual_default = AgentConfigs().visual.model
    assert visual_default not in lib_settings.llm.available_models

    invalid_model_changes(session, lib_settings.llm.default_model)

    (models,) = _models(session, "projectcontentversion")
    assert models["visual"] == visual_default


def test_what_ran_is_not_rewritten(db):
    session, _ = db

    invalid_model_changes(session, lib_settings.llm.default_model)

    assert session.execute(text("SELECT answering_model FROM testrun")).scalar() == OLD


def test_the_draft_revision_moves_so_open_editors_cannot_save_the_old_model(db):
    session, project_id = db
    revision = session.execute(
        select(ProjectContentTable.revision).where(
            ProjectContentTable.project_id == project_id
        )
    ).scalar_one()

    invalid_model_changes(session, lib_settings.llm.default_model)

    after = session.execute(
        text("SELECT revision FROM projectcontent WHERE project_id = :id"),
        {"id": project_id.hex},
    ).scalar_one()
    assert after == revision + 1
