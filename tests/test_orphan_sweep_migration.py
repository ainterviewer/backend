"""Revision 4f8273d9e6f2 deletes only rows whose parent is really gone.

Its first version compared a self-referencing key against the subquery's own
row, so every child code whose parent was not itself counted as orphaned: on
staging it deleted 68 of 72 codes and with them nearly every coding.
"""

import uuid

from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.db.tables import Base, CodeTable, ProjectFolderTable, ProjectTable, UserTable
from app.db.triggers import install_triggers
from tests.test_testrun_migration_triggers import alembic_config


def test_the_sweep_keeps_children_of_existing_parents(tmp_path):
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        install_triggers(connection)

    with Session(engine) as session:
        user = UserTable(email="a@example.com", password="x", first_name="A")
        project = ProjectTable(
            title="p", folder=ProjectFolderTable(title="f"), owner=user
        )
        root = CodeTable(id=uuid.uuid4(), project=project)
        child = CodeTable(id=uuid.uuid4(), project=project, parent=root)
        grandchild = CodeTable(id=uuid.uuid4(), project=project, parent=child)
        orphan = CodeTable(id=uuid.uuid4(), project=project, parent_id=uuid.uuid4())
        session.add_all([user, project, root, child, grandchild, orphan])
        session.commit()
        kept = {root.id.hex, child.id.hex, grandchild.id.hex}

    command.stamp(alembic_config(url), "0fa4a271248f")
    command.upgrade(alembic_config(url, "upgrade"), "4f8273d9e6f2")

    with engine.connect() as connection:
        remaining = set(connection.execute(text("SELECT id FROM code")).scalars())
    assert remaining == kept
    engine.dispose()
