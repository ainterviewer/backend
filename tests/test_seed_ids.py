"""Development resource URLs survive reseeding; random IDs remain opt-in."""

from contextlib import nullcontext

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from app.db.cli import cli
from app.db.crud import InterviewDataBase
from app.db.seed.accounts import create as create_accounts
from app.db.seed.ids import seed_uuid, stable_ids
from app.db.seed.projects import create as create_projects
from app.db.tables import Base, ProjectFolderTable


def _seed(*, random_uuids=False):
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def enforce_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            db = InterviewDataBase(session)
            with nullcontext() if random_uuids else stable_ids(session):
                create_projects(db, session, create_accounts(db, session))
            ids = {
                table.name: set(session.scalars(select(table.c.id)))
                for table in Base.metadata.sorted_tables
            }
            # The listener must not affect subsequent application inserts.
            folder = ProjectFolderTable(title="Created after seeding")
            session.add(folder)
            session.commit()
            assert folder.id != seed_uuid("projectfolder", 3)
            return ids
    finally:
        engine.dispose()


def test_seed_resource_ids_are_stable():
    first = _seed()
    assert first == _seed()
    assert len(first["project"]) == 6
    assert first["interview"]
    assert first["project"] == {seed_uuid("project", n) for n in range(1, 7)}
    assert all(id.version == 4 for ids in first.values() for id in ids)


def test_random_seed_resource_ids_change():
    first, second = _seed(random_uuids=True), _seed(random_uuids=True)
    for table, ids in first.items():
        assert ids.isdisjoint(second[table])


def test_cli_random_uuids_flag(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.db.cli.reset_and_seed", lambda **kwargs: calls.append(kwargs)
    )
    runner = CliRunner()
    assert runner.invoke(cli, ["seed-dev", "--yes"]).exit_code == 0
    assert runner.invoke(cli, ["seed-dev", "--yes", "--random-uuids"]).exit_code == 0
    assert calls == [
        {"yes": True, "random_uuids": False},
        {"yes": True, "random_uuids": True},
    ]
