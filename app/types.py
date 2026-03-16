from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal, NotRequired, TypedDict

from fastapi import WebSocket
from pydantic import BaseModel, Field, model_validator


class Scope(StrEnum):
    ADMIN = "admin"
    USER = "user"
    DEMO = "demo"
    GUEST = "guest"

    def includes(self, other: Scope) -> bool:
        """Check if this scope includes the permissions of another scope."""
        return other in _SCOPE_HIERARCHY[self]


_SCOPE_HIERARCHY: dict[Scope, set[Scope]] = {
    Scope.ADMIN: {Scope.ADMIN, Scope.USER, Scope.DEMO, Scope.GUEST},
    Scope.USER: {Scope.USER, Scope.DEMO, Scope.GUEST},
    Scope.DEMO: {Scope.DEMO, Scope.GUEST},
    Scope.GUEST: {Scope.GUEST},
}


class CollaboratorRole(StrEnum):
    VIEWER = "viewer"
    ANNOTATOR = "annotator"
    EDITOR = "editor"
    ADMIN = "admin"

    def includes(self, other: CollaboratorRole) -> bool:
        """Check if this scope includes the permissions of another scope."""
        return other in _COLLABORATOR_HIERARCHY[self]


_COLLABORATOR_HIERARCHY: dict[CollaboratorRole, set[CollaboratorRole]] = {
    CollaboratorRole.ADMIN: {
        CollaboratorRole.ADMIN,
        CollaboratorRole.EDITOR,
        CollaboratorRole.ANNOTATOR,
        CollaboratorRole.VIEWER,
    },
    CollaboratorRole.EDITOR: {
        CollaboratorRole.EDITOR,
        CollaboratorRole.ANNOTATOR,
        CollaboratorRole.VIEWER,
    },
    CollaboratorRole.ANNOTATOR: {
        CollaboratorRole.ANNOTATOR,
        CollaboratorRole.VIEWER,
    },
    CollaboratorRole.VIEWER: {
        CollaboratorRole.VIEWER,
    },
}


class WebSocketUsers(TypedDict):
    user: NotRequired[WebSocket]
    interviewer: NotRequired[WebSocket]


class WebSocketConversation(TypedDict):
    message_count: int
    users: WebSocketUsers


class InterviewType(StrEnum):
    CHAT = "chat"
    INTERVIEW = "ai"
    SYNTHETIC = "synthetic"


class WSChatRole(StrEnum):
    USER = "user"
    INTERVIEWER = "interviewer"


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class TestRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ExternalParam(BaseModel):
    """Definition of a single external URL query parameter."""

    name: str = Field(
        ...,
        pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$",
        description="Query parameter name (valid identifier)",
    )
    # TODO: Allow lists?
    type: Literal["str", "int", "float", "bool", "enum"] = Field(
        ...,
        description="Value type for the parameter",
    )
    required: bool = True
    default: str | int | float | bool | None = None
    options: list[str] | None = Field(
        None,
        description="Allowed values (required when type='enum')",
    )
    description: str | None = Field(
        None,
        description="Human-readable description of the parameter's purpose",
    )

    @model_validator(mode="after")
    def validate_enum_options(self) -> ExternalParam:
        if self.type == "enum":
            if not self.options:
                raise ValueError("options must be non-empty when type is 'enum'")
            if self.default is not None and str(self.default) not in self.options:
                raise ValueError(
                    f"default '{self.default}' must be one of {self.options}"
                )
        return self


def build_external_params_model(
    params: list[ExternalParam],
) -> type[BaseModel]:
    """Build a dynamic Pydantic model from stored ExternalParam definitions.

    Used at runtime to validate incoming interview query parameters against the
    project's configured external params schema.
    """
    from enum import Enum as PyEnum

    from pydantic import create_model

    type_map: dict[str, type] = {
        "str": str,
        "int": int,
        "float": float,
        "bool": bool,
    }

    fields: dict[str, Any] = {}
    for param in params:
        if param.type == "enum":
            enum_cls = PyEnum(param.name, {v: v for v in param.options})  # type: ignore[arg-type]
            field_type = enum_cls
        else:
            field_type = type_map[param.type]

        if param.required and param.default is None:
            fields[param.name] = (field_type, ...)
        elif param.default is not None:
            fields[param.name] = (field_type, param.default)
        else:
            fields[param.name] = (field_type | None, None)

    return create_model("ExternalParams", **fields)
