# OCTG Supply Readiness Platform — Visual System

Date: 2026-08-22
Supersedes: the design line in `2026-08-19-octg-platform-rebuild-design.md` §5
("dark navy sidebar + pastel base, Ascend SCM concept").
Authorised by the product owner on 2026-08-22 ("抜本的に見直して" — overhaul it).

## Thesis

The platform answers one question: can this well be covered with steel? The
answer is binary, so the surface is an **instrument**, not a soft dashboard:
flat sheets, hairlines, no shadows. Every soft shadow is a lie about softness in
a domain where you either have the pipe or you do not.

Two devices are borrowed from the subject's own world:

- **Paint bands.** Steel grade is marked on real pipe with painted bands. Status
  in this app is therefore a hard-edged band — on a row's leading edge, on a
  panel, and in place of the usual rounded status pill.
- **Strips and gauges.** The monthly balance strip already used by Material
  Order Requirements is promoted to the app's common instrument: MOR months,
  MRP runouts, and the coverage mix all read as strips.

## Tokens

Ground is zinc / mill-scale grey; sheets are white paper on a bench. The
identity accent is petrol blue-green, deliberately placed **outside** the
semantic hue range so it can never be misread as a status.

| Token | Value | Role |
|---|---|---|
| `--ground` | `#e6e9ed` | page ground |
| `--sheet` | `#ffffff` | panels |
| `--ink` | `#141a21` | text |
| `--muted` | `#5c6773` | secondary text |
| `--line` / `--line-soft` | `#ccd2d9` / `#e2e6ea` | hairlines |
| `--rail` | `#171c20` | navigation rail (painted machinery black) |
| `--accent` / `--accent-deep` | `#0f6d84` / `#0a4d5e` | links, active state, focus |
| `--ok` | `#3f7a52` | covered |
| `--warn` | `#9a7420` | waiting on a human |
| `--bad` | `#a3342f` | steel physically absent |
| `--unmodelled` | `#6b7076` | outside the model |
| `--info` | `#1f4e79` | booked PO receipts |
| `--band` | `3px` | the paint band |

Semantics are unchanged from the parent spec §3: **red means the steel is not
there; amber means a person has to act. The two are never merged.**

## Type

IBM Plex, loaded from Google Fonts with a system fallback stack.

- `--cond` IBM Plex Sans Condensed — headings, navigation, table headers, labels
- `--sans` IBM Plex Sans — body
- `--mono` IBM Plex Mono, tabular figures — every quantity, date, month key and
  count. These are read by comparing them vertically, so the mono face is
  functional, not stylistic. The `.num` class is the hook.

## Status marks

`.verdict-chip` renders a band plus a label instead of a filled pill:

| Verdict | Band |
|---|---|
| Covered | solid green |
| Covered via substitute | hollow green (the steel is there, but not the item asked for) |
| Pending approval | hatched amber (settled states are solid; waiting states are hatched) |
| Uncovered | solid amber |
| Unrecoverable | solid red |
| Not evaluated | hollow grey |

## Signature

`CoverageBand` — a well's verdict mix as one segmented paint band, ordered
best-to-worst and sized by real proportion. Scanning the Coverage grid
vertically shows the yard's state as a stripe pattern. It is data, not
decoration; the counts are exposed via `aria-label` and `title`.

## Structure

- The navigation rail is grouped in business-flow order (Overview, Demand,
  Readiness, Supply, What-if, Data). The group labels encode the shape of the
  work rather than decorating the list.
- A panel only wears a band when it carries a status. A band that always
  appears would encode nothing.
- Filter controls are a hairline toolbar (`.filters`), never a boxed form
  nested inside a white sheet. Real forms keep their fieldsets.
- Verdicts and other enums are written the way a planner says them
  ("Covered via substitute"), never as the raw enum. Timestamps show minutes,
  not microseconds.

## Constraints kept

No dark mode. Plain CSS, no UI component library. Coverage, MRP and MOR grids
keep horizontal scroll and never card-ise. `prefers-reduced-motion` disables all
animation. Function and information structure are unchanged — this was a visual
pass only.
