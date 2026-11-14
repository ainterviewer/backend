from typing import Literal

from pydantic import BaseModel, model_validator

from ainterviewer.interview_guides import InterviewGuide, Question, QuestionSection
from ainterviewer.types import LanguageCode


class AssistanceDependencies(BaseModel):
    guide: InterviewGuide
    user_name: str
    # The language of the guide the user is editing, which generated items use.
    language: LanguageCode


class ChatMessage(BaseModel):
    """Format of messages sent to the browser."""

    type: Literal["message", "question", "section"] = "message"
    role: Literal["user", "model"]
    timestamp: str
    content: str

    @model_validator(mode="after")
    def validate_type(self):
        match self.type:
            case "question":
                Question.model_validate_json(self.content)
            case "section":
                QuestionSection.model_validate_json(self.content)
            case "message":
                pass

        return self
