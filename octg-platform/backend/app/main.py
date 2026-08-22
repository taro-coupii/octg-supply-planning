import os

from fastapi import Depends, FastAPI, Request
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


# Paths that must keep answering a browser navigation with their own
# response rather than the SPA shell.
_API_DOC_PATHS = frozenset({"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"})


def register_spa(application: FastAPI, dist_dir: str) -> None:
    """Serve the built frontend from the same origin (VITE_API_BASE="").

    Registered AFTER the API routers so API paths always win. index.html is
    served with no-store so a redeploy is picked up on the next navigation.
    """

    index_html = os.path.join(dist_dir, "index.html")
    real_dist_dir = os.path.realpath(dist_dir)

    def static_file(full_path: str) -> str | None:
        """Resolve full_path to a real file inside dist_dir, or None.

        Starlette URL-decodes the path but does not resolve ".." segments, so a
        request like /../etc/passwd or an encoded equivalent (/%2e%2e/etc/passwd)
        can otherwise reach files outside dist_dir. Only a candidate that stays
        inside the resolved dist directory is served.
        """
        if not full_path:
            return None
        candidate = os.path.realpath(os.path.join(dist_dir, full_path))
        if (
            os.path.commonpath([candidate, real_dist_dir]) == real_dist_dir
            and os.path.isfile(candidate)
        ):
            return candidate
        return None

    @application.middleware("http")
    async def spa_navigation(request: Request, call_next):
        """Hand browser navigations the app shell, even on API-shaped paths.

        Several client routes share a path with an API route (/coverage,
        /scenarios, /analysis/sharing, /mrp/order-requirements, /wells/{id} …).
        FastAPI matches the API route first, so opening or reloading one of
        those screens in a browser returned raw JSON instead of the app. A
        browser navigation is distinguishable from the SPA's own fetch() calls
        by its Accept header, so those are intercepted here, before routing.
        Everything else — fetch (Accept: application/json or */*), the API docs,
        and real files under dist — falls through untouched.
        """
        if request.method in ("GET", "HEAD"):
            accept = request.headers.get("accept", "")
            path = request.url.path
            if (
                "text/html" in accept
                and path not in _API_DOC_PATHS
                and static_file(path.lstrip("/")) is None
            ):
                return FileResponse(index_html, headers={"Cache-Control": "no-store"})
        return await call_next(request)

    @application.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> FileResponse:
        candidate = static_file(full_path)
        if candidate is not None:
            return FileResponse(candidate)
        return FileResponse(index_html, headers={"Cache-Control": "no-store"})


_spa_dist = os.environ.get("SPA_DIST")
if _spa_dist and os.path.isdir(_spa_dist):
    register_spa(app, _spa_dist)
