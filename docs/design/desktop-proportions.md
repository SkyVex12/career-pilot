# Desktop proportions

The desktop UI uses a compact control scale with a clear hierarchy, rather than applying the same large dimensions to every element.

| Element | Desktop height | Usage |
| --- | --- | --- |
| Utility control | 28px | Copy, Edit, Add, refresh, small segmented choices |
| Standard control | 32px | Inputs, selects, page actions, navigation and content tabs |
| Emphasized action | 40px | Start call and large action buttons |
| Touch control | 48px minimum | Shared controls and navigation when the primary pointer is coarse |

Use the shared control tokens in ui/tokens.css. Horizontal button padding is 8px for utilities, 12px for standard controls, and 16px for emphasized actions. Avoid vertical padding on single-line buttons: line height plus padding and borders can exceed the intended height. Allow controls to grow when text or accessibility settings require it.

## Spacing and hierarchy

- Keep the responsive twelve-column desktop grid with 24px gutters; stack secondary sections at their existing narrow-window breakpoints.
- Use 4px for labels, metadata, and optical adjustments; 8px between related controls; 12–16px inside groups; and 24–32px between sections. Maintain the 8px rhythm for page insets and macro layout.
- Use roughly 5:4 steps for visual emphasis: 13px controls, 16px section emphasis, and 20px page headings. Secondary card headings use 14px. Keep reading text at 13–14px with comfortable 20–24px line heights.
- Use 6px control corners and 8px card corners. Reserve accent fills and stronger weight for primary actions and selected navigation.
- Dialog header, body, and footer share 24px horizontal insets (16px in narrow windows), with smaller vertical insets so the frame does not compete with the form.

## State stability

The Call audio summary keeps the same height in idle, editing, testing, and failure states. Device feedback uses the existing label line and a meter within the control; full messages remain available through focus/hover help and live announcements. Keep test-button width fixed through its loading state.

Visual verification should include populated and empty pages, 400/920/1280px windows, both themes, touch emulation, dialog alignment, keyboard focus, and expanded Customize. At 920 x 720 with the default font scale, Call should fit without scrolling its body.
