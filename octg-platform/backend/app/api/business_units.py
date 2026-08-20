from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import BusinessUnit
from app.schemas import BusinessUnitNode

router = APIRouter()


def _node(bu: BusinessUnit, visited: set) -> BusinessUnitNode:
    visited = visited | {bu.id}
    return BusinessUnitNode(
        id=bu.id,
        name=bu.name,
        children=[
            _node(c, visited)
            for c in sorted(bu.children, key=lambda c: c.name)
            if c.id not in visited
        ],
    )


@router.get("/business-units", response_model=list[BusinessUnitNode])
def list_business_units(db: Session = Depends(get_db)):
    roots = (
        db.query(BusinessUnit)
        .filter(BusinessUnit.parent_id.is_(None))
        .order_by(BusinessUnit.name)
        .all()
    )
    return [_node(r, set()) for r in roots]
