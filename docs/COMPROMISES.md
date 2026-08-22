# Compromise Register

Serves the same purpose as `MVP_COMPROMISES.md` in the original implementation. Whenever a principle is bent, place a marker in the code and add a row here.
Search: `grep -rn "COMPROMISE\[" octg-platform/`

| ID | Description | Introduced at Stage | Resolution Plan |
|---|---|---|---|
| C-01R | All APIs exposed without authentication (introduced per provider structure at Stage 7) | 1 | **Resolved (Stage 7)** |
| C-06R | `Product.weight_kg` is nullable (MT conversion is not guaranteed for all products) | 1 | To be resolved alongside monetary valuation (out of scope) |
| C-03R | Manual maintenance of own inventory (`/company-inventory/on-hand` POST/PATCH/DELETE) — changes to rows with `source_system=="oracle"` are rejected with 409 | 2 | To be retired once the Oracle feed is introduced |
| C-12R | When `AUTH_SECRET` env var is not set, token signing falls back to a fixed dev value (`app/auth/tokens.py`) | 7 | Env var to be made mandatory at production rollout |
| C-13R | `enforce_customer_scope` only checks the explicit `customer_id` (query/path). Direct-object UUID access and automatic BU filtering on list endpoints are not implemented (`app/auth/deps.py`) | 7 | To be addressed when object-level authorization is introduced (out of scope) |
| C-15R | `require_admin` is defined but not applied to `/admin` routes — reachable by PLANNER (`app/auth/deps.py`, each `app/api/admin_*.py`) | 7 | To be addressed when the admin role gate is introduced |
| — | No rate limiting (all APIs, known deferred item) | 7 | Before production rollout (out of scope) |
| — | **[Ruling AU-1 · Difference from original implementation]** The `?access_token=` query-parameter approach for xlsx downloads (template/export), equivalent to the original C-14, is not implemented. Not needed, since the frontend downloads via fetch+Bearer→blob→objectURL — a safer deviation from the original implementation in that no token remains in the URL. `apiDownload` in `src/lib/api.ts` | 7 | Not applicable (intentional non-reproduction) |
| F-1 | **[Ruling F-1]** `frontend/public/manual.html` (user manual) contains no screenshots — since images could not be captured from a live UAT environment, it is replaced with per-screen text descriptions (purpose, key operations, notes) plus a screen-transition table. Recorded here as a known difference. | 8 | Not applicable (to be reconsidered once image capture from a live UAT environment becomes possible) |
| F-2 | The UI loads IBM Plex from Google Fonts, with a system fallback stack. Switch to self-hosted font files if an offline or strict-CSP requirement appears. `frontend/index.html`, `--sans`/`--cond`/`--mono` in `src/index.css` | 8 (UI overhaul, 2026-08-22) | Self-host the font files |
