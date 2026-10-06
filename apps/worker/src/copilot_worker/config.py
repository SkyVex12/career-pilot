"""Worker configuration (ARCHITECTURE.md, "Workers" and "SQS job catalog")."""

from typing import Literal

from pydantic import Field

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
    concurrency: int = 10
    receive_wait_s: int = 20
    heartbeat_s: float = 60.0  # extend the running message's visibility this often
    shutdown_grace_s: float = 100.0  # Spot gives two minutes after SIGTERM

    # Claude for background jobs. Bedrock routes try the Flex tier first, then Standard;
    # the direct API has no Flex tier, so it runs Standard. `bedrock_background_tier` is
    # sent as the request's `service_tier`; empty skips the Flex route.
    bedrock_background_tier: str = "flex"
    extract_effort: Literal["low", "medium", "high"] = "medium"
    background_effort: Literal["low", "medium", "high"] = "medium"
    llm_timeout_s: float = 600.0
    llm_first_token_timeout_s: float = 300.0

    # Pre-recorded STT for the user's own audio.
    deepgram_prerecorded_url: str = "https://api.deepgram.com/v1/listen"

    # Session log (ARCHITECTURE.md, "Session logging").
    me_audio_wait_s: int = 600  # post-call work runs without the upload after this
    log_ttl_after_flush_s: int = 3600
    resume_window_s: float = 120.0  # the gateway's; the sweeper ends sessions idle longer
    # Deep passes run on their own for these types, so a deep answer to any other type means
    # the user tapped expand (the gateway's deep_pass_auto_types).
    deep_pass_auto_types: tuple[str, ...] = ("behavioral", "technical", "system_design")

    # Packs.
    prepared_answers: int = 30
    pack_ttl_s: int = 24 * 3600  # after the call's due time

    # Offline runs (dev and test only): replay recorded Claude and Deepgram responses from
    # this directory instead of calling the providers. See copilot_worker.fixtures.
    fixtures_dir: str | None = None
