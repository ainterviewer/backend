"""Shared test setup that is not a fixture."""

from uuid import UUID

from app.db.crud import InterviewDataBase


def ask_first_question(database: InterviewDataBase, project_id: UUID) -> None:
    """Give a new project's one question text. It starts empty, for its
    editor to discover the question settings on, and neither a save nor a
    publish accepts an empty question."""
    guide = database.projects.get_guide(project_id)
    guide.question_sections[0].questions[0].main_question["EN"] = "Asked"
    database.projects.update_guide(project_id, guide)
