"""add owner_id to project table

Revision ID: 9736839a677f
Revises: 77c4a96153cb
Create Date: 2026-02-23 12:20:42.227224

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

import app.db.types


# revision identifiers, used by Alembic.
revision: str = '9736839a677f'
down_revision: Union[str, None] = '77c4a96153cb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Add column as nullable (skip if already exists from a previous failed run)
    columns = [row[1] for row in conn.execute(sa.text("PRAGMA table_info('project')"))]
    if 'owner_id' not in columns:
        conn.execute(sa.text("ALTER TABLE project ADD COLUMN owner_id CHAR(32)"))

    # 2. Backfill: set owner_id to a folder admin for each existing project
    conn.execute(sa.text("""
        UPDATE project
        SET owner_id = (
            SELECT c.user_id
            FROM collaborator c
            WHERE c.folder_id = project.folder_id
              AND UPPER(c.role) = 'ADMIN'
            LIMIT 1
        )
        WHERE owner_id IS NULL
    """))

    # 3. Recreate table with NOT NULL constraint and FK via batch mode
    with op.batch_alter_table('project', schema=None) as batch_op:
        batch_op.alter_column('owner_id', existing_type=sa.Uuid(), nullable=False)
        batch_op.create_foreign_key(batch_op.f('fk_project_owner_id_user'), 'user', ['owner_id'], ['id'], ondelete='CASCADE')


def downgrade() -> None:
    with op.batch_alter_table('project', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_project_owner_id_user'), type_='foreignkey')
        batch_op.drop_column('owner_id')
