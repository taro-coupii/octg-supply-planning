# OCTG Supply Readiness Platform — Handover

Last updated: 2026-08-11

## 1. What this is

The MVP of an OCTG (oil country tubular goods) supply planning platform, with AkerBP
as the pilot customer. It was built starting from a discovery workshop summary, a
functional spec, and `20221215_AkerBP Exhaust Sheet (3).xlsx` (opened read-only to
check its structure; none of its real data is in the code).

> ## 📌 Read this first: [`MVP_COMPROMISES.md`](MVP_COMPROMISES.md)
>
> It is the **complete list of the places where the §4 design principles were bent on purpose
> for the MVP**. When the real implementation starts, work down that list from the top.
> Every affected place in the code carries a `MVP-COMPROMISE[C-xx]` marker, so one grep returns all of them:
>
> ```bash
> grep -rn "MVP-COMPROMISE" --include=*.py --include=*.tsx octg-platform/
> ```
>
> **When you bend a principle anew, put a marker in the code and add a row to the register. It is finished only when both are done.**

**Where it stands: 19 screens work (Login / Home / Coverage / Approvals / Executive / Demand / Import / MRP / Order requirements / Surplus / By Item / Products / Customer-owned / Company inventory / Scenarios ×2 / Sharing / Well / Admin, plus 404). 628 tests pass. Authentication is implemented (§6) — the authorization holes that remain are C-12/C-13/C-14 plus `require_admin` not being applied (deferred on 8/12, see the backlog above).**

**Work on 8/10: ① the UI overhaul (§0A/0B) ② the full company-inventory maintenance feature (backend CRUD plus screens, C-03) ③ the MT conversion layer (C-04/05/06) ④ an inventory utilisation block on the Executive Dashboard (demand netting, a 12/18/24/36-month variable window, an MT headline with a native breakdown).**

**Work on 8/11: the whole authentication stack (see §6 — dev login with an Entra-swappable structure, enforced BU scope, a Login screen, a user menu in the sidebar). migration head: `c8d41a92e7f3` (applied to dev.db, 3 dev users seeded). Moved to GitHub (repository `taro-coupii/Claude-Private`, branch `octg-supply-planning-platform`). This work was done in a remote container (Linux) — the backend uses `.venv-linux/`, and the Windows paths in §2 are for the local environment.**

**8/11 addition: the Executive Dashboard was unified in MT — every block gained a `*_tonnes` headline (display layer only, the C-05 boundary unchanged, the native breakdown kept as ground truth). The engine side is `pairs_to_tonnes` in `executive.py`; tests are `test_executive_mt.py` (602 tests pass).**

**8/11 data refresh: customer names changed to Norheim Energy (SOFT · North Sea) / Vestfjord Petroleum (HARD · North Sea) / Pelican Gulf Drilling (HYBRID · Gulf), replacing the old Demo Operator A/B/C. `dev.db` was rebuilt with `seed --reset` plus `seed_users` (removing the residue of manual demo operations and refreshing the base date). The canonical way to refresh the data is exactly those two commands → commit dev.db → push (a Render redeploy then lands in a clean state). `--reset` was extended to cover the customer_owned / company_inventory tables; users are kept on purpose. no-store/Vary was added to the SPA index.html (a real bug fix: on `/coverage` and friends, a navigation's cached HTML was being picked up by fetch).**

**8/11 new screen: Material Order Requirements (`/mrp/order-requirements`) — a port of the workbook's Material Order Req tab. Monthly ROS-bucket demand is netted against inventory, customer-owned material and dated POs; the incremental shortfall is booked in the month it first appears; ex-mill (the order-by month) = the need month minus the lead time (ceiling). Engine `app/engines/mor.py`, API `GET /mrp/order-requirements`, tests `test_mor_engine.py` (609 tests pass). The UI is the pilot for the "calm grid" language (summary sentence → row → expand for the monthly ledger, urgency chips, shape before colour). Unit contract: every quantity payload must carry a unit (enforced by `test_units_of_measure.py`; see the `UNIT_REACHED_VIA` registry).**

**8/11 UI refresh: the mock-ups supplied by the user (the Ascend SCM concept) were adopted as the visual language — a dark-navy sidebar rail, a pastel base with an indigo accent, rounded cards with soft shadows, status chips with a dot. Function and information structure are unchanged (the user's instruction: "no functional changes, this is only the look"). The domain colours (`--ok`/`--bad`/`--warn`/`--unmodelled`) kept their meaning while being desaturated to pastel. The token set lives in `:root` in index.css; the rail uses the `--rail` family.**

**8/11 three feature additions: ① Surplus List (`/surplus`) — `app/engines/surplus.py` reuses utilisation (36 months) to decompose on-hand into Allocated + Surplus + Obsolete (the identity is pinned by a test; Obsolete = zero demand within the horizon; monetary valuation deferred until a Price master exists) ② By Item extended — RunoutPoint gained a Primary/Contingency split and two parallel balances (Cust/Owned; customer material is drawn first, an approximation stated in the docstring), and `InventoryOnOrder.booking_status` (migration `e5b2c7d94a18`) splits PO'ed from Book'ed ③ Safety Stock — `safety_stocks` (migration `f7c3a91b5e24`), a fifth Admin tab (unset null ≠ an explicit 0), acting **on MOR only** (an order requirement is raised from the month the buffer is breached) and deliberately not intervening in coverage verdicts. 617 tests pass. migration head: `f7c3a91b5e24`, dev.db reseeded.**

**8/11 feedback pass: ① the demo data was rebuilt wholesale — `seed/seed_demo.py` (new, and the source of truth for dev.db and Render): a 10-product catalogue, BU = SCEU Norway, Equinor Norway (SOFT) / AkerBP Norway (HYBRID), 10 wells per year per customer over 3 years (year 1 almost all Confirmed → year 3 almost all Planned), 5 casing designs (TBG the longest), inventory covering 6 months of confirmed demand (exceptions: item5 short by 60%, item1 heavily over-stocked), on-order in months 7-12 (exceptions: item4 has no row at all = unknown, item9 has a PO with no date), substitutions 2⇄3 / 6→5 / 8→9 plus 3 pending and 2 approved. **The old `seed_from_workbook.py` is kept as test-only** (`test_seed_honesty_paths.py` uses it directly). The canonical data refresh is `seed_demo --reset` → `seed_users` → commit dev.db → push. ② MRP export: Demand Detail became a tab per product (D01..Dnn, the same monthly ledger as the By Item screen plus a detail block). ③ Order requirements: arrival ticks (green top edge), runout month (red border), order-by month (indigo border); the expanded ledger reads Incoming / Outgoing / Ending balance. ④ Executive simplified to 5 blocks (Supply risk merged with First runout, Incoming supply removed, the utilisation detail collapsed by default, MT-unified, sorted by not-tied descending, per-row MT conversion added in the engine). 617 tests pass.**

**8/11 feedback pass 2 (7 items): ① realism in the demo data — `seed_demo` gained a fixed-seed RNG (`RNG = random.Random(20260811)`, so it stays reproducible): quantity jitter of ±15-25% (with a correction that keeps TBG the longest), ROS dispersion (a random day 1-28 plus a ±1 month shift, 10-35 days of phase between the strings of one well, and the date overlap between customers removed), weighted design/asset selection, varying inventory factors, and items 7/9/10 deliberately short so that 5 wells fall in a gap (Unrecoverable/Uncovered) within 120 days. One programme per year starts two months in the past, which brings the Executive demand trend's period-on-period comparison to life. (**Note: the trend cuts off on `DemandLine.created_at > as_of` first, so the seed must backdate the created_at of both revision 1 and the line.**) Demand change history: 10 revisions plus one BUDGETED→CONFIRMED promotion through the production `apply_revision` / `set_well_demand_status`, with created_at backdated by a subsequent UPDATE (giving 15 "Demand changes" on Home). Two safety stocks (item5/8). The seed ends with a demo-case audit (WARNINGs on the gap-well count and the ImpactRecord count). ② Colour — the monochrome constraint was withdrawn in favour of meaning: `--ok` returns as sage green #4a7c59, `--warn` as a muted amber #a8833b, Uncovered becomes a red tint (the solid dark navy is gone), and solid deep red for Unrecoverable is the single heaviest colour. Executive's ByStatusBar gained a legend plus human-readable labels (CoveredViaSubstitute → "Covered via substitute", display layer only). The runout chart and the Home KPIs follow. ③ MOR — the strip was simplified to a single end-of-month balance series (green = covered, red = short, ▲ = the order-by month, ▼ = the runout month) with a fixed legend. The old incoming/demand/requirement decorations were dropped (they survive in the tooltip and the expanded ledger). ④ mrp_export — product tabs gained a title row and header bands (MONTHLY LEDGER / DEMAND LINES), the RUNOUT row is filled red, and the Notes were rewritten. The tests moved to header anchors. ⑤ Surplus — the unlabelled trio of numbers became `Allocated n · Surplus n · Obsolete n` with colour dots, a column header row was added, and the bar went green/yellow/red. ⑥ The manual was fully updated — 17 screenshots retaken, new sections for Order Requirements and Surplus, the 5 Admin tabs (safety stock) reflected, and the numbering reordered to follow the business flow. 617 tests pass, dev.db reseeded.**

**8/12 bug fix: the Customer-Owned Inventory screen was blank (a React crash) — the new seed left the `uploaded_at` of the detail rows unset (`positions[].uploaded_at=null` → a null dereference in `p.uploaded_at.slice()`). The seed now sets `uploaded_at=upload.uploaded_at`, and the front end guards null as "—". dev.db reseeded, and the (blank) manual screenshot retaken. 617 tests pass.**

**8/12 hard-allocation demo: `_seed_hard_assignments()` was added to the seed (two InventoryAssignment cases, run after the revision history and before the proposals). ① The coverage path — item 4 is hard-assigned to an AkerBP well with a later ROS, starving an Equinor well with an earlier ROS, so the well and coverage screens carry the reason "hard-assigned to another customer's demand line … releasing it would close the gap — Release the hard assignment in Oracle" with an ACTION. ② The substitution path — item 8 is hard-assigned to one well and free stock is squeezed, and Equinor's item-9 line is given both a customer and a well approval for 9→8, so the Substitution Workspace shows a "Blocked — Oracle release required" badge with a Free / Hard-assigned / Required breakdown and a recommended action. **Note: the blocking priority is customer > well-approval > oracle-release, so showing the Oracle block requires a line that has already cleared its approvals** (recorded in a seed comment). The same comment notes that the standard substitute-candidate API reports availability from the raw pool (it does not deduct its own demand), so staging the block requires making the substitute's own stock tight. The display consistency of Company Inventory's "Oracle assignments" tab, MRP and Sharing was verified too. The shortfall factors were tuned to 7:0.35 / 9:0.7 / 10:0.6 (coverage 55.3%). Two release recommendations were added to the manual, and the substitution screenshot was replaced with a live example of the block. 617 tests pass.**

**8/12 submission-time alert: in the Substitution Workspace, a "HARD ALLOCATION INVOLVED" warning now sits immediately before the request button (when `hard_assigned_qty > 0` and the line is unsubmitted and not Oracle-blocked). It is display only, driven by the server's value, and the blocking priority customer > well-approval > oracle-release is unchanged. The wording: this request will not touch Oracle's reservation — only free stock backs it. CSS `.sub-hard-warning` (amber). A bullet was added to the manual and the substitution screenshot replaced with the alert in view.**

**8/12 scope filter: the demand scope (well status / profile) of Executive and Surplus became variable per request. The mechanism is `scope_override` in coverage_scope (the official window that pre-fills the `Session.info` memo with the requested scope, so every engine reached through the accessors — executive's `_in_scope`, well_dates, compute_customer_coverage, inventory_utilisation — switches at one point) plus `scoped_verdicts` in coverage_view (default scope = a fast read of the stored official verdicts; non-default = check the session is clean → recompute every customer under `scope_override` (uncommitted) → yield → rollback + expire; the blocks that read stored rows and the blocks resolved through accessors then agree on one scope). API: `/dashboard/executive` and `/analysis/surplus` take `status[]`/`profile[]` and answer with `status_scope`/`profile_scope`/`scope_is_default`. UI: checkboxes on both screens (initialised to the default; the last one cannot be unticked) and, when non-default, an amber banner reading "Recomputed read-only … NOT the official stored verdicts". Tests `test_scope_filter_api.py` (widening raises the ratio, rollback leaves the stored rows unchanged, an explicit default takes the fast path). 621 tests pass.**

**8/12 full review plus backlog triage (user's decisions). 【Deliberately deferred】① The whole security set — `require_admin` is defined but never applied (`/admin/*`, `POST /business-units`, `PATCH /customers` are unguarded for a planner; more serious than C-13 and not yet in the register), C-12 (the AUTH_SECRET dev fallback, which should refuse to start), C-13 (object-level authorization: `/wells/{id}` and friends are readable across BUs by hitting the UUID directly, and scenarios are writable; list APIs have no automatic BU filter), C-14 (the four `?access_token=` downloads), no rate limiting on login. **All of this must be closed before the real implementation (the pilot goes public).** ② Monetary valuation — introducing a Price master and converting Surplus/Executive to money (the reason for deferring is at the top of `surplus.py`). Inventory ageing and condition data belong to the same lump. ③ Notifications and alerts (push to Teams/mail: newly uncovered, awaiting approval, an approaching order deadline). The user explicitly deferred these three (8/12). 【Found in review and NOT deferred】The MOR engine was summing inventory/POs across BU boundaries (`_arrivals_by_month` / `_inventory_position` in `mor.py` — a wrong-numbers class of bug, invisible only because the demo has one BU) / MOR was dropping undated POs despite its docstring promising otherwise (it was discarding the value) / C-07 (one customer with no BU takes the whole coverage grid down with a 409) / C-08 (a verdict does not show its computed_at) / the Admin substitution panel linked to a non-existent `/substitution` route and there was no 404 page at all / missing markers in the compromise register (C-02/07/08/09/10). The main front-end candidates: no table sorting at all, no approval queue listing, Home's "Demand changes" had no links, filters were URL-backed on only half the screens, no freshness display or Refresh, the status/profile constants were hardcoded in five files, and there were four separate checkbox implementations. Details in the 8/12 session log.**

**8/12 review findings addressed in one batch (everything except security, money and notifications): ① Engine correctness — the BU crossing under a customer filter in MOR was fixed (`_inventory_position` gained `business_unit_id`/`customer_id` so one implementation covers both cases; with a customer named, only that BU's inventory and POs and that customer's owned material count; with none, the previous system-wide procurement view stands, stated in the notes; a customer with no BU mapping is unavailable per row with a reason. Test `test_customer_filter_nets_against_that_bu_only`). The double counting of undated POs was cleaned up (display goes solely through `position.on_order_undated`). **C-07 closed** — `coverage_view._recompute_all` isolates per customer and `skipped_customers` appears by name in the response and in an on-screen banner. **C-08 advanced** — `CoverageResult.computed_at` (an existing column) is exposed on the grid API (min/max) and shown on the Coverage screen. ② UX — a 404 page plus a catch-all route, the broken link in the Admin substitution panel fixed, **a new Approval Queue screen `/approvals`** (`GET /substitution-approvals`, Pending/Approved/Rejected tabs, in-place Approve/Decline, added to the Readiness nav, SPA regex updated), Home's "Demand changes" linked to the well plus a "see all" link, a link from the MrpByItem detail to Substitution, a link from Surplus to Order requirements, and a "See the coverage impact now" CTA after a DemandImport completes. ③ Table handling — a shared `lib/sort.ts` (three clicks returns to server order, nulls always last) drives full-column sorting on Coverage with aria-sort, sort selects on MOR/Surplus, and text search on Coverage over well/customer/node; DemandList is server-paged, so it is documented as unsortable and PAGE_SIZE dropped to 25. ④ State and freshness — Coverage's filters and search moved into the URL (customer/rollup/status/profile/q, with Home's "see all" pointing at `?rollup=Uncovered`), MOR (customer/horizon) and Surplus (bu) followed, and a shared `Freshness` component (Fetched HH:MM plus Refresh) was placed on Home/Coverage/Executive/MOR/Surplus. ⑤ Maintainability — `lib/enums.ts` (the status/profile constants unified from five files), a shared `ScopeChecks` component (the four duplicate implementations on Exec/Surplus unified), the missing C-02/08/09/10 markers added to the code (C-07 no longer needs one), and the register updated. The manual gained an approval-queue section and an updated Coverage bullet. 623 tests pass.**

**8/12 overnight audit plus autonomous fixes (within the "git can undo it" scope the user approved): a 13-agent all-directions audit (59 findings) → adversarial verification of the important ones → fixes for what was confirmed. ① A HIGH bug — the BU-scope raise in `mrp.py` referenced `InventoryRowMissing` without importing it, so a customer-filtered MOR with a product that has no inventory row in that BU raised NameError and **took the whole grid down with a 500**. Import added plus a degrade regression test. ② C-08 completed — `recompute_customer` now updates `computed_at` (assigned explicitly rather than via onupdate: even a revalidation that changes nothing is still "computed just now"; pinned by a test). ③ scoped_verdicts was discarding skipped customers — it now yields `(is_default, skipped)`, propagating through to the Executive/Surplus responses (`skipped_customers`) and the on-screen banner. The C-07 catch was widened to all of `InventoryNotScoped` (ScopeMissing plus RowMissing). **SAVEPOINT is not used: under pysqlite, RELEASE of a SAVEPOINT effectively commits, which breaks the read-only rollback** (a test caught this; see the comment in coverage_view). ④ `ORDER BY ros_date, id` was added to allocation's pool_lines, removing the non-determinism in HARD's customer-material draw order and in ROS same-day ties. ⑤ API — the approval queue is bulk-loaded (6 queries per row → 3) with a `limit` parameter and three new tests, `POST substitution-approvals` validates the product IDs (404), `/analysis/surplus` answers 404 for an unknown BU, the N+1 in `GET /wells` and `/dashboard/home` was removed with joinedload, an unreachable return in executive_dashboard was deleted, and the demand trend's note is generated from the effective scope rather than hardcoded. ⑥ seed — `safety_stocks` was added to `_DELETE_ORDER` (orphans had been accumulating on every reset: 24 of 26 rows), and the proposal loop's "request before break" bug, which produced an uncounted fourth pending, was fixed by moving the check ahead of the request. ⑦ Front end — Executive/Surplus no longer send scope parameters until touched (so an administrator changing the default still renders correctly; the display derives from the response's scope), Surplus's scope toggling gained a 450 ms debounce plus a stale guard, and ApprovalQueue's tab-switch race, list-wiping error path and colSpan were fixed. DemandList gained a stale guard plus a product → By Item link. ⑧ docs — HANDOFF's stale figures corrected (19 screens, the test count, 20 migrations and the head, C-07 marked done) and C-06's 27 → 10 count. 628 tests pass, tsc/vite clean, dev.db reseeded (safety stock back to 2, pending back to 3), every screen verified with Playwright (zero errors).** 【Remaining backlog — needs a specification call, so raised in the morning】overdue demand is treated inconsistently by MOR (order it) and Surplus/utilisation (treat as obsolete) / sharing's official_status labels a substitute-rescued line as Uncovered / re-deciding a decided approval overwrites silently (should it be blocked?) / the customer-owned opening balance of the with-order runout is pinned at 0 / C-09 (a SOFT customer's own product vs another customer's hard assignment) / rolling the conventions out to the scenario screens (Freshness, URLs, sorting) / DemandList's URL state / `/dashboard/home` has no response_model (so it sits outside the unit contract's net).**

**8/11 deployment: published on Render (Blueprint = `render.yaml` at the repository root, image = `octg-platform/Dockerfile.deploy`, service name `octg-supply-readiness`, free plan = sleeps after 15 minutes idle). The deploy branch is `claude/octg-supply-planning-hpej8e` — when this merges to the mainline, switch Render's branch setting too. A push to that branch redeploys automatically. The URL is on the service page in the Render dashboard. The DB is the dev.db inside the image, so every redeploy resets to the demo state. Login: admin@octg.dev / octg-dev (planners are in seed_users.py). `AUTH_SECRET` is generated by Render.**

---

## 0A. The work interrupted on 8/7 "was fine" (verified 8/10)

On 8/7 two background agents were force-stopped (`killed`) when credits ran out, and the note
here said "files are very likely left half-written". **Measured on 8/10, both had in fact landed
complete, and the only real damage was one missed migration on `dev.db`.**

| Checked | Result |
|---|---|
| `pytest -q` | **628 passed** (as of 8/12; the 547 in the older sections below is the figure of the day) |
| `test_admin_customer_configuration.py` + `test_scenario_new_order.py` | 34 passed (both interrupted agents' tests exist and pass) |
| `app/engines/customer_admin.py` (28KB) | Exists. Registered as a router via `app/api/customers.py` |
| `tsc --noEmit` | exit 0 |
| alembic code head | `a3d70f19c845` (business_unit_name_uniqueness) |
| `dev.db` current | ⚠️ was one behind at `d5f2a4b91c70` → **`alembic upgrade head` applied, resolved** |

So `killed` did not mean "died halfway"; it meant "only the final report failed to arrive".
**Lesson: before doubting the output of a force-stopped agent, run the tests.**
This project had no git at the time, so no diff was visible, which fed a disproportionate fear
that things were broken. **Doing `git init` alone would have been worth it.**

## 0B. Done on 8/10: the UI / design system overhaul

**Zero changes to the backend, engines, API or tests.** Front end only.

### What was done

1. **A design token layer** (the `:root` at the top of `src/index.css`, 56 tokens)
   - surface / line / text / brand / accent, plus the domain statuses (`--ok` / `--bad` / `--warn` / `--unmodelled`)
   - Three radii (`--r-sm/md/lg`), three shadows, seven type steps (`--fs-caption` … `--fs-display`)
   - **Only 26 raw hex values remain outside `:root`** (in 1,382 lines). All of them are chart series
     colours or the stripes of the timeline's `repeating-linear-gradient` (tokenising both colours of
     the stripe would erase the stripe, so one is deliberately left as a raw hex)
   - ⚠️ **Never merge `--bad` (red) with `--warn` (amber).** Red = "the steel genuinely is not there, and
     the fix is a mill order"; amber = "the steel is in the yard, but a person has to release the
     assignment in Oracle". Comments throughout the file explain the distinction.
     Likewise `--unmodelled` (violet) = "not modelled" is a separate channel from green/red.

2. **Header bar → left sidebar** (`src/components/AppShell.tsx` is new; `main.tsx` shrank to routing)
   - The old implementation put 11 links in a row on one navy bar and used `Link`, so **there was no active state at all**
   - Changed to `NavLink`. 244px fixed, white with a hairline. Below 900px it goes off-canvas with a scrim
   - **Page width now varies per screen** (`widthClassFor()`): 1120px by default, 1480px for table screens,
     full width for the scenario timeline. The old implementation pinned every screen at `max-width: 1100px`,
     so `.mrp-table { min-width: 980px }` and the timeline **scrolled horizontally at all times**.
     When adding a screen, touch both `SECTIONS` and `widthClassFor` in `AppShell.tsx`

3. **Tables reworked** — sticky header, ruled grid → hairlines, rounded clipping, row hover

4. **Administration.tsx split into 4 tabs** (77KB → a 79-line shell plus 1,846 lines across four panels in `src/pages/admin/`)
   - `HierarchyPanel` / `LeadTimePanel` / `CoverageScopePanel` / `SubstitutionPanel`
   - The tab selection is **held in the `?tab=` query** (`useTabs` in `src/components/Tabs.tsx`).
     An invalid or absent value falls back to the first tab. `/admin?tab=scope` deep-links
   - **Each panel fetches its own data**, because there was no shared state between the sections to begin with.
     An inactive tab does not fetch

5. **Breadcrumbs added** (`src/components/Breadcrumbs.tsx`) — on the four detail screens (Well / MRP by item / Scenario / Substitution)
   - ⚠️ While loading they show **no UUID**, only a neutral noun (`Well` / `Item` / `Scenario`),
     replaced with the real name once fetched. This follows §4's principle of never confusing unknown with a value
   - Substitution is the only two-level one, because that screen does not hold a well ID.
     **Do not add a fetch purely to feed a breadcrumb**

### What was verified (all measured; screenshots are unusable for the reason in §7, so this is DOM verification)

- All 14 routes: render OK, zero `load-error`, zero unresolved colours, **zero horizontal page scroll** (the old implementation had it constantly)
- Active state: exactly one on every screen
- Admin: all four tabs render, URL sync, deep link, invalid-value fallback,
  and **`admin-scope-ack` (the acknowledgement checkbox) and `admin-scope-warning` (the blast-radius warning) confirmed alive**
- Timeline: `MONTH_PX = 96` matches the measured 96px grid interval, 94 of 95 plots have a grid line
  (the remaining one is the axis row, correctly), 76 demand blocks render, the hatching keeps both colours
- CSS structure: braces balanced 823/823, all 24 `white-space` declarations intact, zero undefined token references, zero empty declarations
- `tsc --noEmit` exit 0

### UI work still outstanding

- Extracting shared primitives (`Button` / `Field` / `Card`). Button styling is still declared per screen as
  `.revise-form button`, `.sub-actions button`, `.override-form button` … Tokenisation means they look
  consistent, but the declarations are still duplicated
- Dark mode — **the user stated they do not want it. Do not build it**

---

## 0. Work log as of 8/7 (resolved in §0A; kept as a record)

### 0-1. Completed on 8/7 (added new in that session)
- **Substitution registration (Admin)**: CRUD for `TechnicalSubstitution` / `CustomerSubstitutionRule`. `app/engines/substitution_admin.py` (new), six endpoints added to `app/api/admin.py`. Migration `d5f2a4b91c70` (uniqueness constraints), applied to `dev.db` and verified. A real bug where registering a BU-independent technical pair dragged a BU-scoped recomputation along was found and fixed with SAVEPOINT-level isolation. 513 tests pass; the front end (Administration.tsx) was verified working.
- **Customer-Owned Inventory template download**: `GET /customer-owned-inventory/{customer_id}/template`. Whether the customer has uploaded before is returned in a header.
- **Dragging on-order (already-placed) quantities on the Scenario timeline**: `PO_ARRIVAL.arrival_date` was taken out of UNMODELLED_KINDS and is consumed in `mrp.on_order_runout`. By design it does not affect coverage verdicts (runout only). On apply it is still rejected as a SUPPLY_KIND, because the data is Oracle-owned.

### 0-2. The two said to be interrupted → **on 8/10 both were confirmed complete (see §0A)**
What follows is the 8/7 record. **The "do not trust this" warning in this section no longer applies** —
547 tests pass, and both features' test files (34 tests in total) exist and pass.

1. **"New-order simulation inside a what-if"** (started from "and the order-addition simulation inside a what-if") — a continuation of the on-order drag feature that lets a scenario add a hypothetical new order (quantity plus arrival date). At the stop it appeared to have reached "Both drag axes work" (the front-end drag may have been working), but tests and verification were incomplete. **If resuming, read the current implementation of `ScenarioTimeline.tsx`, `ScenarioEditor.tsx`, `app/engines/scenario.py`, `app/models/scenario.py`, `app/engines/overrides.py` and `app/engines/mrp.py` before deciding how to continue.**
2. **Admin editing of the BU hierarchy and allocation policy** (started from "the admin business unit - customer hierarchy and allocation policy also need to be variable") — remapping `Customer.business_unit_id` and changing `Customer.allocation_policy` from Admin. At the stop it appeared to be immediately before "Now the tests." (implementation written, about to run tests). **This feature carries large design risk** (a BU remap changes where the whole inventory pool resolves; an allocation policy change alters every coverage verdict for that customer), so on resuming, read the current state of `app/models/customer.py`, `app/models/business_unit.py`, `app/engines/inventory.py`, `app/engines/allocation.py` and `app/api/admin.py`, and check what the agent actually wrote (whether new files such as `business_unit_admin.py` exist under `app/engines/`, and whether half-finished fragments are mixed into `app/api/admin.py` or `Administration.tsx`). A half-written `PATCH /customers/{id}` may also have been executed against the production-equivalent `dev.db`, so check that the customer/BU data in `dev.db` has not changed unintentionally.

### 0-3. The 9-step demo walkthrough (interrupted)
① demand update ② customer inventory upload ③ demand change review ④ substitution proposal submission are done. ⑤ the what-if scenario (pull a well forward → add an order; push one back → adjust on-order) was only started (scenario created, one `ros_date` override added) and is unfinished. ⑥⑦⑧⑨ are untouched. `dev.db` is left in the partially hand-operated state (template edit applied, customer-owned inventory update applied, one substitution approval changed to Approved, one Draft scenario left behind).

---

## 2. How to start it

**Recommended: both are registered in `.claude/launch.json`, so `preview_start` can start
`octg-backend` / `octg-frontend` by name.** The backend goes through `backend/run-dev.cmd`,
where the `DATABASE_URL` setting and the reason for "no `--reload`" are commented.

To start them by hand:

```bash
# Backend (always start it without --reload; the reason is in §7)
cd "C:\Users\tsato\Claude\Projects\OCTG Supply Readiness Platform\octg-platform\backend"
DATABASE_URL="sqlite:///./dev.db" ./.venv/Scripts/python -m uvicorn app.main:app --port 8000

# Frontend
cd "C:\Users\tsato\Claude\Projects\OCTG Supply Readiness Platform\octg-platform\frontend"
export PATH="/c/Program Files/nodejs:$PATH"
npm run dev
```

- Front end: http://localhost:5173
- Backend API: http://localhost:8000 (Swagger UI: `/docs`)
- Tests: `DATABASE_URL="sqlite:///:memory:" ./.venv/Scripts/python -m pytest -q` → **547 passed**
- Type check: `cd frontend && npx tsc --noEmit` → exit 0
- Migrations: `alembic heads` → **`a3d70f19c845`** (`dev.db` was upgraded to the same head on 8/10)

**If the backend dies ("Failed to fetch" appears):**
```bash
# Kill whatever holds port 8000, then restart (PowerShell)
Get-NetTCPConnection -LocalPort 8000 | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Stop-Process -Id $_ -Force }
```
Then re-run the backend command above.

---

## 3. Directory layout

```
OCTG Supply Readiness Platform/
  octg-platform/
    backend/    FastAPI + SQLAlchemy + Alembic + pytest
      app/models/      SQLAlchemy models
      app/engines/     domain logic (coverage, allocation, substitution, mrp, sharing, scenario, executive...)
      app/api/         FastAPI routers
      app/schemas/     Pydantic schemas (all in one __init__.py)
      alembic/versions/ migrations (20, head: f7c3a91b5e24; the count is the 8/9 record)
      seed/seed_from_workbook.py  demo data generation (--reset rebuilds)
      run-dev.cmd      dev backend launcher (sets DATABASE_URL, no --reload)
      tests/           547 tests
    frontend/   React + TypeScript + Vite (plain CSS, no libraries)
      src/main.tsx        routing only
      src/components/
        AppShell.tsx      sidebar plus page-width control (add new screens to SECTIONS here)
        Tabs.tsx          URL-linked segmented control
        Breadcrumbs.tsx   breadcrumbs for detail screens
        (also: CoverageBadge / LoadError / ReasonText / LeadTime / SharingPanel / WorkQueueCard)
      src/pages/          screens (see §5)
      src/pages/admin/    the four Administration tab panels
      src/api/client.ts   typed fetch layer
      src/index.css       all hand-written CSS (design tokens in :root at the top; see §0B)
  .claude/plans/       past plan files
  HANDOFF.md           this file
```

---

## 4. The design principles held throughout (read before touching anything new)

The discipline kept consistent across this codebase. New features follow it.

1. **Never fabricate numbers.** Always distinguish "unknown" from "zero". Return `available: false` plus a `reason`, and never render it as a 0 or a dash (`InventoryOnHand` missing → a 424 exception; `InventoryOnOrder` missing → `available:false`; `CustomerOwnedInventory` not uploaded → `has_uploaded:false`).
2. **Make invalid states unrepresentable.** Rather than validating against them, shape the model so they cannot exist (e.g. `demand_status` sits once on `Well` and not on `DemandLine`).
3. **The BU is an absolute boundary; the Customer is the default boundary.** Inventory can never cross a BU. Customers are separated by default, and only read-only what-if analysis (cross-customer sharing) may cross, within a BU.
4. **Customer-owned inventory is consumed before company-owned**, and is never shared with another customer (a stricter rule than the BU boundary).
5. **The platform never creates, releases or overrides a hard reservation.** The Oracle family (`InventoryOnHand` / `InventoryAssignment` / `InventoryOnOrder`) is a read-only projection. Only `CustomerOwnedInventory` is platform-owned and writable.
6. **Coverage is always written by the engine.** There is no manual setting. No `CoverageResult` means "not evaluated" — never "no problem".
7. **One computation, one implementation.** Preview, scenario and sharing all call the same `compute_customer_coverage` and friends as production. No second implementation.
8. **Unit of measure is mandatory on every screen.** `Product.unit_of_measure` (Mtr/PC/MT). Aggregations over several products return a `quantities_by_unit` array, and never a scalar total or ratio when units are mixed.

---

## 5. Screen list (the 8/9 record — there are 19 now; the authoritative list is the "where it stands" line at the top and the Routes in `frontend/src/main.tsx`)

| Screen | State |
|---|---|
| Home Dashboard | ✅ |
| Well Workspace | ✅ |
| Demand List | ✅ |
| Demand Import (template export, conflict review) | ✅ |
| Coverage Workspace | ✅ |
| Substitution Workspace (with approval-by date) | ✅ |
| MRP Summary / By Item (Excel export, with-order runout) | ✅ |
| Scenario List / Editor (form plus drag-and-drop timeline) | ✅ |
| Executive Dashboard (quantity coverage, soft allocation, first runout, incoming supply) | ✅ |
| Administration (4 tabs: BU hierarchy / lead time / coverage scope / substitution master data) | ✅ |
| Product Workspace | ✅ |
| Cross-Customer Sharing | ✅ |
| Customer-Owned Inventory (with upload) | ✅ |
| **Login / authentication** (added 8/11) | ✅ |

---

## 6. Authentication (implemented 8/11)

Built exactly to the agreed approach: a development dummy login with a structure that can be swapped for Entra ID.

**Backend** (four files in `app/auth/` plus `app/api/auth.py`):
- `provider.py` is the seam for Entra. Today it is `DevPasswordProvider` (email plus a PBKDF2 password). Switched by the `AUTH_PROVIDER` env var
- `tokens.py`: HMAC-signed self-expiring tokens (12h). JWT was deliberately not adopted (the reason is in the docstring). **`AUTH_SECRET` env is required (leaving it unset falls back to a dev default = C-12)**
- `deps.py`: `get_current_user` plus `enforce_customer_scope`. **Every router is registered through `AUTH_DEPS` in `main.py`** (add new routers with `_include_protected`)
- Two roles: admin (all BUs) / planner (pinned to one BU; naming another BU's customer_id gives 403). A planner with no BU is refused with 403 (no silent widening)
- `POST /auth/login` and `GET /auth/me`. There is deliberately no logout endpoint (no server-side session state)
- Migration `c8d41a92e7f3` (users table); dev users come from `seed/seed_users.py`: **admin@octg.dev / planner+north-sea-operations@octg.dev / planner+gulf-operations@octg.dev, all with the password `octg-dev`** (applied and seeded in dev.db)

**Front end**:
- `src/auth.ts`: token storage (localStorage), `authFetch` (every fetch in client.ts goes through it; a 401 uniformly destroys the session and goes to /login), and `withToken` (the `?access_token=` on the four download links)
- `src/pages/Login.tsx`, `RequireAuth` in main.tsx, and a user menu at the bottom of the AppShell sidebar (display name, BU scope, Sign out)

**Tests**: 12 in `tests/test_auth.py`. Existing tests still pass because an autouse fixture in conftest overrides `get_current_user` as admin (the authorization dependency graph itself stays live).

**Remaining holes (see the register)**: C-12 (the dev secret default), C-13 (object-level authorization on deep resources plus automatic BU filtering on list endpoints), C-14 (the token in download URLs).
**Closed (8/12)**: the whole-grid 409 on `GET /coverage` (C-07). `coverage_view._recompute_all` isolates failures per customer, and skipped customers appear by name in the response and on screen.

---

## 7. Known environment constraints

- **`uvicorn --reload` is reliably broken in this environment.** WatchFiles detects the change and tries to reload, but "Started server process" never appears and it hangs. Several agents confirmed this independently. **Always stop and restart the backend by hand after a change.**
- The screenshot tool (`computer{action:"screenshot"}`) fails frequently here ("Browser pane is not displayed"). The established fallback is DOM verification through `get_page_text` / `read_page` / `javascript_tool`.
- Stored verdicts such as `CoverageResult.reason` **stay stale until a recompute trigger fires** (a revision applied, an approval, a well status change), even after the engine code is fixed. "The code is right but `dev.db` is old" happened repeatedly. Suspect it.

---

## 8. What we learned about running models

**On tasks at the scale of synchronising several front-end screens at once, discovering a new contract, or building a new screen, Sonnet twice self-reported "done" after a single tool_use while having actually done nothing.** An `ls` showed the files did not exist.

The practices that stuck as a countermeasure:
- Default to **Opus** for tasks of that scale
- Write into the agent's instructions that **verbatim evidence is mandatory and delegating to a sub-agent and relaying its report is forbidden**
- If a report is thin (few tool_uses, no quoted measurements), verify it directly (`curl`, `ls`, an actual look in the browser)

### The model split established on 8/10 (which worked)

**Design decisions go to Opus, mechanical porting to Sonnet, and verification is always done by Opus directly.**
The UI overhaul (§0B) ran this way and all three Sonnet agents landed correctly. What made it work:

1. **Reduce the task until no judgement is left in it.** For the CSS token port, the whole old-hex → new-token
   mapping was written out and handed over. Sonnet only performed the substitution
2. **Split by file and run them in parallel.** index.css / Administration.tsx / the four detail screens, divided so they could not collide
3. **State the inviolable constraints as a numbered list.** "Do not rename, add or remove a single selector",
   "do not delete a single comment", "do not touch the px geometry of `.tl-*`", and so on.
   In particular, **"do not merge two rules that look identical but are deliberately different"** will be violated unless stated
4. **Require verbatim output of the verification commands in the report**

### ⚠️ The 8/10 failure: **the tests were ratifying the bug**

When Sonnet wrote the Dashboard's inventory utilisation block, **all 20 tests passed while the
implementation was fundamentally wrong.** It surfaced only when real data was queried.

- **Symptom**: the North Sea BU's real inventory is 25 rows / 3,071,500 Mtr. The block, on a 12-month horizon,
  saw only 12 products / 3,013,000 Mtr (**58,500 Mtr invisible**).
  Worse, the inventory total moved with the horizon (stock in a yard cannot grow or shrink with how far ahead you look)
- **Cause**: the product population was built **from demand** (`for bu_id, product_id in demand_by_key`),
  when the denominator should be inventory. A product with no demand in the window vanished from the list entirely.
  Worse still, zero demand returned `available=False`
- **Why that is the worst possible failure**: what disappears is **the most idle inventory**.
  Idle inventory vanishing from the block whose purpose is to reveal idle inventory is the exact opposite of the goal
- **Why the tests passed**: the fixtures gave every product demand.
  Worse, two tests **pinned the bug as the correct answer**
  ("BU A holds 5000 of stock but has no demand → assert `available is False`"),
  with a comment saying "so nothing is netted"

**Lesson: a test written by an agent is only a mirror of the implementation.**
"The tests pass" is not evidence that "the implementation matches the specification".
**Always query real data and check invariants from the outside.**
What worked here were two invariants raised from outside the tests: `tied + not_tied == the BU's real inventory total`,
and `the inventory total does not change with the horizon`.

And **always verify the report.** All three Sonnet reports were in fact accurate, but verification
still turned things up (`admin-policy-soft` is generated by a template literal, so grep missed it and
for a moment it looked like the class had disappeared).
The CSS-port agent self-reported breaking `white-space` and fixing it again, so the surviving count of
`white-space`, the brace balance and undefined token references were all counted explicitly (all intact).

---

## 9. The main things implemented in this session (chronological)

### Initial MVP build
The coverage / allocation / substitution / lead-time / MRP engines, scenario planning, the BU hierarchy, Alembic, and 13 of the 14 screens.

### Design corrections from review (later in the session, from user review)
1. **22 items of product-owner feedback** (missing UoM across all screens, product names printed as UUIDs, missing well dates, inconsistent counts on the coverage grid, etc.) addressed in four phases
2. **An important domain correction**: `demand_status` moved to the well (`Well.demand_status`) and was removed from `DemandLine`, on the user's observation that "status essentially never varies within one well"
3. **Three important design reversals** (following the user's principle that the platform never creates a hard reservation):
   - Partial coverage for HYBRID/SOFT: "deduct nothing" → "consume partially in ROS order"
   - A SOFT customer's hard-assigned stock: from "becomes COVERED_VIA_SUBSTITUTE normally" → offered as a candidate but blocked pending an Oracle release
   - A PendingApproval substitute reservation: from "reserve it" → do not reserve; make the over-subscription visible
4. **A new feature: customer-owned inventory** (an added user requirement) — consumption priority, no sharing, upload
5. **Phases 1-4**: UoM on every screen, well dates, quantity-based Executive Dashboard with soft-allocation coverage, BU filter and incoming supply, substitution approval-by dates, MRP with-order runout, Admin editing, an Import template with conflict review, MRP Excel export, and the scenario drag-and-drop timeline

The dozen-plus **real bugs** found along the way (inventory double-counted between wells, the substitute API leaking another BU's stock, `still_recoverable` missing from the API, `target_well_id` missing from ScenarioOverrideInput, and others) are all fixed and pinned by tests.

---

## 10. Candidates for what to do next (discuss with the user)

1. ~~Implement authentication~~ → **done 8/11** (§6). Next is deployment (the direction already agreed with the user) and its prerequisites C-12 (make AUTH_SECRET mandatory) and C-13 (object-level authorization)
2. ~~Remove the whole-grid 409 risk on `GET /coverage` (C-07)~~ ✅ done (8/12)
2.5. ~~`git init`~~ → **done 8/10** (C-11. GitHub `taro-coupii/Claude-Private`)
3. Open questions still awaiting a decision:
   - Should cross-customer sharing analysis move to partial consumption (it is all-or-nothing today)?
   - Should `blocking_layer` change from a single value to a set?
   - A SOFT customer's own-product allocation ignoring another customer's hard assignment (the substitute search handles it; the own-product side does not — deliberately left out of scope)

---

## 11. The 8/12 product-owner ruling batch (decisions and implementation for consultation items ①-⑧)

Every item was ruled on in the morning consultation. Implemented, 636 tests green, deployed:

1. **① Overdue demand (ruling c)**: demand past its ROS is "counted, but labelled as its own bucket". Implemented in Executive utilisation (`demand_overdue`), Surplus (an "incl. … overdue demand" note on the row) and MOR (`total_overdue_demand` plus notes). It is never silently blended in
2. **② Re-deciding a decided approval (ruling a)**: `POST /substitution-approvals/{id}/decision` answers **409** if the approval is already decided. Overturning it means raising a new approval request (the existing "Approved beats a later Pending" rule then settles it)
3. **③ Sharing labels (ruling a)**: `official_status` displays the stored real verdict as-is (Uncovered / PendingApproval / Unrecoverable). The hardcoded "Uncovered (customer-scoped)" is gone (display-only change)
4. **④ With-order runout (ruling a)**: `runout_with_recommended_order` and the on-order series receive the same `customer_owned_opening` as the baseline (so the ownership split does not contradict itself between series)
5. **⑤ C-09 (ruling a)**: deduct foreign hard assignments on the own-product path too — **investigation showed the code already did this and had tests** (`reserved_elsewhere` in `coverage.py`). Only the register was older than the implementation. C-09 was marked ✅ and the stale marker removed
6. **⑦ DemandList in the URL**: all seven filters plus the page moved into the URL (the user specified "all of them")
7. **⑧ `/dashboard/home`**: given a `HomeDashboardOut` response_model (bringing it inside the units-of-measure walk)
8. **⑥ Rolling the conventions out to the scenario screens**: "after the others are done" — left untouched, to be done with the next scenario work

Security (applying require_admin, C-12/C-13/C-14, rate limiting), monetary valuation and Teams/mail push remain on the "must be closed before the pilot goes public" list (§10).

### §11 addendum (8/12 afternoon): ⑥ done

The shared conventions were rolled out to the scenario screens (ScenarioList / ScenarioEditor). Zero features added; only the conventions unified:
- Freshness (Fetched HH:MM plus Refresh) on both screens
- On error, keep the data on display (the "wipe everything and `return`" path is gone)
- A stale-response guard on reload (a sequence number — the editor reloads on every override edit)
- The Form / Timeline toggle saved in the URL as `?view=timeline` (restored on reload and from a bookmark)

URL persistence and the Freshness display were verified with Playwright on a live instance. The scenario created for verification was removed by restoring dev.db with `git checkout` (there is deliberately no DELETE endpoint for scenarios).

## 12. 8/12 evening: mobile optimisation (method A = responsive, passes 1 and 2)

Agreed with the user: **no separate UA-detected site** (the risk of the implementation splitting in two and drifting, and of breaking URL sharing). Method A is the same SPA changing shape through CSS.

- **Pass 1 (skeleton)**: the hamburger arrived with the 8/10 overhaul, so this was only the ≤640px finish — Home's KPI band in 2×2, header and filter bars stacked, h1/h2 reduced, page padding adjusted (the MOBILE block at the end of `index.css`)
- **Pass 2 (cards)**: Approvals and the Well Workspace demand table gained a card twin. The same row data renders twice and CSS shows one of them (cards at ≤640px, the table otherwise). Actions go through the same decide / ReviseRow — this is not a second implementation
- **Deliberately not done**: the Coverage grid, MOR's 18-month grid and MRP keep horizontal scrolling. Folding them to phone width would make the information lie
- Verification: Playwright at 390×844 (iPhone equivalent) with screenshots of Home / Approvals / Well / Demand, plus a check at 1440px that the desktop did not regress

### §12 addendum: MOR marker legibility, and physical runout vs a safety-stock breach (8/12)

- ▲ (order-by) and ▼ (runout) were **printed on top of each other** in the same month (two separate absolute placements at the same coordinate) → they now sit side by side on a single marker rail (flex). The glyphs grew from 9 to 11px and gained a ground-colour halo so they read over both the green and the red band. The band cell reserves rail space with padding-top
- **Physical runout separated from a safety-stock breach** (a user request): ▼ = a negative balance (physical), ▽ = a positive balance below safety stock (eating into the buffer, amber), and the band cell goes warn-soft in that month. The tags in the expanded ledger split into PHYSICAL RUNOUT / INTO SAFETY STOCK. The distinction is derived in the display layer from the existing projected_balance and safety_stock — no engine change

## 13. 8/12 night: the MRP By Item monthly ledger rework (user request)

"Runout is hard to follow" → the ledger became a complete accounting form: **opening balance (carried forward, split company/customer-owned) + incoming (split by certainty) − outgoing demand = ending balance (same split)**.

- `RunoutPoint` gained `opening_customer_owned/company`, `incoming_on_order` and `incoming_recommended`. `_runout_series` takes incoming in two tiers (real PO / assumed supply) and records them separately on each point (the summation logic is unchanged)
- `by_item` gained a **`ledger` series** (real PO arrivals by month plus the arrivals of recommended orders, i.e. the full plan), plus `ledger_runout_month` and `ledger_undated_on_order` (a PO with no date cannot be placed in a month, so it is stated separately). The three existing series (baseline / with-recommended / on-order) are untouched — each answers a different question
- **The colour decision**: incoming is split by certainty. Blue = on order (a promised PO), amber = suggested order (not yet placed, italic). "A suggestion never wears a promise's colour"
- UI: By Item's old text table was replaced by a Monthly ledger section (11 columns). Excel (the item tabs of mrp_export) follows the same columns and the same two fills
- Regression test: `test_ledger_splits_on_order_from_recommended_and_carries_openings` (the carry-forward identity, the tier split, the separate undated note). 637 green

## 14. 8/12 night (continued): the scope note plus the safety-dip demo

- **The shared ScopeNote component** (`components/ScopeNote.tsx`): MRP Summary / By Item / Order Requirements display one line — "Demand scope: Confirmed wells · Primary, Contingency profiles — the platform default. Change it in Administration. The checkboxes on Coverage/Executive/Surplus do not affect this screen." It reads GET /admin/coverage-scope-defaults (and displays nothing if that fails — never guess at the scope)
- **Safety-dip demo data** (a deliberate dev.db update): a safety stock of 1,200 Mtr set on CSG 13-3/8" 72.00# L80. The minimum balance is 419 Mtr, so from 2027-10 onwards it shows a safety-stock breach (▽, amber) without ever running out physically — staging that shows the new distinction directly
- **The demo scenario** "Safety-stock dip deepens — AkerBP 13-3/8 pull" (Draft): quantity overrides SK-A03 950→1,600 and VF-A06 900→1,700. Applying it turns the same dip into a physical runout (▼) — a scenario that tells the story of the difference between amber and red. The preview shows line changes

### §14 addendum: the demo scenario extended to a platform-wide one (8/12 night)

"Safety-stock dip deepens" was renamed and extended to **"AkerBP full replan — demand, supply & approvals what-if"** (Draft, 7 overrides, covering every family):
1. Quantity increase (SK-A03 950→1,600, VF-A06 900→1,700) — the safety dip becomes a physical runout
2. ROS pulled forward (SK-C09 TBG 5-1/2 L80 4,300 Mtr: 2029-07→2027-09)
3. Well status (SK-A04 Confirmed→Budgeted: it leaves the confirmed envelope and frees steel)
4. PO arrival slip (CSG 13-3/8 P110 4,637 Mtr: 2027-02→2027-08)
5. Oracle release (VF-A06's hard assignment 3,950 Mtr→0)
6. Substitute approval (VF-B03 Pending→Approved)

Measured preview: covered wells 1→3, covered lines 43→45 (13 rows flip verdict), unrecoverable 16,450→7,400, 6 MRP rows move. The dev.db update was committed.

## 15. 8/12 late night: the apple-design UI review (ultracode, 10 agents)

Against `.claude/skills/apple-design` (the web edition of the WWDC design principles) plus `emil-design-eng`: a five-lens parallel audit (40 findings) → design synthesis → three packages implemented in series → live verification. **Zero functional changes, the calm palette unchanged, zero dependencies added.**

- **foundation**: motion tokens (--dur-fast/base/slow = 120/180/240ms, --ease-out/--ease-drawer and friends), a rem type scale with size-specific tracking (the uniform -0.015em on headings is gone — the larger the type, the tighter it sets), :active press feedback (scale 0.97) on every pressable element, unified focus-visible, tabular-nums throughout
- **motion**: the mobile sidebar became a drawer on an iOS curve, the scrim fades (@starting-style), MOR row expansion, details and banner entrances got a reveal, hover gained transitions. prefers-reduced-motion is the surgical version — transform motion suppressed, colour and opacity kept
- **depth**: th became a translucent material that works under scroll (backdrop-filter with a reduced-transparency fallback), the shadow hierarchy was tidied (elements inside a card are flattened), tables gained scroll-edge shadows (the kind that disappear at the edge — keeping the grid honest), tap targets reached 44px, safe-area was handled, and the remaining chart hexes were tokenised (--chan-owned, --ok-mid, --iu-tied)
- Verification: four desktop screens plus four mobile states inspected by eye, zero regressions. The 10 rejected proposals are recorded too (e.g. a mask fade was not adopted because it hides the numbers at the edge)

## 16. 8/14: independent QA (black-box, 6 agents) and same-day fixes

Five independent testers **forbidden from reading the source** (desktop UI / mobile UI / API contract / data consistency / planner E2E) plus a QA lead (who reproduced every finding personally before confirming it) were pointed at an isolated instance (:8001) running on a copy of dev.db. 16 raw findings → **10 confirmed, 3 rejected** (rejections were non-reproducible or tester misreadings). Of note: **the data-consistency lens matched on every item** (the uncovered count agreed across three screens, the surplus identity held, MOR equalled the demand-detail total, the ledger identity held, and the overdue boundary was right) — its verdict was "internally consistent down to the unit". E2E completed every business flow.

**Fixed the same day** (see the commits):
- CRITICAL "the API base is pinned to localhost:8000" → **production is innocent** (Dockerfile.deploy sets VITE_API_BASE="" for relative URLs). Confirmed as an artefact of the isolated QA environment (:8001) only
- Horizontal overflow on four mobile screens (Surplus 814px / MOR 608px / Executive 537px / Home 407px) → three causes: Executive's inline-style non-wrapping flex (replaced with .exec-filters), .mor-controls not wrapping (wrap at ≤640), and .home-approval-product's .num nowrap plus a grid min-content (white-space normal plus min-width 0). All screens verified at 390px
- The drawer's ✕ sat underneath the sidebar (z50) → toggle raised to z60, hit test passes

**Left as decisions to make** (functional changes, so not taken unilaterally):
1. Approve/Decline is a single click with no confirmation, while the manual calls it FINAL → add a confirmation step?
2. Home's "Pending approvals" card lists only lines whose verdict is PendingApproval (a request whose verdict differs never appears) → change the card's definition?
3. Overdue is defined at different granularity by MOR (past the ROS month) and Surplus/Exec (past the ROS date), each documented in its own note → align them?
4. Sort state is not held in the URL (filters are) → bring it in line with the convention?
5. `GET /wells` returns JSON even for `Accept: text/html` (an API route colliding with an SPA path) → fixing it needs middleware

## 17. 8/14: the five QA decisions implemented (product-owner rulings)

1. **Two-step confirmation on approvals**: a shared `ConfirmButton` (the first click arms it into "Confirm approve?", auto-disarming after 4 seconds and on blur, in the warn-soft palette). Applied to every decision button on Approvals (table plus mobile cards), Home and the Substitution Workspace. Native `confirm()` was rejected (an error-shaped face, it blocks the whole tab, and it cannot be coloured)
2. **The Home approval card is a union**: "every undecided request (with an approval_id, buttons enabled)" plus "PendingApproval-verdict lines with no request yet (scope-guarded as before, no buttons)". Two regression tests (one existing updated, one new for the union)
3. **Overdue unified to "past the ROS month" on every screen** (the cutoff in executive.py moved to the first of the month). Verified live that MOR and Surplus agree on overdue for every product. The notes, tooltips and manual wording were unified too
4. **Sort in the URL**: `useSortable` gained a urlKey (`?sort=<col>.<dir>`, server order = no parameter) → Coverage. The MOR/Surplus selects also write `?sort=`
5. **HTML navigation to /wells**: `wells$` added to the SPA regex (removing the dead end where trimming the URL dropped you into JSON)

638 tests green. Three sections of the manual updated.
