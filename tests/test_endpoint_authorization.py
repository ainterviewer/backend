"""Every project-scoped analysis and review endpoint checks the caller's role.

A scope check says the caller is a signed-in user; it says nothing about *which*
projects are theirs. Since `project_id` comes off the URL, an endpoint carrying
only a scope check hands any account the analysis of any project it can name --
which is what `/report/projects/{project_id}/item-distributions` did until this
test was written.

The routers are read rather than called: since FastAPI 0.14x `include_router`
keeps sub-routers lazy, so the leaf modules are the one place every route of the
analysis API is visible at once. See the note in `app/api/main.py`.
"""

import pytest
from fastapi.routing import APIRoute

from app.api.dashboard import reports
from app.api.dashboard.analysis import (
    codes,
    comments,
    embeddings,
    monitoring,
    report,
)
from app.dependencies import ResourceRoleChecker

# `reports` is not an analysis module, but its routes are project-scoped and
# act on ids that arrive in a request body, which is exactly the shape these
# two tests exist to guard.
MODULES = [codes, comments, embeddings, monitoring, report, reports]


def project_routes():
    """Every route whose path names a project."""
    for module in MODULES:
        for route in module.router.routes:
            if isinstance(route, APIRoute) and "{project_id}" in route.path:
                yield pytest.param(
                    route, id=f"{min(route.methods or {'?'})} {route.path}"
                )


def checks_role(dependant) -> bool:
    """Whether a `ResourceRoleChecker` runs anywhere in the dependency tree."""
    return any(
        isinstance(dependency.call, ResourceRoleChecker) or checks_role(dependency)
        for dependency in dependant.dependencies
    )


@pytest.mark.parametrize("route", list(project_routes()))
def test_a_project_route_checks_the_caller_s_role_on_that_project(route):
    assert checks_role(route.dependant), (
        f"{route.path} takes a project id from the URL without checking that "
        "the caller has a role on it"
    )


@pytest.mark.parametrize(
    "route",
    [
        pytest.param(route, id=f"{min(route.methods or {'?'})} {route.path}")
        for module in MODULES
        for route in module.router.routes
        if isinstance(route, APIRoute)
    ],
)
def test_every_analysis_route_names_the_project_it_acts_in(route):
    """A route keyed only by a message, coding, comment or code id has
    nothing to check a role against, which is how those went ungated. Naming
    the project in the path is what makes the check above possible -- and the
    repository then scopes its queries by it, so the id cannot come from
    somewhere else."""
    assert "{project_id}" in route.path


def test_there_are_project_routes_to_check():
    """The parametrization above passes vacuously if the routers stop being
    readable this way, which the lazy `include_router` makes a real risk."""
    assert len(list(project_routes())) > 10
