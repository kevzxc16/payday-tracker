"""
SMTP email sender.

Stdlib-only: `smtplib` + `email.message`.

Behavior:
- Reads SMTP config from app.config.settings.
- If `SMTP_HOST` is empty (default), runs in *console mode*: prints the email
  to stdout instead of sending. This lets you develop the app without
  configuring SMTP and without losing visibility into what would have shipped.
- On real SMTP failure, raises the underlying exception so the notifications
  layer can record `status='failed'` with the error message.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from app.config import settings

log = logging.getLogger("payday_tracker.email")


def send_email(to: str, subject: str, body: str, *, html_body: str | None = None) -> None:
    """
    Send an email via SMTP. Console-mode if SMTP_HOST is empty.

    `html_body` is optional. If provided, the email becomes multipart with the
    plain text as fallback.
    """
    msg = EmailMessage()
    msg["From"] = _from_header()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    if not settings.SMTP_HOST:
        # Console mode — print the email and stop.
        log.info("EMAIL (console mode) → %s | %s", to, subject)
        print("---- EMAIL (console mode) ----")
        print(f"To:      {to}")
        print(f"Subject: {subject}")
        print(f"---\n{body}\n------------------------------")
        return

    if settings.SMTP_USE_TLS:
        context = ssl.create_default_context()
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            if settings.SMTP_USERNAME:
                smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30) as smtp:
            if settings.SMTP_USERNAME:
                smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            smtp.send_message(msg)
    log.info("Sent email to %s: %s", to, subject)


def _from_header() -> str:
    name = settings.SMTP_FROM_NAME or "Payday Tracker"
    email = settings.SMTP_FROM_EMAIL or settings.SMTP_USERNAME or "noreply@localhost"
    return f"{name} <{email}>"
