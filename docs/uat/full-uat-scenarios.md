# OCTG Rebuild — Full UAT Scenarios

Target: https://octg-rebuild-uat.onrender.com (branch `claude/using-superpowers-skill-0ix8nt`)
Login: admin@octg.dev / octg-dev (Administrator), planner@octg.dev / octg-dev (SCEU Norway Planner)
*Free-tier hosting, so the first access may take up to 1 minute to spin up. **Redeploying resets the environment to its demo state.***
*Operation manual: /manual.html (no login required)*

For each scenario: record PASS/FAIL and any observations.

## A. Authentication & Common
- A1 Open `/` while logged out → redirected to Login. Wrong password → inline error. Correct credentials → Home
- A2 User menu at the bottom of the sidebar shows email/role. Sign out → returns to Login
- A3 Log in as planner → Administration is accessible (known gap C-15R — no need to file a bug)
- A4 Copy the URL on any screen → open in a new tab → filter/tab state is restored
- A5 Mobile width (or resize the window below 640px) → sidebar collapses; Coverage/MOR/MRP tables scroll horizontally without breaking layout

## B. Home
- B1 The KPI band (customers/well-status breakdown/coverage rate + unevaluated count/pending approvals) displays, and figures are not masked with dashes or zero
- B2 15 demand changes, well links navigate correctly. Pending approvals card shows rows from both "request" and "verdict" origins

## C. Demand
- C1 On /demand/import: select customer → download template → fill in a few rows → upload → conflict table (qty_by_unit broken out by unit) → Apply (two-step) → application summary
- C2 Deliberately break rows (unknown product, negative quantity, duplicate product) → error list with line numbers, nothing applied
- C3 The 7 filters and pagination on /demand. Rows with ROS in a prior month show an amber "Overdue" chip
- C4 On the well screen, change status (two-step ConfirmButton; same value returns a 409 error) → appended to revision history

## D. Coverage & Actions
- D1 On /coverage, Recompute → grid shows 5 verdict types color-coded (red is Unrecoverable only). If any customers are skipped, their names appear in an amber banner
- D2 Review the reason/action on a well's detail line → navigate to /substitution from a substitution-type action
- D3 The candidate table's blocked_by badge; anything involving a hard allocation shows a "HARD ALLOCATION INVOLVED" warning → submit request
- D4 On /approvals, approve (both checks → Approved / unchecking one → Rejected, matching the description). A decided item cannot be re-decided
- D5 /analysis/sharing — read-only what-if notice and a warning that "the donor side may become short"

## E. Supply Planning
- E1 /mrp: 3 runout series, POs without a due date listed separately. Excel export → same column layout as the screen, runout month highlighted in red
- E2 By Item: ledger (opening breakdown / inbound in two tiers = blue and amber italic / outbound / closing breakdown)
- E3 /mrp/order-requirements: strip (color = balance sign only, ▲▼▽ markers + legend, overdue items get an "▲ overdue" chip). Numbers change with the customer filter
- E4 /surplus: three-way breakdown of Allocated · Surplus · Obsolete (no identity-mismatch banner should appear)

## F. Executive & Scenarios
- F1 /executive: 5 blocks, MT headline with native breakdown, "some products excluded" note. Changing the scope check → amber "Recomputed read-only…" banner, official verdict unchanged (compare against the /coverage value)
- F2 /scenarios: seeded "What-if: Equinor uplift" → Preview shows a before/after comparison table (verdict-count differences highlighted)
- F3 On the Editor timeline, drag a line-item chip to a different month → exactly one ros_date override is added
- F4 Add a po_arrival override and Apply → red warning + 422 rejected list (nothing applied). Remove the override and Apply → Applied; re-applying returns 409

## G. Master Data & Inventory
- G1 /admin, 5 tabs: in the BU hierarchy, attempting to make a grandchild its own parent → error (screen does not go blank). Safety stock distinguishes "unset = —" from "explicitly 0"
- G2 /inventory/company: Oracle-sourced rows are not editable (409); manually entered rows support full CRUD (C-03R). POs without a due date shown in a "Date TBD" slot
- G3 /inventory/customer-owned: customers with no upload show a "No data uploaded — 0 is not the same as no data" banner. Template round-trip
- G4 /catalog: sorting (aria-sort), detail panel shows safety stock/lead time/substitution relationships

## H. Known Limitations (please do not report these as bugs)
- No role gate on the admin screens (C-15R), no object-level authorization (C-13R), no rate limiting — to be addressed in the production-hardening phase
- MT values exclude products with unset weight (noted in the UI)
- No screenshots in the manual (F-1)
- Monetary valuation and notifications are not implemented (out of scope)
