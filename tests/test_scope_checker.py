"""The 403 a scope check answers.

A demo account refused a user-scoped route gets code `demo_restricted`,
which the dashboard uses to leave a section demo accounts may not use in
its loading state behind the restriction overlay. Any other refusal keeps
the plain `Insufficient permissions` with no code.
"""

import uuid

import pytest
from fastapi import HTTPException

from app.auth import AuthToken
from app.dependencies import ScopeChecker
from app.errors import AppError
from app.types import ErrorCode, Scope


def check(required: Scope, scope: Scope) -> AuthToken:
    token = AuthToken(user_id=uuid.uuid4(), scope=scope)
    return ScopeChecker(required)(auth_token=token)


def test_a_demo_account_on_a_user_route_is_demo_restricted():
    with pytest.raises(AppError) as refused:
        check(Scope.USER, Scope.DEMO)

    assert refused.value.status == 403
    assert refused.value.code == ErrorCode.DEMO_RESTRICTED


def test_a_user_on_an_admin_route_gets_the_plain_refusal():
    with pytest.raises(HTTPException) as refused:
        check(Scope.ADMIN, Scope.USER)

    assert refused.value.status_code == 403
    assert refused.value.detail == "Insufficient permissions"


def test_a_demo_account_on_a_demo_route_passes():
    assert check(Scope.DEMO, Scope.DEMO).scope == Scope.DEMO
