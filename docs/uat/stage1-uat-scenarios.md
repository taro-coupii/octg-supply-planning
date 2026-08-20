*Historical — superseded by full-uat-scenarios.md*

# OCTG Rebuild — Stage 1 UAT Scenarios

Target: `octg-rebuild-uat` (Render, branch `claude/using-superpowers-skill-0ix8nt`)
Scope: foundation only (read APIs for the 3 master-data types + SPA shell). No authentication (C-01R); business screens arrive in Stage 2 onward.

For each scenario: follow the steps and mark PASS if the result matches expectations. Record PASS/FAIL and any observations in the Result field.

## S1. Startup and shell display
1. Open the UAT URL in a browser (free-tier hosting, so the first access may take up to 1 minute to spin up)
2. Expected: a dark navy sidebar ("OCTG Readiness") with a light pastel content area. Tab title reads "OCTG Supply Readiness Platform"
Result:

## S2. Product count on Home (API connectivity)
1. Home should display "3 products in the catalog."
2. Expected: no error text (red) and no screen stuck on "Loading…"
Result:

## S3. Nonexistent page → 404
1. Append `/zonzai-page` to the URL and open it
2. Expected: a "Page not found" card with a "Back to Home" link. The link returns to Home
Result:

## S4. Reload resilience (SPA fallback)
1. Reload the browser on the S3 404 page
2. Expected: no blank screen or server error; the same 404 card is shown again
Result:

## S5. API contract — products
1. Open `<URL>/products` directly
2. Expected: a JSON array of 3 items. Each row has `unit_of_measure` ("Mtr"/"PC"/"MT"), and `weight_kg` for `Tubing 5-1/2"` is `null` (not masked as 0 — per the design principle of distinguishing "unknown" from "zero")
3. Open `<URL>/products/00000000-0000-0000-0000-000000000000` → `{"detail":"Product not found"}` (404)
Result:

## S6. API contract — BU hierarchy and customers
1. `<URL>/business-units` → JSON with `SCEU Norway` nested under `SC Global`
2. `<URL>/customers` → 2 entries, `AkerBP Norway` and `Equinor Norway` (in name order), each with a `business_unit_id`
Result:

## S7. Update propagation (no-store)
1. (After the dev side pushes a change) a normal browser reload alone should show the new screen — no hard/super reload required
2. Expected: stale HTML is not served from cache
Result:

## S8. Confirming intentional limitations (not bugs)
- No login screen (authentication is introduced at Stage 7 — C-01R)
- Sidebar menu shows only Home (business screens are added in Stages 2–6)
- Home's appearance is minimal (the full Home Dashboard arrives at Stage 8)
Confirm the above are "not to be reported as bugs."
Result:

---
Once filled in, please share this file as-is. If there are any FAILs, including reproduction steps and screenshots will speed up the fix.
