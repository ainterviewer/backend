from typing import Optional

from pydantic import UUID4, BaseModel, Field, model_validator

from ainterviewer.types import Feedback


class ServerUpdate(BaseModel):
    activate: set[str] = Field(default_factory=set)
    deactivate: set[str] = Field(default_factory=set)

    @model_validator(mode="after")
    def check_activations(self):
        if self.activate and self.deactivate:
            if self.activate - self.deactivate:
                raise ValueError(
                    "Cannot activate and deactivate the same server at the same time"
                )
        if self.activate is None and self.deactivate is None:
            raise ValueError("Either 'activate' or 'deactivate' must be provided")

        return self


class MessageFeedback(BaseModel):
    interview_id: UUID4
    project_id: UUID4
    message_id: int
    feedback: Optional[Feedback]


class Broadcast(BaseModel):
    message: str
