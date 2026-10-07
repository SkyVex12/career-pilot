"""Settings shared by the gateway, the API and the workers.

Each service subclasses `CoreSettings` with its own fields. Values come from the environment
(prefix COPILOT_, except the providers' own variable names) or a local .env file.
"""

from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from copilot_core.pricing import DEFAULT_PRICES, DEFAULT_TIERS, ModelPrice, model_key


def split_list(value: object) -> object:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


class CoreSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="COPILOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # Environment. `dev` is the only one that may use the dev token verifier or the dev key
    # provider.
    env: Literal["dev", "test", "staging", "prod"] = "prod"

    # Auth (ARCHITECTURE.md, "Connection and auth" and "REST API").
    auth: Literal["dev", "cognito"] = "cognito"
    dev_token: SecretStr | None = None
    dev_user_id: str = "0192f3a9-55c2-7b10-a4e1-1c2d3e4f5a6b"
    dev_user_name: str = "Dev User"
    dev_user_email: str = "dev@copilot.local"
    cognito_user_pool_id: str | None = None  # us-east-1_AbC123
    cognito_client_ids: Annotated[list[str], NoDecode] = Field(default_factory=list[str])
    # The user pool's OAuth domain, for the userInfo endpoint when an access token carries
    # no email claim (first login only). https://auth.<domain>
    cognito_domain: str | None = None
    jwks_ttl_s: int = 3600

    # Claude. Providers in fallback order; the first gets one retry before the next.
    llm_providers: Annotated[list[Literal["bedrock", "anthropic"]], NoDecode] = Field(
        default_factory=lambda: ["bedrock", "anthropic"]
    )
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    aws_region: str = Field(default="us-east-1", validation_alias="AWS_REGION")
    bedrock_api_key: SecretStr | None = Field(
        default=None, validation_alias="AWS_BEARER_TOKEN_BEDROCK"
    )
    haiku_model: str = "claude-haiku-4-5"
    sonnet_model: str = "claude-sonnet-5-5"
    opus_model: str = "claude-opus-5-5"
    bedrock_haiku_model: str = "anthropic.claude-haiku-4-5"
    bedrock_sonnet_model: str = "anthropic.claude-sonnet-5-5"
    bedrock_opus_model: str = "anthropic.claude-opus-5-5"
    # Prices per million tokens, for answers.cost_usd (copilot_core.pricing). JSON; entries
    # given are merged over the defaults, e.g.
    # {"claude-haiku-4-5": {"input": 1, "cached": 0.1, "output": 5}}.
    model_prices: dict[str, ModelPrice] = Field(default_factory=lambda: dict(DEFAULT_PRICES))
    tier_price_multipliers: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_TIERS))

    # Embeddings for prepared-answer matching. "auto" uses Bedrock when Bedrock is a Claude
    # provider and turns matching off otherwise (the direct API has no embeddings).
    embedder: Literal["auto", "bedrock", "none"] = "auto"
    embed_model: str = "cohere.embed-v4:0"

    # Deepgram: streaming STT in the gateway, pre-recorded STT in the workers.
    deepgram_api_key: SecretStr | None = Field(default=None, validation_alias="DEEPGRAM_API_KEY")
    deepgram_model: str = "nova-3"

    # Valkey and the jobs queue.
    valkey_url: str = "redis://localhost:6379/0"
    valkey_cluster: bool = False
    sqs_endpoint: str | None = "http://localhost:9324"  # unset in AWS (empty string)
    sqs_queue_url: str = "http://localhost:9324/000000000000/jobs"

    @property
    def embeddings_on_bedrock(self) -> bool:
        if self.embedder == "auto":
            return "bedrock" in self.llm_providers
        return self.embedder == "bedrock"

    @property
    def cognito_issuer(self) -> str | None:
        if not self.cognito_user_pool_id:
            return None
        region = self.cognito_user_pool_id.split("_", 1)[0]
        return f"https://cognito-idp.{region}.amazonaws.com/{self.cognito_user_pool_id}"

    @field_validator("model_prices", mode="after")
    @classmethod
    def _merge_prices(cls, value: dict[str, ModelPrice]) -> dict[str, ModelPrice]:
        return {**DEFAULT_PRICES, **{model_key(k): v for k, v in value.items()}}

    @field_validator("tier_price_multipliers", mode="after")
    @classmethod
    def _merge_tiers(cls, value: dict[str, float]) -> dict[str, float]:
        return {**DEFAULT_TIERS, **value}

    @field_validator("llm_providers", "cognito_client_ids", mode="before")
    @classmethod
    def _comma_list(cls, value: object) -> object:
        return split_list(value)


class DataSettings(CoreSettings):
    """The API's and the workers' extra stores: Postgres, S3 and KMS."""

    # Each service connects as its own role (README, "Database roles"), from its own
    # variable: COPILOT_DATABASE_URL is the migration runner's superuser URL.
    database_url: str = "postgresql://app_api:app_api-local@localhost:5432/copilot"
    db_pool_min: int = 1
    db_pool_max: int = 10

    # S3. Locally MinIO; in AWS the endpoint is unset and the bucket comes from Terraform.
    s3_endpoint: str | None = "http://localhost:9000"
    s3_bucket: str = "copilot-uploads"
    s3_access_key: SecretStr | None = Field(default=None, validation_alias="COPILOT_S3_ACCESS_KEY")
    s3_secret_key: SecretStr | None = Field(default=None, validation_alias="COPILOT_S3_SECRET_KEY")

    # Per-user envelope encryption (ARCHITECTURE.md, "Security and privacy").
    # "kms" in AWS; "dev" is a local stand-in with a master key from dev_master_key.
    key_provider: Literal["kms", "dev"] = "kms"
    kms_key_id: str | None = None  # alias/copilot-user-data
    dev_master_key: SecretStr | None = None  # base64 of 32 bytes
    dek_cache_ttl_s: float = 300.0
