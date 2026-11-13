from uuid import uuid4

from ainterviewer.interview_guides import GuideError
from app.types import ContentError


def test_a_guide_error_keeps_where_it_is():
    question_id = uuid4()
    error = ContentError.of(
        GuideError(message="m", fix="f", question_id=question_id, field="conditions")
    )

    assert error.model_dump(mode="json", exclude_none=True) == {
        "message": "m",
        "fix": "f",
        "question_id": str(question_id),
        "field": "conditions",
    }
