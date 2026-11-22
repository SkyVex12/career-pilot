"""Outgoing mail: the sign-in code (ARCHITECTURE.md, "Email sign-in").

Two senders behind one interface:
- `LogMailer` writes the message to the service log instead of sending it. It is the local
  stack's sender, and refuses to start anywhere else: a code in a log is a code anyone with
  the log can use.
- `SmtpMailer` submits the message to a mail provider over SMTP with TLS (STARTTLS on 587,
  implicit TLS on 465). Every provider takes SMTP, so changing provider is a change of
  settings. With COPILOT_ENV=dev or test it only talks to a mail catcher on this machine,
  so local runs and tests can never reach a real provider.

Nothing here logs a code or an address outside the local stack.
"""

import asyncio
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from typing import Protocol

from copilot_api.config import ApiSettings
from copilot_core.config import cloud_runtime

log = logging.getLogger(__name__)

LOCAL_ENVS = ("dev", "test")
LOCAL_SMTP_HOSTS = ("localhost", "127.0.0.1", "::1", "mailpit")
SMTP_TIMEOUT_S = 15


class Mailer(Protocol):
    async def send(self, to: str, subject: str, text: str) -> None:
        """Send one plain-text message; raises when it could not be handed over."""
        ...


class LogMailer:
    def __init__(self, settings: ApiSettings) -> None:
        markers = cloud_runtime(os.environ)
        if settings.env not in LOCAL_ENVS or markers:
            raise RuntimeError(
                "COPILOT_EMAIL_SENDER=log writes sign-in codes to the log: it needs "
                "COPILOT_ENV=dev or test on a machine of your own; set COPILOT_EMAIL_SENDER=smtp"
            )

    async def send(self, to: str, subject: str, text: str) -> None:
        log.info("mail to %s (not sent, COPILOT_EMAIL_SENDER=log): %s\n%s", to, subject, text)


class MemoryMailer:
    """Tests: what would have been sent."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.fail = False

    async def send(self, to: str, subject: str, text: str) -> None:
        if self.fail:
            raise OSError("the mail provider is down")
        self.sent.append((to, subject, text))


class SmtpMailer:
    def __init__(self, settings: ApiSettings) -> None:
        if not settings.smtp_host or not settings.email_from:
            raise RuntimeError(
                "COPILOT_EMAIL_SENDER=smtp needs COPILOT_SMTP_HOST and COPILOT_EMAIL_FROM"
            )
        if not parseaddr(settings.email_from)[1]:
            raise RuntimeError("COPILOT_EMAIL_FROM is not an address")
        self.local = settings.smtp_host.lower() in LOCAL_SMTP_HOSTS
        if settings.env in LOCAL_ENVS and not self.local:
            raise RuntimeError(
                f"COPILOT_ENV={settings.env} never sends through a mail provider "
                f"({settings.smtp_host}); use COPILOT_EMAIL_SENDER=log or a local mail catcher"
            )
        self.host = settings.smtp_host
        self.port = settings.smtp_port
        self.username = settings.smtp_username
        self.password = (
            settings.smtp_password.get_secret_value() if settings.smtp_password else None
        )
        self.sender = settings.email_from
        if not self.local and not (self.username and self.password):
            raise RuntimeError("COPILOT_SMTP_USERNAME and COPILOT_SMTP_PASSWORD must be set")

    def _send(self, message: EmailMessage) -> None:
        context = ssl.create_default_context()
        if self.port == 465:
            with smtplib.SMTP_SSL(
                self.host, self.port, timeout=SMTP_TIMEOUT_S, context=context
            ) as s:
                self._submit(s, message)
            return
        with smtplib.SMTP(self.host, self.port, timeout=SMTP_TIMEOUT_S) as s:
            if not self.local:  # a provider: never in the clear
                s.starttls(context=context)
            self._submit(s, message)

    def _submit(self, smtp: smtplib.SMTP, message: EmailMessage) -> None:
        if self.username and self.password:
            smtp.login(self.username, self.password)
        smtp.send_message(message)

    async def send(self, to: str, subject: str, text: str) -> None:
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = to
        message["Subject"] = subject
        message["Date"] = formatdate(localtime=False)
        message["Message-ID"] = make_msgid(domain=parseaddr(self.sender)[1].rsplit("@", 1)[-1])
        # Not a list mail, and nobody should answer it with an out-of-office note.
        message["Auto-Submitted"] = "auto-generated"
        message.set_content(text)
        await asyncio.to_thread(self._send, message)


def make_mailer(settings: ApiSettings) -> Mailer:
    return SmtpMailer(settings) if settings.email_sender == "smtp" else LogMailer(settings)


def sign_in_message(code: str, minutes: int) -> tuple[str, str]:
    """The sign-in mail: subject and plain text. The code is in the text only, so it does not
    show in a notification's subject line."""
    text = (
        f"Your CoRuby sign-in code is:\n\n    {code}\n\n"
        f"It works once and expires in {minutes} minutes.\n\n"
        "If you didn't ask for this code, ignore this email. Nobody can sign in without it.\n"
    )
    return "Your CoRuby sign-in code", text
