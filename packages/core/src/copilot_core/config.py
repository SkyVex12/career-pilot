"""Settings shared by the gateway, the API and the workers.

Each service subclasses `CoreSettings` with its own fields. Values come from the environment
(prefix COPILOT_, except the providers' own variable names) or a local .env file.
"""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from copilot_core.pricing import DEFAULT_PRICES, DEFAULT_TIERS, ModelPrice, model_key

LLMProviderName = Literal["bedrock", "anthropic", "openai"]
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
# Model routes, one per job: the gateway's live jobs and the workers' background jobs.
OPENAI_JOBS = ("detector", "fast", "deep", "summary", "extract", "job", "pack", "postcall")
EMBED_DIMENSIONS = 1024  # memory_nodes.embedding is vector(1024)


# Set inside AWS compute (ECS on Fargate or EC2, Lambda).
AWS_RUNTIME_MARKERS = (
    "AWS_EXECUTION_ENV",
    "ECS_CONTAINER_METADATA_URI",
    "ECS_CONTAINER_METADATA_URI_V4",
    "AWS_LAMBDA_FUNCTION_NAME",
)


# Compute Engine names itself in the VM's DMI data, which containers can read; nothing in the
# environment says so (Stage 0 on GCP runs plain Docker containers on one VM).
GCE_DMI_PRODUCT = Path("/sys/class/dmi/id/product_name")


def aws_runtime(environ: Mapping[str, str]) -> list[str]:
    """The AWS compute markers present; empty off AWS."""
    return [m for m in AWS_RUNTIME_MARKERS if environ.get(m)]


def gce_runtime(product: Path = GCE_DMI_PRODUCT) -> list[str]:
    """["GCE"] on a Compute Engine VM (or in a container on one); empty elsewhere."""
    try:
        name = product.read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return []
    return ["GCE"] if name.startswith("Google Compute Engine") else []


def cloud_runtime(environ: Mapping[str, str]) -> list[str]:
    """Markers of cloud compute (AWS or Compute Engine); empty on a laptop or a CI runner."""
    return aws_runtime(environ) + gce_runtime()


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
    # cognito on AWS; google on GCP (Google ID tokens, for allow-listed emails only).
    auth: Literal["dev", "cognito", "google"] = "cognito"
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
    # Google sign-in (Stage 0 on GCP): the desktop OAuth client the app signs in with, and the
    # emails let in. Google treats an installed app's client secret as not confidential; it
    # lives in Secret Manager and reaches the app through GET /v1/auth/config.
    google_client_ids: Annotated[list[str], NoDecode] = Field(default_factory=list[str])
    google_client_secret: SecretStr | None = None
    google_allowed_emails: Annotated[list[str], NoDecode] = Field(default_factory=list[str])

    # Model providers in fallback order; the first gets one retry before the next. Claude
    # (bedrock, anthropic) is the design default; openai is a supported alternative.
    llm_providers: Annotated[list[LLMProviderName], NoDecode] = Field(
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
    # OpenAI (Responses API). Model and reasoning effort per job; no model names are built
    # in, so each job the services run must be set when openai is a provider (.env.example
    # has suggestions). An unset effort sends no reasoning setting (the model's default).
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    openai_base_url: str | None = Field(default=None, validation_alias="OPENAI_BASE_URL")
    openai_detector_model: str | None = None
    openai_fast_model: str | None = None
    openai_deep_model: str | None = None
    # The live call's rolling summary (gateway conversation memory). Unset: the detector's
    # model and reasoning, the cheapest live route.
    openai_summary_model: str | None = None
    openai_extract_model: str | None = None  # memory extraction from documents
    openai_job_model: str | None = None  # job description to company, title, summary
    openai_pack_model: str | None = None
    openai_postcall_model: str | None = None  # post-call summary and feedback
    openai_detector_reasoning: ReasoningEffort | None = None
    openai_fast_reasoning: ReasoningEffort | None = None
    openai_deep_reasoning: ReasoningEffort | None = None
    openai_summary_reasoning: ReasoningEffort | None = None
    openai_extract_reasoning: ReasoningEffort | None = None
    openai_job_reasoning: ReasoningEffort | None = None
    openai_pack_reasoning: ReasoningEffort | None = None
    openai_postcall_reasoning: ReasoningEffort | None = None
    # OpenAI counts reasoning tokens in max_output_tokens (Claude's thinking also counts in
    # max_tokens, but the live passes run Claude without it): a job with a reasoning effort
    # other than "none" gets this many extra output tokens, so reasoning cannot eat the
    # answer's budget.
    openai_reasoning_headroom_tokens: int = 2048
    # First-token timeouts for OpenAI's live routes (None: the gateway's first_token_timeout_ms,
    # like every route). Measured from a desktop in October 2026, warm and cached, OpenAI's
    # first token came after 0.8 to 2.5 s on the fast pass and 2 to 4.3 s on a deep pass that
    # reasons first, so the live 1.5 s would give up on most of them.
    openai_first_token_timeout_ms: int | None = None  # detector and fast pass
    openai_deep_first_token_timeout_ms: int | None = None
    # How long idle connections to every model provider (OpenAI, the Claude API, Bedrock)
    # stay open. The SDKs' default (5 s) makes nearly every live question pay a new TLS
    # handshake (about 2 s to OpenAI and 4.6 s to the Claude API measured from a desktop in
    # October 2026); the gateway also opens the connections when it starts and when a session
    # starts. COPILOT_OPENAI_KEEPALIVE_S is the old name.
    llm_keepalive_s: float = Field(
        default=120.0,
        validation_alias=AliasChoices(
            "llm_keepalive_s", "COPILOT_LLM_KEEPALIVE_S", "COPILOT_OPENAI_KEEPALIVE_S"
        ),
    )
    # HTTP/2: the fast and deep passes share one warm connection, and a route given up at
    # its first-token timeout cancels only its stream, not the connection the retry needs.
    openai_http2: bool = True

    # Prices per million tokens, for answers.cost_usd (copilot_core.pricing). JSON; entries
    # given are merged over the defaults, e.g.
    # {"claude-haiku-4-5": {"input": 1, "cached": 0.1, "output": 5}}.
    model_prices: dict[str, ModelPrice] = Field(default_factory=lambda: dict(DEFAULT_PRICES))
    tier_price_multipliers: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_TIERS))

    # Embeddings for prepared-answer matching, always 1024 dimensions. "auto" uses Bedrock
    # (Cohere) when bedrock is a model provider, else OpenAI when openai is one, and turns
    # matching off otherwise (the direct Claude API has no embeddings). Vectors from
    # different embedding models are never compared (copilot_core.embed).
    embedder: Literal["auto", "bedrock", "openai", "none"] = "auto"
    embed_model: str = "cohere.embed-v4:0"  # Bedrock
    openai_embed_model: str | None = None

    # Deepgram: streaming STT in the gateway, one stream per channel.
    deepgram_api_key: SecretStr | None = Field(default=None, validation_alias="DEEPGRAM_API_KEY")
    deepgram_model: str = "nova-3"

    # Valkey and the jobs queue.
    valkey_url: str = "redis://localhost:6379/0"
    valkey_cluster: bool = False
    sqs_endpoint: str | None = "http://localhost:9324"  # unset in AWS (empty string)
    sqs_queue_url: str = "http://localhost:9324/000000000000/jobs"

    # The oldest desktop app the gateway and the API serve (copilot_core.versions). Older
    # release builds get close code 4426 or HTTP 426 `client_outdated` and update at once.
    # 0.1.0 is the first release, so every installed app works until this is raised.
    min_client_version: str = "0.1.0"

    @field_validator("min_client_version", mode="after")
    @classmethod
    def _semver(cls, value: str) -> str:
        from copilot_core.versions import parse

        if parse(value) is None:
            raise ValueError(f"min_client_version {value!r}: expected 1.2.3")
        return value

    @property
    def embedding_provider(self) -> Literal["bedrock", "openai"] | None:
        if self.embedder == "auto":
            if "bedrock" in self.llm_providers:
                return "bedrock"
            if "openai" in self.llm_providers and self.openai_embed_model:
                return "openai"
            return None
        if self.embedder == "openai" and not self.openai_embed_model:
            return None
        return None if self.embedder == "none" else self.embedder

    @property
    def embedding_model_id(self) -> str | None:
        """Which embedding model makes the vectors: `provider:model@dimensions`. Stored with
        every vector (packs, memory nodes); vectors with another id are never compared."""
        provider = self.embedding_provider
        if provider is None:
            return None
        model = self.embed_model if provider == "bedrock" else self.openai_embed_model
        return f"{provider}:{model}@{EMBED_DIMENSIONS}"

    def openai_first_token_timeout_s(self, job: str) -> float | None:
        """An OpenAI live route's own first-token timeout, if one is set."""
        if job == "deep":
            ms = self.openai_deep_first_token_timeout_ms
        else:
            ms = self.openai_first_token_timeout_ms
        return ms / 1000 if ms else None

    def openai_route(self, job: str) -> tuple[str, ReasoningEffort | None]:
        """The OpenAI model and reasoning effort for a job; an unset model is an error."""
        if job not in OPENAI_JOBS:
            raise ValueError(f"no OpenAI settings for job {job!r}")
        if job == "summary" and not self.openai_summary_model:
            job = "detector"
        model = getattr(self, f"openai_{job}_model")
        if not model:
            raise ValueError(
                f"COPILOT_OPENAI_{job.upper()}_MODEL is not set; it is needed when openai is "
                "in COPILOT_LLM_PROVIDERS (see .env.example)"
            )
        return str(model), getattr(self, f"openai_{job}_reasoning")

    @property
    def cognito_issuer(self) -> str | None:
        if not self.cognito_user_pool_id:
            return None
        region = self.cognito_user_pool_id.split("_", 1)[0]
        return f"https://cognito-idp.{region}.amazonaws.com/{self.cognito_user_pool_id}"

    @model_validator(mode="after")
    def _no_dev_or_test_on_aws(self) -> Self:
        # dev and test unlock the dev token, the dev key provider and recorded fixtures; none
        # of them may ever run on AWS or Compute Engine, whatever else is configured.
        if self.env in ("dev", "test") and (markers := cloud_runtime(os.environ)):
            raise ValueError(
                f"COPILOT_ENV={self.env} cannot run in the cloud ({', '.join(markers)}); "
                "deployments use staging or prod"
            )
        return self

    @field_validator("model_prices", mode="after")
    @classmethod
    def _merge_prices(cls, value: dict[str, ModelPrice]) -> dict[str, ModelPrice]:
        return {**DEFAULT_PRICES, **{model_key(k): v for k, v in value.items()}}

    @field_validator("tier_price_multipliers", mode="after")
    @classmethod
    def _merge_tiers(cls, value: dict[str, float]) -> dict[str, float]:
        return {**DEFAULT_TIERS, **value}

    @field_validator(
        "llm_providers",
        "cognito_client_ids",
        "google_client_ids",
        "google_allowed_emails",
        mode="before",
    )
    @classmethod
    def _comma_list(cls, value: object) -> object:
        return split_list(value)

    @field_validator(
        *(f"openai_{job}_{part}" for job in OPENAI_JOBS for part in ("model", "reasoning")),
        "openai_embed_model",
        "openai_base_url",
        "openai_first_token_timeout_ms",
        "openai_deep_first_token_timeout_ms",
        mode="before",
    )
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value


class DataSettings(CoreSettings):
    """The API's and the workers' extra stores: Postgres, S3 and KMS."""

    # Each service connects as its own role (README, "Database roles"), from its own
    # variable: COPILOT_DATABASE_URL is the migration runner's superuser URL.
    database_url: str = "postgresql://app_api:app_api-local@localhost:5432/copilot"
    db_pool_min: int = 1
    db_pool_max: int = 10

    # S3. Locally SeaweedFS; in AWS the endpoint is unset and the bucket comes from Terraform;
    # on GCP it is Cloud Storage's XML API (https://storage.googleapis.com) with HMAC keys.
    s3_endpoint: str | None = "http://localhost:9000"
    # The region requests are signed for; empty uses AWS_REGION. Cloud Storage takes "auto".
    s3_region: str | None = None
    s3_bucket: str = "copilot-uploads"
    s3_access_key: SecretStr | None = Field(default=None, validation_alias="COPILOT_S3_ACCESS_KEY")
    s3_secret_key: SecretStr | None = Field(default=None, validation_alias="COPILOT_S3_SECRET_KEY")

    # Per-user envelope encryption (ARCHITECTURE.md, "Security and privacy").
    # "kms" in AWS, "gcp_kms" (Cloud KMS) on GCP; "dev" is a local stand-in with a master key
    # from dev_master_key.
    key_provider: Literal["kms", "gcp_kms", "dev"] = "kms"
    kms_key_id: str | None = None  # alias/copilot-user-data
    gcp_kms_key: str | None = None  # projects/<p>/locations/<l>/keyRings/<r>/cryptoKeys/<k>
    dev_master_key: SecretStr | None = None  # base64 of 32 bytes
    dek_cache_ttl_s: float = 300.0
