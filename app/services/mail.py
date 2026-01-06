from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from typing import overload

import aiosmtplib
import css_inline
from aiosmtplib import SMTPResponse
from html2text import html2text
from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape

from ..settings import app_settings

email_templates = Environment(
    loader=PackageLoader("app.services"),
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
) -> tuple[dict[str, SMTPResponse], str]: ...


@overload
async def send_email(
    recipients: str | list[str],
    subject: str,
    *,
    body: str | None = None,
    html_content: str,
) -> tuple[dict[str, SMTPResponse], str]: ...


async def send_email(
    recipients: str | list[str],
    subject: str,
    *,
    body: str | None = None,
    html_content: str | None = None,
) -> tuple[dict[str, SMTPResponse], str]:
    message = _create_email_message(recipients, subject, body, html_content)

    return await aiosmtplib.send(
        message,
        hostname=app_settings.services.email.smtp_server,
        port=app_settings.services.email.smtp_port,
        username=app_settings.services.email.sender.email,
        password=app_settings.services.email.sender.password.get_secret_value(),
    )


def _create_email_message(
    recipients: str | list[str],
    subject: str,
    body: str | None = None,
    html_content: str | None = None,
) -> MIMEMultipart:
    if isinstance(recipients, list):
        recipients = ", ".join(recipients)

    message = MIMEMultipart("alternative")
    message["From"] = app_settings.services.email.sender.email
    message["To"] = recipients
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain="ainterviewer.dk")

    if body is not None:
        plain_text_message = MIMEText(body, "plain", "utf-8")
    elif html_content is not None:
        plain_text_message = MIMEText(html2text(html_content), "plain", "utf-8")
    else:
        raise ValueError("Either body or html_content must be provided.")

    message.attach(plain_text_message)

    if html_content is not None:
        html_content = css_inline.inline(html_content)
        html_message = MIMEText(html_content, "html", "utf-8")
        message.attach(html_message)

    return message
