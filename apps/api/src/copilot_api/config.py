"""Copilot API configuration."""

from pydantic import Field

from copilot_core.config import DataSettings


class ApiSettings(DataSettings):
    database_url: str = Field(
        default="postgresql://app_api:app_api-local@localhost:5432/copilot",
        validation_alias="COPILOT_API_DATABASE_URL",
    )
    max_document_bytes: int = 10 * 1024 * 1024
    max_me_audio_bytes: int = 300 * 1024 * 1024  # four hours of 16 kHz mono PCM is ~460 MB
    upload_url_ttl_s: int = 900
