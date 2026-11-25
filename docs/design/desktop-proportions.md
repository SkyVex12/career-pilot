# Desktop proportions

The desktop UI uses a compact control scale with a clear hierarchy, rather than applying the same large dimensions to every element.

| Element | Desktop height | Usage |
| --- | --- | --- |
| Utility control | 28px | Copy, Edit, Add, refresh, small segmented choices |
| Standard control | 32px | Inputs, selects, page actions, Start call, and content tabs |
| Workspace navigation | 36px | Flat tabs with a bottom indicator and room for icons |
| Emphasized action | 40px | Larger dialog actions |
| Touch control | 48px minimum | Shared controls and navigation when the primary pointer is coarse |

Use the shared control tokens in ui/tokens.css. Horizontal button padding is 8px for utilities, 12px for standard controls, and 16px for emphasized actions. Avoid vertical padding on single-line buttons: line height plus padding and borders can exceed the intended height. Allow controls to grow when text or accessibility settings require it.

## Spacing and hierarchy

- Keep the responsive twelve-column desktop grid. Call pairs an explicit Call type radio group with the context selector, then follows a top-to-bottom sequence: context details, two compact Customize groups, audio, then the primary action. Use 16px between sections. Customize pairs Language/Answers with Assistants/Transcript in aligned rows: labels sit beside content-sized controls, with 8px row gaps and 32px between groups. Keep Language as a single bounded selector and stack the groups in small windows. Changing it applies to the current call without rewriting the saved profile preference. Customize uses a text/icon/chevron hover treatment, without a full-width background fill; preserve the visible keyboard focus indicator. On a language-loading error, reopening Customize retries without a persistent Retry button.
- Use 4px for labels, metadata, and optical adjustments; 8px between related controls; 12–16px inside groups; and 24–32px between sections. Maintain the 8px rhythm for page insets and macro layout.
- Use roughly 5:4 steps for visual emphasis: 13px controls, 16px section emphasis, and 20px page headings. Secondary card headings use 14px. Keep reading text at 13–14px with comfortable 20–24px line heights.
- Use 6px control corners and 8px card corners. Reserve accent fills and stronger weight for primary actions and selected navigation.
- Dialog header, body, and footer share 24px horizontal insets (16px in narrow windows), with smaller vertical insets so the frame does not compete with the form.

## Window and field widths

The workspace opens at 800 × 672 logical pixels. At launch, fit it to the current monitor’s usable area, accounting for display scaling and leaving 24px around the window where possible. Center within that usable area so the taskbar remains clear. Apply this only at startup; navigation must preserve manual resizing, and returning from compact mode restores the previous full size. The live overlay keeps its separate sizing. On Windows, both windows may sit partly off-screen, but retain at least a 48 × 24 logical-pixel patch of the header or Small launcher in a connected monitor’s work area. Use the smallest position correction across all displays, including negative coordinates and mixed scaling. Protect dragging, resizing, compact shape changes, and display/work-area changes without resizing or activating the window. Start call uses a 112 × 32px desktop button; its accent fill supplies emphasis without extra height, and touch input retains a 48px target.

Size short selectors for their content: Company is at most 352px; Application is at most 528px for the combined company and role. Keep Add/Manage 8px beside the field. Both shrink with narrow windows. Reading and editing areas can still use the full content width.

Shared segmented controls use an accent outline and tint for their selected option in both themes. Call type uses compact outlined choices with icons, a visible radio indicator, and an accent border/tint for selection. Keep it beside Application/Company on desktop, using five columns for Call type and seven for context below an 800px content width; stack below a 680px content width. Field labels and descriptive metadata stay beside their controls; operational notifications float in the bottom-right notification stack. Empty job descriptions are a short text hint; populated descriptions grow to a bounded scroll region. Agenda starts at 96px instead of stretching to fill the page.

Application details give company, role, and pack status their own full-width block. Place actions in a separate compact row so they cannot squeeze the title. Keep the same page inset on narrow screens. Past-call filters use All / Interviews / Work; Work includes stored standup and meeting calls. Transcripts have one top-right Copy action for the complete, time-ordered conversation, with no per-entry practice controls.

## State stability

The Call audio summary keeps the same height in idle, editing, testing, and failure states. Device feedback uses the existing label line and a meter within the control; full messages remain available through focus/hover help and live announcements. Keep test-button width fixed through its loading state.

Quick settings in medium mode open below its 280 × 56px toolbar. Extend only the narrow canvas needed for the menu, cap its height to the monitor work area, and keep the setup page hidden. Closing the menu restores the toolbar bounds; explicit expansion still restores the saved full-window size. Pause automatic collapse while the menu is open. Medium also stays expanded while the native window has focus, even after the pointer leaves. After focus moves elsewhere, Small expands temporarily on hover and collapses on leave. Clicking or keyboard-selecting the window keeps Medium expanded until focus leaves again. Clicking the ruby in Medium selects it without opening Full; use the expand arrow for Full.

The assistant picker opens with a search field and a compact selection count. People use a single 32px row with the name and email side by side; long values truncate, with the full identity available on hover. Show matching people or necessary input guidance as the user types; avoid repeating the help text from the parent control.

Visual verification should include populated, empty, and failed-loading pages, 400/920/1280px windows, both themes, touch emulation, dialog alignment, keyboard focus, and expanded Customize. At the 800 × 672 default size and font scale, Call should fit without scrolling its body.

## Scrolling and overlay interactions

Hide scrollbar tracks and their gutters throughout the app, including dialogs, menus, transcript panes, and text editors. Overflow keeps native wheel, touch, and keyboard scrolling. A subtle surface fade and directional chevron at each unfinished edge indicate more content; remove the cue at the corresponding boundary. Clip cues to their actual scroll region and hide them behind modal surfaces. Overflow containers without their own keyboard interaction gain a visible focus stop.

Operational errors, session warnings, and success notices share the bottom-right stack and never add rows to a form. A notice can be dismissed, and hidden pages do not continue showing their notifications. Dialogs own a notification outlet inside the focus trap, positioned at the window edge so retry actions stay keyboard-accessible. Field validation and saved document content remain with their fields.

The overlay combines status, a live connected-time timer, language, Assistants, and window actions in one 40px header. At widths of 600px or less, call tools wrap into a second row while window actions remain at the top right. The timer shows minutes and seconds (hours for long calls), pauses during reconnection, and reconciles to confirmed time when the call ends; account totals remain server-confirmed.

The host overlay keeps conversation content visible without a Screen tools tab or a read-only Live audio popover. Pending screenshots appear as a floating strip immediately above Ask anything, with only thumbnails and individual remove actions. The composer capture icon and existing shortcuts add screenshots; Answer sends pending screenshots when the text input is empty. Typed questions retain their existing behavior. Guest shared-screen viewing remains available when permitted.

Assistants and Quick settings open on hover without moving keyboard focus. Moving into the panel keeps it open; leaving closes hover-opened panels, while click-opened panels remain until dismissed. Answer-format choices have equal widths within the settings panel.

## App lifecycle

With Stealth off, the visible workspace or live overlay keeps its taskbar/dock entry. Reopening from the tray or another app instance brings an active hosted/guest call overlay forward. Left-clicking the tray shows Pause/Resume notifications and Exit; right-clicking brings CoRuby forward, with an active call opening its overlay. The pause action controls automatic call-detection notifications. Windows and macOS use a compact menu with a red Exit label; Linux retains its native tray menu.

Quit immediately hides app windows and the tray icon, then finishes session shutdown in the background. Local audio stops, the host sends session.end, and the process exits after confirmation. If acknowledgement is lost, retry ending only that exact session with the original account. A failed cleanup restores the app with a retryable notification. Leaving a guest session never ends its host’s call. A simultaneous startup is allowed to settle before cleanup; new starts and updates are blocked during shutdown.

## Returning to tabs

Leaving Past calls closes the open detail, so returning shows the list. Leaving an ended-call review returns Call to Start a call; a running call stays live. Unfinished call-note drafts stay in memory for the current account/profile and reappear when that call is opened again, without being saved automatically.

## Shared window settings

Main and overlay use one set of Transparency, Keep on top, Click-through, Stealth, Text size, and Answer format preferences. A change in either quick-settings panel updates the other window even while it is hidden. Text size also applies to workspace typography and History replay; answer-format changes update Customize and live suggestions.

The transparency slider moves from clear on the left (10% opacity / 90% transparency) to solid on the right. Show Clear and Solid endpoints without a numeric readout. Text stays opaque. Save opacity once for both windows, migrating the last overlay monitor’s value for existing installs; moving monitors never changes it. Geometry and compact view remain specific to each window.

Keep on top applies to both native windows and survives restart. Click-through is shared for the current app run, with one recovery shortcut for both windows. Consent prompts temporarily remain interactive and on top without changing those preferences. Native failures are reported; unsuccessful on-top changes roll back both windows.

Answer cards show content and source references without Copy, Copy detailed, or feedback buttons. An empty action footer reserves no space.
