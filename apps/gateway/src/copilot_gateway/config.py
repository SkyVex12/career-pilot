"""Gateway configuration.

Live tuning settings carry the defaults from ARCHITECTURE.md ("Settings"). They come from the
environment (prefix COPILOT_) or a .env file for now; AWS AppConfig replaces that later.
"""

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import NoDecode

from copilot_core.config import CoreSettings, ReasoningEffort, split_list
from copilot_core.pricing import model_key


def fastest_deep_tier(model: str | None) -> Literal["ultrafast"] | None:
    """Only opt documented models into Ultrafast; unknown models keep their default."""
    # Verified 2026-11-26: https://developers.openai.com/api/docs/guides/ultrafast-mode
    if model and model_key(model) in {
        "gpt-6.2-sol",
        "gpt-6.2-astra",
        "gpt-6.1-sol",
        "gpt-6-astra",
    }:
        return "ultrafast"
    return None


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
    "behavioral",
    "technical",
    "system_design",
    "coding",
    "standup_status",
    "small_talk",
    "respond",
    "other",
    # Not from the detector: the user has started answering, and the answer follows them
    # (Session._maybe_follow). Sent to the client as "other".
    "follow",
    # At work only (prompts.WORK_TYPES), each with its own shape; sent to the client as the
    # protocol type in prompts.WIRE_TYPES.
    "decision",
    "how_it_works",
    "status_follow_up",
    "estimate_risk",
]


class AdaptiveExpertRoute(BaseModel):
    """Explicit OpenAI route for adaptive expert answers; no global deep overrides.

    Null reasoning/tier omits that wire field; null retry tier keeps the primary
    tier. Null first-token timeout uses the router default; null hedge disables it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    model: str = Field(strict=True, min_length=1)
    reasoning: ReasoningEffort | None = None
    service_tier: Literal["default", "priority", "fast", "ultrafast", "flex"] | None = None
    retry_tier: Literal["default", "priority", "fast", "ultrafast", "flex"] | None = None
    first_token_timeout_ms: int | None = Field(default=None, strict=True, gt=0)
    hedge_ms: int | None = Field(default=None, strict=True, gt=0)

    @field_validator("model")
    @classmethod
    def _exact_model_id(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("Adaptive expert model must be a nonempty exact model ID")
        return value


def _openai_only(values: dict[str, Any]) -> bool:
    # Promote only the evaluated topology; mixed and other-provider deployments retain
    # their existing defaults. Explicit constructor/environment fields skip factories.
    return values.get("llm_providers") == ["openai"]


def _default_adaptive_expert(values: dict[str, Any]) -> AdaptiveExpertRoute | None:
    if not _openai_only(values):
        return None
    return AdaptiveExpertRoute(
        model="gpt-6.2-sol",
        reasoning="low",
        service_tier="ultrafast",
        retry_tier="fast",
        first_token_timeout_ms=20000,
        hedge_ms=None,
    )


class Settings(CoreSettings):
    """Shared fields (auth, Claude providers and models, Valkey, the queue) are in
    `CoreSettings`; these are the gateway's own."""

    # Live settings (ARCHITECTURE.md "Settings").
    personal_ai_api_url: str | None = None
    personal_ai_timeout_s: float = 0.35
    silence_timer_ms: int = 400
    speculative_start_ms: int = 300
    # Start the fast pass at the speech end, alongside the detector, holding its output on the
    # gateway until the detector confirms (turn.py). Only for text that passes the local check
    # (`likely_question`) with at least this many words. Off: the fast pass waits for the
    # detector, as before.
    speculative_hold: bool = True
    speculative_min_words: int = 3
    # Standups and meetings: hold a pass for any utterance of this many words, not only those
    # the local check takes for a question ("so you're good to start there" is one). About
    # $0.0002 per discarded pass on gpt-6-luna, ~$0.06 per live hour (2026-10-30). None: off.
    group_hold_min_words: int | None = 4
    # Standups and meetings: start the detector and the held pass on the live text each time
    # it ends a sentence, at most once per this many ms, so the decision is ready at the
    # speech end (turn.py, "Early decision"). None: off.
    early_decision_interval_ms: int | None = 700
    # The early decision in interviews too, not only standups and meetings (2026-11-16).
    early_decision_all_modes: bool = True
    # Experimental hidden drafts from unchanged, unpunctuated question partials.
    # Keep disabled until speech replays establish accuracy and cancellation overhead.
    early_stable_partial_ms: int | None = Field(default=None, ge=100, le=2000)
    # Reused early work with a positive detector decision and matching final transcript
    # may bypass the display floor; confirmation and interruption rules still apply.
    early_final_release: bool = False
    # Recheck committed transcript revisions before the delayed utterance-flush ACK.
    # Ordinary segment finals still do not confirm an entire spoken turn.
    committed_final_recheck: bool = False
    # Keep a held fast pass when the STT final only adds this many words at the end of the
    # text it started on; only the detector decides again (turn.py, "Kept on a longer final").
    # The final changed the words of 105 of 141 utterances in October's benchmark runs, and
    # each change restarted the pass. None: every change restarts it.
    held_keep_added_words: int | None = 2
    stage1_min_score: float = 0.3
    detector_max_tokens: int = 80
    # How the detector asks (detector.py): "decisions" is OpenAI's Decisions API, typed
    # choices in about 0.5 s against 1.2 s for the same rules as generated JSON (2026-11-16,
    # the 119 labelled lines of the evals); it needs an OpenAI provider and falls back to
    # "responses" without one. "responses" is the detector prompt on the job's routes.
    detector_api: Literal["decisions", "responses"] = "decisions"
    # The Decisions API's model (gpt-6-luna is the only one it serves, November 2026), when a
    # second request starts alongside the first (None: only after a failure), and how long a
    # request may take before it counts as failed.
    decisions_model: str = "gpt-6-luna"
    decisions_hedge_ms: int | None = 900
    decisions_timeout_ms: int = 3000
    # A detector request with no first token after this long gets a second one alongside it
    # (the job's retry route), and the first to answer is used. Benchmark, October 2026: 5% of
    # detector calls stalled until the 3 s first-token timeout and its retry (5 to 6 s), while
    # 90% finished within 3.3 s. A hedge costs one more detector call (about $0.0001). None:
    # retry only after the timeout. 2,000 ms until 2026-11-11: in slow spells 11 to 16% of
    # decisions then took 2.5 to 3.2 s (the wait, plus the second route's 0.8 s), and every
    # card waited with them; the fast pass has hedged at 1,200 ms since 2026-10-28.
    detector_hedge_ms: int | None = 1200
    # One-on-one calls (interviews): a plain question of at least this many words is shown
    # as soon as the STT final is in, before the detector has decided; the detector then
    # types it or the card is withdrawn (turn.py, "Shown before the detector"). No extra
    # model call: the held pass is already running. None: every card waits for the detector.
    first_show_min_words: int | None = 8
    # The OpenAI tier of the detector's second route, which the hedge and the retry use. In
    # the 2026-10-27 standup a slow spell on gpt-6-luna failed 10 decisions in 50 s, because
    # the hedge went to the same model on the same tier. Priority costs 1.75x on the few calls
    # that hedge (about $0.0001 each). None: the same tier as the first route.
    openai_detector_retry_tier: str | None = "priority"
    # The same for the fast pass. In the 2026-10-28 interview three questions in a row got no
    # fast-pass first token within the 3 s timeout, and the retry went to the same model and
    # tier: first words at 6.4 to 8.1 s, one fast pass lost, while the user had started
    # answering at about 1.6 s. A hedge costs one more fast call (about $0.0003, 1.75x on
    # Priority) on the few that stall. None: retry only after the timeout, on the same tier.
    fast_hedge_ms: int | None = 1200
    openai_fast_retry_tier: str | None = "priority"
    # Supported deep models use Ultrafast. Explicit None preserves the provider default;
    # retry tiers remain independent so capacity/access failures can fall back to Fast.
    openai_fast_service_tier: Literal["default", "priority", "fast", "ultrafast", "flex"] | None = (
        None
    )
    openai_deep_service_tier: Literal["default", "priority", "fast", "ultrafast", "flex"] | None = (
        Field(default_factory=lambda data: fastest_deep_tier(data.get("openai_deep_model")))
    )
    # The same for the deep pass. In the 2026-10-29 interviews 7 deep passes got no first token
    # within their timeout and retried on the same model and tier: their first words came at
    # 9.7 to 11 s instead of about 4 s (two more were lost in a runaway session). Its first
    # token normally comes within 4.2 s. A hedge costs one more deep call, 1.75x on Priority,
    # on the few that stall. None: retry only after the timeout, on the same tier.
    deep_hedge_ms: int | None = 4500
    openai_deep_retry_tier: str | None = "priority"
    # In standups and meetings, a question that addresses the user by name waits this long
    # for the detector, then shows as `respond` (detector.named_or_model). None: off.
    name_address_grace_ms: int | None = 1000
    # Per embedding model id (`provider:model@dimensions`); entries given (JSON) are merged
    # over the calibrated defaults. A model without an entry uses
    # prepared_answer_min_similarity and no margin.
    prepared_answer_thresholds: dict[str, PreparedThreshold] = Field(
        default_factory=lambda: dict(DEFAULT_PREPARED_THRESHOLDS)
    )
    prepared_answer_min_similarity: float = 0.85
    # Defaults select the evaluated OpenAI-only system; explicit legacy remains rollback.
    answer_architecture: Literal[
        "legacy_dual", "adaptive", "adaptive_hybrid", "adaptive_on_demand"
    ] = Field(default_factory=lambda data: "adaptive" if _openai_only(data) else "legacy_dual")
    # Experimental: one independent expert attempt after a typed transient primary failure,
    # only before any answer text. Off until paired failure-recovery evidence supports it.
    adaptive_failure_recovery: bool = False
    # Isolated adaptive expert route. Explicit None restores the global deep route;
    # legacy and screens ignore this profile even when it remains configured.
    adaptive_expert_route: Annotated[AdaptiveExpertRoute | None, NoDecode] = Field(
        default_factory=_default_adaptive_expert
    )
    # Standard technical answers retain the dependent legacy detail that won full-view
    # review. Expert/coding/design plans keep precedence; False restores pure adaptive.
    adaptive_technical_legacy: bool = Field(default_factory=_openai_only)
    adaptive_max_tokens: int = Field(default=1200, ge=100, le=4000)
    adaptive_stream_idle_timeout_s: float = Field(default=30.0, gt=0, le=120)
    adaptive_stream_total_timeout_s: float = Field(default=90.0, gt=0, le=300)
    fast_pass_max_tokens: int = 250
    fast_pass_priority: bool = False
    deep_pass_max_tokens: int = 800
    # Coding mode (copilot_core.screen): the fast pass reads the screen and answers every task
    # that needs no code, several on one page; the code pass writes complete solutions.
    screen_fast_max_tokens: int = 1500
    screen_code_max_tokens: int = 4000
    screen_default_language: str = "python"
    # First-token timeouts of coding mode's routes ("screen", "screen_code"), on every
    # provider; they never hedge (router.py).
    screen_first_token_timeout_ms: int = 10000
    screen_code_first_token_timeout_ms: int = 15000
    # When the code pass starts: "with_fast", at the shot, alongside the fast pass, reading the
    # screen itself (cancelled if the reading shows no task needs it), or "after_read", once
    # the fast pass's reading is in. And its reasoning effort on OpenAI (None: the deep pass's).
    # 2026-11-18 (copilot-coding-eval, same hour): "with_fast" had the code done at 4.6 s
    # (interview) and 3.5 s (test) at the median, against 6.8 s and 5.6 s after the reading,
    # every check passing. gpt-6.1-sol takes no reasoning below "low"; gpt-6-luna at "low"
    # was faster still (4.0 s) but missed 1 exact output in 4.
    screen_code_start: Literal["with_fast", "after_read"] = "with_fast"
    screen_code_reasoning: ReasoningEffort | None = None
    # The code pass's OpenAI model (None: the deep pass's).
    screen_code_model: str | None = None
    # Follow-ups: the last screen's problem goes with every question for this long after its
    # shot, so "optimize it" or "write it in Java" is answered against it. 0: off.
    screen_followup_s: float = 1800.0
    # The deep pass starts on its own once the fast pass is done (and the question is
    # confirmed), for these types; other types only on expand. Off: every deep pass waits for
    # expand. A newer confirmed question cancels it. Until October 2026 only behavioral,
    # technical and system_design started on their own; that list is the setting to turn the
    # deep pass's cost down (ARCHITECTURE.md, "Cost estimate").
    deep_pass_auto: bool = True
    # Which model writes the detailed answer (answer.AnswerEngine.deep_job): "by_need", the
    # deep model only when the detector says the answer needs expert reasoning, else the fast
    # model with the detailed answer's prompt (route "deep_light"); "always_deep", as before.
    # On the VM (2026-11-16) gpt-6-luna's detailed answer showed its first word at 1.0 s and was
    # done at 3.0 s, against 3.1 s and 8.0 s on gpt-6.1-sol; as complete on stories, less so on
    # technical depth, design and the job and goals questions.
    deep_routing: Literal["by_need", "always_deep"] = "by_need"
    deep_pass_auto_types: Annotated[list[QuestionType], NoDecode] = Field(
        default_factory=lambda: [
            "behavioral",
            "technical",
            "system_design",
            "coding",
            "standup_status",
            "other",
            "respond",
            "follow",
            "decision",
            "how_it_works",
            "status_follow_up",
            "estimate_risk",
        ]
    )
    rerank_keep: int = 6
    retrieval_budget_ms: int = 100
    # Live memory search (company workspaces). The gateway's own role, read only; unset, the
    # gateway has no database and standups and meetings use their packs alone.
    database_url: str | None = Field(default=None, validation_alias="COPILOT_GATEWAY_DATABASE_URL")
    db_pool_max: int = 5
    # How long a pass waits for search results: the fast pass only for full text (local
    # Postgres answers in a few ms), the deep pass for the hybrid result, which also waits for
    # the question's embedding.
    memory_fast_wait_ms: int = 30
    memory_deep_wait_ms: int = 150
    memory_fast_hits: int = 4
    # Engineering context (copilot_core.knowledge): sections of the company's documents next
    # to its facts. The short answer gets excerpts found by keyword in memory (no wait); the
    # detailed answer whole sections, with the vector search when it is in within
    # memory_deep_wait_ms. 0 turns either off.
    knowledge_fast_sections: int = 3
    knowledge_deep_sections: int = 4
    # Rendering a company's pack at session start when no current one is stored (DB only).
    pack_render_timeout_ms: int = 400
    first_token_timeout_ms: int = 1500
    # Conversation memory (ARCHITECTURE.md, "Conversation memory"): what the detector and both
    # passes see of the call, in a hard budget (about 4 characters a token). The latest lines
    # verbatim get what the summary and the earlier answers leave; when they overflow, the
    # oldest `conversation_summary_chunk_tokens` of them go to the summary model in the
    # background (job "summary"), so a summary call happens every few minutes of talk, never
    # per line and never on the answer's path.
    conversation_budget_tokens: int = 3000
    conversation_summary_tokens: int = 500
    conversation_answers_tokens: int = 600
    conversation_summary_chunk_tokens: int = 500
    conversation_summary_retry_s: float = 30.0
    # The detector sees only the latest lines of it, this many tokens.
    detector_context_tokens: int = 300
    # Live transcription of the user's own microphone, as a second STT stream. Off: the client
    # is told to send speech flags only on the me channel (session.ready me_audio), and the
    # conversation holds only "(spoke for N s)" for the user.
    me_stt: bool = True
    # Echo: a me segment whose words mostly (this share) appeared in the other side's speech
    # around the same time is the other side leaking into the mic, not the user. When the
    # other side was speaking then, the check waits up to `echo_wait_ms` for its transcript.
    echo_min_overlap: float = 0.6
    echo_wait_ms: int = 2000
    sessions_per_task_target: int = 60
    # Not in the doc's table: how long confirmation waits for the STT final after the
    # silence timer before it uses the interim text.
    final_wait_max_ms: int = 1500
    # Seconds of "them" transcript a hotkey question.force without text uses.
    force_context_s: int = 20
    # A forced question that takes over the turn still deciding (the hotkey, Enter or Respond
    # with nothing typed): how long it waits for that turn's detector to type it, for the deep
    # pass's shape. Its fast pass shows at once either way.
    force_type_wait_ms: int = 1500
    # Follow the user (2026-10-28, the user's call): once the user has said this many words in
    # reply to the other side, the answer follows what they are saying, whether or not a
    # question was detected: a new card that continues in their direction. Once per turn of
    # the other side, within follow_window_ms of its end. Off: answers only on detection.
    follow_user: bool = True
    follow_min_words: int = 8
    follow_window_ms: int = 12000
    # A question that goes on after a pause (2026-10-29): when the other side starts speaking
    # again within this long of a question's end and the user has not spoken, the next
    # question is the same one continued, and its passes get both parts. 0: off.
    continuation_gap_ms: int = 5000
    # While a session is live, open (or keep open) each model provider's connection this often,
    # with a free request (ModelRouter.warm), so the first call after a quiet spell does not
    # pay a new TLS handshake: about 0.7 s more to the first token, measured 2026-11-16, while
    # idle connections are dropped after llm_keepalive_s (120 s). None: only at session start.
    llm_warm_interval_s: float | None = 60.0
    # Passes whose prompt cache is written at session start (and on a mode change), so the
    # first question is not cold: the fast pass, whose first word everyone waits on, and the
    # light detailed answer, on the same cheap model (about $0.0002 a session). The deep
    # model's is not: one cache write on it costs about as much as a detailed answer.
    prewarm_jobs: Annotated[list[Literal["fast", "deep", "deep_light"]], NoDecode] = Field(
        default_factory=lambda: ["fast", "deep_light"]
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
    deepgram_url: str = "wss://api.deepgram.com/v1/listen"
    stt_stall_s: float = 3.0
    # A stream silent for stt_stall_s gets a Finalize first and this long to answer before it
    # is reconnected (stt/resilient.py). None: reconnect at once.
    stt_nudge_s: float | None = 1.0
    stt_failures_before_switch: int = 2
    stt_keepalive_s: float = 5.0

    # Session lifecycle and limits (ARCHITECTURE.md section 1).
    first_message_timeout_s: float = 5.0
    resume_window_s: float = 120.0
    idle_end_s: float = 600.0
    # An interview whose other side said goodbye ends after this long with no speech on either
    # side (2026-10-29: a session outlived its call by an hour, the next call starting 7.2
    # minutes later, and answered another candidate's interview). None: idle_end_s only.
    idle_end_after_close_s: float | None = 90.0
    # With no speech from the mic at all once the other side has talked this long, the status
    # badge says the mic is not heard (2026-10-29: one computer sent none for two whole
    # interviews). None: off.
    mic_silent_warn_s: float | None = 120.0
    # The mirror: the user's mic has this much speech and no speech at all has come from the
    # call. The badge says the call's audio is not coming through. None: off.
    call_silent_warn_s: float | None = 120.0
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
    # A screenshot message: up to 6 shots of at most 500,000 base64 characters each.
    max_screenshot_bytes: int = 4 * 1024 * 1024
    ws_ping_interval_s: float = 30.0
    ws_ping_timeout_s: float = 60.0  # with the 30 s interval: a socket silent 90 s is closed
    store_buffer_max: int = 50_000
    # Retained history cannot be evicted like replay events. An outage that fills either
    # budget ends the session explicitly, leaving accepted entries available to drain.
    retained_log_max_entries: int = Field(default=1000, ge=1)
    retained_log_max_bytes: int = Field(default=2 * 1024 * 1024, ge=1)
    # A slow host socket is disconnected resumably when its outbound queue fills.
    host_send_max_entries: int = Field(default=500, ge=1)
    host_send_max_bytes: int = Field(default=2 * 1024 * 1024, ge=1)
    # Server-side usage checkpoints and the price snapshot attached to each live session.
    # These calculate usage estimates only; payment collection is handled independently.
    usage_interval_s: float = Field(default=10.0, gt=0)
    billing_price_version: str = Field(default="live-usd-v1", min_length=1, max_length=128)
    billing_currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    billing_rate_minor_per_hour: int = Field(default=500, ge=0, lt=2**63)

    # Guest seats (ARCHITECTURE.md, "Guest seats"). Off: /v1/seat refuses every guest (4412)
    # and host sessions never look for seats; the host's call is unchanged either way.
    seats_enabled: bool = True
    seat_max_guests: int = 2  # admitted at once, per session (the API enforces it first)
    # How often a session with no seats looks for its first one, and how long one blocking
    # read waits (so a removal or the end of the call reaches a guest within about this).
    seat_poll_s: float = 1.0
    seat_block_ms: int = 1000
    # The host's view snapshot is written at most this often, and only once a seat exists.
    seat_view_interval_s: float = 2.0
    seat_queue_max: int = 5000  # a guest further behind than this gets a fresh snapshot
    seat_note_ttl_s: float = 60.0  # a note older than this when the host's session reads it
    seat_note_min_interval_s: float = 1.0
    seat_notes_per_minute: int = 30
    seat_view_wait_s: float = 1.5  # how long a join waits for the host's first snapshot

    def prepared_threshold(self, model_id: str | None) -> PreparedThreshold:
        found = self.prepared_answer_thresholds.get(model_id or "")
        return found or PreparedThreshold(min_similarity=self.prepared_answer_min_similarity)

    @field_validator("adaptive_expert_route", mode="before")
    @classmethod
    def _adaptive_expert_profile(cls, value: Any) -> Any:
        # Keep an explicit environment JSON null distinct from an omitted field.
        return json.loads(value) if isinstance(value, str) else value

    @field_validator("prepared_answer_thresholds", mode="after")
    @classmethod
    def _merge_thresholds(cls, value: dict[str, PreparedThreshold]) -> dict[str, PreparedThreshold]:
        return {**DEFAULT_PREPARED_THRESHOLDS, **value}

    @field_validator("deep_pass_auto_types", "prewarm_jobs", mode="before")
    @classmethod
    def _gateway_lists(cls, value: object) -> object:
        return split_list(value)
