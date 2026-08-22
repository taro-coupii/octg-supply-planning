from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Product
from app.schemas import ProductOut

router = APIRouter(prefix="/products", tags=["products"])


@router.get("", response_model=list[ProductOut])
def list_products(
    db: Session = Depends(get_db),
    active_only: bool = Query(default=True),
    q: str | None = Query(default=None, description="Case-insensitive substring of the description"),
):
    """The product CATALOGUE. Carries no quantity -- see `ProductOut`.

    Exists so a product-centric screen can offer a picker. `GET /demand-lines`
    would be the obvious alternative source, but it only ever surfaces products
    something currently demands, and the catalogue deliberately contains products
    that nothing demands -- including the two that demonstrate the platform
    refusing to invent a number (a product whose lead time is not modelled, and
    one with no `InventoryOnHand` row in any Business Unit). A picker built from
    demand could not reach either, which is exactly backwards: those are the
    cases a planner most needs to be able to look up.

    No Business Unit scoping applies here, and that is not an omission: a Product
    row is a catalogue entity shared across every BU. The quantity standing on a
    shelf is a property of (BU, product) and lives in `InventoryOnHand`, resolved
    through `app.engines.inventory` -- which raises rather than defaulting when it
    has no row. Nothing here can leak a quantity across the boundary because
    nothing here carries one.
    """
    query = db.query(Product)
    if active_only:
        query = query.filter(Product.active.is_(True))
    if q:
        query = query.filter(Product.description.ilike(f"%{q}%"))
    return query.order_by(Product.description, Product.id).all()
