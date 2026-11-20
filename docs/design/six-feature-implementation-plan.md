# Implementation plan for Materials 01-06

Status: Proposed execution plan saved on 2026-11-19. The six feature materials are agreed directions. This document organizes future implementation; it does not start product development, deployment, or paid provider calls.

## Feature index

| Material | Scope |
| --- | --- |
| [01: Background calls and calendar](material-01-background-calls-and-calendar.md) | Native background operation, call suggestions without an extension, and optional meeting preparation. |
| [02: Practice interviews](material-02-practice-interviews.md) | Spoken rehearsal, adaptive questions, grounded feedback, retries, and progress. |
| [03: Central Ask](material-03-central-ask.md) | Persistent conversations using authorized career information, work knowledge, and saved calls. |
| [04: Mobile companion](material-04-mobile-companion.md) | iOS and Android companion for the existing desktop capture session, followed by standalone and other mobile experiences. |
| [05: Personal AI context](material-05-personal-ai-context.md) | User's AI publishes compact sourced briefs through Career Pilot MCP tools. |
| [06: Call language selection](material-06-call-language-selection.md) | One selected spoken language per call across recognition, detection, answers, and retained outputs. |

## Recommended order

For a single implementation stream: **06 -> 03 -> 05 -> 02 -> 01 -> 04**.

This is a delivery recommendation, not a chain of hard technical dependencies. Background detection and practice do not need Central Ask or MCP to work. Mobile's core companion does not need those features either, but benefits from stabilizing the shared session behavior before extending it to another device.

| Order | First usable milestone | Remaining work needed for the full material |
| --- | --- | --- |
| 1. Language selection | One selection carried through a complete call, remembered per profile, with English and an agreed tested initial language set. | Expand the verified language set and finish the full failure, retrieval, and compatibility matrix. |
| 2. Central Ask | Saved text conversations using selected profile/company/application context, existing facts/documents, and clickable sources. | Cross-call transcript retrieval, contextual entry points, full conversation/source lifecycle, and additional available context sources. |
| 3. Personal AI context | One verified AI host, request/publish MCP tools, versioned context card, manual refresh handoff, and use of the brief in a real call or Ask. | Other supported hosts, complete deletion/access behavior, and a clear paste/import fallback. Direct invocation remains a later provider-specific option. |
| 4. Practice | A short behavioral voice interview, selected application context, explicit end-of-answer control, text fallback, saved attempts, and grounded end-of-session feedback. | More interview types, adaptive depth, retries/progress, prior-call questions, full playback/capture recovery, and tested language/voice combinations. |
| 5. Background and preparation | Background lifecycle, tray/menu-bar controls, and opt-in suggestions for a small verified application/OS set. | Broaden verified browser/emulator/phone-link support and add optional Google/Outlook calendar preparation in separate increments. |
| 6. Mobile | Installable iOS/Android apps that display and control the same desktop call, with headphone compatibility and reconnect recovery. | Standalone room-microphone mode, Ask/practice/history/preparation integrations as ready, and full device/release validation. |

A first usable milestone is not completion of its full material. Track deferred requirements explicitly and complete or separately agree any scope change before closing the feature. Do not discard the emulator, phone-linking, second mobile platform, or remaining calendar requirements because an initial slice works.

## Small shared contracts

Establish these incrementally in the first feature that needs them. Do not create a separate platform project or migrate infrastructure preemptively.

- Account, selected profile, company/application, and audience remain distinct. A profile can be shared; private imported material still belongs to its authorizing account.
- A session has an effective spoken language that remains stable across reconnects.
- Context items have stable source references, scope, version, preparation/source time where known, and deletion/access behavior.
- Live calls, practice sessions, and Ask conversations have distinct lifecycles while reusing suitable storage, retrieval, and generation components.
- Mobile attaches to an existing session with one capture owner. Ordered events, replay, and acknowledged idempotent commands must be verified before real multi-device use.

Reuse the existing backend, authentication, and knowledge paths when suitable. Change a component when the feature demands it; do not require a new vector database, a universal source connector framework, or a microservice split for this plan.

## Work organization and parallelism

Use one bounded implementation task and reviewable branch per milestone. The saved materials contain implementation prompts; they are requirements to read, not six independent commands to launch blindly against the same checkout.

1. Inspect current main/base, repository instructions, existing changes, and the feature's affected paths.
2. Define the milestone's acceptance criteria and record all remaining full-material requirements.
3. Agree the small shared schema/API changes needed for that milestone.
4. Implement a complete user journey, including persistence and failure behavior, then test it.
5. Review a focused PR with clear evidence and remaining limitations. Merge and release through the existing authorized workflow.
6. Integrate against the resulting base before beginning another milestone that changes the same contract.

When using parallel implementation agents or tasks, use separate worktrees/branches. Give one integration owner responsibility for shared protocol changes, migration numbering, generated types, the desktop app shell/API client, gateway session lifecycle, core prompts, and native settings. Assign parallel work by module and merge dependent changes in order.

Useful independent work after interfaces settle:

- Central Ask or MCP backend work alongside native background detection.
- Practice UI and turn logic alongside background lifecycle work, with coordinated audio ownership.
- Mobile layout and device setup alongside backend companion event/command work after the event contract is defined.

Avoid simultaneously rewriting the app shell, session protocol, or shared prompts in six different tasks. Parallel read-only reviews and test design can start earlier than overlapping implementation.

## Verification and cost controls

- Use deterministic audio/transcript/provider fixtures for routine tests. Run small intentional live checks for behavior that fixtures cannot establish.
- Run checks appropriate to each changed area during development and preserve required CI before merging.
- Keep native packaging/signing in the existing relevant manual/release workflows; cache dependencies and cancel obsolete builds where configured. Do not remove required correctness checks merely to save minutes.
- Keep MCP import validation deterministic and brief refresh user-triggered initially.
- Keep background detection and calendar matching in ordinary code; background startup must not open paid recognition sessions or wake the backend unnecessarily.
- Start practice feedback with one focused end-of-session report, then assess the value and cost of more frequent feedback.
- Reuse the same transcription/session when a phone attaches; do not create a second paid capture pipeline.
- Add the selected language to the existing request path; do not create a continual translation pipeline.
- Record actual latency and provider usage where applicable instead of estimating savings as guaranteed.

For every milestone, retain a concise result: what works, relevant automated checks, actual platform/device checks, unresolved production configuration, and which material requirements remain.

## Decisions and external setup

Resolve these at the relevant milestone, not as a large prerequisite questionnaire:

- Material 06: the initial non-English language priorities and the configured recognition model's verified capabilities.
- Material 05: the first target AI host, actual account/tool availability, and an authenticated stable HTTPS endpoint for remote hosts. Check commercial distribution conditions for any later direct provider adapter.
- Material 02: a verified speech-generation provider and voice/language combinations, plus microphone/playback testing.
- Material 01: calendar OAuth registration and native detection tests on available Windows/macOS machines. Incoming call, active call, and accessible audio are separate support claims.
- Material 04: iPhone/Android test devices, signing/distribution setup, and encrypted reachable production transport for companion connections.

Configuration or hardware gaps should be documented without presenting mocks as a working production integration. Prefer completing independent work while a narrowly defined external step remains pending.

## Reusable implementation-task prompt

Copy this into an implementation task after choosing a milestone. Replace the bracketed material and milestone fields with the exact selection.

```text
Implement [material number and name], milestone [specific first release or follow-on scope], in Career Pilot.

Read docs/design/six-feature-implementation-plan.md and the matching material file. Inspect current repository instructions and code before editing. Preserve user changes and use an isolated codex/ branch/worktree when appropriate. Do not start unrelated materials.

Complete the selected user journey end to end with its required authentication, persistence, lifecycle, error handling, and meaningful validation. Reuse suitable existing components and keep the UI consistent. Coordinate shared protocol/schema changes and keep expensive provider calls and native packaging bounded.

Track every requirement from the full material: completed in this milestone, still remaining, or blocked by a specific external configuration/hardware dependency. A smaller first release does not silently replace the agreed full feature.

Finish with the concrete changes, validation evidence, remaining limitations, and a focused reviewable diff. Follow the user's explicit instructions for committing, PR creation, merging, and deployment.
```

Suggested first task: **Material 06, first complete selected-language call**, starting with the language contract and checking which initial languages the production recognition configuration can support. The priority language list remains a product choice; all other ready work can be scoped around that decision.
