# Handoff: Compound Page — Report Generator Agent

## Overview

The Report Generator Agent drafts research reports for a compound or target, with every
claim cited back to the row, page or query it came from. It runs locally against local data.

This handoff covers the **compound page** — the centre of the product. The redesign moves
the app from a report-type gallery front door to a **compound-first** information
architecture: you search a compound, land on the compound, and report types are offered as
actions against it. Report type is a parameter of the compound, not a peer of it.

The compound page has one job: **tell the user what this compound can currently evidence,
and let them start the report that best fits.**

Design target is a single screen at 1440×900 with no scrolling. Audience is discovery
scientists on desktop, typically dual-monitor.

## About the Design Files

The files in this bundle are **design references created in HTML** — prototypes showing
intended look and behaviour. They are **not production code to copy directly.**

The task is to **recreate these designs in the target codebase's existing environment**
(React, Vue, Svelte, server-rendered templates, whatever is in use) using its established
patterns, component library and styling approach. If no environment exists yet, choose the
most appropriate framework for the project and implement the designs there.

Two specific notes on the prototype's construction:

- Styles are written as **inline styles** in the prototype for streaming-render reasons
  particular to the authoring tool. **Do not carry that pattern into production.** Convert
  to whatever the codebase uses — CSS modules, Tailwind, styled-components, a stylesheet.
  The token table below is the source of truth; extract it into the project's token layer.
- The prototype's data is **hardcoded sample data** for compound `XYZ-001`. Real values come
  from the backend. See *State Management* and *Data Requirements*.

The existing app is a Flask/Jinja application with a hand-written `gsk.css`. That stylesheet
is well built (complete token layer, two-tone focus rings, real dark mode, print styles) —
this redesign replaces its **application**, not its engineering standard. If the target is
that same app, expect to rewrite templates and add tokens rather than start over.

**Brand note:** GSK brand compliance is explicitly **not** required for this project. The
palette and typefaces below are deliberate choices outside the GSK system and should be
implemented as specified.

## Fidelity

**High-fidelity.** Final colours, typography, spacing, radii, shadows, motion and copy.
Recreate pixel-accurately using the codebase's libraries. Every value in this document was
measured in the rendered prototype, not estimated.

Accessibility was audited by measurement, not by eye. **All contrast ratios in this document
are measured values.** Several are close to the 4.5:1 threshold — the rules in
*Accessibility Rules* are not advisory and must survive implementation.

---

## Screens / Views

### Screen: Compound page

**Route:** something like `/compound/<compound_id>` — the compound ID is the primary key of
the whole experience.

**Purpose:** the user arrives knowing a compound ID. They need to know what evidence exists
for it right now, then start a report.

**Page structure** — four stacked bands, no page scroll at 1440×900:

| Band | Height | Notes |
|---|---|---|
| Header | 56px | `position: sticky; top: 0; z-index: 30` |
| Identity band | ~68px | compound identity + coverage + primary action |
| Main | fills | two-column grid |
| — | | total measured page height 694px at 924px wide |

Root element: `min-height: 100vh`, `background: #D9DDE0` (FIELD), `color: #191C1E`,
`font-family: Chivo`, `font-size: 14px`, `line-height: 20px`, `-webkit-font-smoothing: antialiased`.

---

#### 1. Header (56px, sticky)

`display: grid` · `grid-template-columns: max-content minmax(0,440px) max-content max-content`
· `gap: 20px` · `align-items: center` · `padding: 0 20px`

**Critical sizing rule:** the search field track is `minmax(0,440px)` and the nav track is
`max-content` (nav also carries `min-width: max-content; white-space: nowrap`). This makes
grid size the nav to its content **first** and lets the search field absorb any shortfall.
An earlier version used `minmax(240px,440px)` for search and `minmax(0,1fr)` for nav; the
search field greedily took its 440px maximum and the nav overflowed its track and painted on
top of the status indicator. **Do not reverse this priority.**

- Background: `linear-gradient(#F3F5F6, #E9ECEE)`
- Shadow: `inset 0 1px 0 #FFFFFF, 0 1px 0 #C4CACE, 0 4px 14px -6px rgba(25,28,30,0.22)`

**Column 1 — brand.** `display: flex; align-items: center; gap: 10px`
- Mark: 10×10px square, `background: #C43D1C`, no radius
- Wordmark: "Report Gen" — Chivo Mono, 600, 11px, `letter-spacing: 0.14em`, uppercase, `#191C1E`

**Column 2 — compound search.** A `<label>` wrapping the input so the whole pill is a hit target.
- 34px high, `padding: 0 12px`, `border-radius: 17px` (fully rounded)
- `background: #FFFFFF`, `border: 1px solid #C4CACE`
- `box-shadow: inset 0 1px 2px rgba(25,28,30,0.10)`
- Hover: `border-color: #8D969C`
- Leading glyph `⌕` — Chivo Mono 12px `#4C555B`
- Input: transparent, no border/outline, Chivo 400 13px/20px, `#191C1E`,
  placeholder "Jump to compound, target or run"
- Trailing `<kbd>` showing `/` — Chivo Mono 10px, `padding: 2px 6px`, `background: #F3F5F6`,
  `border: 1px solid #C4CACE`, `border-radius: 5px`, `box-shadow: 0 1px 0 #C4CACE`, `#4C555B`

**Column 3 — nav.** `display: flex; gap: 2px`. Chivo Mono, 11px, `letter-spacing: 0.1em`, uppercase.
Items: Compounds (active), Runs, Templates.
- Each: 28px high, `padding: 0 12px`, `border-radius: 14px`, `text-decoration: none`
- Active: `background: #FFFFFF`, `color: #191C1E`,
  `box-shadow: 0 1px 2px rgba(25,28,30,0.14), inset 0 0 0 1px #C4CACE`
- Inactive: `color: #4C555B`, transparent; hover `background: rgba(255,255,255,0.7)`

**Column 4 — data source status.** `display: flex; align-items: center; gap: 12px`
- Label "local data" — Chivo Mono 11px `#4C555B`
- Dot: 6px circle, `background: #3F7A34` (green = connected)

---

#### 2. Identity band (~68px)

`display: grid` · `grid-template-columns: minmax(0,1fr) auto` · `gap: 32px` ·
`align-items: center` · `padding: 18px 20px`
- Background: `linear-gradient(#E7EBEC, #DFE3E6)`
- Shadow: `inset 0 1px 0 rgba(255,255,255,0.7), 0 1px 0 #C9CFD3`

**Left — identity.** `display: flex; flex-wrap: wrap; align-items: baseline; gap: 16px; min-width: 0`
- `<h1>` compound ID: Chivo 600, 32px/36px, `letter-spacing: -0.03em`, `#191C1E` — e.g. `XYZ-001`
- Subtitle: Chivo 300, 17px/24px, `#3A4247` — e.g. `Kinase Z inhibitor`
- Metadata row: `display: flex; flex-wrap: wrap; gap: 14px`, Chivo Mono 11px/16px, `#4C555B`.
  Values in order: programme (`PSS`), modality (`small molecule`), document count (`13 docs`),
  run count (`3 runs`), last run (`last 19 Aug`).

Rationale: identity, subtitle and metadata sit on **one baseline** rather than stacking, which
is most of the vertical saving versus the original.

**Right — coverage + primary action.** `display: flex; align-items: center; gap: 20px`

*Coverage readout:* `display: flex; align-items: baseline; gap: 8px`
- Count: Chivo Mono 600, 20px/24px, `#191C1E` — the number of resolving bindings
- Label: Chivo Mono 12px, `#4C555B` — `of 8 bindings resolve`

*Coverage ticks:* `display: flex; gap: 2px` — one tick per binding, in binding order
- Each tick 12×7px, `border-radius: 4px`
- Resolved: `background: <accent>`, `box-shadow: 0 1px 2px rgba(150,40,15,0.35)`
- Unresolved: `background: transparent`, `box-shadow: inset 0 0 0 1.5px #A9B0B5`

*Primary button:* label `Draft Target Assessment` (the highest-coverage report)
- `min-height: 36px`, `padding: 0 18px`, `border: 0`, `border-radius: 18px`
- `background: linear-gradient(#D24C29, #B83718)`, `color: #FFFFFF`, Chivo 600 13px/20px
- `box-shadow: inset 0 1px 0 rgba(255,255,255,0.28), 0 2px 6px -1px rgba(150,40,15,0.45)`
- Hover: `box-shadow: inset 0 1px 0 rgba(255,255,255,0.34), 0 4px 12px -2px rgba(150,40,15,0.55)`
  plus `transform: translateY(-1px)`
- Active: `transform: translateY(0)`

Coverage and the action a user would take about it are deliberately adjacent.

---

#### 3. Main (two columns)

`display: grid` · `grid-template-columns: minmax(0,7fr) minmax(300px,5fr)` · `gap: 20px` ·
`padding: 20px` · `align-items: start`

Both columns carry `min-width: 0` so grid children can shrink and text can ellipsis.

##### 3a. Left column — Evidence bindings

**Section header row:** `display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 8px`
- `<h2>` "Evidence bindings" — Chivo Mono 600, 11px, `letter-spacing: 0.14em`, uppercase, `#4C555B`
- Segmented filter, `display: flex; gap: 2px`, Chivo Mono 11px. Segments: `all 8`, `ready 4`, `gaps 4`
  - Each: `min-height: 26px`, `padding: 0 10px`, `border: 0`, `border-radius: 13px`
  - Active: `background: #FFFFFF`, `color: #191C1E`,
    `box-shadow: 0 1px 2px rgba(25,28,30,0.16), inset 0 0 0 1px #C4CACE`
  - Inactive: transparent, `color: #4C555B`; hover `background: rgba(255,255,255,0.75)`

**Panel** (this treatment is shared by all three panels on the page — extract it as one component):
```
background: #EDF0F1;
border: 1px solid #C9CFD3;
border-radius: 12px;
overflow: hidden;
box-shadow: 0 1px 2px rgba(25,28,30,0.06),
            0 6px 18px -8px rgba(25,28,30,0.28),
            inset 0 1px 0 #FFFFFF;
```

**Table header row:** `display: grid` ·
`grid-template-columns: 20px minmax(0,1fr) 76px 80px` · `gap: 12px` · `align-items: center` ·
`padding: 6px 12px`
- `background: linear-gradient(#E9ECEE, #E1E5E7)`, `border-bottom: 1px solid #C9CFD3`
- Chivo Mono 10px, `letter-spacing: 0.1em`, uppercase, `#4C555B`
- Labels: (empty), `binding · resolves to`, `returns`, `used by`

**Binding rows** — 8 rows, same grid as the header. `padding: 6px 12px` (compact) /
`10px 12px` (comfortable). `border-bottom: 1px solid #E3E7E9`. `background: #EDF0F1`.
Hover `background: #F6F8F8`, transition `background 140ms cubic-bezier(.2,.6,.2,1)`.

Cells:
1. **Status dot** — 9×9px, `border-radius: 50%`, `box-sizing: border-box`
   - Resolved: `background: <accent>`,
     `box-shadow: 0 0 0 3px rgba(196,61,28,0.16), 0 1px 2px rgba(150,40,15,0.40)`
   - Unresolved: `background: transparent`, `border: 2px solid #5D666C`, no shadow
2. **Name + target**, two lines in a `min-width: 0` wrapper
   - Binding name: Chivo Mono 500, 12px/17px, `#191C1E`, ellipsis, nowrap
   - Resolve target: Chivo 11px/16px, ellipsis, nowrap.
     `#3A4247` when resolved, `#5D666C` when not.
   - **This is a two-line layout by necessity, not preference.** An earlier five-column
     single-line version gave the resolve-target column 48px against 150–288px of content,
     so every value was clipped to ~7 characters. Two lines give the target the full
     remaining width. Do not flatten it back to one line.
3. **Returns** — Chivo Mono 11px
   - Resolved: `#191C1E`, weight 400 — e.g. `2 pages`, `1 row`, `6 docs`
   - Unresolved: `#8F2A11`, weight 500, text `not reg.`
4. **Used by** — Chivo Mono 11px, `#4C555B`, `white-space: nowrap` — e.g. `3 reports`

Sample data, in display order:

| Dot | Binding | Resolves to | Returns | Used by |
|---|---|---|---|---|
| ● | `target_rationale` | Confluence · space PSS · target rationale pages | 2 pages | 3 reports |
| ● | `project_background` | Confluence · space PSS · programme background | 1 page | 4 reports |
| ● | `chembl_mechanism` | ChEMBL · mechanism of action, bound target | 1 row | 2 reports |
| ● | `prior_reports` | Evidence folder · nonclinical, prior_report | 6 docs | 4 reports |
| ○ | `potency` | `preclinical_assays.assay_potency_selectivity_v1` | not reg. | 2 reports |
| ○ | `dmpk` | `dmpk.dmpk_summary_v1` | not reg. | 3 reports |
| ○ | `pivotal_tox` | `nonclinical_safety.pivotal_tox_summary_v2` | not reg. | 2 reports |
| ○ | `developability` | `cmc.developability_metrics_v1` | not reg. | 1 report |

**Footnote** below the panel: `margin: 10px 0 0`, Chivo 12px/18px, `#3A4247`,
`max-width: 88ch`, `text-wrap: pretty`. Path set in Chivo Mono 11px inline.

> Four queries are absent from the registry. Add a YAML file for each under
> `samples/synthetic_compound/queries/` and they resolve on the next run. Missing bindings
> never block a run — they become stated gaps in the draft.

**This footnote is the single place a missing-source warning appears.** The original UI
stated the same four warnings three times in three components (a per-source amber block, a
"what will be missing" table, and an amber summary panel). Do not reintroduce duplicates.

##### 3b. Right column

`display: flex; flex-direction: column; gap: 20px`. Three sections, each with the same
`<h2>` treatment as the left column.

**Section: Draft a report.** Panel treatment, 4 rows.
- Row: `display: grid` · `grid-template-columns: minmax(0,1fr) auto 16px` · `gap: 12px` ·
  `align-items: center` · `width: 100%` · `padding: 10px 12px` (compact) / `14px 12px`
  (comfortable) · `border: 0` · `border-bottom: 1px solid #E3E7E9` · `background: #EDF0F1` ·
  `cursor: pointer` · `text-align: left`. Hover `background: #F6F8F8`.
- Cell 1: report name — Chivo 500, 13px/18px, `#191C1E`, ellipsis, nowrap, `margin: 0 0 2px`;
  below it the kicker — Chivo Mono 10px, `letter-spacing: 0.08em`, uppercase, `#5D666C`
- Cell 2: `display: flex; flex-direction: column; align-items: flex-end; gap: 4px`
  - Ratio — Chivo Mono 600, 12px. Lead row `<accent>`, others `#4C555B`. Format `4/5`
  - Tick track — `display: flex; gap: 2px; width: 72px`, one tick per source
    - Each: `flex: 1`, `height: 5px`, `border-radius: 3px`
    - Filled: `background: <accent>` (lead row) or `#4C555B`,
      `box-shadow: 0 1px 1px rgba(25,28,30,0.20)`
    - Empty: `background: #CDD3D7`, `box-shadow: inset 0 1px 1px rgba(25,28,30,0.10)`
- Cell 3: `→` — Chivo Mono 12px. Lead row `<accent>`, others `#5D666C`

Rows, ordered by coverage descending (the first is the "lead" and gets accent treatment):

| Kicker | Name | Ready |
|---|---|---|
| Discovery | Target Assessment / Validation | 4/5 |
| Milestone | Candidate Selection Dossier | 4/8 |
| Discipline | DMPK / ADME Summary | 1/4 |
| Regulatory | Investigator's Brochure — nonclinical | 1/5 |

**Section: Recent runs.** Header row has the `<h2>` plus a right-aligned `all 3` link
(Chivo Mono 11px). Panel treatment, 3 rows.
- Row: `display: grid` · `grid-template-columns: minmax(0,1fr) 52px 84px` · `gap: 12px` ·
  `align-items: center` · `width: 100%` · `padding: 9px 12px` (compact) / `13px 12px`
  (comfortable), otherwise as the report row. Hover `background: #F6F8F8`.
- Cell 1: report name — Chivo 13px/18px, `#191C1E`, ellipsis, nowrap
- Cell 2: evidenced ratio — Chivo Mono 11px, `#191C1E` — e.g. `12/14`
- Cell 3: timestamp — Chivo Mono 11px, `#4C555B`, `justify-self: end` — e.g. `19 Aug 11:21`

| Name | Evidenced | When |
|---|---|---|
| Target Assessment / Validation | 12/14 | 19 Aug 11:21 |
| Candidate Selection Dossier | 9/21 | 14 Aug 09:04 |
| DMPK / ADME Summary | 3/18 | 02 Aug 16:38 |

**Section: Related.** `display: flex; flex-wrap: wrap; gap: 6px`. Chips as links:
- `display: inline-flex; align-items: center`, `min-height: 30px`, `padding: 0 14px`
- `background: #EDF0F1`, `border: 1px solid #C9CFD3`, `border-radius: 15px`
- `box-shadow: 0 1px 2px rgba(25,28,30,0.10), inset 0 1px 0 #FFFFFF`
- Chivo Mono 11px, `text-decoration: none`, `color: #191C1E`
- Content: `XYZ-014`, `XYZ-022`, `Kinase Z · target`

---

## Other screens in the flow (designed, lower fidelity)

These exist in `Report Generator.dc.html` in an earlier visual language. Structure and IA are
correct; restyle to the Titanium tokens when implementing.

**Search / home.** One large search field, nothing else above it. Compound ID, target name or
run reference. Below: recent compounds as plain text rows (140px mono ID column, name,
right-aligned "3 runs · 19 Aug"), 56px row height, not cards. **The report-type gallery must
not be the front door.** The existing gallery survives as a secondary template-browse surface
reachable from nav.

**Draft / report view.** Carries the redesign's signature idea, the **provenance gutter**: a
44px left column running the full height of the draft. Every paragraph's left edge carries a
2px rule — solid where the claim is backed by retrieved data, absent where the model wrote it
unsupported. Citation markers live in the gutter aligned to their claim, not inline in the
text, and resolve their source in a panel without a round trip. This replaces both the sticky
five-number stat strip and the Draft/Sources tab split of the original. Retrieved tables sit
inline on a recessed surface; model prose sits on the base surface — the recessed surface
means "retrieved" and nothing else.

**Not designed yet:** the template editor (currently ~40 flat inputs with `Move up`/`Move
down` links; recommend moving it behind an admin route) and empty/first-run states.

---

## Interactions & Behavior

**Navigation**
- Header search is present on every screen — compound switching never requires going home.
  Pressing `/` focuses it (the `<kbd>` advertises this). Implement the shortcut.
- Search submit → compound page for the matched compound. Match against compound IDs,
  programme names, targets and past run references.
- Report row or primary button → start a run for that report type against this compound.
- Recent-run row → that run's draft view.
- Related chip → that compound's page.

**Evidence filter** — `all` / `ready` / `gaps` filters the rows client-side. Counts in the
labels come from data.

**Hover** — rows go `#EDF0F1` → `#F6F8F8`; ghost buttons and nav items go to a translucent
white; the primary button raises its shadow and lifts 1px.

**Motion** — one transition rule on `button, a, input, label`:
`background`, `box-shadow`, `border-color`, `transform`, all **140ms `cubic-bezier(.2,.6,.2,1)`**
(`border-color` uses `ease-out`). Everything is wrapped in
`@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }`.

Do **not** animate: page transitions, list reordering, number counters, panel mount.

**Focus** — `*:focus-visible { outline: 2px solid #C43D1C; outline-offset: 2px; border-radius: 4px }`.
Every interactive element is ≥26px tall; primary targets are ≥36px. Keep keyboard focus order
matching visual order.

**Loading** — a run is asynchronous. The original polls. Show progress with a 200ms opacity
crossfade on updated values; do not animate a progress bar or count up numbers.

**Responsive** — designed for 1440px+. Verified down to ~900px: the search field absorbs
header shortfall and binding text ellipsises by 6–25px. Below ~900px the two-column main
should stack (left column first). No mobile design exists — ask before inventing one.

---

## State Management

Client state on this screen is small:

| State | Type | Purpose |
|---|---|---|
| `query` | string | header search input |
| `bindingFilter` | `'all' \| 'ready' \| 'gaps'` | evidence table filter |
| `density` | `'compact' \| 'comfortable'` | row padding; default `compact` |
| `accent` | hex string | accent colour; default `#C43D1C` |

`density` and `accent` were prototype tweak controls. `density` is worth keeping as a real
user preference (persist per user). `accent` is a design exploration — **do not ship it as a
user setting**; pick one value and hard-code it. If it ever becomes configurable, read
*Accessibility Rules* first: accent-as-text only clears 4.5:1 on the panel surface, with no
margin.

### Data requirements

Per compound: `id`, `name`, `programme`, `modality`, `documentCount`, `runCount`, `lastRunAt`.

Per binding: `name`, `resolveTarget` (human-readable string), `system`
(Confluence / ChEMBL / BigQuery / folder), `resolved` (bool), `returnCount` + `returnUnit`
(pages / page / row / docs), `usedByReportCount`.

Per report type: `kicker`, `name`, `sourcesReady`, `sourcesTotal`, and the per-source resolved
flags for the tick track. **Sort by coverage descending;** the top row is the lead and takes
accent treatment.

Per run: `reportName`, `claimsEvidenced`, `claimsTotal`, `ranAt`, plus a link to the draft.

Open question for the backend, unresolved at design time: **is source readiness knowable
per-compound independent of report type, or is it template-dependent?** This page assumes the
former — one readiness ledger for the compound. If bindings resolve differently per template,
this section needs a per-report-type readiness matrix instead. Confirm before building.

---

## Design Tokens

### Colour

| Token | Hex | Role |
|---|---|---|
| `FIELD` | `#D9DDE0` | page background |
| `PANEL` | `#EDF0F1` | raised panel surface |
| `PANEL_HOVER` | `#F6F8F8` | row hover |
| `HEAD_FROM` / `HEAD_TO` | `#F3F5F6` / `#E9ECEE` | header gradient |
| `BAND_FROM` / `BAND_TO` | `#E7EBEC` / `#DFE3E6` | identity band gradient |
| `THEAD_FROM` / `THEAD_TO` | `#E9ECEE` / `#E1E5E7` | table header gradient |
| `INK` | `#191C1E` | primary text |
| `SEC` | `#3A4247` | secondary text |
| `MUT` | `#4C555B` | tertiary text, links |
| `DIM` | `#5D666C` | lightest text / state indicator allowed |
| `LINE` | `#C9CFD3` | panel border |
| `RULE` | `#E3E7E9` | row divider |
| `TICK_EMPTY` | `#CDD3D7` | empty coverage tick |
| `TICK_RING` | `#A9B0B5` | unresolved band tick ring |
| `ACCENT` | `#C43D1C` | accent |
| `ACCENT_FROM` / `ACCENT_TO` | `#D24C29` / `#B83718` | primary button gradient |
| `ACCENT_DARK` | `#8F2A11` | accent text where accent is too light |
| `OK` | `#3F7A34` | connected status dot |

### Measured contrast

On `PANEL #EDF0F1`: `INK` 13.6:1 · `SEC` 8.94:1 · `MUT` 6.65:1 · `DIM` 5.12:1 ·
`ACCENT` 4.55:1 · `ACCENT_DARK` 7.31:1 · `LINE` 1.45:1.

On `FIELD #D9DDE0`: `MUT` 5.57:1 · `DIM` 4.29:1 · `ACCENT` **3.81:1 (fails)** ·
`ACCENT_DARK` 6.13:1 · `SEC` 7.50:1.

### Typography

Two faces, three jobs. **Chivo** (sans) and **Chivo Mono**, both Google Fonts, weights
300/400/500/600. Load: `Chivo:wght@300;400;500;600;700` and `Chivo+Mono:wght@400;500;600`.

Mono is not decoration. **Mono is reserved for strings with a canonical external identity** —
compound IDs, binding names, query paths, ratios, timestamps, counts — plus uppercase micro
labels. It never sets prose. Sans sets names, prose and buttons. This split is what lets a
user tell what kind of thing they are reading from the face alone.

| Step | Family / size / weight / line-height / tracking | Use |
|---|---|---|
| 1 | Chivo 600 · 32/36 · `-0.03em` | compound ID |
| 2 | Chivo Mono 600 · 20/24 | coverage count |
| 3 | Chivo 300 · 17/24 | compound subtitle |
| 4 | Chivo 400/500 · 13/18 | row titles, buttons |
| 5 | Chivo 400 · 12/18 and 11/16 | footnote, row detail |
| 6 | Chivo Mono 400/500/600 · 12 / 11 / 10 · `0.08–0.14em` upper | data, labels, section heads |

Section headings are Chivo Mono 600, 11px, `letter-spacing: 0.14em`, uppercase, `MUT`.

### Space

Base unit **2px**, working scale **2 · 4 · 6 · 8 · 10 · 12 · 14 · 16 · 18 · 20 · 32**.
Every spacing value in the build comes from this scale. Panel gutter 20px, page padding 20px,
cell gap 12px, row padding 6/10px vertical by density, `<h2>` to panel 8px.

### Radius

`4` focus ring · `5` kbd · `12` panels · `13/14/15/17/18` pills (half of element height —
compute rather than hard-code) · `50%` dots · `3/4` ticks.

### Shadow

Two-part philosophy: a tight 1–2px contact shadow plus a wide soft spread, with a
white inset top edge so panels read as raised metal rather than outlined boxes.

| Token | Value |
|---|---|
| `panel` | `0 1px 2px rgba(25,28,30,0.06), 0 6px 18px -8px rgba(25,28,30,0.28), inset 0 1px 0 #FFFFFF` |
| `header` | `inset 0 1px 0 #FFFFFF, 0 1px 0 #C4CACE, 0 4px 14px -6px rgba(25,28,30,0.22)` |
| `band` | `inset 0 1px 0 rgba(255,255,255,0.7), 0 1px 0 #C9CFD3` |
| `control-raised` | `0 1px 2px rgba(25,28,30,0.14), inset 0 0 0 1px #C4CACE` |
| `control-inset` | `inset 0 1px 2px rgba(25,28,30,0.10)` |
| `primary` | `inset 0 1px 0 rgba(255,255,255,0.28), 0 2px 6px -1px rgba(150,40,15,0.45)` |
| `primary-hover` | `inset 0 1px 0 rgba(255,255,255,0.34), 0 4px 12px -2px rgba(150,40,15,0.55)` |
| `dot-halo` | `0 0 0 3px rgba(196,61,28,0.16), 0 1px 2px rgba(150,40,15,0.40)` |

### Motion

One duration, one easing: **140ms `cubic-bezier(.2,.6,.2,1)`**. Properties: `background`,
`box-shadow`, `border-color`, `transform`.

---

## Accessibility Rules

These were each found by measurement after being wrong in an earlier revision. They are
requirements, not guidance.

1. **`DIM #5D666C` is the lightest value allowed to carry text or a state-bearing
   indicator.** `LINE`/`RULE` are hairlines only — at 1.45:1 on panel they must never carry
   text. **Do not add a fourth grey.** Three earlier revisions each invented a lighter grey
   by eye (`#8E4C51`, `#8D969C`, `#6E777D`) and each failed contrast.
2. **Accent as text is safe on `PANEL` only, and only just** (4.55:1, no margin). On `FIELD`
   it measures 3.81:1 and fails. Any accent-coloured text on the field must use
   `ACCENT_DARK #8F2A11` instead. This is why links are `MUT`, not accent — `MUT` passes on
   both surfaces (5.57 field / 6.65 panel) and cannot be broken by changing the accent.
3. **Unresolved bindings must read at full strength.** They are distinguished by *treatment* —
   hollow dot, `not reg.` in `ACCENT_DARK` at weight 500 — never by fading. The gaps are what
   the user opens this page to find; an earlier revision greyed them until they were the
   least legible thing on screen.
4. Non-text state indicators (dots, ticks) need **3:1**, per WCAG 1.4.11. The hollow dot uses
   a 2px `DIM` border to clear it. Note that sub-pixel borders are floored at DPR 1 — a
   `1.5px` border computes to `1px`, so specify `2px` when the weight matters.
5. Contrast must be verified against the **resolved ancestor background**, not the nearest
   element with a `background` declared. Most failures here came from checking a colour
   against `PANEL` when the element actually sat on `FIELD`.

---

## Assets

No images, icons or illustrations. The only glyphs are `⌕`, `→` and `·`, set in the two type
families. The brand mark is a 10×10px accent square.

Fonts are Google Fonts (Chivo, Chivo Mono) — self-host if the deployment is offline, which
matters here since the app runs locally against local data.

---

## Files

| File | What it is |
|---|---|
| `Compound Page - Titanium.dc.html` | **The design to build.** Final compound page, all values above. |
| `Report Generator.dc.html` | Earlier three-screen flow: search, compound, draft view. Reference for the **provenance gutter** and the search screen. Older visual language. |
| `Compound Page - 10 Directions.dc.html` | 26 rejected explorations across three rounds. Reference only — shows what was considered and dropped. |

Each is a self-contained HTML file; open directly in a browser. They share a `support.js`
runtime, included for completeness — it is the authoring tool's runtime and has no place in
production.

`ORIGINAL_UI_AUDIT.md` records the ten problems in the pre-redesign UI that this work set out
to fix. Worth reading before implementing, so the same patterns are not reintroduced.
