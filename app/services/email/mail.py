import mimetypes
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from typing import overload

import aiosmtplib
import css_inline
from aiosmtplib import SMTPResponse
from html2text import html2text
from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape

from ...settings import app_settings

EmailAttachment = tuple[str, bytes]  # (filename, content)

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

    if attachments:
        message = MIMEMultipart("mixed")
        message.attach(alternative)
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
        message = alternative

    message["From"] = app_settings.services.email.sender.email  # ty:ignore[unresolved-attribute]
    message["To"] = recipients
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain="ainterviewer.dk")

    return message
