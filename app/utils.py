import copy
import enum
import sys
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import fastapi.openapi.constants
import qrcode
from pydantic import UUID4, BaseModel
from qrcode.constants import ERROR_CORRECT_H
from qrcode.image.styledpil import StyledPilImage
from qrcode.image.styles.colormasks import (
    HorizontalGradiantColorMask,  # noqa: F401
    RadialGradiantColorMask,
    SolidFillColorMask,  # noqa: F401
)
from qrcode.image.styles.moduledrawers.pil import RoundedModuleDrawer
from typer import Context, Typer

from ainterviewer.interfaces import (
    OutgoingData,
    OutgoingHistoryMessage,
    OutgoingMessage,
)
from ainterviewer.lpm.types import CustomTokens

from .db.models import MessagePublic
from .paths import APP_DIR

cli = Typer(no_args_is_help=True)


@cli.command()
def generate_qr_img(
    payload: str,
    file_path: Path = None,  # ty: ignore[invalid-parameter-default]
    ctx: Context = None,  # ty: ignore[invalid-parameter-default]
) -> bytes:
    img_byte_array = BytesIO()

    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_H)
    qr.add_data(payload)

    icon_path = APP_DIR.resolve() / "assets" / "favicon.png"

    img = qr.make_image(
        image_factory=StyledPilImage,
        module_drawer=RoundedModuleDrawer(),
        color_mask=RadialGradiantColorMask(
            center_color=(28, 40, 38), edge_color=(25, 104, 88)
        ),
        # HorizontalGradiantColorMask(
        #     left_color=(28, 40, 38), right_color=(25, 104, 88)
        # ),
        # SolidFillColorMask(front_color=(25, 104, 88)),
        embeded_image_path=icon_path,
    )
    img.save(img_byte_array, format="PNG")

    img_byte_array = img_byte_array.getvalue()

    if file_path:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        if not file_path.suffix == ".png":
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


def replay_history(
    interview_history: list[MessagePublic],
    project_id: UUID4,
    interview_id: UUID4,
) -> tuple[list[OutgoingHistoryMessage | OutgoingData | OutgoingMessage], bool]:
    """Replays the messages from the history through the websocket. Returns
    True if the interview should continue or False if it has reached the end
    already."""
    # TODO: Send images from history as well
    # - Should this be moved to the AInterviewer instance?
    #   - Currently this function is used both by AI and Human chat.

    messages: list[OutgoingHistoryMessage | OutgoingData | OutgoingMessage] = []
    continue_from_history = True

    for message in interview_history[:-1]:
        # NOTE: Why did we skip these in the first place?
        # if message.content in CustomTokens.all:
        #     continue

        if message.skipped_by_condition:
            continue

        if message.image:
            message.image.encode(project_id)

        data = OutgoingHistoryMessage(
            content=message.content,
            role=message.role,
            interview_id=interview_id,
            message_id=message.message_id,
            feedback=message.feedback,
            image=message.image,
            include_in_history=message.include_in_history,
        )

        messages.append(data)

    if (last_message := interview_history[-1]).role == "user":
        data = OutgoingHistoryMessage(
            content=last_message.content,
            role=last_message.role,
            interview_id=interview_id,
            message_id=last_message.message_id,
            include_in_history=last_message.include_in_history,
        )
        messages.append(data)
    else:
        if last_message.content == CustomTokens.end_of_interview:
            data = OutgoingData(
                content=CustomTokens.end_of_interview,
            )
            messages.append(data)
            continue_from_history = False
        elif last_message.content in CustomTokens.all:
            pass
        else:
            if last_message.image:
                last_message.image.encode(project_id)

            data = OutgoingMessage(
                content=last_message.content,
                role=last_message.role,
                interview_id=interview_id,
                message_id=last_message.message_id,
                feedback=last_message.feedback,
                image=last_message.image,
                include_in_history=last_message.include_in_history,
            )
            messages.append(data)

    return messages, continue_from_history


def extend_openapi_schema(
    openapi: dict[str, Any],
    models: list[type[BaseModel] | type[enum.Enum]],
) -> dict[str, Any]:
    """Adds extra pydantic models or enums to the `openapi[\"components\"][\"schema\"]`"""
    openapi = copy.deepcopy(openapi)

    for extra_model in models:
        # Handle Enum types (including StrEnum)
        if isinstance(extra_model, type) and issubclass(extra_model, enum.Enum):
            enum_schema = {
                "title": extra_model.__name__,
                "type": "string",
                "enum": [member.value for member in extra_model],
            }
            openapi["components"]["schemas"][extra_model.__name__] = enum_schema
            continue

        # Generate the JSON schema with the correct reference template
        # Use .model_json_schema() for Pydantic v2, or .schema() for v1
        if hasattr(extra_model, "model_json_schema"):
            extra_model_schema = extra_model.model_json_schema(
                ref_template=fastapi.openapi.constants.REF_TEMPLATE
            )
        else:
            extra_model_schema = extra_model.schema(
                ref_template=fastapi.openapi.constants.REF_TEMPLATE
            )

        # 1. Extract nested definitions ($defs in Pydantic v2, definitions in v1)
        # We pop them out so they don't remain inside the specific model schema
        definitions = extra_model_schema.pop("$defs", None) or extra_model_schema.pop(
            "definitions", {}
        )

        # 2. Hoist the definitions to the global openapi components
        # This makes the keys (e.g. "Feedback", "Image") available at #/components/schemas/
        if definitions:
            openapi["components"]["schemas"].update(definitions)

        # 3. Add the main model to schemas
        # Use the model's title as the key
        model_title = extra_model_schema["title"]
        openapi["components"]["schemas"][model_title] = extra_model_schema

    return openapi


if __name__ == "__main__":
    cli()
