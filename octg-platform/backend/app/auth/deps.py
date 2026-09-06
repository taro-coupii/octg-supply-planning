"""FastAPI dependencies: authentication and Business-Unit scoping.

Enforcement model
-----------------
1. `get_current_user` is attached to EVERY router at include time
   (app.main.AUTH_DEPS), so no endpoint is reachable without a valid bearer
   token. New routers must be included with the same dependency list --
   forgetting it means an unauthenticated endpoint, which is why main.py
   includes routers through one helper rather than fourteen bare calls.
2. `enforce_customer_scope` (also in AUTH_DEPS) closes the hole HANDOFF §6
   names: every endpoint that accepts a customer id -- as the `customer_id`
   query parameter or a `customer_id` path parameter, the two spellings the
   API actually uses -- refuses ids outside the caller's Business Unit with a
   403 that says whose scope was violated. Admins (business_unit_id NULL)
   pass unrestricted.

MVP-COMPROMISE[C-13]: scoping stops at explicit customer ids. Endpoints that
address deeper resources directly (/wells/{id}, /scenarios/{id},
/demand-lines/{id}/...) authenticate but do not yet walk the resource back up
to its customer/BU, so a planner who guesses a foreign UUID can read (and for
scenario endpoints, write) across the BU wall. See MVP_COMPROMISES.md C-13.
"""

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.auth.tokens import verify_token
from app.db import get_db
from app.models import Customer, User, UserRole


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    request: Request, db: Session = Depends(get_db)
) -> User:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        # MVP-COMPROMISE[C-14]: `?access_token=` exists solely for the four
        # <a href> download links (templates, MRP export), which are plain
        # browser navigations and cannot carry an Authorization header.
        # Tokens in URLs leak into server/proxy logs and browser history.
        # See MVP_COMPROMISES.md C-14.
        token = request.query_params.get("access_token", "")
    if not token:
        raise _unauthorized("Not authenticated")
    user_id = verify_token(token)
    if user_id is None:
        raise _unauthorized("Invalid or expired token")
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise _unauthorized("Invalid or expired token")
    if user.role == UserRole.PLANNER and user.business_unit_id is None:
        # The User docstring's invariant: an unscoped planner is a
        # misconfiguration, refused loudly rather than silently widened to
        # all BUs -- the same conservative failure as an unmapped customer.
        raise HTTPException(
            status_code=403,
            detail=(
                f"User {user.email} is a planner with no Business Unit "
                "assigned. Assign one in user administration."
            ),
        )
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=403, detail="Administrator role required"
        )
    return user


_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def require_admin_for_writes(
    request: Request, user: User = Depends(get_current_user)
) -> User:
    """Reads stay open to every authenticated user; writes need the admin role.

    Attached to the /admin router at include time (app.main). The router's GETs
    -- lead-time components, coverage-scope defaults, the substitution tables,
    safety stocks -- are master data every planner screen reads, so they are
    not gated. Its writes change every Business Unit's verdicts at once, which
    is not a planner's call.

    Product-owner ruling, 2026-09-06 (review F02): this gate covers the
    /admin/* writes and ONLY those. Business Unit creation/rename/delete, the
    customer BU remap, and the manual company-inventory edits (C-03) remain
    open to planners by the owner's explicit choice; see MVP_COMPROMISES.md.
    `require_admin` (above) exists for a route that must be admin-only in
    every method; this one exists so a whole router can split by verb.
    """
    if request.method in _WRITE_METHODS and user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=403,
            detail=(
                f"{request.method} {request.url.path} changes platform-wide "
                "master data and requires the administrator role. Your account "
                f"({user.email}) is a planner; ask an administrator to make "
                "this change."
            ),
        )
    return user


def enforce_customer_scope(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    """403 when an explicitly named customer lies outside the caller's BU.

    Unknown customer ids pass through untouched: the endpoint's own 404/409
    handling already speaks for missing rows, and pre-empting it here would
    turn one honest error into two competing ones.
    """
    if user.business_unit_id is None:
        return
    customer_id = request.query_params.get(
        "customer_id"
    ) or request.path_params.get("customer_id")
    if not customer_id:
        return
    customer = db.get(Customer, customer_id)
    if customer is None:
        return
    if customer.business_unit_id != user.business_unit_id:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Customer {customer.name} is outside your Business Unit "
                "scope."
            ),
        )
