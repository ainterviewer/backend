"""change conditions action and negated

Revision ID: 3959c384af31
Revises: 03ea14d07326
Create Date: 2026-03-27 11:57:12.222513

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

import app.db.types


# revision identifiers, used by Alembic.
revision: str = '3959c384af31'
down_revision: Union[str, None] = '03ea14d07326'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def process_guide(guide_str: str, downgrade: bool = False) -> str:
    import json
    if not guide_str:
        return guide_str
    
    try:
        if isinstance(guide_str, str):
            guide = json.loads(guide_str)
        elif isinstance(guide_str, dict):
            guide = guide_str
        else:
            return guide_str
    except (json.JSONDecodeError, TypeError):
        return guide_str

    changed = False
    sections = guide.get("question_sections", [])
    if isinstance(sections, list):
        for section in sections:
            questions = section.get("questions", [])
            if isinstance(questions, list):
                for q in questions:
                    conditions_obj = q.get("conditions")
                    if conditions_obj and isinstance(conditions_obj, dict):
                        action = conditions_obj.get("action")
                        
                        # We handle lowercase and uppercase just in case
                        if not downgrade:
                            if action in ("ask_question", "ASK_QUESTION"):
                                conditions_obj["action"] = "SKIP_QUESTION" if action == "ASK_QUESTION" else "skip_question"
                                changed = True
                        else:
                            if action in ("skip_question", "SKIP_QUESTION"):
                                conditions_obj["action"] = "ASK_QUESTION" if action == "SKIP_QUESTION" else "ask_question"
                                changed = True
                        
                        conds = conditions_obj.get("conditions", [])
                        if isinstance(conds, list):
                            for cond in conds:
                                if isinstance(cond, dict) and "negated" in cond:
                                    cond["negated"] = not cond["negated"]
                                    changed = True

    if changed:
        return json.dumps(guide) if isinstance(guide_str, str) else guide
    return guide_str

def process_table(table_name: str, column_name: str, downgrade: bool = False):
    import sqlalchemy as sa
    conn = op.get_bind()
    
    query = sa.text(f"SELECT id, {column_name} FROM {table_name}")
    results = conn.execute(query).fetchall()
    
    for row_id, guide_data in results:
        if guide_data is not None:
            updated_guide = process_guide(guide_data, downgrade)
            if updated_guide != guide_data:
                update_query = sa.text(f"UPDATE {table_name} SET {column_name} = :guide WHERE id = :id")
                conn.execute(update_query, {"guide": updated_guide, "id": row_id})

def upgrade() -> None:
    process_table("projectlocalization", "interview_guide")
    process_table("interview", "interview_guide")

def downgrade() -> None:
    process_table("projectlocalization", "interview_guide", downgrade=True)
    process_table("interview", "interview_guide", downgrade=True)
