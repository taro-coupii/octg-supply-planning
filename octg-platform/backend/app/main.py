import math

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.engines.inventory import InventoryRowMissing, InventoryScopeMissing
from app.auth.deps import get_current_user, require_admin_for_writes
from app.auth.scope import enforce_resource_scope
from app.api import (
    admin,
    auth,
    analysis,
    company_inventory,
    coverage,
    customer_owned_inventory,
    customers,
    dashboard,
    demand,
    demand_imports,
    mrp,
    products,
    scenarios,
    substitution,
    wells,
)

# NOTE: there is deliberately no `Base.metadata.create_all()` here any more.
#
# create_all creates MISSING TABLES but never ALTERS existing ones, so every
# column added to a model after a database was first created was silently absent
# from that database while startup still looked perfectly healthy. That produced
# three separate incidents (missing substitution tables, missing
# CoverageResult.fulfilled_by_product_id, missing Customer.business_unit_id --
# the last one only surfacing as a `no such column` crash inside the seed).
#
# Schema is now owned exclusively by Alembic. Run `alembic upgrade head` before
# starting the app; see README.md.
#
# The test suite calls Base.metadata.create_all itself against a throwaway
# in-memory sqlite DB, which is fine and intentionally untouched -- tests build
# their own schema and do not depend on Alembic.

app = FastAPI(title="OCTG Supply Readiness Platform")


def _json_safe(value):
    """A validation error must be reportable even when the offending input is not.

    Pydantic refuses `1e309` (inf) and `NaN` because app.quantities.Quantity says
    allow_inf_nan=False -- correct. But FastAPI's stock 422 handler then echoes
    the offending `input` back in the error body, json.dumps refuses to encode
    inf/NaN, and the client sees a 500 instead of the 422 it earned. The review
    that found this (2026-09-06, F03) had the value stored; the first fix turned
    "stored" into "crashed", which is not yet "refused with a reason".
    """
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return repr(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


@app.exception_handler(RequestValidationError)
async def _validation_error_as_422(_request, exc: RequestValidationError):
    errors = [
        {**{k: v for k, v in e.items() if k != "ctx"}, "input": _json_safe(e.get("input"))}
        for e in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": errors})

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Every business router is included through ONE helper so that authentication
# (get_current_user) and BU scoping (enforce_customer_scope -- see
# app.auth.deps) cannot be forgotten on a new router. The only router included
# bare is /auth itself: /auth/login must be reachable logged out.
# ---------------------------------------------------------------------------
AUTH_DEPS = [Depends(get_current_user), Depends(enforce_resource_scope)]


def _include_protected(router):
    app.include_router(router, dependencies=AUTH_DEPS)


app.include_router(auth.router)
_include_protected(wells.router)
_include_protected(demand.router)
_include_protected(dashboard.router)
_include_protected(substitution.router)
_include_protected(mrp.router)
_include_protected(customers.router)
_include_protected(products.router)
_include_protected(analysis.router)
_include_protected(coverage.router)
_include_protected(demand_imports.router)
_include_protected(scenarios.router)
# The platform's ONLY inventory WRITE endpoints, and the only ones there should ever
# be: customer-owned material is absent from Oracle, so this platform owns it. Every
# other inventory table here is a read-only Oracle projection -- see
# app.api.customer_owned_inventory.
_include_protected(customer_owned_inventory.router)
# MVP-COMPROMISE[C-03]: the platform's ONLY writes onto Oracle-owned inventory
# projections (InventoryOnHand, InventoryOnOrder, InventoryAssignment). There is
# no Oracle interface in the MVP, so seeded rows had no correction path. See
# app.engines.company_inventory's module docstring and MVP_COMPROMISES.md C-03.
_include_protected(company_inventory.router)
# The platform's own ADJUSTABLE assumptions: the attribute lead-time components and
# the platform-wide coverage scope default. Writeable for the same reason
# customer-owned inventory is -- this platform owns them, Oracle holds no such table,
# and they used to be editable only by a deploy. See app.api.admin.
# The admin router is the one router whose reads and writes have different
# audiences: every planner screen reads its master data, only an administrator
# may change it. See app.auth.deps.require_admin_for_writes for the ruling.
app.include_router(
    admin.router, dependencies=AUTH_DEPS + [Depends(require_admin_for_writes)]
)


# --------------------------------------------------------------------------
# Unresolvable inventory scope -> an explained 4xx, never an opaque traceback
#
# `app.engines.inventory` refuses to invent an on-hand quantity. Both refusals
# reach the API as exceptions, and each gets the status code that describes what
# is actually wrong -- they are NOT the same problem and do not deserve the same
# code:
#
#   InventoryScopeMissing -> 409 Conflict
#       The customer is not mapped to a Business Unit. The request was perfectly
#       well formed (nothing in it could have been different), and the resource
#       exists -- so neither 400 nor 404. What blocks the answer is the STATE of
#       the stored customer record, which is precisely what 409 means, and it is
#       fixable by a named action: map the customer to a BU. 422 was rejected
#       because it describes an unprocessable REQUEST ENTITY, and most of these
#       endpoints are GETs with no entity at all.
#
#   InventoryRowMissing -> 424 Failed Dependency
#       On-hand quantity is a read-only projection of Oracle-owned data
#       (app.models.inventory_on_hand.InventoryOnHand) and the row for this
#       (BU, product) has not arrived. The platform cannot answer because
#       something it DEPENDS ON is absent -- not because the caller erred (400),
#       not because the requested resource is missing (404: the well, product and
#       customer all exist), and not because this service is broken (5xx: nothing
#       here has malfunctioned). 424 says "the dependency failed", which is the
#       true statement. It is also distinguishable from the 409 by a client that
#       wants to route the two to different operators -- the 409 to whoever
#       administers customers, the 424 to whoever owns the inventory feed.
#
# Both responses carry the engine's own message, which names the product, the BU
# (or its absence) and the corrective action. `error` is a stable machine-readable
# discriminator so a client need not parse prose.
# --------------------------------------------------------------------------


@app.exception_handler(InventoryScopeMissing)
def _inventory_scope_missing(request: Request, exc: InventoryScopeMissing):
    return JSONResponse(
        status_code=409,
        content={
            "error": "inventory_scope_missing",
            "detail": str(exc),
            "business_unit_id": exc.business_unit_id,
            "product_id": exc.product_id,
        },
    )


@app.exception_handler(InventoryRowMissing)
def _inventory_row_missing(request: Request, exc: InventoryRowMissing):
    return JSONResponse(
        status_code=424,
        content={
            "error": "inventory_row_missing",
            "detail": str(exc),
            "business_unit_id": exc.business_unit_id,
            "product_id": exc.product_id,
        },
    )


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Single-container deployment: serve the built frontend from this process.
#
# In development the frontend is Vite on :5173 and this block is inert -- it
# activates only when a `static/` directory sits next to `app/` (the deploy
# image copies the Vite build there and builds it with VITE_API_BASE="" so the
# frontend calls its own origin; no CORS involved).
#
# Registered AFTER every router on purpose: FastAPI matches in registration
# order, so every /wells, /mrp, /dashboard... API route wins before the
# catch-all below ever sees a request. The catch-all returns index.html for
# anything else -- that is what makes BrowserRouter's deep links
# (/scenarios/<id>, /admin?tab=scope) survive a hard refresh on the deployed
# host instead of 404ing.
# ---------------------------------------------------------------------------
from pathlib import Path  # noqa: E402

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

if _STATIC_DIR.is_dir():
    import re  # noqa: E402

    from fastapi.responses import FileResponse  # noqa: E402
    from fastapi.staticfiles import StaticFiles  # noqa: E402

    app.mount(
        "/assets", StaticFiles(directory=_STATIC_DIR / "assets"), name="assets"
    )

    # The frontend's routes and the API's paths COLLIDE: /coverage, /wells/{id}
    # and /scenarios/{id} are all simultaneously a React Router route and a JSON
    # endpoint. A planner refreshing /scenarios/<id> must get the app back, not
    # the scenario's JSON -- but the SPA's own fetch() of the very same path
    # must still reach the API. The discriminator is the Accept header: browser
    # NAVIGATION asks for text/html first; the client's fetch() calls send
    # `*/*`.
    #
    # THE RULE IS AN EXEMPTION LIST, NOT A ROUTE LIST
    # -----------------------------------------------
    # This used to be a regex enumerating every client route, carrying a "KEEP
    # IN SYNC with the <Routes> table" warning. That sync is a maintenance
    # hazard that already cost us once: bare /wells was missing from it and a
    # planner trimming the URL landed on a raw JSON "Not authenticated" dead
    # end (QA 2026-08-14). A route list must be updated every time a screen is
    # added, and forgetting is silent.
    #
    # Inverted: a browser navigation gets the app shell UNLESS the path is one
    # of the few that must answer for itself. Adding a screen now needs no
    # change here at all. The exemptions are stable and few:
    #
    #   * the API docs and the health probe -- navigable on purpose
    #   * THE FILE DOWNLOADS. Every one of them is an <a href> navigation, so
    #     it arrives with an HTML-first Accept header and would otherwise be
    #     handed the shell instead of the spreadsheet. They all end in
    #     /template or /export, which is a far more durable pattern than a
    #     per-route list -- but if a download is ever added at some other
    #     shape, it belongs here. (This is the hazard in the sibling
    #     implementation's version of this middleware, which intercepts every
    #     HTML-accepting navigation and would swallow its own downloads.)
    #
    # A real file under the build (favicon, vite.svg) is left alone too, so a
    # direct link to an asset still serves the asset.
    _NAVIGATION_PASSTHROUGH = re.compile(
        r"^/(health|docs|redoc|openapi\.json|docs/oauth2-redirect)$"
        r"|/(template|export)$"
    )

    # index.html responses for API-colliding paths MUST NOT enter the browser's
    # HTTP cache: /coverage is served as HTML to a navigation and as JSON to
    # the SPA's fetch() of the very same URL, and without this the browser
    # satisfies the fetch from the cached NAVIGATION response -- the coverage
    # screen then dies parsing "<!doctype ..." as JSON. `Vary: Accept` states
    # the real contract; `no-store` closes the caches that ignore Vary.
    _SPA_INDEX_HEADERS = {"Cache-Control": "no-store", "Vary": "Accept"}

    def _is_build_file(path: str) -> bool:
        """True when `path` names a real file inside the build directory."""
        candidate = _STATIC_DIR / path.lstrip("/")
        try:
            resolved = candidate.resolve()
        except OSError:
            return False
        return candidate.is_file() and _STATIC_DIR in resolved.parents

    @app.middleware("http")
    async def _spa_navigation(request: Request, call_next):
        path = request.url.path
        if (
            request.method == "GET"
            and request.headers.get("accept", "").startswith("text/html")
            and not _NAVIGATION_PASSTHROUGH.search(path)
            and not _is_build_file(path)
        ):
            return FileResponse(
                _STATIC_DIR / "index.html", headers=_SPA_INDEX_HEADERS
            )
        return await call_next(request)

    @app.get("/{spa_path:path}", include_in_schema=False)
    def spa(spa_path: str):
        candidate = _STATIC_DIR / spa_path
        # A real file at the root of the build (favicon, vite.svg) is served
        # as itself; the path check keeps `..` traversal out.
        if (
            spa_path
            and candidate.is_file()
            and _STATIC_DIR in candidate.resolve().parents
        ):
            return FileResponse(candidate)
        return FileResponse(
            _STATIC_DIR / "index.html", headers=_SPA_INDEX_HEADERS
        )
