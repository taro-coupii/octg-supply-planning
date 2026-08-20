"""FastAPI dependencies for auth (spec §認証コア / §適用)."""

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.auth.tokens import verify
from app.db import get_db
from app.models import User, UserRole

_bearer = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    uid = verify(credentials.credentials)
    if uid is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user = db.query(User).filter_by(id=uid).first()
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user


# COMPROMISE[C-15R]: defined but not applied to any /admin route — faithfully
# reproduces the original implementation's known gap (see
# docs/superpowers/COMPROMISES.md). A PLANNER can currently reach /admin/*.
def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin only")
    return user


def enforce_customer_scope(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    """PLANNER business-unit scoping (spec §認証コア / invariant 2).

    COMPROMISE[C-13R]: only checks an *explicit* `customer_id` present in the
    request's query or path params. It does not resolve object-level
    identifiers (e.g. a bare well/demand-line UUID in the path) back to a
    customer/BU, and it does not auto-filter list endpoints that return
    multiple customers' rows — both are out of scope for this stage (see
    docs/superpowers/COMPROMISES.md).
    """
    if user.role == UserRole.ADMIN:
        return

    # PLANNER without a business unit can never proceed (invariant 2).
    if user.business_unit_id is None:
        raise HTTPException(status_code=403, detail="No business unit assigned")

    customer_id = request.query_params.get("customer_id") or request.path_params.get(
        "customer_id"
    )
    if customer_id is None:
        return

    from app.models import Customer  # local import to avoid a cycle at module load

    customer = db.query(Customer).filter_by(id=customer_id).first()

    if customer is not None and customer.business_unit_id != user.business_unit_id:
        raise HTTPException(status_code=403, detail="Customer outside your business unit")
