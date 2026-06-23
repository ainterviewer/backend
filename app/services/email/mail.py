import mimetypes
from email import encoders
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from functools import lru_cache
from pathlib import Path
from typing import overload

import aiosmtplib
import css_inline
from aiosmtplib import SMTPResponse
from html2text import html2text
from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape

from ...settings import app_settings

EmailAttachment = tuple[str, bytes]  # (filename, content)

# Content-ID for the email header image embedded inline (see base_email.jinja,
# which references it via `<img src="cid:email_header">`).
EMAIL_HEADER_CID = "email_header"
_ASSETS_DIR = Path(__file__).parents[2] / "assets"


@lru_cache(maxsize=1)
def _email_header_image() -> bytes:
    return (_ASSETS_DIR / "email_header.png").read_bytes()


def _inline_images_for(html_content: str | None) -> list[tuple[str, bytes]]:
    """Return (cid, content) pairs for images referenced inline in the HTML."""
    if html_content and f"cid:{EMAIL_HEADER_CID}" in html_content:
        return [(EMAIL_HEADER_CID, _email_header_image())]
    return []


email_templates = Environment(
    loader=PackageLoader(
        package_name="app.services.email",
        package_path="templates/email",
    ),
    autoescape=select_autoescape(),
    undefined=StrictUndefined,
)


@overload
async def send_email(
    recipients: str | list[str],
    subject: str,
    *,
    body: str,
    html_content: str | None = None,
    attachments: list[EmailAttachment] | None = None,
) -> tuple[dict[str, SMTPResponse], str]: ...


@overload
async def send_email(
    recipients: str | list[str],
    subject: str,
    *,
    body: str | None = None,
    html_content: str,
    attachments: list[EmailAttachment] | None = None,
) -> tuple[dict[str, SMTPResponse], str]: ...


async def send_email(
    recipients: str | list[str],
    subject: str,
    *,
    body: str | None = None,
    html_content: str | None = None,
    attachments: list[EmailAttachment] | None = None,
) -> tuple[dict[str, SMTPResponse], str]:
    message = _create_email_message(
        recipients, subject, body, html_content, attachments
    )

    return await aiosmtplib.send(
        message,
        hostname=app_settings.services.email.smtp_server,  # ty:ignore[unresolved-attribute]
        port=app_settings.services.email.smtp_port,  # ty:ignore[unresolved-attribute]
        username=app_settings.services.email.sender.email,  # ty:ignore[unresolved-attribute]
        password=app_settings.services.email.sender.password.get_secret_value(),  # ty:ignore[unresolved-attribute]
    )


def _create_email_message(
    recipients: str | list[str],
    subject: str,
    body: str | None = None,
    html_content: str | None = None,
    attachments: list[EmailAttachment] | None = None,
) -> MIMEMultipart:
    if isinstance(recipients, list):
        recipients = ", ".join(recipients)

    if body is None and html_content is None:
        raise ValueError("Either body or html_content must be provided.")

    alternative = MIMEMultipart("alternative")

    if body is not None:
        plain_text_message = MIMEText(body, "plain", "utf-8")
    else:
        assert html_content is not None
        plain_text_message = MIMEText(html2text(html_content), "plain", "utf-8")
    alternative.attach(plain_text_message)

    if html_content is not None:
        html_content = css_inline.inline(html_content)
        alternative.attach(MIMEText(html_content, "html", "utf-8"))

    # Embed any inline images (e.g. the header) as related CID parts so they
    # render without depending on a publicly reachable URL.
    inline_images = _inline_images_for(html_content)
    if inline_images:
        related = MIMEMultipart("related")
        related.attach(alternative)
        for cid, content in inline_images:
            image = MIMEImage(content)
            image.add_header("Content-ID", f"<{cid}>")
            image.add_header("Content-Disposition", "inline", filename=f"{cid}.png")
            related.attach(image)
        root = related
    else:
        root = alternative

    if attachments:
        message = MIMEMultipart("mixed")
        message.attach(root)
        for filename, content in attachments:
            mime_type, _ = mimetypes.guess_type(filename)
            maintype, subtype = (
                mime_type.split("/", 1)
                if mime_type
                else ("application", "octet-stream")
            )
            part = MIMEBase(maintype, subtype)
            part.set_payload(content)
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                "attachment",
                filename=filename,
            )
            message.attach(part)
    else:
        message = root

    message["From"] = app_settings.services.email.sender.email  # ty:ignore[unresolved-attribute]
    message["To"] = recipients
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain="ainterviewer.dk")

    return message
