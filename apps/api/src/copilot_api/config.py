"""Copilot API configuration."""

from typing import Literal

from pydantic import Field, SecretStr

from copilot_core.config import DataSettings
from copilot_core.languages import DEFAULT_PRACTICE_VOICES
from copilot_core.livekit import LiveKit


class ApiSettings(DataSettings):
    database_url: str = Field(
        default="postgresql://app_api:app_api-local@localhost:5432/copilot",
        validation_alias="COPILOT_API_DATABASE_URL",
    )
    # MCP-only OAuth AS. Empty issuer keeps OAuth discovery/authorization disabled.
    personal_ai_oauth_issuer: str | None = None
    personal_ai_oauth_clients: dict[str, list[str]] = Field(default_factory=dict)
    personal_ai_oauth_token_ttl_s: int = Field(default=1200, ge=60, le=3600)
    practice_tts_models: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_PRACTICE_VOICES)
    )
    ask_semantic_min_similarity: float = Field(default=0.55, ge=0.0, le=1.0)
    max_document_bytes: int = 10 * 1024 * 1024
    upload_url_ttl_s: int = 900
    interactive_concurrency: int = Field(default=2, ge=1, le=100)
    interactive_lease_s: int = Field(default=60, ge=15, le=600)
    interactive_runner_enabled: bool = True

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

    # Guest seats' voice (ARCHITECTURE.md, "Guest seats"): the LiveKit Cloud project. Without
    # all three, voice is off: it cannot be granted and no token is given; the rest of guest
    # seats works as before. The secret comes from the secret store.
    livekit_url: str | None = None  # wss://<project>.livekit.cloud
    livekit_api_key: str | None = None
    livekit_api_secret: SecretStr | None = None

    def livekit(self) -> "LiveKit | None":
        if not (self.livekit_url and self.livekit_api_key and self.livekit_api_secret):
            return None
        return LiveKit(
            self.livekit_url, self.livekit_api_key, self.livekit_api_secret.get_secret_value()
        )
