"""Gateway configuration.

Live tuning settings carry the defaults from ARCHITECTURE.md ("Settings"). They come from the
environment (prefix COPILOT_) or a .env file for now; AWS AppConfig replaces that later.
"""

from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import NoDecode

from copilot_core.config import CoreSettings, split_list

QuestionType = Literal[
    "behavioral", "technical", "system_design", "coding", "standup_status", "small_talk", "other"
]


class Settings(CoreSettings):
    """Shared fields (auth, Claude providers and models, Valkey, the queue) are in
    `CoreSettings`; these are the gateway's own."""

    # Live settings (ARCHITECTURE.md "Settings").
    silence_timer_ms: int = 400
    speculative_start_ms: int = 300
    stage1_min_score: float = 0.3
    detector_min_confidence: float = 0.7
    detector_max_tokens: int = 80
    prepared_answer_min_similarity: float = 0.85
    fast_pass_max_tokens: int = 250
    fast_pass_priority: bool = False
    deep_pass_max_tokens: int = 800
    deep_pass_auto_types: Annotated[list[QuestionType], NoDecode] = Field(
        default_factory=lambda: ["behavioral", "technical", "system_design"]
    )
    rerank_keep: int = 6
    retrieval_budget_ms: int = 100
    first_token_timeout_ms: int = 1500
    transcript_context_s: int = 60
    sessions_per_task_target: int = 60
    # Not in the doc's table: how long confirmation waits for the STT final after the
    # silence timer before it uses the interim text.
    final_wait_max_ms: int = 1500
    # Seconds of "them" transcript a hotkey question.force without text uses.
    force_context_s: int = 20

    # Claude routes beyond the shared model ids.
    # The retry route; set to the `us.` cross-region profile if the account uses one.
    bedrock_haiku_retry_model: str | None = None
    bedrock_sonnet_retry_model: str | None = None
    # Sonnet 5.5 deep pass: "between_tools" is its lowest thinking setting (no extended
    # thinking); "adaptive" lets the model think, still at deep_pass_effort.
    deep_pass_thinking: Literal["between_tools", "adaptive"] = "between_tools"
    deep_pass_effort: Literal["low", "medium", "high"] = "low"
    # Bedrock has no server-side refusal fallback: after a Sonnet 5.5 refusal the gateway
    # retries this model itself, with thinking off. Empty turns the route off.
    bedrock_refusal_model: str = "anthropic.claude-sonnet-5"

    # Speech to text, in fallback order (ARCHITECTURE.md "Failure modes").
    stt_providers: Annotated[list[Literal["deepgram", "assemblyai"]], NoDecode] = Field(
        default_factory=lambda: ["deepgram"]
    )
    deepgram_url: str = "wss://api.deepgram.com/v1/listen"
    stt_stall_s: float = 3.0
    stt_failures_before_switch: int = 2
    stt_keepalive_s: float = 5.0

    # Session lifecycle and limits (ARCHITECTURE.md section 1).
    first_message_timeout_s: float = 5.0
    resume_window_s: float = 120.0
    idle_end_s: float = 600.0
    max_session_minutes: int = 240
    events_maxlen: int = 2000
    state_ttl_s: int = 7200
    log_ttl_after_flush_s: int = 3600
    lease_ttl_s: float = 30.0
    lease_renew_s: float = 10.0
    flush_interval_s: float = 30.0
    auth_expiring_lead_s: int = 120
    force_min_interval_s: float = 1.0
    max_binary_bytes: int = 64 * 1024
    max_json_bytes: int = 256 * 1024
    max_screenshot_bytes: int = 2 * 1024 * 1024
    ws_ping_interval_s: float = 30.0
    ws_ping_timeout_s: float = 60.0  # with the 30 s interval: a socket silent 90 s is closed
    store_buffer_max: int = 50_000

    @field_validator("deep_pass_auto_types", "stt_providers", mode="before")
    @classmethod
    def _gateway_lists(cls, value: object) -> object:
        return split_list(value)
