# OCTG Rebuild — Stage 7: Authentication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** Dev auth (provider structure, PBKDF2, HMAC 12h tokens), BU scoping, Login screen, per the stage-7 spec — including the faithful reproduction of the original's known gaps (register entries C-12R/C-13R/C-15R).

**Spec:** `docs/specs/2026-08-19-octg-stage7-auth.md` (binding; ruling AU-1).

## Global Constraints
Same as prior stages. All existing 281 backend tests must still pass — they use the TestClient without auth, so the conftest must gain an auth fixture (auto-login default ADMIN token applied to `client`) so existing tests stay valid unmodified; add a separate `anon_client` fixture for the 401 tests.

---

### Task 1: Users model + auth core + seed_users
**Files:** `app/models/user.py`, models `__init__`, migration, `app/auth/{passwords,tokens,provider}.py`, `seed/seed_users.py` (+call from seed_minimal), `tests/test_auth_core.py`
**Contracts:** spec §Data Model + §Auth Core. C-12R marker on the dev-secret fallback.
**Required tests:** invariants 1 (token TTL boundaries ±1s via injected now, tamper), 3, 4; PLANNER-requires-BU model validation; users survive seed wipe.

### Task 2: Login/me endpoints + router protection + conftest auth fixture
**Files:** `app/auth/deps.py`, `app/api/auth.py`, `app/main.py` (dependencies on include_router), `tests/conftest.py` (auth'd client + anon_client), `tests/test_auth_api.py`
**Contracts:** spec §Application. require_admin defined-not-applied (C-15R marker + explicit documenting test, invariant 7); enforce_customer_scope explicit-customer_id-only (C-13R marker); exemptions exact (login, openapi/docs, SPA catch-all).
**Required tests:** invariants 2, 5, 6, 7; login happy/wrong-password 401; me; all existing suites green with the auth'd client fixture.

### Task 3: Frontend auth + downloads
**Files:** `src/lib/auth.ts`, extend `src/lib/api.ts` (Bearer header + 401 redirect), `src/pages/Login.tsx`, route `/login` (outside Layout), user menu in `Layout.tsx`, convert xlsx download links (MrpSummary export, demand template, customer-owned template) to fetch→blob per AU-1
**Contracts:** spec §Frontend. Sign out clears token client-side only.
**Gates:** vitest (18 + new auth lib tests), typecheck, build.

### Task 4: Register updates + stage gate
**Files:** `docs/COMPROMISES.md` (C-01R resolved; add C-12R, C-13R, C-15R, no-rate-limit row, AU-1 divergence note); reseed dev.db with users
**Stage gate:** full suites; uvicorn smoke — unauthenticated /coverage → 401, login as planner → other-BU customer_id 403, own-BU 200, admin cross-BU 200, /admin reachable by planner (documented gap), SPA / serves shell without auth, xlsx export with Bearer works; commit.
