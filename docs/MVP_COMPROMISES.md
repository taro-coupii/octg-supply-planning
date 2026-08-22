# MVP Compromise Register

Last updated: 2026-08-11

This platform states eight design principles in `HANDOFF.md` §4.
**This file is the complete list of the places where those principles were bent
on purpose for the MVP.** When the real implementation begins (Oracle
integration, authentication, real data), working down this list leaves nothing
behind.

---

## How to use it — one grep returns everything

Every affected place in the code carries a marker in this exact shape:

```
MVP-COMPROMISE[C-07]: <one line saying what was bent>
    WHY:    why the MVP has no other option
    REMOVE: what the real implementation must do to delete it
```

List them all:

```bash
grep -rn "MVP-COMPROMISE" --include=*.py --include=*.tsx --include=*.ts .
```

Follow one of them:

```bash
grep -rn "MVP-COMPROMISE\[C-04\]" .
```

**When you bend something new, put a marker in the code AND add a row to this
table. It is finished only when both are done.** A marker alone is invisible in
review; a table row alone leaves the implementer unable to find the code.

### What the status symbols mean

| Symbol | Meaning |
|---|---|
| 🔴 | **Must** be closed in the real implementation. Leaving it produces wrong numbers or a safety problem |
| 🟡 | **Revisit** in the real implementation. Correct today, but breaks if its assumption changes |
| ⚪ | Deliberately out of scope. Whether to close it is a judgement call |

---

## Register

| ID | State | Principle bent | Detail |
|---|---|---|---|
| C-01 | ✅ | (pre-principle) | **Closed (2026-08-11).** Dev login plus Entra-swappable authentication implemented. The remaining holes were split out and inherited as C-12 / C-13 / C-14 |
| C-02 | 🔴 | ⑤ read-only projection | **The Oracle feed is not live.** The three inventory tables are a projection written by the seed; `oracle_integrated` is False everywhere and `source_system` defaults to `"synthetic"` |
| C-03 | 🔴 | ⑤ read-only projection | **Manual maintenance screen for company inventory.** The stand-in for C-02. With no Oracle interface, a door was opened for a human to edit directly |
| C-04 | 🔴 | ⑧ units are not converted | **The JT → length conversion is a placeholder.** `1 JT = 40 ft` is an assumed average joint length; the real figure moves with mill tolerance. Implementation: `JOINT_LENGTH_FEET` in `app/engines/units.py` |
| C-05 | 🟡 | ⑧ units are not converted | **The introduction of MT conversion at all.** Confined to the Dashboard display layer. If it leaks to other screens, rounding error could flip a coverage verdict. Implementation: `app/engines/units.py`, called only from `executive.py` (on 8/11 extended to `*_tonnes` headlines on every block; the boundary — never outside `app/engines/executive.py` — is unchanged) |
| C-06 | 🔴 | ① never fabricate numbers | **`Product.weight` is nullable.** MT conversion works for every row today only because all 10 of 10 products (the 8/11 SCEU Norway world) have a weight. Implementation: the NULL branch in `units.py` |
| C-07 | ✅ | (robustness) | **Closed (2026-08-12).** `coverage_view._recompute_all` isolates failures per customer. A customer with no BU is skipped and named in the response as `skipped_customers` (and in a banner on screen). Regression tests: `test_seed_honesty_paths` / `test_scope_filter_api` |
| C-08 | 🟡 | ⑥ coverage is written by the engine | **Coverage recomputation has no automatic trigger.** There is no background job, so a stored verdict stays stale until something happens to it. **8/12: `computed_at` is shown on the grid, so staleness is visible. 8/22: `POST /coverage/recompute` added (all customers or one, committed and isolated per customer) — staleness is now fixable** — what remains is the automatic job |
| C-09 | ✅ | ③ Customer is a default boundary | **Closed (2026-08-12).** Product-owner ruling: the own-product path deducts another customer's hard assignment exactly as the substitute path does. Investigation found the code already did so (`reserved_elsewhere` in `coverage.py`); only the register was out of date. Regression test: `test_allocation_policies.py::test_soft_is_constrained_by_another_customers_hard_assignment` and the rest of that set |
| C-10 | ⚪ | — | **Cross-customer sharing analysis is all-or-nothing.** It does not match production's partial-consumption method |
| C-11 | ✅ | (process) | **Closed (2026-08-10).** git initialised and pushed to GitHub: branch `octg-supply-planning-platform` of `taro-coupii/Claude-Private` |
| C-12 | 🔴 | (safety) | **`AUTH_SECRET` has a dev default.** Deploying without setting it makes tokens forgeable. Implementation: `app/auth/tokens.py` |
| C-13 | 🔴 | ③ the BU is an absolute boundary | **Object-level authorization stops at endpoints that name a customer_id.** Deeper resources such as `/wells/{id}` are only authenticated, so guessing another BU's UUID reads it. List endpoints are not auto-filtered by the planner's BU either. Implementation: `app/auth/deps.py` |
| C-14 | 🟡 | (safety) | **`?access_token=` on download links.** `<a href>` navigation cannot carry a header, and the workaround leaves the token in logs and history. Implementation: `app/auth/deps.py`, `withToken` in `frontend/src/auth.ts` |

---

## Detail per item

### C-01 ✅ No authentication → closed (2026-08-11)

- Implemented as agreed: a dev dummy login (`DevPasswordProvider` in `app/auth/provider.py`) plus a structure that can be swapped for Entra ID (the Protocol in the same file is the seam)
- Every router requires authentication via `AUTH_DEPS` in `app/main.py`. Endpoints that take a `customer_id` are BU-checked by `enforce_customer_scope` (a planner naming another BU's customer gets 403)
- Dev users come from `seed/seed_users.py` (admin@octg.dev / planner+<bu>@octg.dev, password `octg-dev`)
- **The remaining holes were split out and inherited as C-12 (dev secret default) / C-13 (authorization on deep resources) / C-14 (token in download URLs)**

### C-12 🔴 The `AUTH_SECRET` dev default

- **Where**: `app/auth/tokens.py`
- **In the real implementation**: make `AUTH_SECRET` mandatory at deploy time (refuse to start if unset), or inject it from a secret manager

### C-13 🔴 Object-level authorization not reached

- **Where**: `app/auth/deps.py`
- **Today**: authorization stops at an explicitly named customer_id, so (a) `/wells/{id}`, `/scenarios/{id}` and `/demand-lines/{id}/...` never walk from the resource back to a customer/BU, and (b) list endpoints without a customer_id are not auto-filtered by the planner's BU
- **In the real implementation**: resolve resource → customer → BU in each endpoint (or in the engine layer), and make the authenticated user's BU the default filter on list endpoints

### C-14 🟡 `?access_token=` in download URLs

- **Where**: `app/auth/deps.py` (receiving side), `withToken` in `frontend/src/auth.ts` (sending side — three templates plus the MRP export, four links)
- **In the real implementation**: replace it with a short-lived (tens of seconds) single-use download token, or move to a fetch→Blob approach

### C-02 🔴 The Oracle feed is not live

- **Where**: `app/models/inventory_on_hand.py`, `inventory_on_order.py`, `inventory_assignment.py`, `app/engines/executive.py`, `app/engines/mrp_export.py`
- **Important**: this fact is already **handled correctly**. `oracle_integrated` and `source_system` are held independently, and the "a missing row means unknown, not zero" distinction is implemented
- **In the real implementation**: connect the feed. Once connected, `oracle_integrated` becomes True and the C-03 gate starts working by itself

### C-03 🔴 Manual maintenance screen for company inventory

- **Where (implemented 2026-08-10)**:
  - `app/engines/company_inventory.py` (1,126 lines) — read, gate, write, coverage recomputation
  - `app/api/company_inventory.py` (393 lines) — router
  - `app/models/company_inventory_upload.py` — upload audit table
  - migration `d4a7c92e6f18`
  - 22 markers (`app/main.py`, the two files above, `app/schemas/__init__.py`)
- **Design**: a row whose `source_system` falls outside
  `PLATFORM_MAINTAINABLE_SOURCES = frozenset({"synthetic", "manual"})` is refused with 403.
  Writes stamp `source_system="manual"` plus `synced_at`
- **It retreats automatically by design**: once the Oracle feed stamps its own `source_system`,
  this gate makes every row read-only **with no code change**
- **Coverage recomputation**: reuses the existing `app.engines.coverage.recompute_customer`
  (eight other engines call the same function). No second implementation was built.
  On-hand and uploads recompute every customer in the BU inside a SAVEPOINT;
  assignments recompute only that customer; on-order is not a coverage input, so it does not recompute
- **An audit log already exists**: the `company_inventory_uploads` table. **But only for bulk uploads.**
  Inline edits (`PATCH /company-inventory/on-hand/{id}` and friends) stamp `source_system="manual"` and
  `synced_at` only, so **there is no record of who changed what, from what, and when**
- **In the real implementation**:
  1. Decide whether the screen goes away entirely or stays as an emergency manual correction path
  2. **If it stays, add an audit log to inline edits too** (today only bulk upload has one)
  3. Until authentication (C-01) exists, this screen lets **anyone rewrite any BU's inventory**. Look at it together with C-01

### C-04 🔴 The JT → length conversion is a placeholder

- **Where**: the unit conversion layer (the `JOINT_LENGTH_FEET` constant)
- **Placeholder value**: `1 JT = 40 ft`
- **Why 40 ft**: the user supplied both `1 JT = 40 feet` and `1 JT = 12 mtr`, and **those two disagree by 1.6%** (40 ft = 12.192 m). `Product.weight` is stated in lb per **ft**, so going through feet is one conversion shorter and carries one error source fewer
- **In the real implementation**: average joint length follows from the Range (R1/R2/R3) and the actual mill tolerance. It should be held per product, or per received lot. **It is closed inside a single constant, so the replacement is a one-line change**

### C-05 🟡 The introduction of MT conversion at all

- **Where**: the unit conversion layer, the Executive Dashboard
- **Principle bent**: the `UnitOfMeasure` docstring stated *"NOT a conversion table. There is deliberately no metres-per-tonne factor anywhere in this platform"*
- **Why it is acceptable**: the ban existed because "without a per-product weight, any factor would be fabrication". `Product.weight` (lb/ft, the API nominal weight) genuinely exists per product, so a length↔mass conversion is not fabrication
- **What stays banned**: PC → MT, for which **no conversion exists at all** (a piece has neither a length nor a weight). JT → length is C-04
- **The boundary to hold**: **conversion belongs to the Dashboard display layer only.** MRP, coverage and demand lines stay in the native unit. Coverage compares on-hand against needed directly, so introducing a conversion could let rounding error flip a verdict
- **8/11 extension**: on the instruction "the Executive Dashboard should be unified in MT", the MT headline — until then only on the utilisation block — was extended to `*_tonnes: Measure` on every block (demand trend / coverage / supply risk / soft allocation / first runout / incoming supply). The generic conversion is `pairs_to_tonnes` in `executive.py` (floor convention). **The native `quantities_by_unit` is kept everywhere as ground truth**, and nothing was carried into the verdict logic. Tests: `tests/test_executive_mt.py` (hand-calculated against independent constants, plus a partition invariant)
- **In the real implementation**: check that this boundary has not been broken. Counting the callers of the conversion functions with `grep` is the fastest way

### C-06 🔴 `Product.weight` is nullable

- **Where**: `weight = Column(Float, nullable=True)` in `app/models/product.py`
- **Today**: all 10 rows in `dev.db` are filled (zero NULLs after the 8/11 reseed), which is the only reason MT conversion succeeds for every row
- **The danger**: the moment real data contains a NULL, that product cannot be converted to MT. **Never fold it to zero** — report it explicitly and separately as "not convertible to MT"
- **In the real implementation**: if MT conversion becomes a hard requirement of the product master, make it `nullable=False`. If not, check that non-convertible rows are handled consistently on every screen

### C-07 ✅ The 409 whole-grid failure of `GET /coverage` — closed (2026-08-12)

- **Fix**: `_recompute_all` in `app/engines/coverage_view.py` catches `InventoryScopeMissing` per customer. Skipped customers propagate by name through `CoverageProjection.skipped_customers` → API → an on-screen banner (they never disappear silently)
- **Regression tests**: `tests/test_seed_honesty_paths.py` (the old 409 pin updated to 200 + skipped), `tests/test_scope_filter_api.py::test_unmapped_customer_no_longer_breaks_the_grid`

### C-08 🟡 Coverage recomputation has no automatic trigger

- **What it is**: stored verdicts such as `CoverageResult` stay stale until a recompute trigger fires (revision applied, approval, well status change, inventory update)
- **Recorded harm**: "the engine code is fixed but `dev.db` is stale" happened repeatedly (`HANDOFF.md` §7)
- **8/22 progress**: `POST /coverage/recompute` (`app/api/coverage.py`). Every customer, or one named by `customer_id`. **Each customer is recomputed and committed on its own, and a customer that cannot be evaluated is rolled back alone and returned by name with its reason** — the read path's `coverage_view._recompute_all` cannot be reused, because it tolerates partial in-transaction writes on the understanding that its callers always roll the whole transaction back (safe there, unacceptable on a path that commits). It takes no scope arguments: the official verdict is the default-scope verdict, and asking about another scope is the read-only projection's job
- **What remains for the real implementation**: recompute automatically on feed update. **When a verdict was computed is now visible through `computed_at`, and there is now a way to fix it. What is left is for it to fix itself without a human pressing anything**

### C-09 ✅ Own-product allocation for SOFT customers — closed (2026-08-12)

- **Ruling**: the own-product path also deducts foreign hard assignments (the same rule as the substitute path) — product-owner decision
- **Investigation**: the code already deducted them (`_assignment_context` in `coverage.py` carries only foreign assignments under SOFT, and `compute_customer_coverage` deducts the full amount as `reserved_elsewhere`). This register entry was simply older than the implementation
- **Regression tests**: `test_soft_is_constrained_by_another_customers_hard_assignment` / `test_soft_partial_carve_out_leaves_the_unassigned_remainder_usable` / `test_soft_is_not_constrained_by_a_foreign_assignment_in_another_bu` in `test_allocation_policies.py`

### C-10 ⚪ Cross-customer sharing analysis is all-or-nothing

- **What it is**: production coverage uses partial consumption (drawing down in ROS order), but the sharing what-if is still all-or-nothing
- **Deliberately out of scope**. Whether to align it is a judgement call

### C-11 ✅ git not initialised → closed (2026-08-10)

- **Original entry**: nothing under `octg-platform` was under `git init`
- **Recorded harm**: when two agents were force-stopped on 8/7, there was no diff to judge whether their output survived. It had in fact completed, but confirming it cost a full test run and a read of every file (`HANDOFF.md` §0A)
- **How it was closed**:
  - `git init` at the project root, initial commit `2f57206` plus cleanup commit `60ab9eb` (169 files)
  - GitHub: pushed to the **`octg-supply-planning-platform`** branch of `github.com/taro-coupii/Claude-Private`
  - Authentication is a repo-scoped **SSH deploy key** (`~/.ssh/octg_deploy`, with write access).
    It is set in the repo's `core.sshCommand`, so a plain `git push` works from then on.
    To revoke: GitHub → Settings → Deploy keys → delete `octg-deploy-key (tsato dev machine)`
  - `dev.db` (0.6 MB) is **committed on purpose**: it holds the manually staged state of the demo
    walkthrough, which `seed --reset` cannot reproduce (the same note is in `.gitignore`)
  - `.venv` / `node_modules` / `dist` / logs / build output are excluded

---

## Recommended order for closing them

1. ~~**C-11** (git init)~~ ✅ done (2026-08-10)
2. **C-01 + C-07** — do the coverage isolation at the same time as authentication. Doing one alone means doing the work twice
3. **C-06** — decide how weight is handled before real data arrives. Afterwards you find out only once non-convertible rows appear in bulk
4. **C-02 → C-03 → C-04** — the single flow of connecting Oracle. The C-03 gate retreats by itself, so after connecting it is only a verification step
5. **C-05** — verification work: has the boundary held? It can wait until the rest is done
6. **C-08 / C-10** — judgement calls. Discuss with the product owner (C-09 was ruled on and closed on 8/12)
