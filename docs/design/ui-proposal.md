# Desktop UI proposal

Stage 1 of the UI redesign, answering [ui-audit.md](ui-audit.md). Nothing here changes product
code yet. Stages 2–4 build it once this is agreed. **Decisions I need from you** are collected at
the end.

**Goals, in order:**

1. The overlay is readable at a glance mid-sentence.
2. Each screen does one job, with the action you came for above the fold.
3. One look and one behaviour for states, forms and confirmations across every screen.
4. Keyboard and contrast at WCAG AA everywhere.

**Non-goals:** new features, changes to the protocol or API, changes to what the Rust core does.
Every behaviour change the UI needs is listed under [Behaviour changes](#behaviour-changes) and
again in each stage's PR.

## 1. Navigation and information architecture

### Today → proposed

| Today | Proposed | Why |
|---|---|---|
| 6 top tabs + hidden Debug | **Left sidebar** in three groups: *Call*; *Prepare* (Applications, Resumes, Work); *Review* (History). Settings at the foot. | Grouped by intent. Room for a live badge, and for the profile and status that crowd the tab bar now. |
| "Session" tab: setup form in every state | **Call**: four states on one screen: *setup*, *ready* (next up + Start), *live*, *just ended* | Each state shows what it is for (audit: Session). |
| Interviews: a form and a list at the bottom of Applications | **Upcoming interviews** at the top of Applications. Scheduling moves into the application ("Schedule interview…" dialog). The next one is also on Call. | Interviews belong to an application and are what you look for first. |
| Inline application editor above the list | **List and detail side by side** (stacked below 900 px). Add is a dialog with the minimum fields. | Keeps the list in view and shortens the page (1,914 → ~780 px). |
| Pull from Career-OS as an inline panel | **Dialog** | It behaves like one. |
| Resumes: "Replace it" form always open | **Base resume card** with Replace… and Rename. Tailored resumes **grouped by application**, linking to it. | Smaller page, one way to do each thing. |
| Work: company pills + segmented sub-tabs | **Company select in the page header**, then tabs Log · Material (n) · Facts. Edit, Archive and Delete in a "⋯" menu. | Two levels instead of three. Destructive actions out of easy reach. |
| History detail replaces the list, with "Close" | **Detail page with ← History**, tabs Summary · Replay · Transcript | Back-navigation as expected. No nested scroll box. Transcript not shown twice. |
| Settings: one long page, three save models | **Sections with a sub-nav**: General · Account · Audio · Overlay & hotkeys · Advanced. One save model ([§5](#settings-save-model)). | Each section fits on screen. Devices and echo cancellation together under Audio. |
| Profile switch and status badge in the tab bar | **Sidebar footer**: profile, connection status, version and update | Fixed place, no header shift while loading. |
| Locked (Career-OS required): the same callout on every tab | **Setup checklist on Call** with the link step highlighted. Other screens show an empty state pointing there. | One clear path. |

**Merged or cut:**

- **Merged:** Interviews into Applications. Devices into Settings → Audio, with Call showing a
  one-line device summary and a Test button.
- **Cut:** the "Refresh" link on Call (it already reloads when shown). The duplicate Transcript
  `<details>` in a session (it becomes a tab). Most helper notes, which become short hints or move
  behind "Why?" tooltips.

**Kept as is:** Debug (hidden, Ctrl+Shift+D), the update bar's behaviour, the profile model, and
every route into the overlay.

**Window sizes.** The main window keeps 1080×780 by default and 720×520 at minimum. Below 900 px
the sidebar collapses to icons with tooltips, and list/detail stacks.

## 2. Design system

All of it lives in CSS custom properties in `src/ui/tokens.css`. Components read only tokens: no
raw colours or pixel sizes outside that file.

### Colour tokens

Every pair below was checked with the WCAG formula. Text pairs are ≥ 4.5:1 and borders/focus are
≥ 3:1 ([harness/contrast.mjs](harness/contrast.mjs) prints each ratio).

```css
:root, [data-theme="light"] {
  /* surfaces */
  --bg: #f5f6f8;          --surface: #ffffff;     --surface-2: #f1f3f6;   --surface-sunken: #eceff3;
  --border: #dfe3e8;      --border-strong: #858e9b;            /* inputs: 3.31:1 on surface */
  /* text */
  --text: #14171c;        /* 17.96:1 */
  --text-2: #4a5361;      /* 7.77:1  secondary */
  --text-3: #626b79;      /* 5.39:1 on surface, 4.85:1 on surface-2: hints, meta */
  /* accent and states: fg on its soft background */
  --accent: #2457d6;      --accent-hover: #1c47b3; --accent-soft: #e9effc; --on-accent: #ffffff; /* 6.16:1 */
  --success: #14753a;     --success-soft: #e8f5ec; /* 5.14:1 */
  --warning: #8a5300;     --warning-soft: #fdf3e1; /* 5.75:1 */
  --danger: #b42318;      --danger-soft: #fdecea;  /* 5.75:1; white on danger 6.57:1 */
  --focus: #2457d6;                                  /* 2px ring, 6.16:1 */
  --scrim: rgb(16 18 22 / 0.45);
  --shadow-1: 0 1px 2px rgb(16 24 40 / 0.06), 0 1px 3px rgb(16 24 40 / 0.08);
  --shadow-2: 0 8px 24px rgb(16 24 40 / 0.16);
}
[data-theme="dark"] {
  --bg: #0f1115;          --surface: #171a20;     --surface-2: #20242c;   --surface-sunken: #12151a;
  --border: #2a2f38;      --border-strong: #646c7a;            /* 3.29:1 */
  --text: #e8ebf0;        /* 14.58:1 */
  --text-2: #b3bbc7;      /* 9.00:1 */
  --text-3: #8d96a4;      /* 5.83:1, 5.21:1 on surface-2 */
  --accent: #7aa5ff;      --accent-hover: #9bbcff; --accent-soft: rgb(122 165 255 / 0.14); --on-accent: #0b1020; /* 7.80:1 */
  --success: #5bd08a;     --success-soft: rgb(91 208 138 / 0.12);  /* 8.99:1 */
  --warning: #f2b84b;     --warning-soft: rgb(242 184 75 / 0.12);  /* 9.74:1 */
  --danger: #ff8a80;      --danger-soft: rgb(255 138 128 / 0.12);  /* 7.63:1 */
  --focus: #7aa5ff;                                                /* 7.79:1 on bg */
  --scrim: rgb(0 0 0 / 0.6);
  --shadow-1: 0 1px 2px rgb(0 0 0 / 0.4);
  --shadow-2: 0 12px 32px rgb(0 0 0 / 0.5);
}
/* The overlay is always dark: it floats over arbitrary content. */
[data-window="overlay"] {
  --ov-panel: 16 18 22;            /* rgb triplet; alpha comes from the opacity setting */
  --ov-text: #f4f6f9;  --ov-text-2: #c3cad4;  --ov-text-3: #98a1ae;
  --ov-fast: #8db4ff;  --ov-deep: #c3a6ff;    /* pass accents: rule and label colour, never body text */
  --ov-live: #5bd08a;  --ov-warn: #f2b84b;    --ov-bad: #ff8a80;
}
```

**Theme.** It follows the OS by default (`prefers-color-scheme`), with an override (System · Light
· Dark) in Settings → General. The choice is kept per computer in `localStorage`, which both
windows share, and applied as `data-theme` on `<html>` before first paint so there is no flash.
The overlay stays dark in both themes.

### Type, spacing, radius, motion

| Token | Value | Use |
|---|---|---|
| `--font` | "Segoe UI Variable Text", "Segoe UI", system-ui | everything; numbers use `font-variant-numeric: tabular-nums` |
| `--text-xs` | 12px / 16px | meta, badges. **The floor: nothing smaller.** |
| `--text-sm` | 13px / 18px | hints, table meta |
| `--text-md` | 14px / 20px | body, inputs, buttons (base) |
| `--text-lg` | 16px / 24px | section titles |
| `--text-xl` | 20px / 28px | page titles |
| `--text-2xl` | 24px / 32px | stat numbers |
| weights | 400 · 500 · 600 | no 700 |
| `--space-*` | 0 · 2 · 4 · 8 · 12 · 16 · 20 · 24 · 32 · 40 · 48 px | margins, gaps, padding (4 px grid) |
| `--radius-sm` / `-md` / `-lg` / `-pill` | 4 · 6 · 10 · 999 px | chips & kbd / inputs & buttons / cards, dialogs / badges |
| `--control-h-sm` / `-md` / `-lg` | 28 · 32 · 40 px | button and input heights |
| `--dur-fast` / `--dur` | 120 ms · 200 ms, `cubic-bezier(.2,0,0,1)` | hover, enter. **0 ms under `prefers-reduced-motion`.** |

**Overlay type** (each multiplied by the A−/A+ text scale, 0.85–1.6, through `calc()` instead
of `zoom`):

| Text | Size | Colour |
|---|---|---|
| Talking points, current question | 18px / 1.45, weight 500 | `--ov-text` |
| Detailed answer, current question | 15px / 1.5 | `--ov-text` |
| Question, current | 14px / 1.4, weight 500 | `--ov-text-2` |
| Earlier questions (collapsed rows) | 14px | `--ov-text-2` |
| Transcript | 14px / 1.5 | `--ov-text`, partial words `--ov-text-3` italic |
| Labels, state, meta | 12px, weight 600, no all-caps tracking below 12px | `--ov-text-3` |

Compact keeps these sizes. It removes chrome, not type size.

### Components

Built in `src/ui/`, each with a vitest + Testing Library test for its states and keyboard
behaviour.

| Component | Variants and states | Accessibility | Built on |
|---|---|---|---|
| **Button** | primary · secondary · ghost · danger · danger-ghost; sm/md/lg; hover, active, focus, disabled, *loading* (spinner, width kept, `aria-busy`) | Icon-only buttons need `label` (TypeScript-enforced) and render `aria-label` and a tooltip | `<button>` |
| **IconButton** | ghost · subtle; sm/md | same | `<button>` |
| **Field** wrapper | label above, hint, inline error, optional "(optional)" tag | `aria-describedby` for hint and error; `aria-invalid`; error announced politely | — |
| **Input / Textarea** | default · invalid · disabled · read-only; textarea autosizes to a max | as Field | native |
| **Select** | native, restyled; with an empty-option placeholder | native semantics, OS dropdown (works well in WebView2) | `<select>` |
| **SegmentedControl** | 2–4 options, sm/md | `role="radiogroup"`, arrow keys | radio inputs |
| **Tabs** | underline (page) · pill (in a card); optional count | tablist, arrow keys, `aria-selected`, lazy panels | **Radix Tabs** |
| **List / ListRow** | static · clickable (button) · selectable; slots: leading, title, meta, trailing; dense | rows are `<button>` or `<a>`, never `<tr onClick>`; selection `aria-current` | — |
| **Card** | plain · interactive · inset; header/body/footer | interactive card is a single button target | — |
| **Dialog** | sm (400) · md (560) · lg (720); scrollable body; footer actions | focus trap, Esc, return focus, `aria-labelledby` | **Radix Dialog** |
| **ConfirmDialog** | standard · typed confirmation (company, account) · danger | AlertDialog role; destructive button not focused by default | **Radix AlertDialog** |
| **Toast** | success · info · error; 5 s auto-dismiss, error stays until dismissed; one action | `aria-live` polite (error: assertive); pauses on hover/focus | **Radix Toast** |
| **Badge** | neutral · info · success · warning · danger; with dot or icon | never colour alone: text or icon always present | — |
| **Banner** | info · warning · danger · success; inline, full width, optional action | `role="status"` or `alert` | — |
| **EmptyState** | icon, title, one line, primary action, optional secondary | title is a heading | — |
| **Skeleton** | line · block · list (n rows) · card | `aria-hidden`; the region gets `aria-busy` and a visually hidden "Loading…" | — |
| **Spinner** | sm · md | `role="status"` label | — |
| **Switch** | on/off, with label and hint | `role="switch"`, `aria-checked`, Space/Enter | `<button>` |
| **Slider** | the overlay opacity | native `<input type=range>`, value text | native |
| **Menu** | the overlay's ⚙ and the Work "⋯" menu | disclosure + `role="menu"`, arrow keys, Esc, return focus | hand-built (~80 lines); Radix DropdownMenu if it fails review |
| **Tooltip** | for icon buttons and truncated text | `aria-describedby`; shows on hover and focus; never the only way to reach information | hand-built, CSS-positioned |
| **Kbd** | a hotkey chip, from the accelerator ("Ctrl+Shift+G") | text | `<kbd>` |
| **HotkeyInput** | records a key combination; clear; conflict and invalid errors inline | announces the recorded combination; Esc cancels | — |

**Patterns every screen follows:**

- **Every list gets three states.** Loading shows a skeleton list of 3 rows in place. Empty shows
  an EmptyState with the next action. An error shows a Banner in the list's place, with Retry.
  The rest of the page stays usable.
- **Forms validate inline.** Required fields are checked on blur and on submit. The submit button
  stays enabled and focuses the first error on press, rather than staying disabled with no reason
  given.
- **Destructive actions confirm.** ConfirmDialog for deletes. Typed confirmation for "everything
  for a company" and the account. No confirmation for reversible actions (archive, reject a single
  fact): they get an Undo toast instead.
- **Long text truncates.** One line with an ellipsis and the full text in a tooltip for list
  titles. Two lines (`line-clamp`) for list meta. Never truncated in detail views. Paths and IDs
  truncate in the middle.
- **Focus.** A 2 px `--focus` outline with 2 px offset on every interactive element in both
  windows, `:focus-visible` only. It also works in Windows contrast themes, because it is an
  outline and not a box-shadow.

## 3. Library: Radix primitives, styled with our tokens

| Option | Bundle (gzip) | Accessibility | Code it replaces | Verdict |
|---|---|---|---|---|
| **Plain CSS + tokens** | +0 kB | All behaviour hand-written: focus traps, Esc, return focus, live regions, tab arrow keys | none; we would write ~600–800 lines of behaviour and own their bugs | Lightest, but the riskiest for accessibility |
| **Tailwind** | +5–10 kB CSS, 0 JS | No help: it is styling only | Replaces our CSS but not the behaviour; adds a build step, and class churn in every component | Doesn't solve the problem we have |
| **Radix primitives + our CSS tokens** (shadcn's approach, without Tailwind) | Dialog, AlertDialog, Tabs, Toast: **≤ 18.3 kB** (measured upper bound, everything exported). All eight incl. DropdownMenu, Tooltip, Switch, Slider: 38.7 kB | Proven focus management, keyboard models and ARIA; React 19 supported | The four behaviour-heavy components; we write the visuals | **Recommended** |

**Recommendation:** Radix for Dialog, AlertDialog, Tabs and Toast only. Everything else is native
or hand-built on the tokens. Tooltip and DropdownMenu bring a positioning engine (about 20 kB
gzip), and the overlay's ⚙ menu doesn't need one: it is a fixed panel. If the hand-built menu
fails keyboard review in Stage 4, Radix DropdownMenu is the fallback.

**Bundle budget.** JS from 104.0 kB to at most 125 kB gzip (+20%). CSS from 5.9 kB to at most
9 kB gzip. Old styles are deleted screen by screen, so CSS should end up smaller. Every PR states
its from → to.

## 4. Wireframes

Low-fi, at the default window size. `[Button]` is a button, `( )` a radio or segment,
`[x]` a checkbox, `▾` a select, `░` a skeleton.

### App shell

```
┌──────────────────┬──────────────────────────────────────────────────────────────┐
│ ● Copilot        │  Page title                                   [page actions] │
│                  │  ──────────────────────────────────────────────────────────  │
│  ◉ Call     ●LIVE│                                                              │
│                  │                                                              │
│  PREPARE         │                     page content                             │
│  ▢ Applications  │                     (max width 880, centred)                 │
│  ▢ Resumes       │                                                              │
│  ▢ Work      (3) │                                                              │
│                  │                                                              │
│  REVIEW          │                                                              │
│  ▢ History       │                                                              │
│                  │                                                              │
│                  │                                                              │
│ ──────────────── │                                                              │
│  ⚙ Settings      │                                                              │
│  Alex Rivera  ▾  │ ← profile switch (locked during a call, with the reason)     │
│  ● Listening     │ ← connection status, text + icon                             │
│  v0.1.16 · ✓     │ ← version; update progress shows here and as the bar         │
└──────────────────┴──────────────────────────────────────────────────────────────┘
  under 900 px: the sidebar collapses to icons (tooltips), footer to icons
```

### Call: setup (first run, signed out, or locked)

```
  Get ready for your first call
  ┌────────────────────────────────────────────────────────────────────┐
  │ ✓  Sign in                         alex@example.com                │
  │ 2  Link Career-OS   required       Paste the one-time code         │
  │      [ code ______________ ] [Connect]        Where do I get it? ⓘ │
  │ 3  Add your resume                 [Upload…]  [Paste text]         │
  │ 4  Check your audio                Mic ▾ [Test]   Call audio ▾ [Test]│
  └────────────────────────────────────────────────────────────────────┘
                                                      [Start call] (disabled
                                                       until 1–2 are done; 3–4 optional)
```

### Call: ready

```
  Call
  ( Interview )( Standup )( Meeting )

  ┌ NEXT UP ───────────────────────────────────────────────────────────┐
  │ Northwind Freight · Senior Backend Engineer                         │
  │ Technical round · today 11:15 PM (in 45 min)     ● Pack ready (18)  │
  │ Resume: Northwind backend v2                                        │
  │                                              [ Start call ] (lg)    │
  └─────────────────────────────────────────────────────────────────────┘
  Call for   [ Northwind Freight · Technical · today 11:15 PM        ▾ ]
  [x] Store the transcript (everyone on the call agreed)          Why? ⓘ
  🎙 Jabra Evolve2 65 · 🔊 Jabra Evolve2 65 · Echo cancellation on   [Test] [Change]
  ▸ More options   (title, agenda, call app, detailed answer style)

  standup/meeting: "Call for" becomes Company ▾ + "Focus on" chips (material);
  the Next up card shows the company and its pack state
```

### Call: live

```
  ● Live · Interview · 12:41                      [Show overlay]  [End call…]
  ┌────────────────────────────────────────────────────────────────────┐
  │ Northwind Freight · Technical                                      │
  │ Last question  "How would you design the alerting…"   0:04 ago     │
  │ Them ▁▃▆▃▁  You ▁▁▂▁▁                         ● Listening          │
  └────────────────────────────────────────────────────────────────────┘
  Mode ( Interview )( Standup )( Meeting )   Detailed answer ( Bulleted )( Paragraph )
  "Changes apply to the next answers."

  End call… → ConfirmDialog
  ┌ End this call? ────────────────────────────────┐
  │ Summarize if you'll need it later.             │
  │                                                │
  │ [Keep going]  [End, transcript only] [Summarize and end] │
  └────────────────────────────────────────────────┘
```

### Call: just ended

```
  Call ended · Interview · Northwind Freight · 52 min            [New call]
  ┌ Summary ─────────────────────────────────────────────────────┐
  │ ░░░░░░░░░░░░░░░░░░░░░░░░  Writing the summary… (up to 2 min) │
  └──────────────────────────────────────────────────────────────┘
  ┌ Follow-up email ──────────────────────────────────── [Copy] ┐
  │ ░░░░░░░░░░░░░░░                                             │
  └─────────────────────────────────────────────────────────────┘
  [Open in History →]
```

### Applications

```
  Applications                         [Pull from Career-OS…]  [+ Add application]
  ┌ UPCOMING INTERVIEWS ────────────────────────────────────────────────────────┐
  │ Today 11:15 PM  Northwind Freight · Technical        ● Pack ready  [Start]  │
  │ Sat 9:28 PM     Northwind Freight · Final            ◌ Pack building        │
  └─────────────────────────────────────────────────────────────────────────────┘
  ┌───────────────────────────────┬─────────────────────────────────────────────┐
  │ [Search company or role    ]  │ Northwind Freight                       ⋯   │
  │ Stage: (All)(Applied)(Interv…)│ Senior Backend Engineer · Interviewing ▾    │
  │ ───────────────────────────── │ ● Pack ready (18) · From Career-OS, Oct 28  │
  │▌Northwind Freight         ●   │ [Details][Job description][Resume][Interviews]│
  │ Senior Backend Engineer…      │                                             │
  │ Lumen Health              ◌   │  Details: company, role, stage, URL,        │
  │ Staff Engineer, Payments      │  source, note (inline-validated fields,     │
  │ Harbor Analytics          ○   │  Save appears when dirty)                   │
  │ Backend Engineer              │                                             │
  │ …                             │  Interviews: list + [Schedule interview…]   │
  └───────────────────────────────┴─────────────────────────────────────────────┘
  rows: one-line title + one-line meta, ellipsis + tooltip; ● ready ◌ building ○ expired
  (icon + text label in the detail, never colour alone)

  + Add application → Dialog (md):
    Company*  Role*  Job description* [Paste] or [Choose file…]  Stage ▾  ▸ More (URL, source, note)
                                                       [Cancel] [Add application]
```

### Pull from Career-OS (dialog, lg)

```
  Pull from Career-OS                                                 ×
  [Search company or role ____________]  Stage ▾  [Search]
  ┌──────────────────────────────────────────────────────────────────┐
  │ Northwind Freight · Senior Backend Engineer   Changed  [Pull again]│
  │ Applied · Oct 22 · Tailored resume                                │
  │ Cedar Systems · Backend Engineer                         [Pull]   │
  │ Lumen Health · Staff Engineer, Payments          ✓ Pulled         │
  └──────────────────────────────────────────────────────────────────┘
  Copies the application, its JD and resume. Nothing else syncs.   [Done]
```

### Resumes

```
  Resumes
  ┌ BASE RESUME ────────────────────────────────────────────────────────┐
  │ Alex Rivera 2026  ✎                                    ● Ready       │
  │ 42 facts · updated Oct 27 · used by 2 applications                  │
  │                                          [Replace…]  [View text]    │
  └─────────────────────────────────────────────────────────────────────┘
  TAILORED (by application)
  ┌─────────────────────────────────────────────────────────────────────┐
  │ Northwind backend v2  ✎     Northwind Freight · Senior Backend…  ● Ready  ⋯ │
  │ Quarry platform  ✎          Quarry Labs · Platform Engineer   ◌ Reading ⋯ │
  │ Old draft  ✎                Harbor Analytics · Backend…   ✕ Couldn't read ⋯ │
  └─────────────────────────────────────────────────────────────────────┘
  ⋯ = Open application · Delete… (ConfirmDialog)
  Replace… → Dialog: [Choose file…] or paste, optional label; [Cancel] [Replace]
```

### Work

```
  Work   [ Fernhill Logistics ▾ ]  Senior Backend Engineer · Routing     ⋯ (Edit, Archive, Delete…)
         [Log] [Material (3)] [Facts]
  ┌ ‹  Thu, Oct 29  › [Today] ───────────────────┬ PAST ENTRIES ────────────────┐
  │ What you worked on                            │ Oct 29  Reviewed the alert… ◌│
  │ ┌──────────────────────────────────────────┐ │ Oct 28  Finished the quot… 3 │
  │ │                                          │ │ Oct 27  On call. Two pag… ✕ │
  │ └──────────────────────────────────────────┘ │                              │
  │ 📎 migration-plan.md 5 KB  ✕                  │                              │
  │ [Attach file…]                    [Save]      │                              │
  │ From this day: 3 facts  (Work ✓ · Commitment · Person)  [Review]             │
  └───────────────────────────────────────────────┴──────────────────────────────┘
  Material: a "To review (3)" section first (Approve / Edit / Reject per row, Approve all),
  then Documents (status badge, ⋯ Delete… with confirm). Facts: grouped by kind, each row ⋯.
  No companies: EmptyState "Add the company you work at" [Add company] → Dialog.
```

### History

```
  History                    150 live minutes in October · 4 calls
  [All] [Interviews] [Standups] [Meetings]
  ┌─────────────────────────────────────────────────────────────────────┐
  │ Today 7:29 PM   Interview · Northwind technical         52 min  ✓ Ready │
  │ Today 2:29 AM   Standup · Fernhill Logistics            14 min  ✓ Ready │
  │ Wed 8:29 PM     Meeting · Fernhill · Routing design re… 38 min  ◌ Processing │
  └─────────────────────────────────────────────────────────────────────┘
  [Load older calls]
  Live now (when a call runs elsewhere): a Banner at the top with [Open here] [End…]

  Detail:
  ← History    Interview · Northwind technical                         ⋯ (Delete…)
  Oct 29, 7:29 PM · 52 min · 6 questions · Zoom · your side transcribed live
  [Summary] [Replay] [Transcript]
  Summary: Summary / Feedback / Follow-up email [Copy] cards
  Replay: the overlay's layout, themed for the main window, filling the page (no inner box)
```

### Settings

```
  Settings
  ┌────────────────┬────────────────────────────────────────────────────┐
  │ General        │ Audio                                              │
  │ Account        │ Microphone   [ Jabra Evolve2 65           ▾ ] [Test]│
  │ ▌Audio         │              ▁▃▆▃▁  ✓ Heard you clearly            │
  │ Overlay & keys │ Call audio   [ Headset Earphone (Jabra)   ▾ ] [Test]│
  │ Advanced       │ Echo cancellation                             [on ●]│
  │                │   Needed without headphones.                       │
  └────────────────┴────────────────────────────────────────────────────┘
  General: Theme (System)(Light)(Dark) · Updates ▾ · Version 0.1.16 [Check now]
  Account: Signed in as … [Sign out] · Career-OS (connected as … [Disconnect…]) · Delete account…
  Overlay & keys: one row per action: label, HotkeyInput "Ctrl+Shift+Space" [Record] [Clear]; conflicts inline
  Advanced: Debug panel · (dev builds) Environment, URLs, dev token [Save]
```

### Sign-in (inside Account, and step 1 of setup)

```
  Sign in to Copilot
  Opens your browser at the sign-in page. You stay signed in on this computer.
  [ Sign in with your browser ]          ◌ Waiting for the browser…  [Cancel]
  dev builds: Environment (Local)(Cloud) · Cloud host [ ___ ] [Fill URLs] · URLs · [Save]
```

### Overlay: split (default)

```
┌─ ● Listening  ◉ them ─────────────────────────────── A− A+  ◫  ▭  ⚙  × ─┐ 32px, drag region
├─ TRANSCRIPT ─────────────┬─ NOW · System design · 4s ──────────── [Copy] ─┤
│ Them                     │ How would you design the alerting so that…    │ 14px text-2
│ Thanks for joining. So,  │                                               │
│ tell me about a time you │ ● Alert on symptoms users feel: quote         │ 18px fast
│ handled a production…    │   latency and error rate                      │
│               Answer ↗   │ ● Page only when the▍                          │ caret = writing
│ Them                     │                                               │ (≥3 lines reserved)
│ Okay. How did you decide │ TALKING POINTS · ◌ writing                    │ 12px state row
│ what to roll back first? │ ─────────────────────────────────────────────  │
│               Answer ↗   │ DETAILED · ◌ queued                   [Copy]  │
│ Them                     │ ░░░░░░░░░░░░░░░░░░░░░░░░░                      │ 2-line reserve
│ Makes sense. And how     │                                     👍 👎      │
│ would you design the     ├─ EARLIER ──────────────────────────────────────┤
│ alerting… and keep in    │ ▸ How did you decide what to roll back…  ✓ ✓  │ collapsed rows,
│ mind we have about forty │ ▸ Tell me about a time you handled a…    ✓ ✓  │ click to expand
├──────────────────────────┴────────────────────────────────────────────────┤
│ [Type a question, or Enter to answer what was just said ] [Ask] [Respond] │
└───────────────────────────────────────────────────────────────────────────┘
```

### Overlay: one column, compact, menu, click-through, idle

```
 One column (stacked)                       Compact (answers only)
┌─ ● Listening ──────── A− A+ ◫ ▭ ⚙ × ─┐    ┌─ ● Listening ─────── ⚙ ▭ × ─┐
│ Them: Thanks for joining…            │    │ How would you design the…    │ 1 line, ellipsis
│ ▸ Tell me about a time… ✓ ✓          │    │ ● Alert on symptoms users    │ 18px, unchanged
│ Them: Okay. How did you decide…      │    │   feel: latency and errors   │
│ ▸ How did you decide… ✓ ✓            │    │ ● Page only when the▍         │
│ Them: Makes sense. And how…          │    │ ◌ writing        [Copy]      │
├─ NOW (pinned) ───────────────────────┤    │ ──────────────────────────── │
│ How would you design the alerting…   │    │ DETAILED ◌ queued   [Copy]   │
│ ● Alert on symptoms users feel…      │    │ ░░░░░░░░░░░░                 │
│ ● Page only when the▍                │    ├──────────────────────────────┤
│ DETAILED ◌ queued                    │    │ [Ask…          ][Ask][Resp]  │
├──────────────────────────────────────┤    └──────────────────────────────┘
│ [Ask…                    ][Ask][Resp]│     no transcript, no Earlier list;
└──────────────────────────────────────┘     the ask box stays

 ⚙ menu                                     Click-through on
┌──────────────────────────────┐           ┌─ ● Listening ──────── A− A+ ◫ ▭ ⚙ × ─┐
│ Opacity            ━━━━●━ 80%│           ├─ Click-through on · Ctrl+Shift+G turns it off ─┤ strip under
│   Ctrl+Shift+↑ / ↓           │           │  …content…                            │ the header,
│ Keep on top          [on ●]  │           └───────────────────────────────────────┘ never over it
│ Click-through        [off ○] │
│   Ctrl+Shift+T on · G off    │            Idle (no call)
│ ──────────────────────────── │           ┌─ ○ Not in a call ─────────── ⚙ × ─┐
│ All settings…                │           │   No call running.                 │
│ ──────────────────────────── │           │   Start one in the Copilot window. │
│ Quit Copilot…  (confirms if  │           └────────────────────────────────────┘
│   a call is running)         │
└──────────────────────────────┘
```

## 5. Overlay rules

These are the acceptance criteria for Stage 4. The harness checks them with the same streaming
run as the audit.

**1. The latest fast answer is readable in under 2 seconds.**

- It is always in the same place: the top of the answers pane (split, compact), or the pinned Now
  block (one column). The newest non-cancelled question is "Now". When a newer one arrives, the
  old Now folds into Earlier as one change, with no scrolling. A speculative question shows a
  "listening…" badge; if it is cancelled, the previous Now returns.
- It is the largest text in the overlay: 18 px at 100%, weight 500, line length capped at about
  70 characters, bullets with a hanging indent.
- Earlier questions collapse to one row each: question, then ✓ points ✓ detailed. Each expands
  in place. Expanding pauses nothing.
- Contrast is at least 7:1 at full opacity and at least 4.5:1 at the lowest opacity, over both
  light and dark content.

**2. Fast, deep and streaming state are clear.** Each pass has a state row with an icon and text,
never colour alone:

| State | Talking points | Detailed answer |
|---|---|---|
| waiting | ◌ thinking | ◌ queued (auto) or **[Get detailed answer]** button (on request) |
| streaming | ◌ writing + caret ▍ at the end of the text | same |
| done | ✓ (with "prepared" when it came from the pack) | ✓ |
| stopped | ⏹ stopped for a newer question | same |
| failed | ⚠ failed (code) | same |

The two passes are told apart by a 3 px left rule and label colour (`--ov-fast` blue,
`--ov-deep` violet), plus their labels. The status badge in the header keeps its texts (Listening,
Connecting, Reconnecting, the component message) with a distinct icon per state: ● ◌ ▲.

**3. No layout jump while text streams.**

- The answers pane is anchored to the top and does not auto-scroll. Text grows downward, below
  what you are reading. The transcript pane keeps its follow-the-bottom behaviour and "Jump to
  latest".
- When a question is detected, its fast block reserves 3 lines and its deep block 2 lines (a
  skeleton), so the first words don't push anything.
- No empty bullet: a bullet appears with its first word.
- When the fast answer finishes, its reserved space shrinks only if the deep block hasn't started.
  If it has, the reserve stays until the deep block passes it.
- Measured with the harness: the fast block's top edge moves **0 px** while its own deep answer
  streams (today: 228 px in 9 jumps).

**4. Large enough type at reduced opacity.**

- The opacity setting fades the panel background and the chrome. The **answer text stays at
  92–100% opacity**, and the answer block has its own backing at no less than 62% panel
  opacity. Computed over light content, the fast answer stays at 5.3:1 at 35% opacity (today
  2.1:1) and 5.5:1 at 60% (today 4.2:1).
- The slider, the hotkeys, the stored per-monitor value and the 30–100% range are all unchanged.
  Only what the value fades changes. The menu label becomes "Opacity", matching the number shown.
- Nothing in the overlay is below 12 px at 100%. A−/A+ scale everything through `calc()`, so
  wrapping is computed at the new size and nothing is zoomed.

**Everything else is kept as it is:** the hotkeys (answer the last thing, show/hide,
click-through on and off, opacity up and down), Ctrl +/−/0 for text size, click-through and its
always-working off hotkey, keep on top, compact, per-monitor frame/opacity/view, split vs one
column, answer style (bullets/paragraph rendering), the copy buttons, the thumbs, the typed
question, Ask and Respond, the status badge, and "All settings…" and Quit.

## Settings save model

- Toggles, selects, segments and device pickers **save on change** and show a "Saved" toast, as
  the device pickers and Career-OS already do.
- Hotkeys save when a combination is recorded and valid.
- Free-text settings (dev-build URLs, dev token, cloud host) keep an explicit **Save** in their
  section, enabled when dirty.

## Behaviour changes

These are the only non-visual changes the proposal needs. Each will be listed again in its stage's
PR.

| # | Change | Stage | Why |
|---|---|---|---|
| B1 | Theme override (System/Light/Dark), stored per computer in `localStorage` | 2 | The requested manual override. Nothing in the Rust settings file changes. |
| B2 | Settings save on change, except free-text fields ([above](#settings-save-model)) | 3 | One save model instead of three |
| B3 | Hotkeys recorded by pressing them, stored in the same accelerator format | 3 | Typing "CommandOrControl+…" by hand is error-prone. The Rust side and file format are unchanged. |
| B4 | Reversible actions (archive, reject a fact, remove a fact) get an Undo toast. Deleting a company document, "Reject all", removing an attachment, and Disconnect Career-OS get a confirmation they don't have today. | 3 | "Destructive actions confirm" |
| B5 | Overlay answers pane: newest question on top, earlier ones collapsed, no auto-scroll | 4 | Rules 1 and 3 |
| B6 | Overlay opacity fades the background and chrome, not the answer text | 4 | Rule 4 |
| B7 | "Quit Copilot" asks first when a call is running | 4 | It is one slip from "All settings…" |
| B8 | UI copy says "call" instead of "session" ("Start call", "End call…") | 2–3 | Users say call. The API keeps its names. |

**Not proposed** (it would need an API change): starting an application from a PDF-only JD.
Today Add needs JD text and a PDF needs a saved application (audit #6). The Add dialog will say so
plainly ("Paste the JD text, or add the application and upload the PDF from its page") until the
API accepts an application without JD text. Say if you want that API change scheduled separately.

## Stages after this one

| Stage | Branch | Scope | Done when |
|---|---|---|---|
| 2 | `stage0/ui-2-foundation` | Tokens and theme (B1); `src/ui/` components with tests; app shell with sidebar; **History** moved over as the reference screen; the harness moved next to the app and wired to its tests | Keyboard through the shell and History; focus visible; axe clean on History; existing tests pass; bundle within budget |
| 3 | `stage0/ui-3-screens` | Call, Applications, Resumes, Work, Settings, sign-in and setup on the system; list states, inline validation, confirms (B2–B4, B8); old styles deleted per screen | Every list's three states tested; no `confirm()` left; `styles.css` reduced to the overlay's part |
| 4 | `stage0/ui-4-overlay` | Overlay per §5 (B5–B7), including History's replay | The harness streaming run: 0 px movement; contrast met at 30% opacity; every listed behaviour covered by a test |

Each PR has before/after screenshots of every changed screen (from the harness), the bundle size
from → to, the list of behaviour changes, and waits for every CI job to pass.

## Decisions for you

1. **Overlay order (B5).** Newest on top with earlier questions collapsed (recommended), or
   today's chronological order with the newest card pinned at the bottom?
2. **Overlay opacity (B6).** Fade the background only and keep answer text near-opaque
   (recommended), or keep today's whole-window fade?
3. **Navigation.** A left sidebar (recommended) or keep top tabs with the same regrouping?
4. **"Call" instead of "Session"** in the UI copy (B8)?
5. **Settings save on change** (B2), with explicit Save only for free-text fields?
6. **Library.** Radix for Dialog, AlertDialog, Tabs and Toast (+≤18 kB gzip), the rest native or
   hand-built?
7. **Your pain points.** The request's "what bothers me most" list was still the template. Is the
   audit's ranking right, or is there something to move to the top?
