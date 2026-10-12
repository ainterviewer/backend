"""The touch-last_updated triggers stamp the same clock the app does.

They used to write `CURRENT_TIMESTAMP` -- UTC, whole seconds -- beside
app-written Copenhagen wall-clock time with microseconds, so a project bumped
by a trigger could show a `last_updated` hours before its `created_at`. They now
call `app_now()`, registered per connection by `register_trigger_functions`,
and revision 6b1d0e7a9c35 converts the values the old triggers left behind.
"""

from datetime import timedelta

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ainterviewer.types import TestType
from ainterviewer.utils import now
from app.db.tables import (
    Base,
    ProjectFolderTable,
    ProjectTable,
    TestRunTable,
    TestSetupTable,
    UserTable,
)
from app.db.triggers import install_triggers, register_trigger_functions
from tests.test_testrun_migration_triggers import alembic_config


def _engine(url: str = "sqlite://", *, register: bool = True):
    engine = create_engine(url)
    if register:
        register_trigger_functions(engine)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        install_triggers(connection)
    return engine


def _setup(session: Session) -> TestSetupTable:
    user = UserTable(email="a@example.com", password="x", first_name="A")
    project = ProjectTable(title="p", folder=ProjectFolderTable(title="f"), owner=user)
    setup = TestSetupTable(
        project=project, type=TestType.FIXED_ANSWERS, answering_model="m"
    )
    session.add_all([user, project, setup])
    session.commit()
    return setup


def _raw(session: Session, table: str, row_id) -> tuple[str, str]:
    created_at, last_updated = session.execute(
        text(f"SELECT created_at, last_updated FROM {table} WHERE id = :id"),
        {"id": row_id.hex},
    ).one()
    return created_at, last_updated


def test_a_trigger_bump_writes_the_app_clock():
    engine = _engine()
    with Session(engine) as session:
        setup = _setup(session)
        session.add(TestRunTable(test_setup=setup, n_interviews=1))
        session.commit()

        for table, row_id in (
            ("testsetup", setup.id),
            ("project", setup.project_id),
        ):
            created_at, last_updated = _raw(session, table, row_id)
            # Rendered the way SQLAlchemy renders a DateTime, so the two sort
            # correctly as strings -- and a trigger bump comes after creation.
            assert len(last_updated) == len(created_at) == 26
            assert last_updated >= created_at

        session.expire_all()
        bumped = session.get_one(ProjectTable, setup.project_id).last_updated
        assert bumped is not None
        assert abs(now().replace(tzinfo=None) - bumped) < timedelta(minutes=1)


def test_without_the_function_a_triggering_write_fails_loudly():
    engine = _engine(register=False)
    with (
        Session(engine) as session,
        pytest.raises(OperationalError, match="no such function: app_now"),
    ):
        _setup(session)


def test_the_migration_converts_only_trigger_written_values(tmp_path):
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    engine = _engine(url)
    with Session(engine) as session:
        setup = _setup(session)
        project_id, setup_id = setup.project_id.hex, setup.id.hex
    command.stamp(alembic_config(url), "4f8273d9e6f2")

    with engine.begin() as connection:
        # testsetup first: updating it bumps the project through a trigger.
        # Its old bump, truncated to the second, lands before its creation.
        connection.execute(
            text(
                "UPDATE testsetup SET created_at = '2026-07-06 15:38:10.359846', "
                "last_updated = '2026-07-06 13:38:10'"
            )
        )
        connection.execute(
            text(
                "UPDATE project SET created_at = '2026-01-15 12:00:00.123456', "
                "last_updated = '2026-06-29 11:22:23'"
            )
        )

    # The second run sees only app-shaped values and must leave them alone.
    for _ in range(2):
        command.upgrade(alembic_config(url, "upgrade"), "6b1d0e7a9c35")
        command.stamp(alembic_config(url), "4f8273d9e6f2")

    with engine.connect() as connection:
        project = connection.execute(
            text("SELECT last_updated FROM project WHERE id = :id"), {"id": project_id}
        ).scalar_one()
        test_setup = connection.execute(
            text("SELECT last_updated FROM testsetup WHERE id = :id"), {"id": setup_id}
        ).scalar_one()

    # UTC 11:22 in summer is 13:22 in Copenhagen.
    assert project == "2026-06-29 13:22:23.000000"
    # 13:38:10 is 15:38:10 local, a fraction before creation: clamped to it.
    assert test_setup == "2026-07-06 15:38:10.359846"
    engine.dispose()
