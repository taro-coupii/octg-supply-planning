import os

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.api import (
    admin_business_units,
    analysis,
    admin_lead_times,
    admin_safety_stocks,
    admin_settings,
    admin_substitutions,
    auth,
    business_units,
    company_inventory,
    coverage,
    customer_owned,
    customers,
    dashboard,
    demand,
    demand_imports,
    mrp,
    products,
    scenarios,
    substitution,
    substitution_approvals,
    wells,
)
from app.auth.deps import enforce_customer_scope, get_current_user

app = FastAPI(title="OCTG Supply Readiness Platform")

# Dev convenience: the Vite dev server runs cross-origin. Production serves the
# SPA from the same origin (VITE_API_BASE=""), so this list stays localhost-only.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# /auth/login is exempt from auth (it issues the token); /auth/me requires
# a token via its own get_current_user dependency, not router-level auth.
app.include_router(auth.router)

# Every business router is protected: 401 without a valid bearer token, and
# PLANNER requests are BU-scoped by explicit customer_id (spec §適用).
_protected = [Depends(get_current_user), Depends(enforce_customer_scope)]

app.include_router(analysis.router, dependencies=_protected)
app.include_router(business_units.router, dependencies=_protected)
app.include_router(dashboard.router, dependencies=_protected)
app.include_router(customers.router, dependencies=_protected)
app.include_router(products.router, dependencies=_protected)
app.include_router(admin_business_units.router, dependencies=_protected)
app.include_router(admin_lead_times.router, dependencies=_protected)
app.include_router(admin_settings.router, dependencies=_protected)
app.include_router(admin_substitutions.router, dependencies=_protected)
app.include_router(admin_safety_stocks.router, dependencies=_protected)
app.include_router(company_inventory.router, dependencies=_protected)
app.include_router(customer_owned.router, dependencies=_protected)
app.include_router(demand_imports.router, dependencies=_protected)
app.include_router(demand.router, dependencies=_protected)
app.include_router(mrp.router, dependencies=_protected)
app.include_router(scenarios.router, dependencies=_protected)
app.include_router(wells.router, dependencies=_protected)
app.include_router(coverage.router, dependencies=_protected)
app.include_router(substitution.router, dependencies=_protected)
app.include_router(substitution_approvals.router, dependencies=_protected)


def register_spa(application: FastAPI, dist_dir: str) -> None:
    """Serve the built frontend from the same origin (VITE_API_BASE="").

    Registered AFTER the API routers so API paths always win. index.html is
    served with no-store so a redeploy is picked up on the next navigation.
    """

    index_html = os.path.join(dist_dir, "index.html")
    real_dist_dir = os.path.realpath(dist_dir)

    @application.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> FileResponse:
        if full_path:
            candidate = os.path.realpath(os.path.join(dist_dir, full_path))
            # Starlette URL-decodes the path but does not resolve ".."
            # segments, so a request like /../etc/passwd or an encoded
            # equivalent (/%2e%2e/etc/passwd) can otherwise reach files
            # outside dist_dir. Only serve candidate if it actually stays
            # inside the resolved dist directory.
            if (
                os.path.commonpath([candidate, real_dist_dir]) == real_dist_dir
                and os.path.isfile(candidate)
            ):
                return FileResponse(candidate)
        return FileResponse(index_html, headers={"Cache-Control": "no-store"})


_spa_dist = os.environ.get("SPA_DIST")
if _spa_dist and os.path.isdir(_spa_dist):
    register_spa(app, _spa_dist)
