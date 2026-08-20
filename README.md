# OCTG Supply Readiness Platform

Supply planning platform for OCTG (Oil Country Tubular Goods): per-well demand coverage
judgment against company stock, customer-owned material, and open POs, with substitution
approvals, MRP/MOR supply planning, executive views, and what-if scenarios.

- Backend: FastAPI + SQLAlchemy 2.0 + Alembic (SQLite dev / Postgres-capable)
- Frontend: React + TypeScript + Vite, plain CSS
- Deploy: Render (Docker), see `render.yaml`

## Quick start

```bash
cd octg-platform/backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q          # test suite
.venv/bin/uvicorn app.main:app --port 8000
cd ../frontend && npm install && npm run dev
```

Docs: `docs/` (specs, implementation plans, compromise register, UAT scenarios).
User manual is served at `/manual.html` on the running app.
