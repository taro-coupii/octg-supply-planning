from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.engines.lead_time import resolve_lead_time
from app.engines.mor import DEFAULT_HORIZON_MONTHS, order_requirements
from app.engines.mrp import by_item, mrp_summary
from app.engines.mrp_export import build_mrp_export
from app.auth.scope import planner_bu
from app.models import Customer, Product
from app.schemas import (
    ByItemAnalysisOut,
    LeadTimeBreakdownOut,
    MorGridOut,
    MrpRecommendationOut,
)

router = APIRouter(prefix="/mrp", tags=["mrp"])

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# Declared BEFORE nothing in particular -- `/export` is a literal segment and the
# other MRP routes are `/summary`, `/by-item/{id}` and `/lead-time/{id}`, so
# there is no `/{something}` catch-all here for it to be shadowed by. Noted
# because `app/api/demand_imports.py` DOES have that hazard and orders around it.
@router.get("/export")
def export_mrp(
    customer_id: str | None = Query(
        None,
        description=(
            "Optional. Omit for the system-wide export, exactly as "
            "/mrp/summary's own customer_id is optional -- MRP is a system-wide "
            "procurement view by default and this file must not be narrower "
            "than the screen it exports."
        ),
    ),
    db: Session = Depends(get_db),
    bu_scope: str | None = Depends(planner_bu),
):
    """Download the MRP as an .xlsx: summary on tab 1, justification on tabs 2+.

    A GET, and it writes nothing: generating a report is a read of the plan.

    Every number comes from `mrp_summary` and `by_item` -- the same calls
    `/mrp/summary` and `/mrp/by-item` serve the screens from. See
    `app.engines.mrp_export` for the sheet layout and for why the supporting
    detail carries a Product column instead of one tab per product.

    A scope with no recommendations yields a workbook whose summary sheet says
    so in prose, not a 404 and not a header-only sheet. "Nothing to order" is a
    real and welcome MRP answer; a 404 would misreport it as "no such thing",
    and an empty sheet would be indistinguishable from a broken export.
    """
    customer = None
    if customer_id is not None:
        customer = db.get(Customer, customer_id)
        if customer is None:
            raise HTTPException(
                status_code=404, detail=f"Customer {customer_id!r} not found"
            )

    export = build_mrp_export(db, customer=customer, business_unit_id=bu_scope)
    return Response(
        content=export.content,
        media_type=XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{export.filename}"',
            # So a client can report "3 to order, 1 to escalate" without opening
            # the workbook. Exposed through CORS explicitly -- a browser cannot
            # read a response header that is not listed there, and the frontend
            # runs on another origin.
            "X-Mrp-Recommendation-Count": str(export.recommendation_count),
            "X-Mrp-Orderable-Count": str(export.orderable_count),
            "X-Mrp-Unrecoverable-Count": str(export.unrecoverable_count),
            "Access-Control-Expose-Headers": (
                "Content-Disposition, X-Mrp-Recommendation-Count, "
                "X-Mrp-Orderable-Count, X-Mrp-Unrecoverable-Count"
            ),
        },
    )


@router.get("/summary", response_model=list[MrpRecommendationOut])
def get_mrp_summary(
    customer_id: str | None = None,
    db: Session = Depends(get_db),
    bu_scope: str | None = Depends(planner_bu),
):
    """MRP Layer 1: products needing procurement, most urgent order date first.

    System-wide by default; for a planner, "the system" is their Business Unit."""
    return [
        MrpRecommendationOut.model_validate(r, from_attributes=True)
        for r in mrp_summary(db, customer_id=customer_id, business_unit_id=bu_scope)
    ]


@router.get("/by-item/{product_id}", response_model=ByItemAnalysisOut)
def get_by_item(
    product_id: str,
    db: Session = Depends(get_db),
    bu_scope: str | None = Depends(planner_bu),
):
    """MRP Layer 2: consuming wells, inventory position, runout curve and the
    recommendation overlaid on that timeline.

    A planner gets their Business Unit's position and demand (C-15); an
    administrator the system-wide view. `/mrp/lead-time/{id}` needs no scoping:
    lead time is master data, not stock."""
    try:
        analysis = by_item(db, product_id, business_unit_id=bu_scope)
    except ValueError:
        raise HTTPException(status_code=404, detail="Product not found")
    return ByItemAnalysisOut.model_validate(analysis, from_attributes=True)


@router.get("/lead-time/{product_id}", response_model=LeadTimeBreakdownOut)
def get_lead_time_breakdown(product_id: str, db: Session = Depends(get_db)):
    """The attribute lead-time breakdown for one product: OD/WT + Grade +
    Connection + Logistics, each with the attribute value it matched.

    Both `/mrp/summary` rows and `/mrp/by-item` already embed this, so this route
    exists for the two cases they cannot serve. First, `by-item` deliberately
    RAISES when a product has no inventory row in any Business Unit (unknown is
    not zero -- see app.engines.mrp), and the lead-time question is answerable
    with no inventory data at all; refusing it along with the runout curve would
    hide a fact that is available. Second, any screen showing a product without
    building a whole By Item report -- the product picker, a demand line detail --
    can explain a date from one cheap call.

    Same `resolve_lead_time` as every other consumer, so this endpoint can never
    disagree with the order date printed next to it.
    """
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return LeadTimeBreakdownOut.model_validate(
        resolve_lead_time(db, product), from_attributes=True
    )

@router.get("/order-requirements", response_model=MorGridOut)
def get_order_requirements(
    customer_id: str | None = None,
    horizon_months: int = Query(default=DEFAULT_HORIZON_MONTHS, ge=3, le=36),
    db: Session = Depends(get_db),
    bu_scope: str | None = Depends(planner_bu),
):
    """The monthly Material Order Requirements grid -- see app.engines.mor.

    Same demand scope as /mrp/summary (the lines coverage evaluates), so the
    two screens can never disagree about what counts as demand.
    """
    if customer_id is not None and db.get(Customer, customer_id) is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    return order_requirements(
        db,
        customer_id=customer_id,
        business_unit_id=bu_scope,
        horizon_months=horizon_months
    )

