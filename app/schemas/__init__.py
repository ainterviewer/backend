"""The pydantic models the API speaks, one module per domain.

Requests, responses and the models repositories return all live here: a
repository hands back the same `*Public` model a route serves, so the two
layers share one definition rather than each keeping a copy. Nothing here
imports from `app.api` or `app.db.repositories`, which is what lets both import
from it.

Import from the domain module (`from app.schemas.users import UserPublic`);
there are no re-exports.
"""
