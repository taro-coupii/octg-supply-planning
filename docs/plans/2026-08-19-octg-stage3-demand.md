# OCTG Rebuild — Stage 3: Demand Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Checkbox steps.

**Goal:** Demand domain per stage-3 spec: wells/demand lines/revisions/import staging, the three screens, seeded demo data.

**Spec:** `docs/specs/2026-08-19-octg-stage3-demand.md` (binding; contracts live there).

## Global Constraints

Same as stage 2 (clean-room, branch, no auth C-01R, unit on every quantity payload, TDD, gates per task, standard commit trailers, one migration for the stage created in Task 1).

---

### Task 1: Models + migration
**Files:** `app/models/{well,demand,demand_import}.py`, models `__init__`, one alembic revision, `tests/test_stage3_models.py`
**Contracts:** spec §Data Model verbatim (uniques, enums, NOT NULLs, no status column on demand_lines, append-only revisions have no updated path).
**Required tests:** unique(customer,name) on wells; unique(well,revision_no); demand_line requires ros_date/unit/profile; quantity>0 CHECK; FK enforcement; DemandLine has no `status` attribute (assert hasattr false).

### Task 2: Import APIs (template, upload+staging, conflicts, apply, discard)
**Files:** `app/api/demand_imports.py`, register in main, `tests/test_demand_imports.py`
**Contracts:** spec §API rows 1-5. Validate-all-first (row-numbered 422, nothing staged); conflicts computed per staged well vs existing lines with qty_by_unit dicts; apply transactional full-replace per well + revision(source=import) + auto-create wells (Planned) + 409 on non-pending; discard 409 on non-pending.
**Required tests:** invariants 3 and 4; conflict payload shape (existing vs staged counts + qty_by_unit); template round-trip (openpyxl); non-pending 409s both endpoints; validation failure leaves no staging.

### Task 3: Demand list + well APIs
**Files:** `app/api/demand.py`, `app/api/wells.py`, register, `tests/test_demand_list.py`, `tests/test_wells.py`
**Contracts:** spec §API rows 6-8. Paging (page_size default 25, cap 100); all 7 filters combinable; overdue month-boundary semantics; names joined in (well_name, customer_name, product_name) with joinedload (no N+1); wells detail includes lines + revisions desc; status change records revision, same-value 409, invalid enum 422.
**Required tests:** invariants 1, 2 (both boundary days), 5, 7; filter combos (status+profile+ros range); page 2 correctness; status change revision content (before→after in summary).

### Task 4: Frontend — Demand Import screen
**Files:** `src/pages/DemandImport.tsx`, route `/demand/import`, nav, styles
**Contracts:** spec §Frontend. Customer selector URL-persisted; template download; upload (apiUpload); conflict table with qty_by_unit rendered per unit (never summed); Apply via ConfirmButton, Discard; past imports list with status chips; per-row 422 errors rendered as list (e.body.detail pattern from stage 2 fix); keep-data-on-error.
**Gates:** vitest 18, typecheck, build.

### Task 5: Frontend — Demand List + Well Workspace
**Files:** `src/pages/DemandList.tsx`, `src/pages/WellWorkspace.tsx`, routes `/demand`, `/wells/:id`, nav (Demand), styles
**Contracts:** spec §Frontend. All 7 filters in URL via lib/urlState; pager with total; overdue amber chip; row links to well; Well Workspace header status select + ConfirmButton, lines table with per-unit summary row, revision history list; breadcrumbs "Wells / {name}" with "…" while loading; Freshness both screens.
**Gates:** vitest 18, typecheck, build.

### Task 6: Seed + stage gate
**Files:** `seed/seed_minimal.py` extension; reseed dev.db; report
**Contracts:** add 2 wells per customer (mixed statuses), demand lines across products/profiles incl. one overdue ROS (previous month) and future ROS dates, one applied import with revisions, keep idempotent FK-safe reset. Stage gate: full backend suite, frontend gates, uvicorn curl smoke of all stage-3 endpoints (template xlsx bytes, upload→conflict→apply flow with a generated workbook, list filters, status change).
