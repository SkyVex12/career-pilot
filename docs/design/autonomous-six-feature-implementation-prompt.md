# Autonomous implementation prompt for Materials 01-06

Copy the prompt below into an implementation task. Preparing this file does not start implementation. The original workspace contains the requirements, including files that may not yet be committed.

## Master prompt

```text
Implement all six agreed Career Pilot feature materials end to end. This is an implementation request, not a request for another plan. Work through the entire program autonomously, step by step, without asking me to approve routine decisions or continue after each milestone.

SOURCE WORKSPACE

C:\Users\A\WorkSpace\career-pilot

Read these authoritative requirements from that workspace before creating an isolated checkout:

- docs/design/material-01-background-calls-and-calendar.md
- docs/design/material-02-practice-interviews.md
- docs/design/material-03-central-ask.md
- docs/design/material-04-mobile-companion.md
- docs/design/material-05-personal-ai-context.md
- docs/design/material-06-call-language-selection.md
- docs/design/six-feature-implementation-plan.md

Also inspect applicable repository instructions and current code. The materials' statements that saving them did not start implementation describe their earlier drafting stage; this prompt now authorizes implementation. The plan's individual-task template does not limit this request to one material.

1. AUTONOMY AND FINISH LINE

Your objective is the complete, integrated implementation of all six full materials, with meaningful validation and usable delivery artifacts.

- If the environment provides an explicit persistent-goal tool such as create_goal, create a goal for this full objective and use its supported continuation mechanism. Reuse an existing matching goal when appropriate. Mark it complete only when the objective is actually achieved; a milestone, checkpoint, or external blocker is not completion. If such tools are unavailable, continue normally with the progress ledger.
- Make routine architecture, UI, library, implementation, and sequencing decisions yourself. Prefer the simplest maintainable approach that meets the requirements.
- Record important assumptions and decisions, then continue. Do not ask me which ordinary implementation approach to choose.
- Milestones and first releases organize the work. They are not permission gates or stopping conditions.
- After completing and checking one milestone, immediately continue to the next unblocked milestone in this same task.
- Do not stop at a plan, scaffold, mock, proof of concept, one successful feature, or the first usable slice of every feature.
- Preserve every full-material requirement in a checklist. Complete the remaining requirements after each first slice; do not silently reduce the agreed scope.
- Send concise progress updates describing useful findings, completed behavior, and the next step. Progress updates do not require a reply.
- If I send a correction, incorporate it while retaining the overall objective unless I explicitly stop or replace it.
- Respect the execution environment's permissions and limits. Do not bypass a required approval or claim that this prompt can extend a runtime beyond its actual limits.

2. PRESERVE CURRENT WORK AND SET UP THE INTEGRATION

- Inspect and record the initial Git status, current branch, and relevant existing changes.
- There may be concurrent work in shared gateway, protocol, prompt, or desktop files. Do not reset, clean, overwrite, stage, or commit unrelated changes.
- Use an isolated codex/ implementation branch and worktree when appropriate. Choose the intended base from repository context and record it.
- The requirement documents may be untracked. Read them from the source workspace and deliberately carry their exact contents into the implementation checkout. A new worktree does not automatically include them.
- Integrate required existing changes deliberately; do not assume every dirty file belongs to this task or exclude a required change without checking.
- Make focused local commits of your own completed work so progress is recoverable. Verify staged files before committing.
- Use one coherent integration branch for the finished program. Parallel contributors may use separate worktrees/branches, but their work must be reviewed and integrated before it counts as delivered.

3. DURABLE PROGRESS

Create and maintain docs/implementation/six-feature-progress.md in the implementation checkout.

Track:

- Every substantive requirement from Materials 01-06, with a stable identifier and a status: pending, in progress, implemented, verified, or externally blocked.
- Relevant files/commits and acceptance criteria.
- Validation evidence, separating fixture tests, actual provider checks, native builds, and real-device tests.
- Architectural and product defaults chosen, with short reasons.
- Remaining work and external blockers, including exactly what is missing.
- The next concrete action, current branch/worktree, and commands needed to resume.

Update this record after meaningful milestones and before any unavoidable interruption. On resumption or context compaction, reconcile the record with actual files and Git state, then continue from the next unfinished action. Do not restart completed work merely because conversation context was shortened.

4. DELIVERY ORDER AND PARALLEL WORK

Use this default order:

Material 06 -> Material 03 -> Material 05 -> Material 02 -> Material 01 -> Material 04.

Adjust sequencing where real dependencies or available hardware justify it. Calendar, practice, MCP, and mobile do not need to become artificial prerequisites for one another.

For each material:

1) Inspect the existing behavior and establish acceptance criteria.
2) Implement a complete first user journey, including persistence and failures.
3) Validate it, fix defects, and record evidence.
4) Complete its remaining agreed requirements, or record precise external blockers and continue independent work.
5) Integrate the result and proceed automatically.

Delegate bounded independent work to subagents when useful. Do not create user-facing tasks that require me to coordinate the program. Give one integration owner responsibility for shared protocol/schema changes, migration numbering, generated types, app shell/API client, gateway session lifecycle, core prompts, and native settings. Avoid overlapping edits to those files.

5. DEFAULTS TO AVOID ROUTINE QUESTIONS

- Reuse the existing backend, authentication, storage, and configured providers when they fit. Change architecture when necessary, with a specific reason; avoid a speculative platform rewrite.
- Material 06: one selected spoken language per call. Keep English as the existing default. Choose a small initial non-English set from the configured recognition provider's officially supported, testable languages. Use Spanish and Vietnamese as starting priorities where supported; record and explain any necessary substitution. This is a default to unblock implementation, not a claim of verified support. Make the catalog extensible and expose only languages that pass the required checks. Do not wait for me to choose the first languages.
- Material 03: begin with existing authorized facts/documents, then finish cross-call retrieval and the remaining conversation/source lifecycle. General Ask must work without starting a call.
- Material 05: implement Career Pilot as an MCP server with the specified request/publish workflow. Prefer Codex for the first locally testable host when available, then validate ChatGPT through its supported remote connection. Use current official documentation. Do not assume MCP or sign-in exposes private chat history. Keep briefs private to the authorizing account and reuse them without a second AI processing pipeline.
- Material 02: reuse an existing suitable speech-generation provider when configured; otherwise implement a configurable adapter for a currently documented provider without purchasing a plan. Keep practice microphone capture separate from system-call audio and generated interviewer playback. Finish adaptive questions, feedback, retries, and progress rather than leaving a static interview script.
- Material 01: use native background/call signals without a browser extension. Calendar access remains optional. Choose the first calendar provider from available configuration; absent configuration, implement Google first, then Outlook. Retain the full emulator and phone-link support targets with honest verification status.
- Material 04: choose one maintainable mobile approach that produces actual iOS and Android apps and fits the repository and current platform requirements. Deliver desktop capture with phone display/control first, then the remaining mobile requirements. A responsive webpage alone does not satisfy the mobile material.
- Preserve Windows support and macOS 13+ on Apple Silicon and Intel. Guard newer native APIs and test compatible paths.
- Keep interface changes consistent with the current ruby branding and compact UI. Preserve dragging, window modes, overlay lifecycle, profile switching, and existing call behavior.
- Carry account/profile scope, source references, context versions, and selected language consistently across features.

Check current official documentation when provider or platform behavior matters. Do not guess eligibility, API support, pricing, OS availability, or distribution requirements.

6. VALIDATION AND COST DISCIPLINE

- Implement real authentication, backend behavior, persistence, cancellation, deletion, and failure states. Keep production controls honest about availability.
- Use deterministic transcript/audio fixtures and mocked provider boundaries for routine tests. Mocks are test tools, not evidence that a production integration works.
- Run affected checks during development and all applicable required checks before final delivery. Fix failures caused by your changes; do not weaken assertions or disable required CI to obtain green results.
- Use a small number of deliberate live checks with existing authorized accounts where needed. Do not repeatedly spend model credits or rebuild unchanged native artifacts.
- Keep expensive packaging/signing in relevant existing release workflows, reuse caches, and avoid pushing every intermediate edit when pushes trigger CI.
- Do not purchase services, upgrade plans, or provision significant new paid infrastructure as an assumed implementation step. Prepare the required configuration and continue independent work if such a dependency is unavoidable.
- Do not run destructive migrations against production data. Validate migrations and recovery with appropriate test data and supported environments.
- Keep background startup from opening recording/transcription or unnecessarily waking the backend. Calendar matching and MCP validation should use ordinary code.
- Mobile attachment must reuse the desktop session instead of starting another paid audio pipeline.
- Measure relevant latency and usage; do not claim guaranteed savings without evidence.

Before delivery, exercise cross-feature journeys, including selected-language calls, profile changes, a personal-AI brief used by Ask and a live session, practice-to-call transitions, background suggestions without duplicate sessions, mobile reconnect/commands, and source deletion/access changes. Cover the complete accepted UI and data lifecycle, not just isolated happy-path functions.

7. EXTERNAL BLOCKERS WITHOUT UNNECESSARY STOPS

Missing credentials, provider approval, OAuth registration, signing identity, store accounts, Mac/iOS tooling, or physical devices can prevent activation or verification. They do not automatically prevent the rest of the implementation.

For each real blocker:

- Record exactly which requirement is blocked and the evidence.
- Complete the feasible adapters, UI, configuration, tests, error handling, and setup documentation.
- Keep unavailable functionality clearly identified rather than making a placeholder look operational.
- Continue all other unblocked work, including other materials.
- Revisit the blocker after available engineering work is finished.

Do not invent credentials, read private credential stores to bypass authorization, claim a simulator proves real hardware behavior, or mark an unverified platform complete. Use existing authorized build/test hosts where available.

Ask me only when an indispensable secret, permission, external action, or genuinely consequential unresolved choice cannot be obtained or reasonably resolved, and no useful independent work remains. Bundle remaining requests into one concise actionable list. Never ask "Should I continue?" or require approval between normal phases.

8. DELIVERY AND CONTINUATION

Complete one final reconciliation against every requirement in all six materials. Review integrated changes, fix material defects, and update setup, compatibility, and release documentation.

Deliver:

- The integrated implementation and focused commits.
- Available installable development/release artifacts, clearly labeled for actual signing and distribution status.
- Appropriate automated and real-environment validation evidence.
- Exact setup instructions for any unavoidable external configuration.
- The completed progress record with every remaining limitation or blocker explicit.
- A concise final report of what works, where to find it, what was tested, and any indispensable external steps still outstanding.

When an authenticated repository remote is available, push the implementation branch and create or update one focused draft PR after integration checks, using local commits for intermediate checkpoints to reduce CI usage. Include the full scope and honest validation status. Follow any existing explicit authorization for merging or production release; otherwise finish the reviewable implementation and artifacts without treating a production merge as a prerequisite for completing engineering work.

Do not end the task while useful authorized implementation, integration, or validation remains. If the runtime itself must stop, save the exact next action and report that work is incomplete; do not declare success merely because a time/context/tool limit was reached. A progress record enables resumption but does not imply the agent runs after its runtime has ended.

Start now by inspecting the repository and all six materials, creating the requirement ledger, and implementing Material 06. Continue through the entire program without routine interruptions.
```

## Short launch prompt

Use this in a task that can access the source workspace, including a task starting in another local worktree:

```text
Read C:\Users\A\WorkSpace\career-pilot\docs\design\autonomous-six-feature-implementation-prompt.md and execute its Master prompt. Create a persistent goal for implementing all six materials if that capability is available, and continue autonomously from one completed milestone to the next. Preserve existing work, maintain the progress ledger, and keep going until all feasible implementation and validation is complete. Do not stop after planning or ask for routine phase approvals.
```
