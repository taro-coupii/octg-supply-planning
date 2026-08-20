# OCTG Rebuild — Stage 6: Executive & Scenarios Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** Executive dashboard (5 blocks, MT headlines, scope override), Surplus scope filters, scenario CRUD/preview/apply with the same-engine guarantee, and the three screens.

**Spec:** `docs/specs/2026-08-19-octg-stage6-executive-scenarios.md` (binding; rulings E-1..E-5).

## Global Constraints
Same as prior stages. Preview must run the production engines against an in-memory SQLite copy (E-3) — no second implementation of any calculation.

---

### Task 1: Scenario models + migration
**Files:** `app/models/scenario.py`, models `__init__`, one alembic revision, `tests/test_stage6_models.py`
**Contracts:** spec §Data Model. Status enum Draft/Applied; override kind enum (6 kinds); payload JSON text.
**Required tests:** enums, FKs, migration named constraints, dev.db upgraded.

### Task 2: Executive engine + API; Surplus scope
**Files:** `app/engines/executive.py`, `app/api/dashboard.py`, extend `app/api/analysis.py` + coverage engine with an in-memory judge path (no persistence) for scope override, register, `tests/test_executive.py`, `tests/test_scope_override.py`
**Contracts:** spec §Executive + E-1 + E-2 + invariants 1/2/5. 5 blocks exactly; MT conversion in API layer only; in-memory judge = refactor coverage engine so the verdict computation is callable without writing CoverageResults (recompute keeps using it then persists — one code path).
**Required tests:** invariants 1, 2, 5; each block's shape incl. available:false path; scope override returns different rollup for widened scope while stored rows unchanged.

### Task 3: Scenario engine + APIs
**Files:** `app/engines/scenario.py` (in-memory copy + override application), `app/api/scenarios.py`, register, `tests/test_scenario_engine.py`, `tests/test_scenarios_api.py`
**Contracts:** spec §Scenario API + E-3 + E-4 + invariants 3/4/6. sqlite3 backup API copy; override kinds applied to the copy (quantity/ros_date/well_status → demand rows; po_arrival → on_order expected_date; hard_release → delete assignment row; approval_flip → decided flags) — ALL kinds allowed in preview, only platform-owned in apply; preview runs coverage recompute + mrp on the copy and diffs against current stored/base values.
**Required tests:** invariants 3 (row counts + AST-level no-second-judge), 4 (atomic 422 with rejected list; happy apply; re-apply 409), 6 (no DELETE route on /scenarios/{id}).

### Task 4: Frontend — Executive + Surplus scope + ScopeChecks
**Files:** `src/components/ScopeChecks.tsx`, `src/pages/ExecutiveDashboard.tsx`, route `/executive`, nav "Executive"; extend `src/pages/SurplusList.tsx`
**Contracts:** spec §Frontend rows 1-2. Send params only after user touches a checkbox; amber non-default banner; MT headline + native breakdown + mt_incomplete note; human-readable coverage labels (display map); last checkbox can't be unchecked.
**Gates:** vitest 18, typecheck, build.

### Task 5: Frontend — Scenario List + Editor with timeline
**Files:** `src/pages/ScenarioList.tsx`, `src/pages/ScenarioEditor.tsx`, routes `/scenarios`, `/scenarios/:id`, nav "Scenarios"
**Contracts:** spec §Frontend rows 3-4 + E-5. HTML5 drag&drop timeline (month columns × line chips → ros_date override on drop); override forms per kind with name selectors; preview before/after panel; apply flow with supply-override red warning + rejected 422 rendering; Draft-only override deletion (ConfirmButton).
**Gates:** vitest 18, typecheck, build.

### Task 6: Seed + stage gate
**Files:** seed extension (one Draft scenario with a quantity + ros_date override; ensure preview shows a verdict delta); reseed dev.db; stage gate: full suites; uvicorn smoke of executive (default + widened scope), surplus scoped, scenario create→override→preview→apply happy path AND a supply-override 422 path; commit.
