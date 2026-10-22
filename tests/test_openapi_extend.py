"""Extra models join the schema without replacing what FastAPI generated.

`extend_openapi_schema` adds the models no route references (the websocket
messages, tokens) together with the models they nest -- some of which routes
use too. It used to write over FastAPI's copies, and its own enum schema
dropped the description FastAPI's carried.
"""

from enum import StrEnum

import pytest
from pydantic import BaseModel

from app.main import app
from app.openapi import build_openapi_schema
from app.utils import extend_openapi_schema


class Colour(StrEnum):
    """A colour."""

    RED = "red"


class Swatch(BaseModel):
    colour: Colour
    note: str | None = None


def schema_with(**components):
    return {"paths": {}, "components": {"schemas": components}}


def test_an_enum_keeps_its_description():
    extended = extend_openapi_schema(schema_with(), [Colour])

    assert extended["components"]["schemas"]["Colour"]["description"] == "A colour."


def test_nested_models_are_hoisted():
    extended = extend_openapi_schema(schema_with(), [Swatch])

    assert set(extended["components"]["schemas"]) == {"Swatch", "Colour"}


def test_fastapis_copy_is_kept_when_only_null_defaults_differ():
    """FastAPI leaves out `"default": null`; pydantic writes it."""
    fastapi_copy = {
        "properties": {
            "colour": {"$ref": "#/components/schemas/Colour"},
            "note": {"anyOf": [{"type": "string"}, {"type": "null"}], "title": "Note"},
        },
        "required": ["colour"],
        "title": "Swatch",
        "type": "object",
    }

    extended = extend_openapi_schema(schema_with(Swatch=fastapi_copy), [Swatch])

    assert extended["components"]["schemas"]["Swatch"] == fastapi_copy


def test_a_different_model_of_the_same_name_is_refused():
    other = {"properties": {"hue": {"type": "integer"}}, "title": "Swatch"}

    with pytest.raises(ValueError, match="Swatch"):
        extend_openapi_schema(schema_with(Swatch=other), [Swatch])


def test_the_real_schema_builds():
    assert "ApiError" in build_openapi_schema(app)["components"]["schemas"]
