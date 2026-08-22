# OCTG Supply Readiness Platform

Supply planning platform for OCTG (oil country tubular goods). For each well's demand it nets
company inventory, customer-owned material and open purchase orders, decides whether the well is
covered in steel, and — where it is not — names the remedy: a substitute, a mill order, or the
release of a hard assignment in Oracle.

- **Backend**: FastAPI + SQLAlchemy + Alembic (SQLite for dev, Postgres-capable)
- **Frontend**: React + TypeScript + Vite, plain hand-written CSS, no UI library
- **Deploy**: this repository deliberately carries **no Render Blueprint**. See *Deployment* below.

## Quick start

```bash
cd octg-platform/backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
DATABASE_URL="sqlite:///:memory:" .venv/bin/python -m pytest tests -q     # 666 tests
DATABASE_URL="sqlite:///./dev.db" .venv/bin/python -m uvicorn app.main:app --port 8000

cd ../frontend && npm install && npm run dev
```

Dev logins are seeded by `backend/seed/seed_users.py`: `admin@octg.dev` / `octg-dev`
(planners are `planner+<business-unit>@octg.dev`, same password).

> **On the history of this repository.** `main` previously held a separate,
> spec-driven rebuild of this same product. It was retired on 2026-08-22 after
> the four things it did better were adopted here; it is preserved whole on the
> branch `superseded/rebuild-2026-08-20`. See
> [`docs/VOID-rebuild-2026-08-20.md`](docs/VOID-rebuild-2026-08-20.md) for what
> was taken, what was deliberately left, and why.

## Deployment

**Nothing deploys from this repository, on purpose.** There is no `render.yaml`
here and one should not be added.

The running demo is built and deployed from the private working repository, and
its Blueprint declares a service named `octg-supply-readiness`. A second
Blueprint in this repository declaring the same service would fight the real one
for the name; a Blueprint declaring a different one would quietly stand up a
second, divergent instance of the same demo. Both have already happened once
here — see [`docs/VOID-rebuild-2026-08-20.md`](docs/VOID-rebuild-2026-08-20.md).

`octg-platform/Dockerfile.deploy` is kept, because it documents how the image is
actually built (Vite build, then FastAPI serving the result from one process).
Running it by hand is fine. Wiring it to a hosting provider from this repository
is not.

## Documentation

| Document | What it covers |
|---|---|
| [`octg-platform/REQUIREMENTS.md`](octg-platform/REQUIREMENTS.md) | Consolidated requirements: purpose, roles, the eight domain principles, features per screen, non-functional requirements, deferred scope |
| [`docs/VOID-rebuild-2026-08-20.md`](docs/VOID-rebuild-2026-08-20.md) | Why the earlier implementation on this repository was retired, and what was salvaged from it |
| [`docs/HANDOFF.md`](docs/HANDOFF.md) | Full handover: how to run it, directory layout, the design principles, the chronological work log, environment constraints |
| [`docs/MVP_COMPROMISES.md`](docs/MVP_COMPROMISES.md) | The register of principles deliberately bent for the MVP (C-01 … C-14), with what closes each one |
| [`docs/octg-demo-slideshow.html`](docs/octg-demo-slideshow.html) | A narrated nine-step demo walkthrough |
| `/manual.html` on the running app | The end-user manual, with screenshots. Bilingual (English / Japanese) via a toggle in the header |

**Read `docs/MVP_COMPROMISES.md` before the real implementation.** Every place in the code that
bends a principle carries a `MVP-COMPROMISE[C-xx]` marker, so one grep returns all of them:

```bash
grep -rn "MVP-COMPROMISE" --include=*.py --include=*.tsx octg-platform/
```

## Before a pilot goes public

Deliberately deferred, and all of it must be closed first: applying `require_admin` to the admin
routes, C-12 (the `AUTH_SECRET` dev fallback), C-13 (object-level authorization), C-14 (tokens in
download URLs), and login rate limiting. Monetary valuation and push notifications are deferred by
choice rather than by risk. See the register for detail.
