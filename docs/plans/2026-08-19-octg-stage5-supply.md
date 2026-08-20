# OCTG Rebuild — Stage 5: Supply Planning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** MRP/MOR/Surplus engines, APIs incl. Excel export, four screens, per the stage-5 spec.

**Spec:** `docs/specs/2026-08-19-octg-stage5-supply.md` (binding).

## Global Constraints
Same as prior stages. No new models/migrations expected (engines read existing tables). Time-phased engines are NEW modules; physical pool math reuses allocation helpers where point-in-time (Surplus allocated; MRP opening). Excel via openpyxl.

---

### Task 1: MRP engine
**Files:** `app/engines/mrp.py`, `tests/test_mrp_engine.py`
**Contracts:** spec §MRP Engine + M-0..M-3. Pure function e.g. `mrp_rows(db, horizon, recommended_by_product=None) -> list[ProductLedger]`; owned-first issue split; identity per month; 3 runout series; undated separated.
**Required tests:** invariants 1, 2, 7; negative closing visible; owned/company split arithmetic; horizon clamp.

### Task 2: MOR engine
**Files:** `app/engines/mor.py`, `tests/test_mor_engine.py`
**Contracts:** spec §MOR Engine + M-4. `mor_rows(db, horizon, customer_id=None)`; lead-time resolution specificity order; ex-mill ceil months; safety-stock trigger independent of shortage; markers; customer filter BU rule with unavailable_reason.
**Required tests:** invariants 3 (with a coverage-regression assertion), 4, 5; incremental shortfall charged to first month; overdue ex-mill flag; safety breach month vs runout month distinct.

### Task 3: Surplus engine + all APIs
**Files:** `app/engines/surplus.py`, `app/api/mrp.py`, `app/api/analysis.py` (extend), register, `tests/test_surplus_engine.py`, `tests/test_mrp_api.py`, `tests/test_mrp_export.py`
**Contracts:** spec §Surplus Engine + §API. Surplus identity by construction and pinned; APIs exactly per spec shapes incl. scope field; export tabs mirror screen columns with header-anchor tests and runout red fill; MOR recommended orders feed MRP's receipts_recommended (API composes: call mor engine, pass into mrp engine).
**Required tests:** invariants 6, 8; API shapes; export header anchors; scope note present.

### Task 4: Frontend — MRP Summary + By Item
**Files:** `src/pages/MrpSummary.tsx`, `src/pages/MrpByItem.tsx`, routes `/mrp`, `/mrp/items/:id`, nav "MRP", styles
**Contracts:** spec §Frontend rows 1-2: horizon URL-persisted; export button (window.location to /mrp/export); booked=blue tint, recommended=amber italic (new CSS classes on tokens); scope banner; runout months shown; undated column.
**Gates:** vitest 18, typecheck, build.

### Task 5: Frontend — MOR + Surplus
**Files:** `src/pages/MaterialOrderReq.tsx`, `src/pages/SurplusList.tsx`, routes `/mrp/order-requirements`, `/surplus`, nav "Order Reqs" + "Surplus"
**Contracts:** spec §Frontend rows 3-4: strip cells with ▲▼▽ markers + fixed legend; customer filter URL-persisted; unavailable reasons; Surplus dotted triple with colors; identity_ok defensive banner; sort via lib/sort.
**Gates:** vitest 18, typecheck, build.

### Task 6: Seed cases + stage gate
**Files:** seed extension if needed (ensure: a product with months-out shortage → MOR requirement with ex-mill in future, one with overdue ex-mill, safety-stock breach distinct from runout, obsolete product with stock and no demand); reseed dev.db; stage gate: full suites + uvicorn smoke of all 5 endpoints + export xlsx bytes + verify identity_ok true and all three MOR marker kinds occur; commit.
