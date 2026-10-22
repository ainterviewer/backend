"""No schema is named like a type the client generates for an operation.

hey-api names an operation's types after its id -- `upload_participants`
gets `UploadParticipantsData`, `...Response`, `...Errors` and so on -- and a
schema of the same name is renamed with a numeric suffix
(`UploadParticipantsResponse2`), which then leaks into every import.
"""

from app.main import app
from app.openapi import build_openapi_schema

SUFFIXES = ("Data", "Error", "Errors", "Response", "Responses")


def pascal(operation_id: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in operation_id.split("_"))


def test_no_schema_shadows_an_operation_type():
    schema = build_openapi_schema(app)
    generated = {
        pascal(operation["operationId"]) + suffix
        for operations in schema["paths"].values()
        for operation in operations.values()
        if "operationId" in operation
        for suffix in SUFFIXES
    }

    assert sorted(generated & schema["components"]["schemas"].keys()) == []
