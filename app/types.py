from __future__ import annotations

from enum import StrEnum
from typing import NotRequired, TypedDict

from fastapi import WebSocket


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
