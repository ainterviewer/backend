from __future__ import annotations

from enum import StrEnum
from typing import NotRequired, TypedDict

from fastapi import WebSocket


class Scope(StrEnum):
    ADMIN = "admin"
    USER = "user"
    GUEST = "guest"

    def includes(self, other: Scope) -> bool:
        """Check if this scope includes the permissions of another scope."""
        return other in _SCOPE_HIERARCHY[self]


_SCOPE_HIERARCHY: dict[Scope, set[Scope]] = {
    Scope.ADMIN: {Scope.ADMIN, Scope.USER, Scope.GUEST},
    Scope.USER: {Scope.USER, Scope.GUEST},
    Scope.GUEST: {Scope.GUEST},
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
