# OCTG Rebuild Stage 5 Spec — Supply Planning (MRP / MOR / Surplus)

Per REQUIREMENTS §4.3. Proceeding under blanket approval. Per the Stage 4 review guidance, **time-series netting gets a new engine**
(allocation stays a point-in-time snapshot). Physical-pool computation reuses allocation's helper as a first-pass implementation.

## Common rulings

- [M-0] Calculation scope = the Administration defaults (settings: Confirmed / Primary,Contingency). The screen shows a scope note
- [M-1] Month bucket = the calendar month of the ROS date / PO expected_date. **A PO with no date is never placed into a month**; it's reported separately as `on_order_undated`
- [M-2] Units: computed independently per product unit (no mixed sums). Every quantity carries unit
- [M-3] horizon parameter is in months (default 12, max 36). Base point = current month

## MRP engine (app/engines/mrp.py)

A monthly ledger per (bu, product):
- Opening balance = company OH (on_hand) and customer-owned inventory (summed across all customers in the BU), **kept as a breakdown**. Month 0's opening = the current value; subsequent months carry forward the prior month's closing
- Receipts, two tiers: `receipts_booked` (dated POs, regardless of booking_status) / `receipts_recommended` (the MOR engine's recommended orders injected — joined at the API layer; the engine takes it as an argument)
- Issues = the sum of in-scope demand for the ROS month. **Consumes customer-owned inventory first**, as an approximation, reducing that side of the breakdown (any shortfall comes from the company side)
- Closing balance = opening + receipts − issues (breakdown maintained; negative values allowed — makes shortfall visible)
- **Identity (pinned by tests)**: for each month, opening_total + receipts_booked + receipts_recommended − issues = closing_total, and the breakdown sums to the total
- 3 runout series: `baseline` (no receipts) / `with_recommended` (both tiers) / `on_order` (booked only) — each series is a column of month-end balances. The first month it drops below 0 = the runout month

## MOR engine (app/engines/mor.py)

Per (bu, product):
- Nets monthly ROS-bucketed demand against current inventory (company + customer-owned) plus dated POs. Any incremental shortfall is booked as `required_qty` in its first shortfall month
- **Safety stock affects only this engine** (does not touch coverage): the first month the closing balance drops below safety_stock also triggers an order requirement (marked independently of the shortfall month)
- `ex_mill_month` (order deadline) = required month − lead_time_months (LeadTime resolution: BU+product > product > BU > overall default > 0). Once it falls in the past, flag it "overdue"
- Strip series: one month-end balance series + markers: `order_deadline` (▲) / `physical_runout` (▼, first month balance<0, red) / `safety_breach` (▽, first month balance<safety, amber)
- When filtered by customer [M-4]: nets using that customer's BU's on_hand/POs plus **only that customer's own customer-owned inventory** (crossing BUs is forbidden; a customer with no BU set is unavailable, with a reason)

## Surplus engine (app/engines/surplus.py)

Per (bu, product), horizon fixed at 36 months:
- `allocated` = company inventory consumed by customer demand via the allocation engine (hard + free consumption combined)
- `obsolete` = the entire unallocated company OH of a product with zero in-scope demand within the horizon
- `surplus` = OH − allocated − obsolete (has demand, but more than needed)
- **Identity: on_hand_total = allocated + surplus + obsolete, pinned by tests** (both per-product and overall total)
- Monetary valuation is out of scope (Price master not yet introduced)

## API

- `GET /mrp/summary?horizon=` → per product {product, unit, opening, runout_months:{baseline,with_recommended,on_order}, months:[{month, opening{company,owned}, receipts_booked, receipts_recommended, issues, closing{company,owned}}], on_order_undated}
- `GET /mrp/items/{product_id}?horizon=&bu_id=` → detail for the above one product, plus its line items (in-scope demand lines)
- `GET /mrp/export?horizon=` → xlsx: a Summary tab + a per-product tab (same layout as the screen: a MONTHLY LEDGER block + a DEMAND LINES block, runout month filled red)
- `GET /mrp/order-requirements?horizon=&customer_id=` → MOR: per product {strip:[{month, closing_balance}], markers:{order_deadline, physical_runout, safety_breach}, requirements:[{need_month, qty, unit, ex_mill_month, overdue:bool}], safety_stock, unavailable_reason?}
- `GET /analysis/surplus` → {rows:[{product, unit, on_hand, allocated, surplus, obsolete}], totals_by_unit, identity_ok:bool}
- Every response carries a scope note field {scope:{statuses,profiles}}

## Frontend (4 screens)

- **MRP Summary** `/mrp`: product table (the 3 runout series' months, a separate on_order_undated column), URL-persisted horizon selector, row → By Item. Excel export button. Scope-note banner ("Computed using the Administration default scope")
- **MRP By Item** `/mrp/items/:id`: monthly ledger table (opening breakdown / receipts two tiers: booked shown in blue tones, recommended in amber italic / issues / closing breakdown), a mini display of the 3 runout series, demand line-item block
- **Material Order Requirements** `/mrp/order-requirements`: per-product strip (month cells: balance, color = coverable (--ok tones) / short (--bad tones), ▲order_deadline · ▼physical_runout(red) · ▽safety_breach(amber) + a fixed legend), requirements table (need/qty/ex-mill/overdue chip), URL-persisted customer filter, unavailable items show their reason
- **Surplus List** `/surplus`: row = product: OH / `Allocated n · Surplus n · Obsolete n` (color dots: --ok/--warn/--bad tones), a red banner if identity_ok is false (a defensive display for a state that should never occur), sort selector (lib/sort)
- Common: Freshness, breadcrumbs, scope note, keep-data-on-error, unit display

## Invariants pinned by tests

1. The MRP identity (every month, breakdown included) and the definition of the 3 runout series
2. Undated POs never leak into a month + are reported separately as undated
3. MOR: safety stock affects only MOR's order requirements (coverage results are unchanged — a regression test)
4. MOR ex-mill = demand month − lead time (ceil), following lead-time resolution specificity order
5. MOR's customer filter respects BU boundaries (no other-BU inventory/POs enter; only that customer's own customer-owned inventory)
6. The Surplus identity OH = Allocated + Surplus + Obsolete (per product and overall)
7. The approximation that customer-owned inventory is consumed before company inventory shows up in the MRP breakdown
8. Excel export's column layout matches the screen (verified via header anchors)

## Out of scope
- Monetary valuation, notifications, scenarios (Stage 6), Executive (Stage 6)
