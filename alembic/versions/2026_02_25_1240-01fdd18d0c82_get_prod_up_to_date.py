"""get prod up to date

Revision ID: 01fdd18d0c82
Revises:
Create Date: 2026-02-25 12:40:35.782850

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import sqlite

# revision identifiers, used by Alembic.
revision: str = "01fdd18d0c82"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Inspect current state to handle partial migration (SQLite DDL is not transactional)
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    def has_col(table, col):
        if table not in tables:
            return False
        return col in [c["name"] for c in insp.get_columns(table)]

    # --- Create new tables (skip if already exist from partial run) ---
    if "assistance_session" not in tables:
        op.create_table(
            "assistance_session",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("project_id", sa.Uuid(), nullable=False),
            sa.Column("user_id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["project_id"],
                ["project.id"],
                name=op.f("fk_assistance_session_project_id_project"),
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["user_id"],
                ["user.id"],
                name=op.f("fk_assistance_session_user_id_user"),
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_assistance_session")),
            sa.UniqueConstraint("id", name=op.f("uq_assistance_session_id")),
        )

    if "experiment_project" not in tables:
        op.create_table(
            "experiment_project",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("experiment_id", sa.Uuid(), nullable=False),
            sa.Column("project_id", sa.Uuid(), nullable=False),
            sa.Column("weight", sa.Float(), nullable=True),
            sa.Column("added_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["experiment_id"],
                ["experiment.id"],
                name=op.f("fk_experiment_project_experiment_id_experiment"),
            ),
            sa.ForeignKeyConstraint(
                ["project_id"],
                ["project.id"],
                name=op.f("fk_experiment_project_project_id_project"),
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_experiment_project")),
            sa.UniqueConstraint(
                "experiment_id", "project_id", name="uq_experiment_project"
            ),
            sa.UniqueConstraint("id", name=op.f("uq_experiment_project_id")),
        )

    if "assistance_message_chunk" not in tables:
        op.create_table(
            "assistance_message_chunk",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("session_id", sa.Uuid(), nullable=False),
            sa.Column("messages_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["session_id"],
                ["assistance_session.id"],
                name=op.f("fk_assistance_message_chunk_session_id_assistance_session"),
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_assistance_message_chunk")),
            sa.UniqueConstraint("id", name=op.f("uq_assistance_message_chunk_id")),
        )

    # NOTE: Removed op.drop_table('_sqliteai_vector') — autogenerate noise

    # --- Experiment: add user_id with data migration ---
    if not has_col("experiment", "user_id"):
        # Step 1: Add user_id as nullable (keep project_ids/weights for data migration)
        with op.batch_alter_table("experiment", schema=None) as batch_op:
            batch_op.add_column(sa.Column("user_id", sa.Uuid(), nullable=True))
            batch_op.create_foreign_key(
                batch_op.f("fk_experiment_user_id_user"), "user", ["user_id"], ["id"]
            )

        # Step 2: Populate user_id from project_ids JSON → project → folder → ADMIN collaborator
        op.execute(
            sa.text("""
            UPDATE experiment SET user_id = (
                SELECT c.user_id FROM project p
                JOIN collaborator c ON c.folder_id = p.folder_id
                WHERE (p.id = json_extract(experiment.project_ids, '$[0]')
                       OR p.id = replace(json_extract(experiment.project_ids, '$[0]'), '-', ''))
                AND c.role = 'ADMIN'
                LIMIT 1
            )
        """)
        )
        op.execute(
            sa.text("""
            UPDATE experiment SET user_id = (
                SELECT c.user_id FROM project p
                JOIN collaborator c ON c.folder_id = p.folder_id
                WHERE (p.id = json_extract(experiment.project_ids, '$[0]')
                       OR p.id = replace(json_extract(experiment.project_ids, '$[0]'), '-', ''))
                LIMIT 1
            ) WHERE user_id IS NULL
        """)
        )
        op.execute(
            sa.text("""
            UPDATE experiment SET user_id = (
                SELECT id FROM "user" LIMIT 1
            ) WHERE user_id IS NULL
        """)
        )

        # Step 3: Make user_id NOT NULL and drop old columns
        with op.batch_alter_table("experiment", schema=None) as batch_op:
            batch_op.alter_column("user_id", existing_type=sa.Uuid(), nullable=False)
            batch_op.drop_column("weights")
            batch_op.drop_column("project_ids")

    elif has_col("experiment", "project_ids"):
        # Partial state: user_id added but old columns not yet dropped
        op.execute(
            sa.text("""
            UPDATE experiment SET user_id = (
                SELECT c.user_id FROM project p
                JOIN collaborator c ON c.folder_id = p.folder_id
                WHERE (p.id = json_extract(experiment.project_ids, '$[0]')
                       OR p.id = replace(json_extract(experiment.project_ids, '$[0]'), '-', ''))
                AND c.role = 'ADMIN'
                LIMIT 1
            ) WHERE user_id IS NULL
        """)
        )
        op.execute(
            sa.text("""
            UPDATE experiment SET user_id = (
                SELECT id FROM "user" LIMIT 1
            ) WHERE user_id IS NULL
        """)
        )
        with op.batch_alter_table("experiment", schema=None) as batch_op:
            batch_op.alter_column("user_id", existing_type=sa.Uuid(), nullable=False)
            batch_op.drop_column("weights")
            batch_op.drop_column("project_ids")

    # --- Interview: update FK constraints (idempotent — drop+create in batch) ---
    with op.batch_alter_table("interview", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_interview_experiment_id_experiment"), type_="foreignkey"
        )
        batch_op.drop_constraint(
            batch_op.f("fk_interview_test_run_id_testrun"), type_="foreignkey"
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_interview_experiment_id_experiment"),
            "experiment",
            ["experiment_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_interview_test_run_id_testrun"),
            "testrun",
            ["test_run_id"],
            ["id"],
            ondelete="CASCADE",
        )

    # --- Interviewee: add cascade FKs (skip if already applied) ---
    interviewee_fks = {fk["name"] for fk in insp.get_foreign_keys("interviewee")}
    if "fk_interviewee_project_id_project" not in interviewee_fks:
        with op.batch_alter_table("interviewee", schema=None) as batch_op:
            batch_op.create_foreign_key(
                batch_op.f("fk_interviewee_project_id_project"),
                "project",
                ["project_id"],
                ["id"],
                ondelete="CASCADE",
            )
            batch_op.create_foreign_key(
                batch_op.f("fk_interviewee_interview_id_interview"),
                "interview",
                ["interview_id"],
                ["id"],
                ondelete="CASCADE",
            )

    # --- Invitation: add new columns (skip if already applied) ---
    if not has_col("invitation", "reuseable"):
        with op.batch_alter_table("invitation", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "reuseable",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text("0"),
                )
            )
            batch_op.add_column(
                sa.Column(
                    "user_scope",
                    sa.Enum("ADMIN", "USER", "DEMO", "GUEST", name="scope"),
                    nullable=False,
                    server_default="USER",
                )
            )
            batch_op.add_column(sa.Column("user_expires", sa.JSON(), nullable=True))
            batch_op.add_column(sa.Column("title", sa.String(), nullable=True))
            batch_op.alter_column("email", existing_type=sa.VARCHAR(), nullable=True)
            batch_op.alter_column(
                "expires_at", existing_type=sa.DATETIME(), nullable=True
            )

    # --- Message: add skipped_by_condition + update FKs (skip if already applied) ---
    if not has_col("message", "skipped_by_condition"):
        with op.batch_alter_table("message", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "skipped_by_condition",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text("0"),
                )
            )
            batch_op.drop_constraint(
                batch_op.f("fk_message_interview_id_interview"), type_="foreignkey"
            )
            batch_op.drop_constraint(
                batch_op.f("fk_message_project_id_project"), type_="foreignkey"
            )
            batch_op.create_foreign_key(
                batch_op.f("fk_message_interview_id_interview"),
                "interview",
                ["interview_id"],
                ["id"],
                ondelete="CASCADE",
            )
            batch_op.create_foreign_key(
                batch_op.f("fk_message_project_id_project"),
                "project",
                ["project_id"],
                ["id"],
                ondelete="CASCADE",
            )

    # --- Message annotation: update FK (idempotent — drop+create in batch) ---
    with op.batch_alter_table("message_annotation", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_message_annotation_message_id_message"), type_="foreignkey"
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_message_annotation_message_id_message"),
            "message",
            ["message_id"],
            ["id"],
            ondelete="CASCADE",
        )

    # --- Project: add owner_id with data migration ---
    if not has_col("project", "owner_id"):
        # Step 1: Add owner_id as nullable
        with op.batch_alter_table("project", schema=None) as batch_op:
            batch_op.add_column(sa.Column("owner_id", sa.Uuid(), nullable=True))
            batch_op.create_foreign_key(
                batch_op.f("fk_project_owner_id_user"),
                "user",
                ["owner_id"],
                ["id"],
                ondelete="CASCADE",
            )

        # Step 2: Populate from ADMIN collaborator on the project's folder
        op.execute(
            sa.text("""
            UPDATE project SET owner_id = (
                SELECT c.user_id FROM collaborator c
                WHERE c.folder_id = project.folder_id
                AND c.role = 'ADMIN'
                LIMIT 1
            )
        """)
        )
        op.execute(
            sa.text("""
            UPDATE project SET owner_id = (
                SELECT c.user_id FROM collaborator c
                WHERE c.folder_id = project.folder_id
                LIMIT 1
            ) WHERE owner_id IS NULL
        """)
        )
        op.execute(
            sa.text("""
            UPDATE project SET owner_id = (
                SELECT id FROM "user" LIMIT 1
            ) WHERE owner_id IS NULL
        """)
        )

        # Step 3: Make owner_id NOT NULL
        with op.batch_alter_table("project", schema=None) as batch_op:
            batch_op.alter_column("owner_id", existing_type=sa.Uuid(), nullable=False)
    else:
        # Column exists but may be nullable (partial run failed at NOT NULL step)
        project_cols = {c["name"]: c for c in insp.get_columns("project")}
        if project_cols["owner_id"].get("nullable", True):
            op.execute(
                sa.text("""
                UPDATE project SET owner_id = (
                    SELECT c.user_id FROM collaborator c
                    WHERE c.folder_id = project.folder_id
                    AND c.role = 'ADMIN'
                    LIMIT 1
                ) WHERE owner_id IS NULL
            """)
            )
            op.execute(
                sa.text("""
                UPDATE project SET owner_id = (
                    SELECT c.user_id FROM collaborator c
                    WHERE c.folder_id = project.folder_id
                    LIMIT 1
                ) WHERE owner_id IS NULL
            """)
            )
            op.execute(
                sa.text("""
                UPDATE project SET owner_id = (
                    SELECT id FROM "user" LIMIT 1
                ) WHERE owner_id IS NULL
            """)
            )
            with op.batch_alter_table("project", schema=None) as batch_op:
                batch_op.alter_column(
                    "owner_id", existing_type=sa.Uuid(), nullable=False
                )

    # --- Task: update FK constraints (idempotent — drop+create in batch) ---
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_task_project_id_project"), type_="foreignkey"
        )
        batch_op.drop_constraint(
            batch_op.f("fk_task_interview_id_interview"), type_="foreignkey"
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_task_project_id_project"),
            "project",
            ["project_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_task_interview_id_interview"),
            "interview",
            ["interview_id"],
            ["id"],
            ondelete="CASCADE",
        )

    # --- User: split name → first_name/last_name, convert invite_token ---
    if not has_col("user", "first_name"):
        # Step 1: Add first_name/last_name as nullable
        with op.batch_alter_table("user", schema=None) as batch_op:
            batch_op.add_column(sa.Column("first_name", sa.String(), nullable=True))
            batch_op.add_column(sa.Column("last_name", sa.String(), nullable=True))

        # Step 2: Split name on first space
        op.execute(
            sa.text("""
            UPDATE "user" SET
                first_name = CASE
                    WHEN instr(name, ' ') > 0 THEN substr(name, 1, instr(name, ' ') - 1)
                    ELSE name
                END,
                last_name = CASE
                    WHEN instr(name, ' ') > 0 THEN substr(name, instr(name, ' ') + 1)
                    ELSE NULL
                END
        """)
        )

        # Step 3: NULL non-UUID invite_tokens before type conversion
        op.execute(
            sa.text("""
            UPDATE "user" SET invite_token = NULL
            WHERE invite_token IS NOT NULL
            AND (length(invite_token) != 36
                 OR substr(invite_token, 9, 1) != '-'
                 OR substr(invite_token, 14, 1) != '-'
                 OR substr(invite_token, 19, 1) != '-'
                 OR substr(invite_token, 24, 1) != '-')
        """)
        )

        # Step 4: Make first_name NOT NULL, convert invite_token type, drop name
        with op.batch_alter_table("user", schema=None) as batch_op:
            batch_op.alter_column(
                "first_name", existing_type=sa.String(), nullable=False
            )
            batch_op.alter_column(
                "invite_token",
                existing_type=sa.VARCHAR(),
                type_=sa.Uuid(),
                existing_nullable=True,
            )
            batch_op.drop_column("name")

    elif has_col("user", "name"):
        # Partial: first_name exists but name not yet dropped
        op.execute(
            sa.text("""
            UPDATE "user" SET
                first_name = CASE
                    WHEN instr(name, ' ') > 0 THEN substr(name, 1, instr(name, ' ') - 1)
                    ELSE name
                END,
                last_name = CASE
                    WHEN instr(name, ' ') > 0 THEN substr(name, instr(name, ' ') + 1)
                    ELSE NULL
                END
            WHERE first_name IS NULL
        """)
        )
        op.execute(
            sa.text("""
            UPDATE "user" SET invite_token = NULL
            WHERE invite_token IS NOT NULL
            AND (length(invite_token) != 36
                 OR substr(invite_token, 9, 1) != '-'
                 OR substr(invite_token, 14, 1) != '-'
                 OR substr(invite_token, 19, 1) != '-'
                 OR substr(invite_token, 24, 1) != '-')
        """)
        )
        with op.batch_alter_table("user", schema=None) as batch_op:
            batch_op.alter_column(
                "first_name", existing_type=sa.String(), nullable=False
            )
            batch_op.alter_column(
                "invite_token",
                existing_type=sa.VARCHAR(),
                type_=sa.Uuid(),
                existing_nullable=True,
            )
            batch_op.drop_column("name")

    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###

    # --- User: recombine first_name/last_name → name ---
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.add_column(sa.Column("name", sa.VARCHAR(), nullable=True))
        batch_op.alter_column(
            "invite_token",
            existing_type=sa.Uuid(),
            type_=sa.VARCHAR(),
            existing_nullable=True,
        )

    op.execute(
        sa.text("""
        UPDATE "user" SET name = first_name || COALESCE(' ' || last_name, '')
    """)
    )

    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.alter_column("name", existing_type=sa.VARCHAR(), nullable=False)
        batch_op.drop_column("last_name")
        batch_op.drop_column("first_name")

    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_task_interview_id_interview"), type_="foreignkey"
        )
        batch_op.drop_constraint(
            batch_op.f("fk_task_project_id_project"), type_="foreignkey"
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_task_interview_id_interview"),
            "interview",
            ["interview_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_task_project_id_project"), "project", ["project_id"], ["id"]
        )

    with op.batch_alter_table("project", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_project_owner_id_user"), type_="foreignkey"
        )
        batch_op.drop_column("owner_id")

    with op.batch_alter_table("message_annotation", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_message_annotation_message_id_message"), type_="foreignkey"
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_message_annotation_message_id_message"),
            "message",
            ["message_id"],
            ["id"],
        )

    with op.batch_alter_table("message", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_message_project_id_project"), type_="foreignkey"
        )
        batch_op.drop_constraint(
            batch_op.f("fk_message_interview_id_interview"), type_="foreignkey"
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_message_project_id_project"),
            "project",
            ["project_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_message_interview_id_interview"),
            "interview",
            ["interview_id"],
            ["id"],
        )
        batch_op.drop_column("skipped_by_condition")

    with op.batch_alter_table("invitation", schema=None) as batch_op:
        batch_op.alter_column("expires_at", existing_type=sa.DATETIME(), nullable=False)
        batch_op.alter_column("email", existing_type=sa.VARCHAR(), nullable=False)
        batch_op.drop_column("title")
        batch_op.drop_column("user_expires")
        batch_op.drop_column("user_scope")
        batch_op.drop_column("reuseable")

    with op.batch_alter_table("interviewee", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_interviewee_interview_id_interview"), type_="foreignkey"
        )
        batch_op.drop_constraint(
            batch_op.f("fk_interviewee_project_id_project"), type_="foreignkey"
        )

    with op.batch_alter_table("interview", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_interview_test_run_id_testrun"), type_="foreignkey"
        )
        batch_op.drop_constraint(
            batch_op.f("fk_interview_experiment_id_experiment"), type_="foreignkey"
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_interview_test_run_id_testrun"),
            "testrun",
            ["test_run_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_interview_experiment_id_experiment"),
            "experiment",
            ["experiment_id"],
            ["id"],
        )

    with op.batch_alter_table("experiment", schema=None) as batch_op:
        batch_op.add_column(sa.Column("project_ids", sqlite.JSON(), nullable=False))
        batch_op.add_column(sa.Column("weights", sqlite.JSON(), nullable=True))
        batch_op.drop_constraint(
            batch_op.f("fk_experiment_user_id_user"), type_="foreignkey"
        )
        batch_op.drop_column("user_id")

    # NOTE: Removed _sqliteai_vector recreation (matching upgrade removal)
    op.drop_table("assistance_message_chunk")
    op.drop_table("experiment_project")
    op.drop_table("assistance_session")
    # ### end Alembic commands ###
