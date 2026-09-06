# OCTG Supply Readiness Platform — Requirements (consolidated, as of 2026-08)

A single-page restatement of the original requirements plus the product-owner rulings made
during development, grounded in the current implementation (19 screens, 720 tests, deployed
on Render). Implementation handover detail lives in `../HANDOFF.md`; deliberate compromises
in `../MVP_COMPROMISES.md`.

## 1. Purpose and background

- A **supply planning platform for OCTG** (oil country tubular goods). MVP with AkerBP as
  the pilot customer.
- For each well's demand (casing/tubing line items), the platform nets against company
  inventory, customer-owned material, and placed purchase orders, so that **the machine
  decides "is this well covered in steel"** and, where it is not, surfaces the remedy
  (substitution, a new mill order, or releasing a hard assignment in Oracle).
- Starting inputs: discovery workshop summary + functional spec + the AkerBP Exhaust Sheet
  (structure only; no real data in the codebase).

## 2. Users and roles

| Role | Access |
|---|---|
| admin | All business units. The only role that may write `/admin/*` master data (lead times, coverage-scope defaults, substitution tables, safety stock) |
| planner | Pinned to one BU. **Every resource the request names — by path, query or JSON body — is walked back to its BU and refused with 403 (zero writes) if foreign; every list, dashboard, MRP view and export is confined to the planner's BU.** A batch (import workbook, scenario overrides) that names anything outside the BU is refused whole. Reads of `/admin/*` master data stay open |

- Authorization (2026-09-06 ruling): what stays **open to planners inside their own BU** is a
  deliberate choice — BU create/rename/delete, customer BU remap and policy, and the manual
  company-inventory edits (C-03). Recorded as C-16 in the compromise register
- Authentication: dev email + password (PBKDF2) with a **provider structure designed to be
  swapped for Entra ID** (`AUTH_PROVIDER` env). Tokens are HMAC-signed and self-expiring
  (12h). There is deliberately no logout endpoint (no server-side session state).

## 3. Domain design principles (invariants every feature obeys)

1. **Never fabricate numbers** — "unknown" and "zero" are always distinct (`available:false`
   + reason; un-uploaded customer inventory is `has_uploaded:false`; the UI never renders
   either as 0 or a dash)
2. **Make invalid states unrepresentable** — e.g. `demand_status` lives once on the Well,
   never on a DemandLine
3. **The BU is an absolute boundary and the unit of allocation** — inventory never crosses a
   BU, and inside one it is divided ONCE across every customer below it, earliest ROS first
   (ruled 2026-09-06). At an equal ROS a Primary line is served before a Contingency line,
   then line id — a fixed order, not a priority, and the line that lost such a tie is told
   so in its reason (ruled 2026-09-06; `app/engines/allocation.py allocation_order`). What
   stays customer-private is ownership, not pooling: customer-owned stock and Oracle
   assignments are never drawn by anyone else
4. **Customer-owned inventory is consumed before company inventory**, and is never shared
   with another customer
5. **The platform never creates, releases, or overwrites a hard reservation** — the Oracle
   projections (OnHand / Assignment / OnOrder) are read-only. The only writable inventory is
   CustomerOwnedInventory
6. **Coverage is only ever written by the engine** — there is no manual override. A missing
   CoverageResult means "not evaluated", never "fine"
7. **One computation, one implementation** — previews and scenarios call the same production
   engines. No second implementation
8. **Unit of measure is mandatory on every screen** (Mtr / PC / MT) — mixed-unit
   aggregations return `quantities_by_unit`, never a scalar sum
9. **One quantity rule for every writer** (`app/quantities.py`, ruled 2026-09-06): finite,
   at most 1e9, demand strictly positive, stock (on hand / on order / assignments / safety
   stock) zero or more, whole numbers only for PC and JT. Request bodies are checked at
   the schema (422); each writer applies the unit-aware half once the product is known
   (400). Excel imports share the same rule
10. **Foreign keys are enforced on every connection**, SQLite included — an orphan row
    cannot be committed

Cross-cutting rules settled by rulings during development:

- **Overdue demand = ROS month strictly before the current month**, uniform across all
  screens. It is counted but labelled as its own bucket — never silently blended in
- **Colour semantics**: red = steel is physically absent (only a mill order fixes it);
  amber = steel is in the yard but waiting on a human action (Oracle release, approval);
  purple = not modelled. **Red and amber are never merged.** The overall palette stays calm
  and pastel (no garish colours)
- **A suggestion never wears a promise's colour** — incoming supply is split by certainty:
  blue = placed PO, amber italic = recommended (not yet placed) order
- **Final decisions take two steps** (ConfirmButton: first click arms, auto-disarms after
  4 s or on blur)
- **Filters, sort, tabs, and view toggles persist in the URL** (state survives reload and
  link sharing)

## 4. Functional requirements (by screen)

### 4.1 Demand side
- **Demand Import**: Excel template export → upload → conflict review → apply as a revision
- **Demand List**: server-side paging; all 7 filters held in the URL
- **Well Workspace**: per-well line items, revision history, status changes
  (Planned / Budgeted / Confirmed)

### 4.2 Verdicts and remedies
- **Coverage Workspace**: customer × well × line coverage grid. Verdicts = Covered /
  CoveredViaSubstitute / PendingApproval / Uncovered / Unrecoverable, each with a reason
  sentence and recommended action. Failures are isolated per customer (one customer's bad
  data never takes the whole grid down; skipped customers are named in the response)
- **Substitution Workspace**: substitution proposals with two approval layers (customer
  approval + well approval). Blocking priority: customer > well-approval > oracle-release.
  Requests that touch hard-assigned stock show a pre-submission warning
- **Approval Queue**: Pending/Approved/Rejected tabs with in-place Approve/Decline
  (two-step confirm; re-deciding a decided approval returns 409 — overturning requires a
  new request)

### 4.3 Supply planning
- **MRP Summary / By Item**: the monthly ledger is full accounting form — **opening balance
  (carried forward, split company/customer-owned) + incoming in two certainty tiers (placed
  PO / recommended) − outgoing demand = ending balance (same split)**. Three runout series
  (baseline / with-recommended / on-order) plus the ledger series. POs without a due date
  cannot be placed in a month and are noted separately. Excel export mirrors the screen's
  columns and colours
- **Material Order Requirements (MOR)**: monthly ROS-bucket demand netted against
  inventory + customer-owned material + dated POs; ex-mill (order-by) month = need month −
  lead time. The strip is a single end-of-month balance series with ▲ order-by, ▼ physical
  runout (red), ▽ dip into safety stock (amber). **Safety stock acts on MOR only** (it
  deliberately does not affect coverage verdicts). With a customer filter, netting uses
  only that BU's inventory and POs (no BU crossing)
- **Surplus List**: on-hand decomposed as OH = Allocated + Surplus + Obsolete (the identity
  is pinned by tests). Monetary valuation is deferred until a Price master exists

### 4.4 Executive view
- **Executive Dashboard**: five blocks (demand trend / coverage / supply risk + first
  runout / soft allocation / inventory utilisation). **MT-unified headlines with
  native-unit breakdowns** (conversion is display-layer only)
- **Scope filter (Executive / Surplus)**: well status and line profile are variable per
  request. Default scope = fast read of the stored official verdicts; non-default =
  **read-only recompute followed by rollback** (stored rows are never dirtied). Non-default
  scope shows an amber banner stating these are not the official verdicts
- **Scope note on MRP/MOR screens**: states that these screens compute on the
  Administration default scope (Confirmed wells · Primary/Contingency) and that the
  Executive/Surplus checkboxes do not affect them

### 4.5 Scenarios (what-if)
- **Scenario List / Editor**: overrides for quantity, ROS date, well status, PO arrival
  date, hard-assignment release, and approval flips. Drag-and-drop timeline. Preview
  recomputes through the production engines. **On apply, Oracle-owned (supply-side) data is
  rejected** — the platform does not touch hard reservations. Scenarios have no DELETE
  endpoint (deliberate)

### 4.6 Inventory and master data
- **Company Inventory** (including an Oracle assignments tab) / **Customer-Owned
  Inventory** (template download + upload; the only platform-writable inventory) /
  **Product Workspace**
- **Business Unit lifecycle**: a BU can be created, renamed, and deleted — but a delete
  is refused (409, itemised) while any customer, inventory row, upload, user or scenario
  override still resolves through it. BUs are flat by design: there is no parent/child
  hierarchy, because the inventory pool is a flat join and no engine walks a tree
- **Explicit coverage recompute**: `POST /coverage/recompute` rewrites the stored
  verdicts for every customer, or one named customer, under the platform default scope.
  Failures are isolated and committed per customer, so a customer with incomplete
  inventory facts is named and skipped rather than costing everyone their recompute.
  It takes no scope filters — the official verdict is the default-scope one
- **Administration (5 tabs)**: BU hierarchy / lead times / coverage scope defaults /
  substitution master data (technical pairs + customer rules) / safety stock
  (unset null ≠ explicit 0)

### 4.7 Home and shared conventions
- **Home Dashboard**: KPI band, Demand changes (linked to wells), and a **Pending approvals
  card defined as a union** (all undecided requests + pre-request PendingApproval-verdict
  lines)
- **SPA navigation**: several client routes share a path with an API route. A browser
  navigation (HTML-first `Accept`) is answered with the app shell; the SPA's own
  `fetch` on the same URL still reaches the API, and the response carries
  `Vary: Accept` plus `no-store` so a cache cannot serve one as the other. The rule is
  an exemption list — the API docs, the health probe, and the `/template` and `/export`
  downloads — so adding a screen needs no server change
- Every screen: Freshness (fetched time + Refresh), errors keep the currently displayed
  data, a 404 page, breadcrumbs (never showing a UUID while loading)
- **User manual**: `/manual.html` (no login required), with screenshots, updated with every
  functional change

## 5. Non-functional requirements

- **Stack**: FastAPI + SQLAlchemy + Alembic (backend); React + TypeScript + Vite with plain
  CSS and no external UI library (frontend). Dev DB = SQLite (`dev.db` committed as the
  staged demo state); Postgres-ready for production
- **Mobile**: approach A = one responsive SPA (no separate UA-detected site). Screens
  collapse to cards at ≤640 px, but **grids that would lie when folded (Coverage, MOR, MRP)
  keep horizontal scrolling**
- **Design**: dark-navy sidebar with a pastel palette (Ascend SCM concept). No dark mode
  (explicit user decision). Motion and depth follow apple-design guidance
  (reduced-motion respected)
- **Deployment**: Render (`render.yaml`, free plan — spins down after ~15 min idle).
  Push auto-redeploys. The DB is the in-image `dev.db` (every redeploy resets to the demo
  state). `VITE_API_BASE=""` yields relative URLs in production
- **Testing**: pytest (currently 750 tests) pinning identities and boundaries.
  Agent-written tests are cross-checked from the outside against real data

## 6. Out of scope / deferred (pre-pilot backlog)

Pilot launch date is undetermined as of 2026-08-20 (grilling session ruling) — the items
below are not calendar-urgent, but security stays pilot-blocking regardless of date per the
existing ruling. Next up, ranked 2026-08-20: **(1) Monetary valuation**, with security vs.
notifications ordering deliberately left open until valuation lands.

1. **Monetary valuation — Price master (in design, ruled 2026-08-20).**
   - Single currency (no FX conversion)
   - POC data: sample rows, platform-editable directly (no Excel template/upload flow,
     unlike Company/Customer-Owned Inventory); Oracle-fed pricing is a future integration
   - New `Numeric`/`Decimal` money type — the first in the codebase (all other quantity
     columns are `Float`; money precision needs its own convention)
   - Obsolete stock valued at a discount to Surplus; applies directly to the existing
     Obsolete bucket (`app/engines/surplus.py`, OH = Allocated + Surplus + Obsolete, itself
     computed from the 36-month demand horizon) — **no new age/condition field needed**.
     Discount rate is undecided pending accounting input; POC proceeds on a placeholder
     until that's settled
   - Scope: **Surplus List first.** Executive Dashboard money figures are a separate,
     later task, not bundled with this one
   - Admin surface: extends **Product Workspace** (not a new Administration tab)
2. Security — ~~apply `require_admin` to the /admin routes~~ (done 2026-09-06, writes only
   by ruling); ~~C-13 object-level authorization and list filtering~~ (done 2026-09-06 except
   `/mrp/by-item`, closed the same day as C-15); C-12 (remove the AUTH_SECRET dev fallback); C-14
   (`?access_token=` download links); login rate limiting. Must be closed before pilot
   exposure regardless of pilot date — ordering vs. item 3 below intentionally undecided
2b. From the 2026-09-06 adversarial review, ruled and queued: ~~F04 net shortfall with a
   breakdown (demand / customer-owned / company / substitute / residual) as the MRP figure;
   F05 recompute every affected customer in the BU on an assignment change; F06 decided
   approvals immutable at the service layer (scenario apply included); F09 actor recorded
   server-side; F07 import approvals bound to the revision they approved; F08 scenario
   apply requires the previewed version~~ (all done 2026-09-06, packages 2 and 3). Open
   question from F04: a substitute-rescued line keeps its partial own-product draw and
   is charged whole against the substitute (C-17) — ruling needed
3. Notifications — Teams/mail push (new uncovered wells, pending approvals, approaching
   order deadlines)

**Ruled acceptable for pilot as-is (2026-08-20), no migration needed beforehand:**
- Render free plan (15-min idle spin-down, `dev.db` reset on every redeploy)
- Dev email+password auth (Entra ID swap is a post-pilot item, not a pilot blocker)

**Explicitly out of scope:**
- Writing to Oracle from the platform (creating or releasing hard assignments)
- Manual coverage overrides
- Dark mode
- ~~C-10~~ — closed by retiring the cross-customer sharing what-if (2026-09-06): with the
  pool divided across the whole BU there is no neighbour's surplus left to ask about
