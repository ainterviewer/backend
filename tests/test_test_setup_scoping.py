"""Test-setup routes only act on setups of the project in their path.

The role check on these routes is against `{project_id}`, but the repository
methods behind them took `test_id` alone -- so an editor of one project could
read, change, run or delete another project's test setup by naming its id.
`_require_test_in_project` closes that; these tests keep every `{test_id}`
route behind it, the way `test_endpoint_authorization.py` keeps the analysis
routes behind a role check.
"""

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ainterviewer.types import TestType
from app.api.dashboard import synthesize
from app.db.crud import InterviewDataBase
from app.db.tables import (
    Base,
    ProjectFolderTable,
    ProjectTable,
    TestSetupTable,
    UserTable,
)
from app.dependencies import ResourceRoleChecker


def _routes(predicate):
    return [
        pytest.param(route, id=f"{min(route.methods or {'?'})} {route.path}")
        for route in synthesize.router.routes
        if isinstance(route, APIRoute) and predicate(route.path)
    ]


def _runs(dependant, check) -> bool:
    return any(
        check(dependency.call) or _runs(dependency, check)
        for dependency in dependant.dependencies
    )


TEST_ROUTES = _routes(lambda path: "{test_id}" in path)


@pytest.mark.parametrize("route", TEST_ROUTES)
def test_a_test_route_checks_the_setup_belongs_to_the_project(route):
    assert _runs(
        route.dependant, lambda call: call is synthesize._require_test_in_project
    ), f"{route.path} acts on a test setup without checking it is in the project"


@pytest.mark.parametrize("route", _routes(lambda path: "{project_id}" in path))
def test_a_project_route_checks_the_caller_s_role(route):
    assert _runs(route.dependant, lambda call: isinstance(call, ResourceRoleChecker)), (
        f"{route.path} takes a project id without checking the caller's role on it"
    )


def test_there_are_test_routes_to_check():
    """Guards the parametrization above against passing vacuously."""
    assert len(TEST_ROUTES) >= 9


def test_test_in_project_is_scoped():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        user = UserTable(email="a@example.com", password="x", first_name="A")
        mine = ProjectTable(
            title="mine", folder=ProjectFolderTable(title="f"), owner=user
        )
        theirs = ProjectTable(
            title="theirs", folder=ProjectFolderTable(title="g"), owner=user
        )
        setup = TestSetupTable(
            project=theirs, type=TestType.FIXED_ANSWERS, answering_model="m"
        )
        session.add_all([user, mine, theirs, setup])
        session.commit()

        tests = InterviewDataBase(session).tests
        assert tests.test_in_project(setup.id, theirs.id)
        assert not tests.test_in_project(setup.id, mine.id)
