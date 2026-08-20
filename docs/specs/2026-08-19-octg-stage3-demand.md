# OCTG Rebuild Stage 3 Spec — Demand

Follows the parent spec. Per REQUIREMENTS.md §4.1, §3-2 (demand_status lives only on Well), and the cross-cutting rule (overdue = ROS month < current month).
Proceeding under blanket approval.

## Data model

- `wells`: (customer_id FK NOT NULL, name, demand_status enum Planned/Budgeted/Confirmed NOT NULL default Planned). unique(customer_id, name). **status lives only here** (§3-2)
- `demand_lines`: (well_id FK, product_id FK, quantity float>0, unit enum, ros_date date NOT NULL, profile enum Primary/Contingency, created_at datetime UTC). **has no status column**
- `demand_revisions`: (well_id FK, revision_no int, applied_at, source enum import/manual/status_change, summary text). unique(well_id, revision_no). Append-only (no API is built to UPDATE/DELETE it)
- Import staging:
  - `demand_imports`: (customer_id FK, uploaded_at, filename, status enum pending/applied/discarded)
  - `demand_import_rows`: (import_id FK, row_no, well_name, product_id FK, quantity, unit, ros_date, profile)
- [Ruling D-1] Imports are per customer. Applying is a full replace per well (the staged rows become that well's new line-item set). New wells are auto-created as Planned
- [Ruling D-2] A conflict = a staged well that already has existing lines. Show the diff (existing line count / new line count · quantity totals per unit); applying is one operation for the whole import (records revision_no++ per well as it applies)

## API

- `GET /demand/template` → xlsx (columns: Well / Product / Quantity / Unit / ROS Date / Profile)
- `POST /demand/imports?customer_id=` — xlsx upload. Validates every row (unknown product, invalid unit/profile/date, quantity<=0 → 422 with row number) → saved to staging, status=pending. Response includes conflicts: [{well_name, existing_line_count, staged_line_count, existing_qty_by_unit, staged_qty_by_unit}]
- `GET /demand/imports` / `GET /demand/imports/{id}` — list / detail (including recomputed conflicts)
- `POST /demand/imports/{id}/apply` — pending only (applied/discarded → 409). Within a transaction, replaces per well + records a revision (source=import). Creates new wells. Response {applied_wells, created_wells}
- `POST /demand/imports/{id}/discard` — pending → discarded (staging retained. Same 409 rule)
- `GET /demand/lines` — server-side paging via page/page_size(25) + **7 filters**: customer_id / well_id / product_id / status (the well's status) / profile / ros_from / ros_to. Response {total, page, page_size, items:[{…, well_name, customer_name, product_name, unit, overdue:bool}]}. overdue = ros_date's month < today's month [cross-cutting rule]. **as_of ("now") is obtained server-side**
- `GET /wells/{id}` — well + lines + revisions (newest first). `GET /wells?customer_id=` list (with line counts)
- `POST /wells/{id}/status` — {status}, validated. On change, records a revision (source=status_change, summary holding before→after). Changing to the same value is 409
- Follows the existing 404 contract. Quantity responses always carry unit. Mixed aggregates use a qty_by_unit dict (scalar summing is forbidden, §3-8)

## Frontend (3 screens)

- **Demand Import** `/demand/import`: customer selection (URL) → template download → upload → conflict table (per well: existing vs. new line count, qty_by_unit) → Apply (ConfirmButton) / Discard. Post-apply summary + a "View Demand List" link. Past import list (with status)
- **Demand List** `/demand`: server-paged table. All 7 filters in the URL. Overdue rows get an amber "Overdue" chip (shown as its own indicator, never silently merged in). Pager (prev/next, total shown). Row → link to Well Workspace
- **Well Workspace** `/wells/:id`: header (customer name, well name, status-change selector + ConfirmButton), line-item table (with unit, a qty_by_unit summary row), **revision history list** (no / timestamp / source / summary — per doc-governs ruling #8). Breadcrumb "Wells / {name}" ("…" while loading)
- Demand (/demand) and Demand Import added to nav. Freshness, data preserved on error, honest empty states

## Invariants pinned by tests

1. demand_status lives only on Well (schema ensures DemandLine has no such column); status changes record a revision
2. Overdue boundary: last day of previous month = overdue, first day of current month = not overdue
3. Import apply is a full replace + new wells created as Planned + sequential revision_no, re-applying an applied import → 409
4. An upload that fails validation leaves no staging behind either (all rows validated up front)
5. Paging and filter combinations (total reflects the post-filter count)
6. Revisions are append-only (no API path exists to change them)
7. qty_by_unit aggregation never returns a scalar sum

## Out of scope

- Reflecting into coverage (Stage 4), Home's Demand changes (Stage 8), Excel export (Stage 5)
