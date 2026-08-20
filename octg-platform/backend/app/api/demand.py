import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Customer, DemandLine, DemandProfile, DemandStatus, Product, Well
from app.services import dates as dates_service

router = APIRouter(prefix="/demand")

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 25


class DemandLineOut(BaseModel):
    id: str
    well_id: str
    well_name: str
    customer_id: str
    customer_name: str
    product_id: str
    product_name: str
    quantity: float
    unit: str
    ros_date: datetime.date
    profile: str
    overdue: bool


class DemandLinesPage(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[DemandLineOut]


@router.get("/lines", response_model=DemandLinesPage)
def list_demand_lines(
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    customer_id: str | None = None,
    well_id: str | None = None,
    product_id: str | None = None,
    status: str | None = None,
    profile: str | None = None,
    ros_from: datetime.date | None = None,
    ros_to: datetime.date | None = None,
    db: Session = Depends(get_db),
):
    page_size = min(page_size, MAX_PAGE_SIZE)

    query = (
        db.query(
            DemandLine,
            Well.name.label("well_name"),
            Customer.id.label("customer_id"),
            Customer.name.label("customer_name"),
            Product.name.label("product_name"),
        )
        .join(Well, DemandLine.well_id == Well.id)
        .join(Customer, Well.customer_id == Customer.id)
        .join(Product, DemandLine.product_id == Product.id)
    )

    if customer_id:
        query = query.filter(Well.customer_id == customer_id)
    if well_id:
        query = query.filter(DemandLine.well_id == well_id)
    if product_id:
        query = query.filter(DemandLine.product_id == product_id)
    if status:
        try:
            status_enum = DemandStatus(status)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"invalid status: {status!r}")
        query = query.filter(Well.demand_status == status_enum)
    if profile:
        try:
            profile_enum = DemandProfile(profile)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"invalid profile: {profile!r}")
        query = query.filter(DemandLine.profile == profile_enum)
    if ros_from:
        query = query.filter(DemandLine.ros_date >= ros_from)
    if ros_to:
        query = query.filter(DemandLine.ros_date <= ros_to)

    total = query.with_entities(func.count(DemandLine.id)).scalar()

    rows = (
        query.order_by(DemandLine.ros_date, DemandLine.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    today = dates_service.today()

    items = []
    for line, well_name, cust_id, cust_name, product_name in rows:
        overdue = dates_service.is_overdue(line.ros_date, today)
        items.append(
            DemandLineOut(
                id=line.id,
                well_id=line.well_id,
                well_name=well_name,
                customer_id=cust_id,
                customer_name=cust_name,
                product_id=line.product_id,
                product_name=product_name,
                quantity=line.quantity,
                unit=line.unit.value,
                ros_date=line.ros_date,
                profile=line.profile.value,
                overdue=overdue,
            )
        )

    return DemandLinesPage(total=total, page=page, page_size=page_size, items=items)
