# OCTG Rebuild Stage 7 Spec — Authentication

Follows the parent spec §3 (authentication section) and REQUIREMENTS §2. **Full-fidelity policy: the original implementation's MVP compromises (equivalent to C-12/C-13/C-14, require_admin not enforced) are reproduced too, and logged in the register.** Proceeding under blanket approval.

## Data model (1 migration)

- `users`: (email unique, password_hash, role enum ADMIN/PLANNER, business_unit_id FK nullable). Invariant: PLANNER requires business_unit_id (validated at the model layer); ADMIN's null = all BUs

## Auth core (app/auth/)

- `passwords.py`: pbkdf2_sha256, 600,000 iterations, salted, verified with hmac.compare_digest
- `tokens.py`: HMAC-SHA256-signed payload {uid, exp}. TTL 12h. **AUTH_SECRET env, falling back to a fixed dev value when unset [C-12R: logged in the register, marked in code]**. No server-side session
- `provider.py`: an `AuthProvider` Protocol (authenticate(db, email, password) → User|None). `DevPasswordProvider` implementation. `get_provider()` selects via the env AUTH_PROVIDER (default "dev"); an unknown value raises RuntimeError. The Entra substitution point is marked with a comment + type (not implemented)
- `deps.py`: `get_current_user` (validates the Bearer token → User, 401 if invalid), `require_admin` (**defined but not applied to the /admin routes — faithfully reproduces the original's known hole [C-15R: logged in the register]**), `enforce_customer_scope` (checks the query/path customer_id against the PLANNER's BU, 403 for another BU. **Checks only an explicit customer_id — no direct-object UUID checks or automatic BU filtering of lists [C-13R]**)
- A PLANNER with no BU assigned gets 403 on every request

## Wiring

- `app/main.py`: attaches `dependencies=[Depends(get_current_user), Depends(enforce_customer_scope)]` via include_router to every business router. **Exceptions**: `/auth/login`, the `/openapi.json` family, the SPA catch-all, and static assets. C-01R updated in the register as now resolved
- `POST /auth/login` {email,password} → {token, user:{email,role,business_unit_id}}. `GET /auth/me`. **No logout endpoint is built** (tokens self-expire)
- Downloads (xlsx template/export) are protected via the Authorization header (**a `?access_token=` query param is not implemented — C-14 is "not reproduced": the frontend downloads via fetch+blob, so it's unnecessary. Logged in the register as a deliberate deviation**) [Ruling AU-1]
- No rate limiting (a known deferral, logged in the register)

## Seed

- `seed_users`: admin@octg.dev / octg-dev (ADMIN), planner@octg.dev / octg-dev (PLANNER, SCEU Norway). Called from seed_minimal (users are excluded from wipe — intentionally retained)

## Frontend

- `lib/auth.ts`: stores the token in localStorage; `apiGet/apiSend/apiUpload` automatically attach Authorization: Bearer; a 401 response redirects to login
- **Login screen** `/login` (a standalone card with no sidebar rail). On success, returns to the original page
- A user menu at the bottom of the sidebar (shows email/role; Sign out = discards the token and goes to login — client-side only)
- xlsx downloads switch to a fetch → blob → objectURL flow (needed to send the Authorization header) [Ruling AU-1]

## Invariants pinned by tests

1. Token: valid at 12h−1s / invalid at 12h+1s / tampering rejected / no Bearer → 401
2. PLANNER specifying a customer_id in another BU → 403, own BU → 200; ADMIN → 200 across BUs; a PLANNER with no BU → 403 on everything
3. PBKDF2 verification (correct/incorrect password)
4. Provider switching: AUTH_PROVIDER=dev works, an unknown value → RuntimeError
5. Every protected router returns 401 when unauthenticated, except /auth/login and the SPA catch-all
6. No logout route exists
7. The faithfully-reproduced hole is pinned: the /admin routes return 200 even for a PLANNER (an explicit test documents that require_admin is not enforced)

## Out of scope
- Entra ID implementation, rate limiting, object-level authorization, automatic BU filtering of lists (all logged in the register)
