# OCTG Rebuild Stage 4 Spec — Coverage / Actions

Follows the parent spec §3 (invariant rules) and REQUIREMENTS §4.2. Proceeding under blanket approval.
Where the requirements document leaves semantics undefined, this spec's [Ruling]s are authoritative (clean-room first-party decisions).

## Data model (1 migration)

- Add `customers.allocation_policy` column: enum SOFT/HARD/HYBRID NOT NULL default SOFT
- `coverage_results`: (demand_line_id FK unique, verdict enum, reason text, action text nullable, covered_qty float, covered_via json-text nullable, computed_at datetime). **Written only by the engine** (§3-6). No row = not yet evaluated
- `substitution_approvals`: (customer_id, well_id, demand_line_id FK, technical_substitution_id FK, status enum Pending/Approved/Rejected, customer_approved bool, well_approved bool, requested_at, decided_at nullable, note nullable). **Immutable once decided — re-deciding returns 409**

## Allocation engine (app/engines/allocation.py — pure function, the sole implementation, §3-7)

[Ruling A-1] Policy semantics (process a customer's lines in ROS-date → id order, allocate only within matching units):
- Common to all policies: **customer-owned inventory (belonging to that customer) is always consumed first** (§3-4. Never issued to another customer)
- HARD: next, only that customer's hard allocation (inventory_assignments.customer_id = self). Does not touch free inventory
- SOFT: next, BU-wide free inventory (on_hand − sum of all hard allocations, floored at 0)
- HYBRID: hard → free inventory, in that order
[Ruling A-2] Coverage nets only physical stock (on-hand + customer-owned). on_order is not used in the judgment (that's MRP/MOR's domain)
[Ruling A-3] BU is an absolute boundary: only the customer's own BU's inventory. For a customer with no BU set, the engine raises `InventoryScopeMissing` and the caller skips it (per-customer failure isolation)

## Coverage engine (app/engines/coverage.py)

- Scope: filters well status / line profile using settings' coverage_scope_statuses/profiles
- Judgment (per line item):
  - `Covered` — fully satisfied by allocation
  - `CoveredViaSubstitute` — the shortfall is satisfied by an **Approved** (customer_approved ∧ well_approved) substitution approval plus the substitute product's free inventory
  - `PendingApproval` — would be filled by substitution but approval is Pending (an undecided request exists)
  - `Uncovered` — physical stock sits in the yard but is unusable pending human action: (a) releasing a hard allocation to another customer would fill it → action "Release the hard assignment in Oracle" (b) a technical pair exists but no request has been made → action "Request substitution approval"
  - `Unrecoverable` — no physical quantity exists in the BU by any means → action "Order from mill"
  - Priority order: Covered > CoveredViaSubstitute > PendingApproval > Uncovered > Unrecoverable (the better verdict wins when multiple apply)
  - reason is always a human-readable sentence (English) that includes the quantity basis
- `recompute_customer(db, customer_id)`: fully replaces the CoverageResult rows for that customer's in-scope lines. **computed_at is always "now"** (updated even when the recomputed value is unchanged — lesson from C-08)
- `recompute_all(db)`: loops per customer; a failing customer is collected into skipped as (customer_name, reason) and processing continues (never aborts the whole run)
- [Ruling C-1] Verdict colors use the existing tokens: Covered=--ok / CoveredViaSubstitute=--ok(outlined) / PendingApproval=--warn / Uncovered=--warn(dark) / Unrecoverable=--bad. **Red (--bad) is used only when the item is physically absent**. Uncovered and Unrecoverable are never merged

## API

- `POST /coverage/recompute` (body {customer_id?}; unspecified = all customers) → {computed, skipped_customers:[{name,reason}]}
- `GET /coverage` → customer × well grid: {customers:[{id,name,policy,wells:[{id,name,status,line_count,verdict_rollup:{verdict:count},worst_verdict}]}], skipped_customers, computed_at_min/max}. Reads only the saved CoverageResult (does not recompute here). Unevaluated lines count in the rollup as "NotEvaluated" (not treated as 0)
- `GET /wells/{id}` response includes per-line coverage {verdict,reason,action,covered_qty,computed_at} (null when unevaluated)
- `GET /substitution/candidates?demand_line_id=` → candidates for a shortfall line: [{substitution_id, to_product(name), technical_ok, customer_rule_allowed, free_qty_by_unit, hard_assigned_qty, verdict_if_applied, blocked_by}]
  [Ruling S-1] blocked_by priority: `customer` (customer rule disallows/absent) > `well-approval` (well-level approval not yet complete) > `oracle-release` (substitute's free inventory is short, but releasing a hard allocation would cover it) > null
- `POST /substitution-approvals` {demand_line_id, technical_substitution_id} → creates a Pending request (duplicate Pending → 409; customer_rule_allowed=false → 422)
- `GET /substitution-approvals?status=` list (joined with customer/well/product names)
- `POST /substitution-approvals/{id}/decide` {customer_approved,well_approved,note?} → Approved if both true, Rejected if either is false, sets decided_at. **Re-deciding an already-decided item → 409 (a new request is needed to reverse it)**
- `GET /analysis/sharing?customer_id=` — **read-only what-if** (§3-3): for each Uncovered/Unrecoverable line of the target customer, uses the production engine to simulate whether it could be filled by other same-BU customers' releasable allocation → [{line, official_verdict (the saved value, unchanged), peers:[{customer_name, releasable_qty_by_unit, would_cover}]}]. **Customer-owned inventory is never eligible for sharing, under any circumstance**. Writes nothing to the DB at all (enforced by tests)

## Frontend (4 screens + extensions to existing ones)

- **Coverage Workspace** `/coverage`: customer × well grid (verdict chip colors per Ruling C-1, rollup counts), URL-persisted filters for worst_verdict/customer/search, Recompute button (no ConfirmButton needed — non-destructive), skipped_customers shown as a named amber banner, computed_at display + Freshness. Clicking a well → Well Workspace
- **Well Workspace extension**: line rows get a verdict chip + reason + action. If the action is substitution-related, a link to Substitution
- **Substitution Workspace** `/substitution?demand_line_id=`: candidate table for the shortfall line (blocked_by badge: customer=gray / well-approval=--warn / oracle-release=--warn + "Blocked — Oracle release required"). Request button (**if hard_assigned_qty>0 and not yet requested, show an amber warning before submitting: "HARD ALLOCATION INVOLVED — this request does not touch the Oracle reservation"**)
- **Approval Queue** `/approvals`: Pending/Approved/Rejected tabs (URL-persisted), inline Approve/Decline (ConfirmButton, with customer/well 2 checkboxes), decided items shown read-only
- **Sharing Analysis** `/analysis/sharing`: URL-persisted customer selector, official_status shown as the saved value + the what-if results. Note: "read-only what-if — the official verdict does not change"
- Nav: adds Coverage / Substitution / Approvals / Sharing

## Invariants pinned by tests

1. Customer-owned inventory is always consumed first and never flows to another customer (sharing included)
2. Policy: HARD never uses free inventory / SOFT never uses hard allocation / HYBRID's ordering
3. All 5 verdict types reproduce against seed-equivalent fixtures, each with a reason/action
4. Recompute advances computed_at even when the recomputed value is unchanged
5. Per-customer failure isolation: with a customer missing a BU, other customers still compute, and its name appears in skipped
6. Re-deciding a decided approval → 409; both true = Approved / either false = Rejected
7. Sharing never dirties the DB (row counts across all tables and CoverageResult unchanged before/after execution)
8. CoverageResult can only be written via the engine (no API writes it directly)
9. Unevaluated (no CoverageResult) is "NotEvaluated" — never treated as 0 or Covered

## Out of scope
- MRP/MOR/Surplus (Stage 5), Executive/scenarios (Stage 6), notifications
