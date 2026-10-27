import copy
import datetime
import enum
import secrets
import sys
import uuid
from collections.abc import Sequence
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import fastapi.openapi.constants
import qrcode
from pydantic import UUID4, BaseModel, TypeAdapter
from pydantic_settings import BaseSettings
from qrcode.constants import ERROR_CORRECT_H
from qrcode.image.styledpil import StyledPilImage
from qrcode.image.styles.colormasks import (
    HorizontalGradiantColorMask,  # noqa: F401
    RadialGradiantColorMask,
    SolidFillColorMask,
)
from qrcode.image.styles.moduledrawers.pil import RoundedModuleDrawer
from typer import Context, Typer
from typing_extensions import TypedDict

from ainterviewer.interfaces import (
    OutgoingData,
    OutgoingHistoryMessage,
    OutgoingMessage,
)
from ainterviewer.interview_guides import InterviewGuide
from ainterviewer.lpm.types import CustomToken
from ainterviewer.settings import settings as lib_settings
from ainterviewer.types import MessageType

from .paths import APP_DIR
from .schemas.interviews import MessagePublic

cli = Typer(no_args_is_help=True)


@cli.command()
def generate_qr_img(
    payload: str,
    icon_path: Path | None = None,
    color_rgb: tuple[int, int, int] | None = None,
    file_path: Path | None = None,
    ctx: Context = None,  # ty: ignore[invalid-parameter-default]
) -> bytes:
    img_byte_array = BytesIO()

    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_H)
    qr.add_data(payload)

    if icon_path is None:
        icon_path = APP_DIR.resolve() / "assets" / "favicon.png"

    if color_rgb is None:
        color_mask = RadialGradiantColorMask(
            center_color=(28, 40, 38), edge_color=(25, 104, 88)
        )
        # HorizontalGradiantColorMask(
        #     left_color=(28, 40, 38), right_color=(25, 104, 88)
        # )
    else:
        color_mask = SolidFillColorMask(front_color=color_rgb)

    img = qr.make_image(
        image_factory=StyledPilImage,
        module_drawer=RoundedModuleDrawer(),
        color_mask=color_mask,
        embeded_image_path=icon_path,
    )
    img.save(img_byte_array, format="PNG")

    img_byte_array = img_byte_array.getvalue()

    if file_path:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        if file_path.suffix != ".png":
            file_path = file_path.with_suffix(".png")
        with open(file_path, "wb") as f:
            _ = f.write(img_byte_array)
    elif ctx:
        if not ctx.resilient_parsing:
            sys.stdout.buffer.write(img_byte_array)

    return img_byte_array


@cli.command()
def generate_random_filename() -> str:
    return str(uuid.uuid4())


def parse_url_query_params(url: str) -> dict[str, list[str]]:
    parsed_url = urlparse(url)
    return parse_qs(parsed_url.query)


def _can_skip(message: MessagePublic, interview_guide: InterviewGuide | None) -> bool:
    """Whether the guide lets the respondent skip the question `message` asks.

    Not stored on the message, so read off the interview's own guide, whose
    positions are the ones the message was asked at. Anything that is not a
    guide question (or a guide it cannot be found in) keeps the default.
    """
    if (
        interview_guide is None
        or message.section is None
        or message.main_question is None
    ):
        return True
    try:
        section = interview_guide.question_sections[message.section]
        return section.questions[message.main_question].can_skip
    except IndexError:
        return True


def replay_history(
    interview_history: list[MessagePublic],
    project_id: UUID4,
    interview_id: UUID4,
    interview_guide: InterviewGuide | None = None,
) -> tuple[list[OutgoingHistoryMessage | OutgoingData | OutgoingMessage], bool]:
    """Replays the messages from the history through the websocket. Returns
    True if the interview should continue or False if it has reached the end
    already.

    The last message, the one the interview is waiting on, goes out live, so it
    carries the question's `can_skip` from `interview_guide`."""
    # TODO: Send images from history as well
    # - Should this be moved to the AInterviewer instance?
    #   - Currently this function is used both by AI and Human chat.

    messages: list[OutgoingHistoryMessage | OutgoingData | OutgoingMessage] = []
    continue_from_history = True

    for message in interview_history[:-1]:
        # NOTE: Why did we skip these in the first place?
        # if message.content in CustomTokens:
        #     continue

        # An override answer was given in the intervention's modal, never in
        # the chat.
        if (
            message.skipped_by_condition
            or message.message_type == MessageType.SECURITY_OVERRIDE
        ):
            continue

        if message.image:
            message.image.encode(project_id)  # ty:ignore[unresolved-attribute]

        data = OutgoingHistoryMessage(
            content=message.content,
            role=message.role,
            message_id=message.message_id,
            feedback=message.feedback,
            image=message.image,
            security_intervention=message.security_intervention,
        )

        messages.append(data)

    if (last_message := interview_history[-1]).message_type == (
        MessageType.SECURITY_OVERRIDE
    ):
        pass
    elif last_message.role == "user":
        data = OutgoingHistoryMessage(
            content=last_message.content,
            role=last_message.role,
            message_id=last_message.message_id,
        )
        messages.append(data)
    else:
        if last_message.content == CustomToken.end_of_interview:
            data = OutgoingData(
                content=CustomToken.end_of_interview,
            )
            messages.append(data)
            continue_from_history = False
        elif last_message.content in CustomToken:
            pass
        else:
            if last_message.image:
                last_message.image.encode(project_id)  # ty:ignore[unresolved-attribute]

            data = OutgoingMessage(
                content=last_message.content,
                role=last_message.role,
                message_id=last_message.message_id,
                can_answer=last_message.can_answer,
                can_skip=_can_skip(last_message, interview_guide),
                feedback=last_message.feedback,
                image=last_message.image,
                survey_item=last_message.survey_item,
                security_intervention=last_message.security_intervention,
            )
            messages.append(data)

    return messages, continue_from_history


def extend_openapi_schema(
    openapi: dict[str, Any],
    models: Sequence[type[BaseModel] | type[enum.Enum] | type[TypedDict]],  # ty: ignore[invalid-type-form]
) -> dict[str, Any]:
    """Adds extra pydantic models, enums, or TypedDicts to the `openapi[\"components\"][\"schema\"]`"""
    openapi = copy.deepcopy(openapi)

    for extra_model in models:
        # One path for all three: pydantic writes an enum's description and a
        # TypedDict's fields the way FastAPI does for the same types on a route.
        extra_model_schema = TypeAdapter(extra_model).json_schema(
            ref_template=fastapi.openapi.constants.REF_TEMPLATE
        )
        extra_model_schema.setdefault("title", extra_model.__name__)

        # Hoist nested definitions to the components, where the main model's
        # `$ref`s point (e.g. "Feedback", "Image").
        for name, definition in extra_model_schema.pop("$defs", {}).items():
            _add_component(openapi, name, definition)

        _add_component(openapi, extra_model_schema["title"], extra_model_schema)

    return openapi


def _add_component(openapi: dict[str, Any], name: str, schema: dict[str, Any]) -> None:
    """Add `schema` to the components as `name`, refusing to replace a
    different one already there.

    FastAPI may already have the name -- a nested model a route also uses
    (`Image`, the survey items) -- and it can build a model in a request and a
    response variant where `model_json_schema()` builds only the request one.
    Replacing it silently would change that type in the generated client. The
    same model is kept as FastAPI wrote it, which differs only in leaving out
    `"default": null`.
    """
    schemas = openapi["components"]["schemas"]
    if name not in schemas:
        schemas[name] = schema
    elif _without_null_defaults(schemas[name]) != _without_null_defaults(schema):
        raise ValueError(
            f"Extra model schema {name!r} differs from the component FastAPI "
            "generated under that name"
        )


def _without_null_defaults(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {
            key: _without_null_defaults(value)
            for key, value in schema.items()
            if not (key == "default" and value is None)
        }
    if isinstance(schema, list):
        return [_without_null_defaults(item) for item in schema]
    return schema


def clean_schema(obj):
    """Remove defaults containing SecretStr masked values and add $schema."""

    _MASKED_SECRET = "**********"

    if isinstance(obj, dict):
        # Remove 'default' if it contains masked secret values
        if "default" in obj:
            default = obj["default"]
            if (
                isinstance(default, dict)
                and any(v == _MASKED_SECRET for v in default.values())
                or default == _MASKED_SECRET
            ):
                del obj["default"]
        return {k: clean_schema(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [clean_schema(i) for i in obj]
    return obj


def merge_config_schemas(models: list[type[BaseSettings]]) -> dict[str, Any]:
    """Merge JSON schemas from multiple Pydantic BaseSettings models.

    Deep-merges `properties`, `$defs`, and `required`. Overlapping keys in
    `properties` or `$defs` are combined using `allOf`.
    """
    merged: dict[str, Any] = {}

    for model in models:
        schema = clean_schema(model.model_json_schema())

        if not merged:
            merged = schema
            continue

        for key in ("properties", "$defs"):
            if key not in schema:
                continue
            merged.setdefault(key, {})
            for prop, value in schema[key].items():
                if prop in merged[key]:
                    merged[key][prop] = {"allOf": [merged[key][prop], value]}
                else:
                    merged[key][prop] = value

        if "required" in schema:
            existing = set(merged.get("required", []))
            merged["required"] = merged.get("required", []) + [
                r for r in schema["required"] if r not in existing
            ]

    return merged


def ensure_filename(filename: str | None, fallback_ext: str = ".bin") -> str:
    """
    Return the original filename if present, otherwise generate a random one.
    """
    if filename and filename.strip():
        return Path(filename).name  # strips any path components

    random_name = secrets.token_hex(16)
    return f"{random_name}{fallback_ext}"


if __name__ == "__main__":
    cli()


def as_aware(value: datetime.datetime) -> datetime.datetime:
    """Attach the app timezone to a naive datetime.

    Values read from datetime columns already come back aware (see
    `app.db.types.AppDateTime`), so for them this is a no-op. It is for naive
    values from elsewhere -- a model built by hand, an old row in a test --
    which mean app wall-clock time, like everything stored.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=lib_settings.tzinfo)
    return value
