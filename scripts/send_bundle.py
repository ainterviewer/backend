# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "jinja2>=3.1.6",
#     "rich>=15.0.0",
#     "typer>=0.25.1",
# ]
# ///
"""Self-contained CLI for sending participant emails from an exported bundle
via the KU Exchange SMTP gateway.

This file is intentionally standalone — it has no intra-project imports — so
it can be copied to any machine that can reach `exchange.ku.dk:587` (KU
network or VPN). Only third-party deps are `typer` and `jinja2`.

Bundle layout (zip), produced by `GET /api/projects/{id}/email-bundle`:
    participants.csv
    project.json            # project_id, project_title, base_url, default_language
    templates/<lang>.subject.txt
    templates/<lang>.html
    attachments/<lang>/<file>

Usage:
    python send_bundle.py send bundle.zip \\
        --user abc123@ku.dk --from-addr your.name@samf.ku.dk \\
        --status completed --skip-already-sent

KU SMTP settings (from KU IT guide):
    Host: exchange.ku.dk   Port: 587   Security: STARTTLS   Auth: KUnet password
"""

from __future__ import annotations

import csv
import getpass
import json
import os
import re
import smtplib
import tempfile
import zipfile
from datetime import datetime, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from typing import Annotated

import typer
from jinja2 import StrictUndefined, TemplateError, select_autoescape
from jinja2.sandbox import SandboxedEnvironment
from rich.progress import track

SMTP_HOST = "exchange.ku.dk"
SMTP_PORT = 587

SECTION_SEPARATOR = "\n<hr/>\n"
SUBJECT_SEPARATOR = " / "
SENT_LOG_NAME = "sent.csv"

cli = typer.Typer(
    help="Send participant emails from an exported bundle via KU Exchange.",
    no_args_is_help=True,
)


def ku_send_email(
    *,
    ku_username: str,
    password: str,
    from_addr: str,
    to_addrs: list[str],
    subject: str,
    body: str,
    body_html: str | None = None,
    attachments: list[str] | None = None,
    cc_addrs: list[str] | None = None,
    bcc_addrs: list[str] | None = None,
    from_name: str | None = None,
) -> None:
    """Send one email via KU Exchange (SMTP/STARTTLS).

    Requires the caller to be on KU network or VPN.
    """
    msg = MIMEMultipart("alternative") if body_html else MIMEMultipart()
    msg["From"] = formataddr((from_name, from_addr)) if from_name else from_addr
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)

    msg.attach(MIMEText(body, "plain", "utf-8"))
    if body_html:
        msg.attach(MIMEText(body_html, "html", "utf-8"))

    for path in attachments or []:
        if not os.path.isfile(path):
            typer.echo(f"  [!] Attachment not found, skipping: {path}")
            continue
        with open(path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f'attachment; filename="{os.path.basename(path)}"',
        )
        msg.attach(part)

    recipients = list(to_addrs) + list(cc_addrs or []) + list(bcc_addrs or [])
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(ku_username, password)
        smtp.sendmail(from_addr, recipients, msg.as_bytes())


def _render(template: str, context: dict[str, str]) -> str:
    env = SandboxedEnvironment(
        autoescape=select_autoescape(default_for_string=True),
        undefined=StrictUndefined,
    )
    return env.from_string(template).render(**context)


def _load_templates(extracted: Path) -> dict[str, tuple[str, str]]:
    """Return {lang: (subject, html)} for every language with an html template."""
    tmpl_dir = extracted / "templates"
    out: dict[str, tuple[str, str]] = {}
    if not tmpl_dir.exists():
        return out
    for html in sorted(tmpl_dir.glob("*.html")):
        lang = html.stem
        subj_file = tmpl_dir / f"{lang}.subject.txt"
        subject = subj_file.read_text("utf-8") if subj_file.exists() else ""
        out[lang] = (subject, html.read_text("utf-8"))
    return out


def _load_attachments(extracted: Path) -> dict[str, list[Path]]:
    """Return {lang: [attachment_paths]}."""
    base = extracted / "attachments"
    out: dict[str, list[Path]] = {}
    if not base.exists():
        return out
    for lang_dir in sorted(base.iterdir()):
        if lang_dir.is_dir():
            out[lang_dir.name] = sorted(p for p in lang_dir.iterdir() if p.is_file())
    return out


def _resolve_template(
    participant_lang: str | None,
    templates: dict[str, tuple[str, str]],
    default_language: str,
) -> tuple[str, str, list[str]] | None:
    """Mirror backend logic: pick per-language template, else combine all."""
    if participant_lang and participant_lang in templates:
        subj, tmpl = templates[participant_lang]
        if tmpl:
            return subj, tmpl, [participant_lang]

    ordered = sorted(
        templates.items(), key=lambda kv: (kv[0] != default_language, kv[0])
    )
    subjects = [s for (_, (s, t)) in ordered if t and s]
    bodies = [t for (_, (_, t)) in ordered if t]
    langs = [lang for (lang, (_, t)) in ordered if t]
    if not bodies:
        return None
    return SUBJECT_SEPARATOR.join(subjects), SECTION_SEPARATOR.join(bodies), langs


def _attachments_for(
    langs: list[str], attachments: dict[str, list[Path]]
) -> list[Path]:
    seen: dict[str, Path] = {}
    for lang in langs:
        for path in attachments.get(lang, []):
            seen.setdefault(path.name, path)
    return list(seen.values())


def _read_sent_log(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open(newline="", encoding="utf-8") as f:
        return {
            row["participant_id"]
            for row in csv.DictReader(f)
            if row.get("participant_id")
        }


def _append_sent_log(path: Path, participant_id: str, email: str, pid: str) -> None:
    new_file = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(["participant_id", "email", "pid", "sent_at"])
        writer.writerow(
            [participant_id, email, pid, datetime.now(timezone.utc).isoformat()]
        )


@cli.command()
def send(
    bundle: Annotated[Path, typer.Argument(help="Path to the email bundle .zip")],
    user: Annotated[
        str, typer.Option("--user", "-u", help="KU username, e.g. abc123@ku.dk")
    ],
    from_addr: Annotated[
        str | None,
        typer.Option("--from-addr", "-f", help="Your KU email address alias if any"),
    ] = None,
    from_name: Annotated[
        str | None,
        typer.Option("--from-name", "-n", help='Display name, e.g. "Your Name"'),
    ] = None,
    password: Annotated[
        str | None,
        typer.Option("--password", "-p", help="KUnet password (prompted if omitted)"),
    ] = None,
    status: Annotated[
        list[str] | None,
        typer.Option(
            "--status",
            help="Only send to participants whose latest_interview_status matches "
            "(repeatable). Values: active, inactive, completed, none.",
        ),
    ] = None,
    only_participating: Annotated[
        bool,
        typer.Option(
            "--only-participating/--include-non-participating",
            help="Restrict to participants with participating=true (default).",
        ),
    ] = True,
    lang: Annotated[
        list[str] | None,
        typer.Option(
            "--lang", help="Restrict to these participant languages (repeatable)."
        ),
    ] = None,
    pid: Annotated[
        list[str] | None,
        typer.Option("--pid", help="Restrict to these participant pids (repeatable)."),
    ] = None,
    email_filter: Annotated[
        list[str] | None,
        typer.Option("--email", help="Restrict to these email addresses (repeatable)."),
    ] = None,
    cc: Annotated[
        list[str] | None, typer.Option("--cc", help="CC recipient(s)")
    ] = None,
    bcc: Annotated[
        list[str] | None, typer.Option("--bcc", help="BCC recipient(s)")
    ] = None,
    skip_already_sent: Annotated[
        bool,
        typer.Option(
            "--skip-already-sent",
            help=f"Skip participants already recorded in {SENT_LOG_NAME} next to the bundle.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Show who would be emailed and a rendered preview, don't send.",
        ),
    ] = False,
    limit: Annotated[
        int | None, typer.Option("--limit", help="Cap the number of recipients sent.")
    ] = None,
):
    """Send participant emails from BUNDLE."""
    if not bundle.exists():
        raise typer.BadParameter(f"Bundle not found: {bundle}")

    sent_log = bundle.with_suffix(".sent.csv")
    already = _read_sent_log(sent_log) if skip_already_sent else set()

    with tempfile.TemporaryDirectory() as tmp:
        extracted = Path(tmp)
        with zipfile.ZipFile(bundle) as zf:
            zf.extractall(extracted)

        manifest_file = extracted / "project.json"
        if not manifest_file.exists():
            raise typer.BadParameter("Bundle is missing project.json")
        manifest = json.loads(manifest_file.read_text("utf-8"))
        base_url = manifest["base_url"].rstrip("/")
        project_id = manifest["project_id"]
        project_title = manifest["project_title"]
        default_language = manifest.get("default_language", "")

        templates = _load_templates(extracted)
        if not templates:
            raise typer.BadParameter("Bundle has no templates/<lang>.html files")
        attachments = _load_attachments(extracted)

        csv_file = extracted / "participants.csv"
        if not csv_file.exists():
            raise typer.BadParameter("Bundle is missing participants.csv")

        with csv_file.open(newline="", encoding="utf-8") as f:
            participants = list(csv.DictReader(f))

        status_set = {s.lower() for s in status} if status else None
        lang_set = set(lang) if lang else None
        pid_set = set(pid) if pid else None
        email_set = {e.lower() for e in email_filter} if email_filter else None

        filtered: list[dict[str, str]] = []
        for p in participants:
            if not p.get("email"):
                continue
            if only_participating and p.get("participating", "").lower() not in (
                "true",
                "1",
            ):
                continue
            if status_set is not None:
                p_status = (p.get("latest_interview_status") or "none").lower()
                if p_status not in status_set:
                    continue
            if lang_set is not None and (p.get("lang") or "") not in lang_set:
                continue
            if pid_set is not None and p.get("pid", "") not in pid_set:
                continue
            if email_set is not None and p.get("email", "").lower() not in email_set:
                continue
            if skip_already_sent and p.get("id", "") in already:
                continue
            filtered.append(p)

        if limit is not None:
            filtered = filtered[:limit]

        typer.echo(
            f"Project: {project_title} ({project_id})\n"
            f"Languages in bundle: {sorted(templates)}\n"
            f"Participants matching filters: {len(filtered)}"
        )
        if not filtered:
            raise typer.Exit(code=0)

        if dry_run:
            preview = filtered[0]
            resolved = _resolve_template(
                preview.get("lang") or None, templates, default_language
            )
            typer.echo("\n=== DRY RUN ===")
            for p in filtered:
                typer.echo(
                    f"  {p.get('email')}  pid={p.get('pid')}  lang={p.get('lang') or '-'}  "
                    f"status={p.get('latest_interview_status') or 'none'}"
                )
            if resolved:
                subj, tmpl, langs = resolved
                ctx = _build_context(preview, base_url, project_id, project_title)
                try:
                    rendered = _render(tmpl, ctx)
                except TemplateError as exc:
                    typer.echo(f"\n[!] Preview render failed: {exc}")
                    raise typer.Exit(code=1) from exc
                attached = _attachments_for(langs, attachments)
                typer.echo(f"\nPreview for {preview.get('email')}:")
                typer.echo(
                    f"  Subject: {_render(subj, ctx) if subj else '(no subject)'}"
                )
                typer.echo(f"  Languages used: {langs}")
                typer.echo(f"  Attachments: {[a.name for a in attached]}")
                typer.echo("  --- HTML (first 500 chars) ---")
                typer.echo(rendered[:500])
            return

        pwd = password or getpass.getpass(f"KUnet password for {user}: ")
        sender = from_addr or user

        sent_count = 0
        for p in track(filtered):
            resolved = _resolve_template(
                p.get("lang") or None, templates, default_language
            )
            if resolved is None:
                typer.echo(f"  [skip] {p.get('email')}: no template")
                continue
            subj_tmpl, body_tmpl, langs = resolved
            ctx = _build_context(p, base_url, project_id, project_title)
            try:
                subject = (
                    _render(subj_tmpl, ctx)
                    if subj_tmpl
                    else f"Invitation: {project_title}"
                )
                body_html = _render(body_tmpl, ctx)
            except TemplateError as exc:
                typer.echo(f"  [skip] {p.get('email')}: render failed: {exc}")
                continue

            attached = _attachments_for(langs, attachments)
            try:
                ku_send_email(
                    ku_username=user,
                    password=pwd,
                    from_addr=sender,
                    to_addrs=[p["email"]],
                    subject=subject,
                    body=_html_to_text(body_html),
                    body_html=body_html,
                    attachments=[str(a) for a in attached],
                    cc_addrs=cc,
                    bcc_addrs=bcc,
                    from_name=from_name,
                )
            except Exception as exc:  # noqa: BLE001
                typer.echo(f"  [fail] {p.get('email')}: {exc}")
                continue

            _append_sent_log(sent_log, p.get("id", ""), p["email"], p.get("pid", ""))
            sent_count += 1

        typer.echo(f"\nDone. Sent {sent_count}/{len(filtered)}. Log: {sent_log}")


def _build_context(
    p: dict[str, str], base_url: str, project_id: str, project_title: str
) -> dict[str, str]:
    pid = p.get("pid") or ""
    interview_url = f"{base_url}/interview?id={project_id}&pid={pid}"
    opt_out_url = (
        f"{base_url}/opt-out/{p['opt_out_token']}" if p.get("opt_out_token") else ""
    )
    return {
        "name": p.get("name") or "",
        "email": p.get("email") or "",
        "pid": pid,
        "interview_url": interview_url,
        "project_title": project_title,
        "opt_out_url": opt_out_url,
    }


def _html_to_text(html: str) -> str:
    """Crude HTML-to-text fallback for the plain part. Good enough for plaintext clients."""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


if __name__ == "__main__":
    cli()
