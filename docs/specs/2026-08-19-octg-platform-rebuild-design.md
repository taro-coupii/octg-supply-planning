# OCTG Supply Readiness Platform — Clean-Room Rebuild Design Spec

Date: 2026-08-19
Target branch: `claude/using-superpowers-skill-0ix8nt`
Source of truth for requirements: `octg-platform/REQUIREMENTS.md` (branch `claude/octg-supply-planning-hpej8e`, commit `1e60cf2`)

## 1. Background and goals

A completed OCTG supply planning platform already exists on `claude/octg-supply-planning-hpej8e`
(19 screens, 638 pytest cases green, published on Render). This project
**rebuilds it from scratch, relying only on REQUIREMENTS.md**.

Goals (product owner ruling, 2026-08-19):

- **Process validation** — validate the effectiveness of the superpowers workflow (brainstorm → spec → plan → TDD) using this material
- **Leading candidate** — if it turns out well, this rebuild becomes the mainline. Specs, plans, and tests are kept at production quality

## 2. Decisions (Q&A record)

| Point | Ruling |
|---|---|
| Approach | Rebuild from scratch (the existing implementation is not used as a deliverable) |
| Fidelity | **Full-fidelity reproduction** — the deliberate deferrals in §6 (the full security suite, monetary valuation, notifications) stay deferred in the rebuild as well. MVP compromises (C-numbers) are reproduced too, after being logged in the compromise register |
| Code reference | **Clean room** — the existing code is not read. Written only from the spec descriptions in REQUIREMENTS.md and HANDOFF/MVP_COMPROMISES. Behavioral questions are resolved by asking the product owner |
| Deployment | **In scope this time.** Includes switching Render's target to this branch and publishing (if changing the Blueprint's branch requires a dashboard action, the user is asked to perform only that one action) |
| Authentication | **Deferred to Stage 7** (the original implementation also introduced auth late, on 8/11. Stages 1–6 are built without auth, logged as the equivalent of C-01 in the compromise register) |
| Progression | Staged construction in 7+1 stages. Each stage has its own independent spec → implementation plan → TDD implementation cycle |

## 3. Overall architecture

### Layout

A new `octg-platform/` directory at the repo root:

```
octg-platform/
  backend/     FastAPI + SQLAlchemy 2.0 + Alembic
  frontend/    React + TS + Vite (plain CSS, no external UI library)
  Dockerfile.deploy
render.yaml    (at repo root. Deploy source = this branch)
```

### Backend — 3 layers

- `app/models/` — SQLAlchemy models. "Make invalid states unrepresentable" (§3-2) is enforced via schema constraints
- `app/engines/` — the **sole** implementation of judgment/calculation logic (coverage / allocation / MRP / MOR / surplus / executive / scenario preview). Only the engines may write calculation results (§3-6). Preview / scenario / sharing all call the same engines too (§3-7)
- `app/api/` — routers. BU-boundary authorization and the input/output contract (`unit` required on every quantity payload; mixed aggregates use `quantities_by_unit`) are enforced here (§3-8)

DB: dev = SQLite (`dev.db` committed in a demo state), Postgres support maintained (psycopg2).
Alembic migrations are added incrementally per stage (not one bulk initial schema).

### Authentication (introduced in Stage 7)

- `AuthProvider` abstraction. `AUTH_PROVIDER` env selects dev (email + PBKDF2); an Entra ID substitution point is provided as a type
- Tokens: HMAC-signed, self-expiring at 12h, no server-side session, no logout endpoint
- Roles: admin (cross-BU) / planner (fixed to 1 BU; specifying another BU's customer is 403)
- The dev fallback for `AUTH_SECRET`, etc. is logged as C-12–C-14 in the compromise register and reproduced faithfully

### Frontend

- A full set of design tokens under `:root` in `index.css`. **The visual language was replaced on 2026-08-22** — the current system is defined by `2026-08-22-octg-visual-system.md` (instrument panel / paint bands / IBM Plex, zinc-grey ground, petrol accent). The meanings of the semantic colors `--ok/--bad/--warn/--unmodelled` and the rule that red and amber are never merged are unchanged.
- No dark mode (explicit requirement). Supports reduced-motion
- SPA routing with a 404 catch-all. Breadcrumbs never show a UUID while loading
- Filters, sort, tabs, and view toggles persist in the URL (§3 cross-cutting rule)
- Color semantics: red = physically absent / amber = awaiting human action / unmodeled is its own independent channel. Red and amber must never be merged. For inbound: blue = PO already placed, amber italic = recommended order

### Testing / Definition of Done (common to all stages)

- TDD (per superpowers test-driven-development). Centered on pinning down engine identities and boundaries
- Gate for each stage: pytest green + `tsc --noEmit` exit 0 + successful vite build
- Example identities: OH = Allocated + Surplus + Obsolete; monthly ledger opening + receipts − issues = closing

## 4. Data model skeleton

**Organization / masters**

- `BusinessUnit` (hierarchical. The boundary inventory must never cross)
- `Customer` (belongs to a BU = default boundary. Has allocation-policy attributes — the precise semantics of SOFT/HARD etc. are an open question to confirm in the Stage 4 spec)
- `Product` (UoM: Mtr/PC/MT. Weight for MT conversion is nullable — reproduced as compromise C-06)
- `LeadTime` (with attribute dimensions), `SafetyStock` (unset null ≠ explicit 0. Affects MOR only)
- `SubstitutionRule` (technical pairs) + customer rules

**Demand side**

- `Well` (belongs to a customer. `demand_status` (Planned/Budgeted/Confirmed) lives **only here**)
- `DemandLine` (casing/tubing line items. ROS date, quantity + unit, profile Primary/Contingency, `created_at`)
- `DemandRevision` + import staging (history from template → conflict review → revision applied)

**Supply side (read-only projection of Oracle)**

- `InventoryOnHand` / `InventoryAssignment` (hard allocation) / `InventoryOnOrder` (dates optional, booking_status)

**The only inventory the platform may write**

- `CustomerOwnedInventory` + upload history (`has_uploaded:false` distinguished from "zero")
- Exception: C-03 (manual maintenance of company-owned inventory) is reproduced after being logged in the compromise register (gated to retire automatically once the Oracle feed is introduced)

**Calculation results / workflow**

- `CoverageResult` (written only by the engine. 5-value judgment enum + reason + recommended action + `computed_at`. No row = not yet evaluated)
- `SubstitutionApproval` (two tiers: customer approval and well approval. Re-deciding an already-decided item is 409)
- `Scenario` + overrides (no DELETE. Applying is rejected when the target is Oracle-owned data)
- `User` (email + PBKDF2, role, planner fixed to a BU)

## 5. Stage breakdown

Each stage has its own independent spec → implementation plan (writing-plans) → TDD implementation cycle.

1. **Foundation (no auth)** — scaffold, design tokens + sidebar shell, 404, shared primitives (`ConfirmButton` · `Freshness` · URL state helpers · `lib/enums` · `lib/sort`), minimal masters (BU hierarchy · customers · products) + Alembic init
2. **Masters / inventory** — Administration (5 tabs), company-owned inventory (Oracle projection + assignments tab), customer-owned inventory (template download + upload)
3. **Demand** — Demand Import / Demand List (server-side paging, 7 filters in the URL) / Well Workspace
4. **Coverage / actions** — the judgment engine (core), Coverage Workspace, two-tier Substitution approval, Approval Queue, Cross-Customer Sharing
5. **Supply planning** — MRP Summary/By Item, MOR, Surplus, Excel export, safety stock
6. **Executive / scenarios** — Executive Dashboard (5 blocks, MT-unified headline), scope filter (read-only recompute → rollback), full Scenario suite
7. **Authentication** — provider structure, PBKDF2, HMAC 12h, BU-scope 403, Login screen, user menu, seed_users
8. **Finishing / deployment** — Home Dashboard, full demo seed, user manual (`/manual.html`), cross-cutting checks for mobile/URL state/freshness, **Render cutover (to this branch) + confirming publication**

Dependency notes: the engine (4) depends on masters/inventory (2) and demand (3). Supply planning (5) uses the allocation results from 4.
Home (8) aggregates every screen, so it comes last. Authentication (7) is a cross-cutting task retrofitted onto all APIs.

## 6. Stage 1 "Foundation" detail

**Backend**

- `octg-platform/backend/` scaffold: `app/` (empty packages for models / engines / api / auth), Alembic init, pytest config, `requirements.txt` (fastapi / uvicorn / sqlalchemy>=2.0 / alembic / pydantic v2 / openpyxl / httpx / pytest / psycopg2-binary / python-multipart)
- First migration: 3 tables — `business_units` (hierarchical), `customers`, `products`
- Minimal API: `GET /business-units` (hierarchy) · `GET /customers` · `GET /products` · `GET /products/{id}` (404 for a nonexistent ID). Write endpoints come in Stage 2 onward. No auth (logged in the register as the equivalent of C-01)

**Frontend**

- Vite + React + TS scaffold, plain CSS
- Full token set in `index.css`, sidebar rail, routing skeleton + 404, breadcrumb foundation
- Shared primitives: `ConfirmButton` (armed on first click → disarms after 4s or on focus loss), `Freshness`, URL state helpers, `lib/enums.ts`, `lib/sort.ts` (3-click cycle back to server order, nulls last)

**Tests (written first, per TDD)**

- Read contracts for the master APIs (404 for nonexistent ID, list shape)
- The 3-state transition of `lib/sort`, nulls last
- Gate: pytest green + tsc clean + successful vite build

## 7. Requirements-document accuracy memo (cross-check results)

On 2026-08-19, 11 agents cross-checked 115 claims in REQUIREMENTS.md against the existing implementation.
**101 matched, 14 partial mismatches/discrepancies.** Every item in the "not yet started backlog" in §6 was confirmed to be genuinely not started.
The rebuild follows **REQUIREMENTS.md as governing (doc governs)**; the following discrepancies are handled per the rulings below:

| # | Discrepancy | Ruling |
|---|---|---|
| 1 | §2 role table says "Administration is admin" → implementation does not apply require_admin (planners can operate it too) | Since §6 explicitly states this non-enforcement is a known hole, **reproduce the hole as-is** and log it in the register |
| 2 | "Planner is fixed to 1 BU" → implementation only checks against an explicit customer_id (C-13) | Reproduce as C-13, log in register |
| 3 | §3-5 "only customer-owned inventory is writable" ⇔ the C-03 company-owned inventory maintenance screen exists | C-03 is part of the requirements (via the register). **Build the maintenance screen.** Re-read §4.6's "the only writable one" as "among the Oracle projections" |
| 4 | §3-7 single engine ⇔ sharing's what-if deviates per C-10 | Reproduce as C-10 (open by choice) |
| 5 | The §3-8 unit list (Mtr/PC/MT) may not be exhaustive | Unit contracts are managed via a registry; the implementation's list is authoritative |
| 6 | "Purple = unmodeled" → implementation uses gray | The essential point is **being an independent channel**. The color is fixed by the design tokens (Stage 1); the tokens are authoritative from then on |
| 7 | URL persistence "for everything" → the Approval Queue tab is component state | doc governs: **the tab is URL-persisted too** (an improvement to match the requirements document) |
| 8 | Well Workspace "revision history" → implementation shows only the latest summary | doc governs: show a history list (assuming DemandRevision is being recorded) |
| 9 | Coverage "customer × well × line item" → the grid is customer × well, line items live on the Well side | Follow the existing screen split (interpreted as satisfying "× line item" via one click through) |
| 10 | 3 runout series → the on-order series is preview-only | Implement all 3 series as part of the engine spec. On-screen display to be finalized in the Stage 5 spec |
| 11 | Freshness "on every screen" → implementation is dashboard/analytics screens only | doc governs: place it on every screen (a shared primitive, so cost is low) |
| 12 | "Preserve displayed data on error" → some screens go blank | doc governs: preserve it on every screen |
| 13 | "Auto-redeploy on push" cannot be statically verified | Confirm live in Stage 8 |
| 14 | §4.4 scope filter's "profile" ⇔ term overlap with customer policy (SOFT/HARD/HYBRID) | Define and separate the terms in the Stage 4 spec (demand profile = Primary/Contingency; customer policy = allocation behavior) |

## 8. Out of scope

- Monetary valuation (Price master), notifications (Teams/email) — deferred per §6
- Writing to Oracle (creating/releasing hard allocations), manual override of coverage, dark mode — explicitly out of scope
- Entra ID implementation (only the substitution point is provided)

## 9. Success criteria

- All functional requirements in REQUIREMENTS.md are met, inclusive of the 14 rulings above
- The project's own test suite (pinning identities and boundaries) is green. The existing 638 tests are not ported over (clean room)
- Each stage's spec, implementation plan, and compromise register are present under `docs/superpowers/`
- This branch is published on Render, with the demo seed carrying the flow from no-login through to (Stage 7 onward) logged-in
