# OCTG Rebuild — Stage 4: Coverage & Actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** Allocation + coverage engines, substitution two-layer approvals, approval queue, cross-customer sharing what-if, and the four screens, per the stage-4 spec.

**Spec:** `docs/specs/2026-08-19-octg-stage4-coverage.md` (binding; all semantics live there — implementers read it first).

## Global Constraints
Same as stages 2-3 (clean-room, branch, C-01R, unit on every quantity, TDD, per-task gates, standard trailers, ONE migration in Task 1). Engines are pure modules under `app/engines/` — API layers never re-implement engine math (§3-7).

---

### Task 1: Models + migration
**Files:** `app/models/{coverage,substitution_approval}.py` (+customer policy column), models `__init__`, one alembic revision, `tests/test_stage4_models.py`
**Contracts:** spec §Data Model. allocation_policy enum SOFT/HARD/HYBRID default SOFT on customers; coverage_results unique(demand_line_id); substitution_approvals fields incl. two bools + status enum.
**Required tests:** uniques; enum values; FK enforcement; coverage_results row deletable/replaceable (engine will full-replace); migration named constraints; dev.db upgraded.

### Task 2: Allocation engine (pure, TDD-heavy)
**Files:** `app/engines/allocation.py`, `tests/test_allocation_engine.py`
**Contracts:** spec ruling A-1/A-2/A-3 exactly. Input: db + customer; output per-line allocation {from_owned, from_hard, from_free, shortfall} in ROS→id order, unit-matched only. Raises InventoryScopeMissing (define in engine module) for BU-less customers.
**Required tests:** invariants 1 and 2 (all three policies, owned-first, HARD never touches free, SOFT never touches hard, HYBRID order, ros-date ordering determinism, unit mismatch ignored, BU boundary — other BU's stock invisible), free = on_hand − total hard (floor 0).

### Task 3: Coverage engine + APIs
**Files:** `app/engines/coverage.py`, `app/api/coverage.py`, extend `app/api/wells.py` detail with coverage, register, `tests/test_coverage_engine.py`, `tests/test_coverage_api.py`
**Contracts:** spec §Coverage Engine + §API rows 1-3. Verdict decision tree exactly as specced (incl. priority order and reason/action text with quantities); scope from settings; recompute_customer full-replace + computed_at always now; recompute_all isolation with named skipped; GET /coverage reads stored only, NotEvaluated counted.
**Required tests:** invariants 3, 4, 5, 8, 9; each verdict reproduced from fixtures (incl. hard-release Uncovered case and substitute-approved case); scope filter respected (Planned well excluded under default Confirmed-only? use settings rows from seed: statuses=Confirmed — verify a Planned well's lines get no results); grid rollup shape.

### Task 4: Substitution candidates + approvals APIs
**Files:** `app/api/substitution.py`, `app/api/substitution_approvals.py`, register, `tests/test_substitution_api.py`, `tests/test_approvals_api.py`
**Contracts:** spec §API rows 4-7 incl. ruling S-1 blocked_by priority, duplicate-pending 409, rule-disallowed 422, decide semantics (both-true Approved / any-false Rejected / re-decide 409), joined names in list.
**Required tests:** invariant 6; blocked_by priority ordering (construct all three block cases); candidates free_qty/hard_assigned figures; decided immutability.

### Task 5: Sharing engine + API
**Files:** `app/engines/sharing.py`, `app/api/analysis.py`, register, `tests/test_sharing.py`
**Contracts:** spec §API row 8. Uses allocation engine (no second implementation); read-only guarantee (no commit; assert row counts unchanged in tests); customer-owned never shareable; official_verdict from stored CoverageResult verbatim; BU boundary.
**Required tests:** invariants 1 (sharing direction), 7; would_cover math; peer releasable = peer's from_free+from_hard? — NO: per spec, releasable = peer's allocated company stock (owned excluded); BU isolation.

### Task 6: Coverage Workspace + Well Workspace extension (frontend)
**Files:** `src/pages/CoverageWorkspace.tsx`, route `/coverage`, nav; extend `src/pages/WellWorkspace.tsx` with verdict chips/reason/action + substitution link; verdict chip styles per ruling C-1 in index.css
**Contracts:** spec §Frontend rows 1-2. Filters URL-persisted; skipped banner; computed_at + Freshness; NotEvaluated rendered as its own neutral chip (never merged).
**Gates:** vitest 18, typecheck, build.

### Task 7: Substitution Workspace + Approval Queue (frontend)
**Files:** `src/pages/SubstitutionWorkspace.tsx`, `src/pages/ApprovalQueue.tsx`, routes `/substitution`, `/approvals`, nav
**Contracts:** spec §Frontend rows 3-4 incl. HARD ALLOCATION INVOLVED pre-submit warning condition (hard_assigned_qty>0 && no existing request && not oracle-blocked) and blocked_by badges; tabs URL-persisted; decided rows read-only.
**Gates:** vitest 18, typecheck, build.

### Task 8: Sharing screen + seed demo cases + stage gate
**Files:** `src/pages/SharingAnalysis.tsx`, route `/analysis/sharing`, nav; seed extension; reseed dev.db
**Contracts:** Sharing screen per spec. Seed adds demo cases that exercise all five verdicts: shortage products (adjust on-hand), a hard assignment blocking another customer's earlier-ROS line (Uncovered w/ release action), an Approved substitution rescuing a line (CoveredViaSubstitute), a Pending approval (PendingApproval), a bare shortage (Unrecoverable); run recompute in seed so dev.db ships with stored verdicts; settings scope kept Confirmed/Primary,Contingency and at least one Confirmed well per case.
**Stage gate:** backend suite green; frontend gates; uvicorn smoke: recompute → grid → well detail → candidates → request → decide → sharing; verify all five verdicts appear in the grid; commit reseeded dev.db.
