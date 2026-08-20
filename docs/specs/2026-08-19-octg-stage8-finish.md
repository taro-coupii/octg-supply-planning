# OCTG Rebuild Stage 8 Spec — Finishing / Deployment (final)

Per REQUIREMENTS §4.7/§5. Proceeding under blanket approval.

## Home Dashboard (replaces `/`)

- KPI strip: customer count / well count (status breakdown) / coverage rate (Covered+CoveredViaSubstitute ÷ evaluated lines, unevaluated excluded from the denominator, with the count shown alongside) / pending approvals count
- **Demand changes**: the most recent 15 DemandRevision entries (timestamp, well-name link, source, summary) + "See all → /demand"
- **Pending approvals card = a union**: all undecided SubstitutionApproval rows + PendingApproval-verdict lines that have no request yet (derived from the saved verdict). Each labeled with its origin, row → /approvals or /substitution
- **Coverage attention**: the top 5 Uncovered/Unrecoverable wells → /coverage
- API: `GET /dashboard/home` (auth required, explicit response_model)
- Freshness, honest empty states

## User manual

- `frontend/public/manual.html` (goes into dist at build time, published **without login** via the SPA catch-all — the structure confirmed in the Stage 7 review)
- Self-contained HTML (plain CSS, no images needed [Ruling F-1: since screenshots can't be captured in the real UAT environment, substitute text plus a screen-flow table instead of figures; logged in the register as a deviation])
- Structure: login → all 19 screens in workflow order, covering purpose, key actions, and caveats (the overdue definition, the meaning of the 5 verdict colors, how to read unknown≠0, known limitations of C-03R/C-15R)

## Cross-cutting checks (audited and fixed together in this stage)

- Every screen: Freshness / breadcrumbs / URL state / keep-data-on-error / unit display / no UUIDs shown — audit and fix any gaps
- Mobile (§5, approach A): collapse `.rail` at ≤640px (hamburger), CSS for card-based screens (Home / list-type screens) vs. **screens that keep horizontal scroll (Coverage/MOR/MRP)**
- Confirm reduced-motion

## Deployment

- `render.yaml`: add `envVars: [{key: AUTH_SECRET, generateValue: true}]` (an item carried over as Important from the Stage 7 review)
- Final reseed (demo data from every stage + users), committing dev.db
- Push → Render auto-redeploy → production URL smoke test (/ → login → representative screens → anonymous access to /manual.html)

## Invariants pinned by tests

1. /dashboard/home has a response_model, and the pending union includes both sources (verified with a case where only one source has data)
2. Demand changes returns the newest 15, in order
3. manual.html returns 200 anonymously (test: anon_client)
4. Unevaluated lines are excluded from the coverage-rate denominator

## Out of scope (final register confirmation)
- Monetary valuation, notifications, Entra, rate limiting, object-level authorization (deferred to the real implementation phase)
