"""Gateway configuration.

Live tuning settings carry the defaults from ARCHITECTURE.md ("Settings"). They come from the
environment (prefix COPILOT_) or a .env file for now; AWS AppConfig replaces that later.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import NoDecode

from copilot_core.config import CoreSettings, split_list


class PreparedThreshold(BaseModel):
    """When a live question matches a prepared answer, for one embedding model: its best
    cosine similarity reaches `min_similarity` and beats the runner-up by `min_margin`."""

    model_config = ConfigDict(extra="forbid")
    min_similarity: float
    min_margin: float = 0.0


# Calibrated with copilot-calibrate-prepared on evals/prepared/calibration.toml (October 2026):
# text-embedding-3-large scores paraphrases 0.40 to 0.88 and unrelated questions up to 0.57,
# so 0.85 matched 3 of 36 paraphrases; 0.63 with a 0.05 margin matches about half with no
# wrong match. Cohere Embed v4 keeps the design value until it is calibrated the same way.
DEFAULT_PREPARED_THRESHOLDS: dict[str, PreparedThreshold] = {
    "openai:text-embedding-3-large@1024": PreparedThreshold(min_similarity=0.63, min_margin=0.05),
    "bedrock:cohere.embed-v4:0@1024": PreparedThreshold(min_similarity=0.85),
}

QuestionType = Literal[
    "behavioral", "technical", "system_design", "coding", "standup_status", "small_talk", "other"
]


class Settings(CoreSettings):
    """Shared fields (auth, Claude providers and models, Valkey, the queue) are in
    `CoreSettings`; these are the gateway's own."""

    # Live settings (ARCHITECTURE.md "Settings").
    silence_timer_ms: int = 400
    speculative_start_ms: int = 300
    # Start the fast pass at the speech end, alongside the detector, holding its output on the
    # gateway until the detector confirms (turn.py). Only for text that passes the local check
    # (`likely_question`) with at least this many words. Off: the fast pass waits for the
    # detector, as before.
    speculative_hold: bool = True
    speculative_min_words: int = 3
    stage1_min_score: float = 0.3
    detector_max_tokens: int = 80
    # A detector request with no first token after this long gets a second one alongside it
    # (the job's retry route), and the first to answer is used. Benchmark, October 2026: 5% of
    # detector calls stalled until the 3 s first-token timeout and its retry (5 to 6 s), while
    # 90% finished within 3.3 s. A hedge costs one more detector call (about $0.0001). None:
    # retry only after the timeout.
    detector_hedge_ms: int | None = 2000
    # Per embedding model id (`provider:model@dimensions`); entries given (JSON) are merged
    # over the calibrated defaults. A model without an entry uses
    # prepared_answer_min_similarity and no margin.
    prepared_answer_thresholds: dict[str, PreparedThreshold] = Field(
        default_factory=lambda: dict(DEFAULT_PREPARED_THRESHOLDS)
    )
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
    # Passes whose prompt cache is written at session start (and on a mode change), so the
    # first question is not cold. Only the fast pass by default: the first word waits on it.
    prewarm_jobs: Annotated[list[Literal["fast", "deep"]], NoDecode] = Field(
        default_factory=lambda: ["fast"]
    )

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

    def prepared_threshold(self, model_id: str | None) -> PreparedThreshold:
        found = self.prepared_answer_thresholds.get(model_id or "")
        return found or PreparedThreshold(min_similarity=self.prepared_answer_min_similarity)

    @field_validator("prepared_answer_thresholds", mode="after")
    @classmethod
    def _merge_thresholds(cls, value: dict[str, PreparedThreshold]) -> dict[str, PreparedThreshold]:
        return {**DEFAULT_PREPARED_THRESHOLDS, **value}

    @field_validator("deep_pass_auto_types", "stt_providers", "prewarm_jobs", mode="before")
    @classmethod
    def _gateway_lists(cls, value: object) -> object:
        return split_list(value)
