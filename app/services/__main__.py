import asyncio
import webbrowser
from tempfile import NamedTemporaryFile

import css_inline
from typer import Typer

from ..settings import app_settings
from .mail import email_templates, send_email

cli = Typer()


@cli.command()
def show_test_email():
    html_content = email_templates.get_template("dummy.jinja").render(
        recipient_name="Tobias Gårdshus",
        invite_link="https://ainterviewer.dk/invite/12345",
    )
    html_content = css_inline.inline(html_content)

    with NamedTemporaryFile("w", encoding="utf-8", delete=False) as f:
        f.write(html_content)

    webbrowser.open(f.name)


@cli.command()
def send_test_email():
    # Initialize the email sender with settings
    print("Email sender initialized with settings:", app_settings.services.email)

    # Send a simple email
    result = asyncio.run(
        send_email(
            recipients=[
                "tobias_gaardhus@hotmail.com",
                # "jonas.raaschou@sodas.ku.dk",
                # "tpg@sodas.ku.dk",
            ],
            subject="Hello from AInterviewer",
            html_content=email_templates.get_template("dummy.jinja").render(
                recipient_name="Tobias Gårdshus",
                invite_link="https://ainterviewer.dk/invite/12345",
            ),
        )
    )

    print("Email sent successfully!" if result else "Failed to send email!")


if __name__ == "__main__":
    cli()
