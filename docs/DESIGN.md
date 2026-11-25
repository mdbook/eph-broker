# Design standards

The dashboard's design system, as it ships today. Every value here comes from the stylesheet in `dash.PAGE`
(`dash.py`) and was checked against the rendered page (`python3 scripts/screenshots.py`). Use these tokens and
components for any UI change, including UI modules. Gaps are listed under [Known drift](#known-drift) and are
not fixed here.

## Principles

- Calm, dense, operational. The page answers "what is running, what is waiting, what needs me" at a glance.
- One brand color. Everything else is a neutral surface or a status color.
- Status is never color alone: always an icon shape plus a word.
- Every text pairing clears WCAG AA (4.5:1) in both themes.
- Read-only by default. Write actions are explicit buttons, and destructive ones confirm first.
- No build step, no external fonts, images or icon sets. Everything is inline CSS and system fonts.

## Color tokens

Set on `:root`. Light is the default; dark applies with `data-theme=dark`, or with the system dark preference
when the theme is `auto`.

| Token | Role |
|---|---|
| `--bg` | Page background |
| `--s1` | Card, header, input surface |
| `--s2` | Hover, code background |
| `--s3` | Pressed, chips, tracks, segmented control |
| `--line` | Borders and dividers |
| `--t1` | Primary text |
| `--t2` | Secondary text, labels |
| `--t3` | Faint text, idle icons |
| `--brand` | Links, primary button, focus, selection |
| `--onbrand` | Text on `--brand` |
| `--run` | Running, healthy, success |
| `--wait` | Waiting, paused, needs a decision |
| `--block` | Blocked, failed, error |
| `--shadow` | Card lift |

The values, as declared in `dash.py`:

```css
/* light (default) */
--bg:#f4f5f7; --s1:#ffffff; --s2:#f0f2f5; --s3:#e6e9ee; --line:#dce0e6;
--t1:#141920; --t2:#475160; --t3:#626c7b; --brand:#2b57c9; --onbrand:#ffffff;
--run:#17734a; --wait:#8a5600; --block:#bb3328; --shadow:0 1px 2px rgba(20,25,32,.05);

/* dark */
--bg:#0f1216; --s1:#161a20; --s2:#1d222a; --s3:#262c35; --line:#2b323c;
--t1:#e8ecf1; --t2:#aab3bf; --t3:#8a94a2; --brand:#8eabff; --onbrand:#0c1120;
--run:#4cc38a; --wait:#e3a63d; --block:#f27c71; --shadow:none;
```

Tints are made with `color-mix`, not new hex values: 7% `--brand` into `--s1` for a selected row, 13% of a
status color for chips and pills, 8% `--block` for the error banner, 16% for status icon backgrounds.

## Type

- UI font: `-apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI Variable Text", "Segoe UI", system-ui,
  sans-serif`. Base `14px/1.5`, antialiased.
- Mono: `ui-monospace, SFMono-Regular, "JetBrains Mono", Menlo, monospace`, for code, commands and paths.
- Numbers that update or line up use `font-variant-numeric: tabular-nums`.

| Use | Size | Weight |
|---|---|---|
| Page title (`.ph h1`) | 28px, line-height 1.2, letter-spacing -.02em (26px on phones) | 650 |
| Big number (`.strip .v b`, `.capv b`) | 26px | 650 |
| Side panel title (`.side h3`) | 18px | 650 |
| Section title (`.sec h2`) | 17px, letter-spacing -.01em | 650 |
| Product name in header | 16px | 650 |
| Body, table cells, buttons | 14px | 400; 600 for buttons and emphasis |
| Secondary text, labels, meta (`.sub`, `.when`) | 13px | 400 |
| Table headers, count badges, tags, form labels | 12px | 500 headers, 600 badges |
| Transcript meta and timestamps | 11 to 11.5px | 400 |

## Spacing and layout

- Spacing steps in use: 4, 8, 12, 16, 20, 24, 32, 40, 64px. Prefer these.
- Page: max width 1440px, padding 32px 40px 64px. Sections are 32px apart (`.sec`).
- Fill views (a module view with `data-fill`, such as the Agents tab): the page is exactly the viewport and
  never scrolls, top and bottom padding are 16px, and there is no page title (the tab names the view). Only
  the view's own panes scroll; the card header and composer always stay on screen.
- Header: sticky, 64px tall, `--s1` with a bottom `--line` border.
- Card content padding: 20px (rows, table edge cells) or 24px (side panels, queue head).
- Rows and table cells: 14 to 16px vertical padding, divided by a 1px `--line` top border.
- Runs view: table plus a 440px side panel (`.cols`), 20px gap.
- Breakpoints: at 1100px the side panel hides; at 720px the nav moves into a menu, padding drops to 16px, the
  summary strip becomes a list and tables become stacked rows.
- Phone tap targets in the menu are at least 48px tall.

## Radius

| Value | Use |
|---|---|
| 999px | Chips, pills, count badges, tags |
| 12px (`--r`) | Cards |
| 10px | Tiles, toast, error banner |
| 8px | Buttons, inputs, selects, textareas |
| 9px / 7px | Segmented control track / its buttons |
| 3px | Progress tracks and meter fills |

## Components

- **Control height** (`--ctl`, 36px on `:root`): buttons, square icon buttons, selects and message boxes take
  their height from it, so a row of controls lines up by construction. The Agents tab and quick view raise it
  to 44px on phones inside their own scope.
- **Button** (`.btn`): `--ctl` tall, 16px side padding, 8px radius, `--line` border, `--s1` fill, 14px/600.
  Row actions share one minimum width (`--btn: 120px`) so a column of them lines up.
  - `.pri`: `--brand` fill, `--onbrand` text. The main action of a panel (for example "Watch live").
  - `.ghost`: no border or fill until hover. Used for icon buttons in the header.
  - `.sq`: 36px square, for a single glyph (settings, menu).
  - `.bad`: `--block` text, for destructive actions.
  - `.on`: `--brand` border and text, for a toggled state.
  - Hover darkens the border; active fills `--s3` and nudges down 1px; disabled is 50% opacity.
- **Link button** (`.lnk`): `--brand`, 600, no underline until hover. Inline actions such as "View".
- **Card** (`.card`): `--s1`, 1px `--line` border, 12px radius, `--shadow`. Everything sits in a card.
- **Summary strip** (`.strip`): four equal columns divided by `--line`, label (13px `--t2`), big number,
  short muted context and an optional inline link.
- **Row list** (`.rows`): status icon, title plus muted detail line, time, actions. Whole row is clickable and
  hovers `--s2`.
- **Table** (`.tbl`): 12px/500 `--t2` headers, 14px cells, numeric columns tabular and right-aligned (`.r`).
  A selected row gets the 7% brand tint and a 3px inset `--brand` bar on the left. On phones each row becomes
  a title line plus one muted detail line.
- **Side panel** (`.side`): ref, 18px title, key/value list (`.kv`, label left `--t2`, value right 600), then
  actions. Desktop run and agent previews stick below the header and scroll within the viewport.
- **Detail sheet** (`.detail-sheet`): native modal dialog for Fleet quick views and explicit run previews,
  up to 640px wide, bounded by the viewport on desktop and phone. A sticky header names the detail and
  keeps Close visible. Opening focuses Close; Close or Escape restores the initiating row and scroll
  position, even after polling replaces the row. Full transcript routes retain the phone layout, focus
  their detail, and return to the originating list position on Back or Escape.
- **Navigation**: header tabs (`.nav`) and in-page sub-tabs (`.subtabs`) both mark the current item with a
  3px `--brand` bottom border and 600 weight. Sub-tabs carry a count badge.
- **Segmented control** (`.seg`): `--s3` track, selected segment raised on `--s1` with `--shadow`. Used for
  filters, with counts inside the label ("Running 3").
- **Chip** (`.chip`): pill, 13px/600. `.wait` for the paused state, `.mode` for neutral mode labels.
- **Count badge** (`.cnt`) and **tag** (`.tag`): 12px/600 `--t2` on `--s3`, pill.
- **Status icon** (`.st`), 16px, always next to a word:

  | Class | Shape | Meaning |
  |---|---|---|
  | `run` | Solid `--run` dot | Running |
  | `wait` | `--wait` ring | Waiting |
  | `idle` | Thin `--t3` ring | Idle |
  | `block` | `--block` rounded square with `!` | Blocked |
  | `ask` | `--wait` circle with `?` | Needs a decision |
  | `ok` | Tinted circle with a check | Succeeded |
  | `fail` | Tinted circle with a cross | Failed |
  | `off` | `--s3` circle with a dash | Off |

- **Pill** (`.pill`): 11.5px/500 with a 6px dot, used in the transcript. Variants `run`, `ok`/`idle`,
  `warn`/`pend`/`close`, `bad`.
- **Meter** (`.meter`): 6px `--s3` track, `--brand` fill, `warm` (amber) past the floor tick, `hot` (red) past
  the ceiling, with a 12px value line underneath.
- **Switch** (`.sw`): 44 by 26px, `--s3` off, `--wait` on (it is used for pause and drain, which hold work).
- **Form fields** (`.search`, `.pick-sel`, `.msg textarea`): 36px tall, 8px radius, `--line` border, `--s1`.
  A field that differs from the default gets a `--brand` border (`.set`). Labels sit above in 12px `--t2`.
- **Empty state** (`.empty`): one short sentence, 13px `--t2`, 18px 24px padding, inside the card it replaces.
- **Error banner** (`.err-banner`): `role=alert`, `--block` text on an 8% `--block` tint, 10px radius, under
  the header.
- **Toast** (`.toast`): bottom center, inverted (`--t1` fill, `--s1` text), 14px/600. `.bad` uses `--block`.
- **Transcript** (`.tl`, `.ev`, `details.tool`): message cards (14px radius) and collapsible tool cards (12px
  radius) with a 20px tinted tool icon, monospace argument summary and a rotating `›` chevron. Errors get a
  `--block` border and tint.
- **Agent view** (`viewer/ui.js`, reference `docs/mockups/agent-view.html`): the Agents tab detail and the
  Fleet quick view. One header (name, status chip, one `⋯` actions menu; then the doing-now line; then kind,
  model, context meter, session, last activity in 13px `--t2`). Replies are the loudest text; thinking and
  tool steps fold into one collapsed 13px `--t2` line per turn ("5 tool calls · 38s · Show steps"). The composer is a text box and one Send split
  button whose arrow, shown only while the agent works, offers Interrupt and send. Header, body and composer
  share one edge (`--gut`: 20px, 24px in the quick view, 16px on phones). Menus are a 10px-radius `--s1`
  popover. No serif, italics, gradients or per-view theme switch.
- **Focus**: every interactive element shows a 2px `--brand` outline, offset 2px (`:focus-visible`).
- **Theme**: the header button cycles auto, light, dark and remembers the choice in `localStorage`
  (`eph-theme`).

## Motion

- Small and functional only. No page transitions.
- Switch knob: `transform .25s cubic-bezier(.3,1.4,.5,1)` (slight overshoot); track color `.2s`.
- Disclosure chevrons: `transform .15s`, rotate 90deg when open.
- Running pill dot: `pulse 1.4s ease-in-out infinite` (opacity to .3).
- Button press: 1px downward nudge.
- `prefers-reduced-motion: reduce` turns off every transition and animation.

## Iconography

- No icon font or image sprites. Status icons and the logo are drawn in CSS; other glyphs are Unicode:
  `⚙` settings, `☰` menu, `☀` `☾` `◐` theme, `›` disclosure, `✓` `✕` `–` `!` `?` inside status icons.
- Icon-only buttons always carry `aria-label` and `title`.

## Copy voice

- Sentence case for every label, heading and button: "Needs you", "Waiting to start", "Watch live".
- Short and literal. Say what the thing is or does, not how it works: "Nothing needs you right now.",
  "No workers running.", "Nothing queued."
- Empty states are one sentence with a period. Labels and buttons have none.
- A confirmation (the browser's native `confirm`) names the item and the consequence: "Cancel #42? The worker stops now and the ticket will
  not start again on its own."
- Errors say what failed and why, briefly: "Failed: HTTP 500", "Use a whole number, 0 or more".
- Refs are `#<id>`; detail lines join parts with ` · ` ("#42 · Running · code/demo").
- Durations are `2m 00s`, token counts `12k`, clock times in the viewer's locale, hours and minutes only ("Updated 12:30 PM").
- No em dashes, no exclamation marks, no emoji in UI text.

## Name and logo

- The name is always lowercase `eph-broker`, including at the start of a heading. Never "Eph-Broker" or
  "EPH broker".
- In the dashboard header: a 24px `--brand` tile (7px radius) with two `--onbrand` bars, then the name at
  16px/650, then the instance label in 13px `--t2` and a 7px live dot (`--run`, `--block` when the state feed
  fails). The favicon is the same tile as an inline SVG.
- The repository icon set (`assets/icon/`) is a coral ticket with a merge arrow on a dark tile. Use it for the
  README, packages and anything outside the dashboard. Its colors and the file to pick for each display size
  are in `assets/icon/README.md`.

## API and log output

The JSON API and log line conventions are in [STANDARDS.md](STANDARDS.md#error-handling-and-logging).

## Known drift

1. Two token layers: the transcript styles use older aliases (`--panel`, `--panel2`, `--ink`, `--muted`,
   `--faint`, `--accent`, `--green`, `--amber`, `--red`, `--user`, `--code`) mapped onto the main tokens. New
   code should use the main tokens.
2. The dark palette is written twice (`[data-theme=dark]` and the `prefers-color-scheme` block). Both must
   change together.
3. Radius has twelve distinct pixel values (2 to 14px) plus 999px, where a four-step scale would do.
4. Transcript styles use half-pixel sizes (11.5, 12.5, 14.5px) outside the type scale.
5. The dashboard logo and favicon (blue tile, two bars) do not match the repository icon set (coral ticket on
   dark), and none of the icon set's colors appear in the UI.
6. The running pill (`.pill.run`) uses `--brand` blue, while every other running indicator uses `--run` green.
7. Hard-coded `#fff` on the switch knob and the bad toast text instead of a token.
8. Several mono stacks (`.targs`, `.sline`, `.fpath`, `.pm`, `.cmd`, `details.harness .ln`) omit
   `"JetBrains Mono"`.
9. The state-fetch error banner text is lowercase ("state fetch failed: ...") while all other UI copy is
   sentence case.
10. On the Now view's Running table the Tokens column is left-aligned; on Runs it is right-aligned.
11. The error banner is capped at 1360px wide while the page is 1440px.
12. The Details drawer's disclosure marker hangs outside the page gutter (`list-style-position: outside`).
13. Seven inline `style=` attributes in the markup bypass the stylesheet.
