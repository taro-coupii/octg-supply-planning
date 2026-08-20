# OCTG Rebuild Stage 6 Spec — Executive View / Scenarios

Per REQUIREMENTS §4.4/§4.5. Proceeding under blanket approval.

## Rulings

- [E-1] MT-unified headline: conversion happens **only at the display layer** (API response assembly). Where `weight_kg` exists, qty×weight/1000 (both Mtr and PC treated approximately as "count × weight"; MT-unit items pass through unchanged). Products with null weight are excluded from the MT headline and flagged `mt_incomplete:true`. The native-unit breakdown (qty_by_unit) is always ground truth
- [E-2] Scope override (the Executive/Surplus status[]/profile[] parameters): the default scope = a fast read of the saved CoverageResult. A non-default scope = **in-memory judgment** (the coverage engine's judgment logic run without persistence — no write→rollback; this structurally avoids pysqlite's SAVEPOINT pitfall). The response includes scope_is_default / status_scope / profile_scope. Non-default responses carry a warning field: "Recomputed read-only — NOT the official stored verdicts"
- [E-3] Scenario preview: apply the scenario overrides to an **in-memory SQLite copy** (via the sqlite3 backup API), then run **the same engines** (coverage/mrp/mor) unmodified against it. Nothing is ever written to the real DB
- [E-4] Scenario apply: writes only platform-owned data — demand (quantity, ROS, well status) and approval flips. **An apply that includes any supply-side override (PO arrival-date change, hard-allocation release) returns 422**, listing the offending overrides (§3-5). Re-applying an already-applied scenario → 409. **No DELETE endpoint is built (deliberately)**
- [E-5] The timeline is a section inside the Editor. Dragging a line-item chip across month columns via HTML5 drag-and-drop generates a ros_date override (if that proves too hard to implement, degrade to click-shift and log it in the compromise register)

## Data model (1 migration)

- `scenarios`: (name, created_at, status enum Draft/Applied, applied_at nullable)
- `scenario_overrides`: (scenario_id FK, kind enum quantity/ros_date/well_status/po_arrival/hard_release/approval_flip, target_id (target row's UUID), payload text (JSON: the new value), created_at). **The target is stored by UUID, not name, but the API response always resolves and returns the target's name**

## Executive engine/API (app/engines/executive.py, GET /dashboard/executive)

5 blocks (all support the scope parameter; each block has an MT headline + a native breakdown [E-1]):
1. `demand_trend`: monthly demand (qty_by_unit) for the past 6 months / next 12 months. **Rows where DemandLine.created_at > as_of are excluded** (keeps the trend consistent with its reference date)
2. `coverage`: line-item counts by verdict + human-readable labels (display names like "Covered via substitute" are a display-layer concern)
3. `supply_risk`: the list of products whose baseline runout falls within the horizon (sorted ascending by first runout month) = the first-runout rollup
4. `soft_allocation`: total free-inventory consumption per customer (how dependent SOFT/HYBRID allocations are on free inventory)
5. `inventory_utilisation`: a summary of the 36-month Surplus engine results (totals of allocated/surplus/obsolete; a collapsed per-product breakdown sorted descending by not-tied amount)
- Uncomputed/missing data returns available:false + reason (numbers are never fabricated)

## Surplus scope support

Adds status[]/profile[] to `GET /analysis/surplus` [E-2]. Default = existing behavior. Non-default = swaps in-scope demand via in-memory judgment and reflects it in the obsolete determination

## Scenario API

- `GET/POST /scenarios` (list / create), `GET /scenarios/{id}` (including overrides, with target names resolved)
- `POST /scenarios/{id}/overrides` (add. Validates kind/target/payload: target existence, payload type per kind) / `DELETE /scenarios/{id}/overrides/{oid}` (Draft only. Applied → 409)
- `GET /scenarios/{id}/preview?sections=coverage,mrp` → before/after comparison of coverage rollup / mrp runouts, run against the in-memory copy [E-3]
- `POST /scenarios/{id}/apply` → [E-4]. Response {applied_overrides, rejected: []} (if rejected is non-empty, returns 422 and applies nothing)
- No DELETE for scenarios themselves

## Frontend

- **Executive Dashboard** `/executive` (nav label "Executive"): 5 block cards. Large MT headline + a small "native breakdown" table. `mt_incomplete` shown as a note: "MT value excludes some products." Scope checkboxes (status/profile — **the parameter is not sent until touched**; built as a new shared `ScopeChecks` component). Non-default state shows an amber banner: "Recomputed read-only — NOT the official stored verdicts." Coverage labels are human-readable
- **Surplus extension**: the same ScopeChecks + banner
- **Scenario List** `/scenarios`: list (status chips), a new-scenario creation form. Row → Editor
- **Scenario Editor** `/scenarios/:id`: an add-override form (input varies by kind, target chosen by name selector), an override list (names resolved, deletable only while Draft), a **timeline section** (month columns × line-item chips, drag-and-drop generates a ros_date override [E-5]), a Preview button → before/after comparison display, Apply (ConfirmButton; if any supply-side override is present, shows a red warning up front and the 422 rejected list)
- Common: Freshness, breadcrumbs, keep-data-on-error, unit display, no UUIDs shown

## Invariants pinned by tests

1. MT conversion happens only at the display layer (engine output stays native — enforced by engine tests); weight-null exclusion + mt_incomplete
2. A non-default scope never alters the saved CoverageResult (all rows unchanged before/after execution)
3. Preview never writes to the real DB (row counts unchanged across all tables) and runs the exact same code path as production (no second implementation — no separate judgment function built just for preview)
4. Apply: any mix of supply-side overrides → 422, atomically (nothing applied); platform-only overrides → applied + transitions to Applied + re-apply → 409
5. demand_trend's created_at cutoff
6. No DELETE route exists for scenarios
