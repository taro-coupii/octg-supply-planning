# OCTG Rebuild Stage 2 Spec — Masters / Inventory

Parent spec: `2026-08-19-octg-platform-rebuild-design.md` (§4/§5). Per REQUIREMENTS.md §4.6.
Approval: proceeding without confirmation gates, per the product owner's blanket approval ("UAT will cover everything as a whole. Proceed.", 2026-08-19).

## Data model (migrations added per stage)

- `lead_times`: (business_unit_id FK nullable, product_id FK nullable, months int>0). A row with both null = the overall default. The most specific row wins (BU+product > product > BU > default) [Ruling LT-1: dimensions start as BU × product. REQUIREMENTS' "with attribute dimensions" is interpreted as these 2 axes]
- `safety_stocks`: (business_unit_id FK, product_id FK, quantity float>=0, unit enum). **No row = unset (null)**; a row present with 0 = explicit zero. unique(bu, product)
- `technical_substitutions`: (from_product_id, to_product_id) unique directed pair. Self-substitution forbidden (CHECK from≠to)
- `customer_substitution_rules`: (customer_id, technical_substitution_id, allowed bool) unique(customer, substitution)
- `settings`: (key unique, value) — holds coverage scope defaults as `coverage_scope_statuses`="Confirmed" / `coverage_scope_profiles`="Primary,Contingency" [Ruling SC-1: a single settings table]
- Oracle read-only projections (the platform does not write to these, in principle — §3-5):
  - `inventory_on_hand`: (business_unit_id, product_id, quantity, unit, source_system str default "manual") unique(bu, product)
  - `inventory_assignments`: (business_unit_id, product_id, customer_id, quantity, unit, reference str nullable) — hard allocation
  - `inventory_on_order`: (business_unit_id, product_id, quantity, unit, expected_date date **nullable**, booking_status enum "PO'ed"/"Book'ed")
- The only inventory the platform may write:
  - `customer_owned_inventory`: (customer_id, product_id, quantity, unit) unique(customer, product)
  - `customer_owned_uploads`: (customer_id, uploaded_at, filename, row_count) — `has_uploaded` is determined by the presence of a row in this table ("not uploaded" ≠ "zero")

## API

All endpoints unauthenticated (C-01R continues). Any response containing a quantity always carries `unit` (§3-8).

**Administration (`/admin` prefix)** [C-xxR: reproduces the non-enforcement of require_admin faithfully, logged in the register]
- `POST/PATCH/DELETE /admin/business-units` — hierarchy edits. **If a parent change would create a cycle, reject with 422** (acceptance criterion from the Stage 1 final review). DELETE on a BU that has children or customers is 409
- `GET/PUT /admin/lead-times` — list + bulk upsert
- `GET/PUT /admin/coverage-scope` — get/save statuses[]/profiles[] (enum-validated)
- `GET/POST/DELETE /admin/substitutions` — technical pairs. `GET/PUT /admin/substitutions/customer-rules?customer_id=` — customer rules
- `GET/PUT /admin/safety-stocks` — upsert. **A PUT sending quantity:null deletes the row (= reverts to unset); 0 saves a row with 0**

**Inventory**
- `GET /company-inventory` → { on_hand: [...], assignments: [...], on_order: [...] }, each row carrying unit. on_order's expected_date null is returned as null (not coerced onto a month)
- C-03R company-owned inventory maintenance: `POST/PATCH/DELETE /company-inventory/on-hand` — changes to a row where `source_system=="oracle"` return 409 [put marker COMPROMISE[C-03R] in the code]
- `GET /customer-owned-inventory?customer_id=` → { has_uploaded: bool, uploaded_at: str|null, positions: [...] }. When not uploaded, positions=[] and has_uploaded=false
- `GET /customer-owned-inventory/template` → xlsx (openpyxl. Columns: Product / Quantity / Unit)
- `POST /customer-owned-inventory/upload?customer_id=` — xlsx multipart. Validation (unknown product name, invalid unit, negative quantity → 422 with row number) → full replace → recorded in uploads

## Frontend (4 screens, added to nav)

- **Administration** `/admin`: 5 tabs (BU hierarchy / Lead times / Coverage scope / Substitutions / Safety stocks). Tabs persist in the URL (?tab=). Destructive operations (DELETE-type) use ConfirmButton. BU hierarchy shown indented + add/rename/reparent/delete
- **Company Inventory** `/inventory/company`: 3 tabs (On hand / Oracle assignments / On order), URL-persisted. The C-03R maintenance form lives inside the On hand tab. on_order rows with no date get a separate "date TBD" aggregate
- **Customer-Owned Inventory** `/inventory/customer-owned`: customer selector (URL-persisted), template download button, upload, positions table. When not uploaded, an explicit banner: "No data uploaded — this is not zero"
- **Product Workspace** `/products`: list (client-side sort via lib/sort) + detail panel (shows unit, weight, safety stocks, lead times, substitution relationships)
- All screens: Freshness, breadcrumbs, honest empty states (no faking it with a dash)

## Invariants pinned by tests

1. The API rejects a BU cycle with 422 (parenting to self, or to a descendant)
2. Safety stock: no row ≠ 0 — GET returns "unset"; sending null deletes the row
3. Customer-owned: upload is a full replace, has_uploaded transitions correctly, never touches another customer's data
4. on_order: a row with expected_date null can be saved and returned
5. C-03R: modifying a row with source_system=oracle returns 409
6. Substitutions: from≠to, directed, customer rules unique
7. Template round-trip: fill in the downloaded xlsx → upload → becomes positions

## Out of scope (not built this stage)

- Reflecting the judgment engine / coverage impact of inventory (Stage 4)
- MT-conversion display, monetary valuation
- The Customer.allocation_policy column (Stage 4)
