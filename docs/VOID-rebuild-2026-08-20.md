# VOID — the spec-driven rebuild (2026-08-19 → 2026-08-20)

**Status: void. Do not build on it, deploy it, or copy from it.**
It is preserved whole, and unmodified, on the branch
[`superseded/rebuild-2026-08-20`](../../../tree/superseded/rebuild-2026-08-20)
(head `34016de`). Nothing was deleted; `main` simply stopped being it.

## What it was

A second, independent implementation of the OCTG Supply Readiness Platform,
rebuilt from written specs over two days in eight staged passes
(`docs/plans/2026-08-19-octg-stage1..8`, preserved on that branch). It reached
21 API routers, 9 engines and 322 tests, and it worked.

## Why it is void

A far more developed implementation of the same product already existed — the
one now on `main`. On the axes that decide which survives, it was not close:

| | Retired rebuild | Adopted implementation |
|---|---|---|
| Backend tests | 322 | **666** |
| Engines | 9 | **26** |
| API surface | 58 paths | **80 paths** |
| Screens | comparable | comparable |
| UI and data presentation | — | **decisively better** (the deciding factor) |
| Open security compromises | identical | identical |

The UI, the shape of the data as it reaches the screen, and the accumulated
domain rulings were the deciding factors. Keeping two implementations of one
product alive is how they both rot, so this one was retired deliberately rather
than left to drift.

## What was taken from it before it was retired

It was read carefully first. Four things it did better were adopted into the
surviving implementation, and each one is a real improvement, not a courtesy:

1. **`PATCH /business-units/{id}`** — a Business Unit could be created and never
   renamed, so a typo in the one field a human navigates by was permanent.
2. **`DELETE /business-units/{id}`** — and never removed. The refusal is the
   feature: a BU that still has customers, stock, uploads, a pinned planner or a
   scenario override cannot be deleted, because stranding those rows against a
   dead id breaks coverage rather than tidying anything. The adopted version
   returns itemised counts so the screen can list what has to move.
3. **`POST /coverage/recompute`** — the explicit trigger that C-08 and
   `HANDOFF.md` §7 both describe as missing: after an out-of-band data change or
   an engine fix, the code is right and the database holds yesterday's answer,
   with no legal way to ask for a new one. The adopted version commits and
   isolates **per customer**, which the rebuild's did not need to and the
   surviving read path deliberately does not do.
4. **The SPA-navigation middleware as an exemption list** — the rebuild had
   inverted the rule from "enumerate every client route" to "exempt the few
   paths that must answer for themselves". That inversion is correct: the route
   list carried a *keep in sync with the router table* warning, and that sync had
   already failed once in production-shaped use. Adopted — with the hole in its
   version closed: it exempts only the API docs, so it would swallow its own file
   downloads, every one of which is an `<a href>` navigation and therefore
   arrives with an HTML-first `Accept` header, indistinguishable from a screen
   reload.

## What was deliberately NOT taken

**Business Units as a parent/child tree.** The rebuild models them as a
hierarchy with cycle-checked reparenting. The surviving implementation is flat
on purpose — `app/engines/coverage.py` states that the pool a customer draws
from is a flat join needing no recursive walk — so importing a hierarchy would
add a field an operator can set, that looks meaningful, and that no engine
reads. A test pins the omission so it reads as a decision rather than a gap.

Its security posture offered nothing either: `require_admin` defined and never
applied, the `AUTH_SECRET` dev fallback, and no object-level authorization —
the same three holes, tracked here as C-12 / C-13 and the `require_admin` item
in `MVP_COMPROMISES.md`.

## If you need something from it

Read it on `superseded/rebuild-2026-08-20`. Its specs and stage plans are the
most useful part that did not come across, because they state the requirements
in a form written down before the code existed. Bring ideas over; do not bring
code over without checking it against the tests here first.
