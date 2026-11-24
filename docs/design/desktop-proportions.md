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

The workspace opens at 800 × 672 logical pixels. At launch, fit it to the current monitor’s usable area, accounting for display scaling and leaving 24px around the window where possible. Center within that usable area so the taskbar remains clear. Apply this only at startup; navigation must preserve manual resizing, and returning from compact mode restores the previous full size. The live overlay keeps its separate sizing. Start call uses a 112 × 32px desktop button; its accent fill supplies emphasis without extra height, and touch input retains a 48px target.

Size short selectors for their content: Company is at most 352px; Application is at most 528px for the combined company and role. Keep Add/Manage 8px beside the field. Both shrink with narrow windows. Reading and editing areas can still use the full content width.

Shared segmented controls use an accent outline and tint for their selected option in both themes. Call type uses compact outlined choices with icons, a visible radio indicator, and an accent border/tint for selection. Keep it beside Application/Company on desktop, using five columns for Call type and seven for context below an 800px content width; stack below a 680px content width. Status messages belong directly beneath their selector. Empty job descriptions are a short text hint; populated descriptions grow to a bounded scroll region. Agenda starts at 96px instead of stretching to fill the page.

## State stability

The Call audio summary keeps the same height in idle, editing, testing, and failure states. Device feedback uses the existing label line and a meter within the control; full messages remain available through focus/hover help and live announcements. Keep test-button width fixed through its loading state.

The assistant picker opens with a search field and a compact selection count. Show matching people or necessary input guidance as the user types; avoid repeating the help text from the parent control.

Visual verification should include populated, empty, and failed-loading pages, 400/920/1280px windows, both themes, touch emulation, dialog alignment, keyboard focus, and expanded Customize. At the 800 × 672 default size and font scale, Call should fit without scrolling its body.
