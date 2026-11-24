"""Worker configuration (ARCHITECTURE.md, "Workers" and "SQS job catalog")."""

from typing import Literal

from pydantic import Field, model_validator

from copilot_core.config import DataSettings


class WorkerSettings(DataSettings):
    # app_worker for user jobs (under row-level security); app_system (BYPASSRLS) only for
    # the system jobs: usage.report, user.delete and the session sweeper.
    database_url: str = Field(
        default="postgresql://app_worker:app_worker-local@localhost:5432/copilot",
        validation_alias="COPILOT_WORKER_DATABASE_URL",
    )
    system_database_url: str = Field(
        default="postgresql://app_system:app_system-local@localhost:5432/copilot",
        validation_alias="COPILOT_SYSTEM_DATABASE_URL",
    )

    # Consumer.
    concurrency: int = Field(default=10, ge=1)
    persistence_concurrency: int = Field(default=2, ge=1)
    receive_wait_s: int = 20
    heartbeat_s: float = Field(default=30.0, gt=0)
    operation_lease_s: float = Field(default=180.0, gt=0, le=3600)
    outbox_interval_s: float = Field(default=1.0, gt=0)
    outbox_batch_size: int = Field(default=25, ge=1, le=100)
    outbox_publish_concurrency: int = Field(default=8, ge=1, le=32)
    outbox_recovery_s: float = Field(default=120.0, gt=0)
    shutdown_grace_s: float = 100.0  # Spot gives two minutes after SIGTERM

    # Claude for background jobs. Bedrock routes try the Flex tier first, then Standard;
    # the direct API has no Flex tier, so it runs Standard. `bedrock_background_tier` is
    # sent as the request's `service_tier`; empty skips the Flex route.
    bedrock_background_tier: str = "flex"
    extract_effort: Literal["low", "medium", "high"] = "medium"
    background_effort: Literal["low", "medium", "high"] = "medium"
    llm_timeout_s: float = 600.0
    llm_first_token_timeout_s: float = 300.0

    # Session log (ARCHITECTURE.md, "Session logging").
    log_ttl_after_flush_s: int = 3600
    resume_window_s: float = 120.0  # the gateway's; the sweeper ends sessions idle longer

    # Packs.
    prepared_answers: int = 30
    pack_ttl_s: int = 24 * 3600  # after the call's due time

    # Offline runs (dev and test only): replay recorded model responses from
    # this directory instead of calling the providers. See copilot_worker.fixtures.
    fixtures_dir: str | None = None

    @model_validator(mode="after")
    def lease_covers_heartbeat(self) -> "WorkerSettings":
        if self.heartbeat_s >= self.operation_lease_s:
            raise ValueError("operation_lease_s must exceed heartbeat_s")
        return self
