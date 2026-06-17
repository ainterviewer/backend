import base64
import json
import uuid
from typing import IO

import polars as pl
from xlsxwriter import Workbook

from .models import MessagePublic


def _json_serialize(x):
    if x is None:
        return None
    if hasattr(x, "to_list"):
        return json.dumps(x.to_list())
    return json.dumps(x)


def fix_nested_columns(df: pl.DataFrame) -> pl.DataFrame:
    """
    Converts nested columns of type Struct or List (ie. dicts or lists) to JSON strings.
    """

    stringified_cols = []

    for col_name, col_type in df.schema.items():
        # Check if the type is Struct or List
        if isinstance(col_type, pl.Struct):
            stringified_cols.append(
                pl.col(col_name).struct.json_encode().alias(col_name)
            )
        elif isinstance(col_type, pl.List):
            stringified_cols.append(
                pl.col(col_name)
                .map_elements(_json_serialize, return_dtype=pl.String)
                .alias(col_name)
            )

    # Apply the casting if any nested columns were found
    if stringified_cols:
        df = df.with_columns(stringified_cols)

    return df


def messages_to_dataframe(messages: list[MessagePublic]) -> pl.DataFrame:
    """Normalize interview messages into a flat DataFrame for export.

    Nested/optional fields (feedback, attachment, image, survey_item,
    annotations) are coerced to strings so the result can be written to
    csv/xlsx without struct columns.
    """
    rows = []

    for message in messages:
        d = json.loads(message.model_dump_json())

        if d.get("feedback") is None:
            d["feedback"] = ""

        if (attachment := d.get("attachment")) is not None:
            d["attachment"] = str(attachment)
        else:
            d["attachment"] = ""

        if (image := d.get("image")) is not None:
            d["image"] = json.dumps(image)
        else:
            d["image"] = ""

        if (survey_item := d.get("survey_item")) is not None:
            d["survey_item"] = json.dumps(survey_item)
        else:
            d["survey_item"] = ""

        if (annotations := d.get("annotations")) is not None:
            d["annotations"] = json.dumps(annotations)
        else:
            d["annotations"] = ""

        rows.append(d)

    return pl.from_dicts(rows)


def write_messages_xlsx(df: pl.DataFrame, target: "str | IO[bytes]") -> None:
    """Write a messages DataFrame to xlsx, one worksheet per interview.

    ``target`` may be a file path or a binary file-like object.
    """
    with Workbook(target) as workbook:
        text_format = workbook.add_format({"text_wrap": True})
        for interview_id, interview in df.group_by("interview_id", maintain_order=True):
            interview.write_excel(
                workbook,
                column_formats={"backend_content": text_format},
                column_widths={"backend_content": 400},
                worksheet=interview_id[0][:31],
            )


def uuid_to_urlid(uuid_: uuid.UUID) -> str:
    """Convert a UUID to a short URL-safe string.

    >>> import base64
    >>> import uuid
    >>> id_ = uuid.UUID('5d98d578-2731-4a4d-b666-70ca16f10aa2')
    >>> url_id = uuid_to_urlid(id_)
    >>> print(url_id)
    XZjVeCcxSk22ZnDKFvEKog
    """
    return base64.urlsafe_b64encode(uuid_.bytes).rstrip(b"=").decode("utf-8")


def urlid_to_uuid(url: str) -> uuid.UUID:
    """Convert a base64url encoded UUID string to a UUID.

    >>> import base64
    >>> import uuid
    >>> url_id = 'XZjVeCcxSk22ZnDKFvEKog'
    >>> id_ = urlid_to_uuid(url_id)
    >>> print(id_)
    5d98d578-2731-4a4d-b666-70ca16f10aa2
    """
    return uuid.UUID(bytes=base64.urlsafe_b64decode(url + "=" * (len(url) % 4)))
