# OCTG Supply Readiness Platform — MVP (pass 1)

Thin vertical slice: schema → coverage engine → API → Home Dashboard + Well Workspace.
See `C:\Users\tsato\.claude\plans\giggly-doodling-sunset.md` for the full plan and scope
decisions (substitution, scenarios, MRP, Excel import, and auth are deferred to pass 2).

## Backend

```bash
cd backend
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
DATABASE_URL="sqlite:///./dev.db" .venv\Scripts\python -m seed.seed_from_workbook
DATABASE_URL="sqlite:///./dev.db" .venv\Scripts\python -m uvicorn app.main:app --reload
```

Runs on http://localhost:8000. `/health`, `/wells`, `/wells/{id}`, `/demand-lines/{id}/revisions`
(POST), `/dashboard/home`. Swap `DATABASE_URL` to the Postgres URL in `docker-compose.yml` for
a real run.

Tests: `.venv\Scripts\python -m pytest` (5 tests, sqlite in-memory, no external DB needed).

## Frontend

**Not yet run in this environment — Node.js/npm is not installed here.** Once Node is
available:

```bash
cd frontend
npm install
npm run dev
```

Runs on http://localhost:5173, talks to the backend at http://localhost:8000 (override with
`VITE_API_BASE`). Pages: Home Dashboard (`/`), Well Workspace (`/wells/:wellId`).

## Full stack via Docker

```bash
docker compose up
```

(Not verified in this environment — Docker is not installed here either.)
