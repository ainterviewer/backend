"""Test-only models and speech keep a project out of distributed interviews
(app/distribution.py): publishing refuses them, a distributed interview is
refused on a published version that uses them, and `fix-distributed-models`
moves published projects off them.

conftest.py pins the model lists: the default model may be distributed,
`TEST_ONLY_MODEL` may not.
"""

import json

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from ainterviewer.agents.config import AgentConfigs
from ainterviewer.config import InterviewConfig
from ainterviewer.settings import settings as lib_settings
from app import distribution
from app.db.cli import distributed_model_changes
from app.db.crud import InterviewDataBase
from app.db.repositories.errors import (
    NotDistributableError,
    NotPublishedError,
    PublishError,
)
from app.db.tables import Base, ProjectFolderTable, UserTable
from app.db.triggers import register_trigger_functions
from app.distribution import SPEECH, uses_of_test_only
from tests.conftest import TEST_ONLY_MODEL

LOCAL = "openai/gpt-oss-120b"


@pytest.fixture
def speech_test_only(monkeypatch):
    monkeypatch.setattr(distribution, "speech_is_test_only", lambda: True)


@pytest.fixture
def speech_allowed(monkeypatch):
    monkeypatch.setattr(distribution, "speech_is_test_only", lambda: False)


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
        yield database, project_id, user.id


def _configs(**models: str) -> AgentConfigs:
    configs = AgentConfigs()
    for name, model in models.items():
        getattr(configs, name).model = model
    return configs


def _set_published_model(database, agent: str, model: str) -> None:
    """What a version published before the rule holds: written past it."""
    database.session.execute(
        text(
            "UPDATE projectcontentversion SET agent_configs = "
            f"json_set(agent_configs, '$.{agent}.model', :m)"
        ),
        {"m": model},
    )
    database.session.commit()
    database.session.expire_all()


# ==================== What counts ====================


def test_the_default_agents_may_be_distributed(speech_allowed):
    assert lib_settings.llm.default_model in lib_settings.llm.models
    assert uses_of_test_only(AgentConfigs(), InterviewConfig()) == {}


def test_an_agent_on_a_test_only_model_is_reported(speech_allowed):
    uses = uses_of_test_only(_configs(probing=TEST_ONLY_MODEL), InterviewConfig())
    assert uses == {"probing": TEST_ONLY_MODEL}


def test_a_model_in_neither_list_counts_as_test_only(speech_allowed):
    uses = uses_of_test_only(_configs(history="unlisted"), InterviewConfig())
    assert uses == {"history": "unlisted"}


def test_the_answering_agent_only_runs_in_synthetic_tests(speech_allowed):
    uses = uses_of_test_only(_configs(answering=TEST_ONLY_MODEL), InterviewConfig())
    assert uses == {}


def test_an_agent_left_out_does_not_count(speech_allowed):
    configs = _configs(security=TEST_ONLY_MODEL)
    configs.security.include = False
    # The visual agent defaults to llava, in neither list, and off.
    assert not configs.visual.include
    assert uses_of_test_only(configs, InterviewConfig()) == {}


def test_audio_counts_only_while_speech_is_test_only(monkeypatch):
    config = InterviewConfig(with_audio=True)
    monkeypatch.setattr(distribution, "speech_is_test_only", lambda: False)
    assert uses_of_test_only(AgentConfigs(), config) == {}
    monkeypatch.setattr(distribution, "speech_is_test_only", lambda: True)
    assert SPEECH in uses_of_test_only(AgentConfigs(), config)
    assert uses_of_test_only(AgentConfigs(), InterviewConfig()) == {}


def test_describe_uses_names_agents_and_speech():
    describe = distribution.describe_uses
    assert describe({"probing": "m"}) == "the probing agent"
    assert describe({"probing": "m", "security": "m", "history": "m"}) == (
        "the probing, security and history agents"
    )
    assert describe({"probing": "m", SPEECH: "s"}) == "the probing agent and speech"
    assert describe({SPEECH: "s"}) == "speech"


# ==================== Publishing ====================


def test_publishing_a_test_only_model_is_refused(db, speech_allowed):
    database, project_id, user_id = db
    database.projects.update_agent_configs(
        project_id, _configs(probing=TEST_ONLY_MODEL)
    )

    with pytest.raises(PublishError) as exc:
        database.versions.publish(project_id, user_id, None)

    assert exc.value.test_only == {"probing": TEST_ONLY_MODEL}
    assert exc.value.body()["test_only"] == {"probing": TEST_ONLY_MODEL}
    assert not database.versions.is_published(project_id)


def test_publishing_with_test_only_speech_is_refused(db, speech_test_only):
    database, project_id, user_id = db
    database.projects.update_interview_config(
        project_id, InterviewConfig(with_audio=True)
    )

    with pytest.raises(PublishError, match="Turn off audio") as exc:
        database.versions.publish(project_id, user_id, None)

    assert SPEECH in exc.value.test_only


def test_the_status_reports_the_draft_and_the_published_version(db, speech_allowed):
    database, project_id, user_id = db
    database.versions.publish(project_id, user_id, None)
    database.projects.update_agent_configs(project_id, _configs(guide=TEST_ONLY_MODEL))

    status = database.versions.status(project_id)
    assert status.test_only == {"guide": TEST_ONLY_MODEL}
    assert status.published_test_only == {}

    _set_published_model(database, "probing", TEST_ONLY_MODEL)
    assert database.versions.status(project_id).published_test_only == {
        "probing": TEST_ONLY_MODEL
    }


# ==================== Distributed interviews ====================


def test_a_distributable_published_version_may_be_distributed(db, speech_allowed):
    database, project_id, user_id = db
    with pytest.raises(NotPublishedError):
        database.versions.check_distributable(project_id)

    database.versions.publish(project_id, user_id, None)
    database.versions.check_distributable(project_id)


def test_a_version_published_before_the_rule_is_not_distributed(db, speech_allowed):
    database, project_id, user_id = db
    database.versions.publish(project_id, user_id, None)
    _set_published_model(database, "probing", TEST_ONLY_MODEL)

    with pytest.raises(NotDistributableError):
        database.versions.check_distributable(project_id)


def test_audio_turned_on_in_the_draft_leaves_respondents_alone(db, speech_test_only):
    database, project_id, user_id = db
    database.versions.publish(project_id, user_id, None)
    # The interview config is versioned: this is a draft change, saved as one.
    database.projects.update_interview_config(
        project_id, InterviewConfig(with_audio=True)
    )

    database.versions.check_distributable(project_id)
    status = database.versions.status(project_id)
    assert status.has_unpublished_changes
    assert status.pending_bump == "minor"
    assert SPEECH in status.test_only and status.published_test_only == {}
    with pytest.raises(PublishError):
        database.versions.publish(project_id, user_id, None)


def test_a_version_published_with_audio_is_not_distributed(db, speech_test_only):
    database, project_id, user_id = db
    database.versions.publish(project_id, user_id, None)
    database.session.execute(
        text(
            "UPDATE projectcontentversion SET config = "
            "json_set(config, '$.with_audio', json('true'))"
        )
    )
    database.session.commit()
    database.session.expire_all()

    with pytest.raises(NotDistributableError):
        database.versions.check_distributable(project_id)


def test_the_interview_config_has_its_own_revision(db, speech_allowed):
    database, project_id, _ = db
    content = database.projects
    from app.db.repositories.errors import ContentRevisionError
    from app.types import ContentPart

    agents = content.get_revision(project_id, ContentPart.AGENTS)
    settings = content.get_revision(project_id, ContentPart.SETTINGS)
    content.update_interview_config(
        project_id, InterviewConfig(with_welcome=False), settings
    )

    assert content.get_revision(project_id, ContentPart.SETTINGS) == settings + 1
    assert content.get_revision(project_id, ContentPart.AGENTS) == agents
    assert not content.get_interview_config(project_id).with_welcome
    with pytest.raises(ContentRevisionError):
        content.update_interview_config(project_id, InterviewConfig(), settings)


def test_only_the_published_version_decides(db, speech_allowed):
    database, project_id, user_id = db
    database.versions.publish(project_id, user_id, None)
    # A draft being tried out on another model closes nothing.
    database.projects.update_agent_configs(
        project_id, _configs(probing=TEST_ONLY_MODEL)
    )
    database.versions.check_distributable(project_id)


# ==================== fix-distributed-models ====================


def _agent_models(database, table: str) -> list[dict[str, str]]:
    return [
        {name: sub.get("model") for name, sub in json.loads(raw).items()}
        for (raw,) in database.session.execute(
            text(f"SELECT agent_configs FROM {table} ORDER BY id")
        )
    ]


def test_published_projects_are_moved_off_test_only_models(db, speech_allowed):
    database, project_id, user_id = db
    database.versions.publish(project_id, user_id, None)
    _set_published_model(database, "probing", TEST_ONLY_MODEL)
    _set_published_model(database, "answering", TEST_ONLY_MODEL)
    database.projects.update_agent_configs(
        project_id, _configs(probing=TEST_ONLY_MODEL, answering=TEST_ONLY_MODEL)
    )

    changes = distributed_model_changes(database.session, LOCAL)
    database.session.commit()
    database.session.expire_all()

    assert len(changes) == 1
    (published,) = _agent_models(database, "projectcontentversion")
    assert published["probing"] == LOCAL
    # Synthetic tests only: left as configured.
    assert published["answering"] == TEST_ONLY_MODEL
    database.versions.check_distributable(project_id)

    # The draft keeps its models, so tests stay on the hosted one; publishing
    # it switches them (`switch_models_to`).
    (draft,) = _agent_models(database, "projectcontent")
    assert draft["probing"] == TEST_ONLY_MODEL
    assert database.versions.status(project_id).test_only != {}


def test_an_unpublished_project_is_left_alone(db, speech_allowed):
    database, project_id, _ = db
    database.projects.update_agent_configs(
        project_id, _configs(probing=TEST_ONLY_MODEL)
    )

    assert distributed_model_changes(database.session, LOCAL) == []


def test_audio_is_reported_and_switched_off_only_when_asked(db, speech_allowed):
    database, project_id, user_id = db
    # Published with audio while speech was allowed; then it no longer is.
    database.projects.update_interview_config(
        project_id, InterviewConfig(with_audio=True)
    )
    database.versions.publish(project_id, user_id, None)
    projects = database.projects
    with pytest.MonkeyPatch.context() as mp:
        # The command reads it through its own import.
        mp.setattr("app.db.cli.speech_is_test_only", lambda: True)

        reported = distributed_model_changes(database.session, LOCAL)
        assert len(reported) == 1
        assert all("still closed" in line for line in reported)
        database.session.expire_all()
        assert projects.get_interview_config(project_id, "published").with_audio

        changed = distributed_model_changes(database.session, LOCAL, disable_audio=True)
        database.session.commit()
        database.session.expire_all()

    assert all("True -> False" in line for line in changed)
    assert not projects.get_interview_config(project_id, "published").with_audio
    assert projects.get_interview_config(project_id).with_audio


# ==================== Switching models when publishing ====================


def test_publishing_can_switch_test_only_models(db, speech_allowed):
    database, project_id, user_id = db
    configs = _configs(
        probing=TEST_ONLY_MODEL, guide=TEST_ONLY_MODEL, answering=TEST_ONLY_MODEL
    )
    database.projects.update_agent_configs(project_id, configs)
    revision = database.versions.status(project_id).revision

    database.versions.publish(project_id, user_id, None, switch_models_to=LOCAL)

    status = database.versions.status(project_id)
    assert status.test_only == {} and status.published_test_only == {}
    assert not status.has_unpublished_changes
    assert status.revision > revision
    draft = database.versions.get_draft(project_id).agent_configs
    assert draft.probing.model == draft.guide.model == LOCAL
    # Only runs in simulations: what the researcher chose stays.
    assert draft.answering.model == TEST_ONLY_MODEL


def test_switching_to_a_test_only_model_is_refused(db, speech_allowed):
    database, project_id, user_id = db
    database.projects.update_agent_configs(
        project_id, _configs(probing=TEST_ONLY_MODEL)
    )

    with pytest.raises(PublishError, match="test interviews only"):
        database.versions.publish(
            project_id, user_id, None, switch_models_to=TEST_ONLY_MODEL
        )


def test_switching_cannot_fix_speech(db, speech_test_only):
    database, project_id, user_id = db
    database.projects.update_interview_config(
        project_id, InterviewConfig(with_audio=True)
    )

    with pytest.raises(PublishError) as exc:
        database.versions.publish(project_id, user_id, None, switch_models_to=LOCAL)
    assert list(exc.value.test_only) == [SPEECH]


def test_switching_back_to_what_is_published_keeps_the_draft(db, speech_allowed):
    database, project_id, user_id = db
    database.versions.publish(project_id, user_id, None)
    database.projects.update_agent_configs(
        project_id, _configs(probing=TEST_ONLY_MODEL)
    )

    database.versions.publish(
        project_id, user_id, None, switch_models_to=lib_settings.llm.default_model
    )
    database.session.rollback()

    draft = database.versions.get_draft(project_id).agent_configs
    assert draft.probing.model == lib_settings.llm.default_model
