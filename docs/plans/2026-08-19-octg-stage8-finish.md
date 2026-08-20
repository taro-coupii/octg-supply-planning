# OCTG Rebuild — Stage 8: Finish & Deploy Implementation Plan (final)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Spec:** `docs/specs/2026-08-19-octg-stage8-finish.md` (binding; ruling F-1).

## Global Constraints
Same as prior stages. This is the LAST stage — after it the whole-branch final review covers stage 8 plus a project-level completeness pass.

---

### Task 1: Home dashboard API + screen
**Files:** `app/api/dashboard.py` (add /dashboard/home with response_model), `src/pages/Home.tsx` (replace placeholder), `tests/test_home_api.py`
**Contracts:** spec §Home. Union card labels sources; KPI coverage rate excludes NotEvaluated from denominator.
**Required tests:** invariants 1, 2, 4.

### Task 2: User manual + cross-cutting audit + mobile CSS
**Files:** `frontend/public/manual.html`; audit fixes across pages (Freshness/breadcrumbs/URL/keep-data/units/no-UUID gaps found by grep+read); mobile CSS in index.css (rail collapse ≤640px with hamburger toggle in Layout, card-ization for list screens, explicit horizontal-scroll wrappers for Coverage/MOR/MRP tables); `tests/test_spa.py` addition for anonymous /manual.html (invariant 3 — note: test serves from a tmp dist containing manual.html).
**Gates:** backend suite, vitest, typecheck, build.

### Task 3: Deploy + final seed + stage gate
**Files:** `render.yaml` (AUTH_SECRET generateValue), final reseed dev.db, COMPROMISES.md final pass (F-1 note; verify all rows current)
**Stage gate:** full suites; local deploy simulation (build frontend with VITE_API_BASE="", uvicorn with SPA_DIST, verify login flow + manual.html anonymous + one authenticated API roundtrip via curl); commit. The controller pushes and verifies the live Render deploy afterward.
