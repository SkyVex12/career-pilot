"""Copilot API configuration."""

from typing import Literal

from pydantic import Field, SecretStr

from copilot_core.config import DataSettings


class ApiSettings(DataSettings):
    database_url: str = Field(
        default="postgresql://app_api:app_api-local@localhost:5432/copilot",
        validation_alias="COPILOT_API_DATABASE_URL",
    )
    max_document_bytes: int = 10 * 1024 * 1024
    upload_url_ttl_s: int = 900

    # Email sign-in (ARCHITECTURE.md, "Email sign-in"; on with COPILOT_EMAIL_SIGN_IN). The code:
    # how long it lives, how many wrong tries kill it, and how long the app waits before it
    # offers to send another.
    email_code_ttl_s: int = 600
    email_code_max_tries: int = 5
    email_resend_after_s: int = 60
    # The access token (a JWT, short) and the refresh token (idle time until it expires; every
    # refresh replaces it).
    email_access_ttl_s: int = 3600
    email_refresh_ttl_days: int = 60
    # Who sends the code. "log" writes the mail to the API's log instead: the local stack and
    # tests only (copilot_api.mailer refuses it elsewhere). "smtp" submits it to a mail
    # provider; the password comes from the secret store.
    email_sender: Literal["log", "smtp"] = "log"
    email_from: str | None = None  # Copilot <sign-in@example.com>
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
