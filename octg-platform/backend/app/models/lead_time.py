"""Attribute-based lead-time components.

The spec builds a lead time out of ATTRIBUTE components, never SKU by SKU::

    OD/WT        4 months
    Grade       +2 months
    Connection  +0 months
    Logistics   +2 months
    ------------------------
    Total        8 months

This table stores exactly that: one row per (dimension, attribute value) pair,
plus the months that dimension contributes. A product's total is resolved by
`app.engines.lead_time.resolve_lead_time`, which is the ONLY matcher in the
codebase.

Why this shape and not `grade_type + component`
-----------------------------------------------
The previous shape keyed rows by `grade_type` + a free-text `component` name and
summed every row matching the product's `grade_type`. Two products identical in
grade_type but different in size, weight or connection were therefore GUARANTEED
identical lead times: the size and connection dimensions were unrepresentable,
not merely unpopulated, so `order_feasibility` returned the same order date for a
4-1/2 TBG and a 13-3/8 CSG of the same grade family. That total feeds
`app.engines.order_dates.is_recoverable`, which decides the terminal
`CoverageStatus.UNRECOVERABLE` claim, so the missing dimensions could produce a
wrong order date and wrongly declare demand impossible.

`dimension` is a closed enum, not free text. The set of dimensions is a
modelling decision (the spec names four), and a typo'd `component` string used to
silently create a new additive term nobody could find.

How each dimension is keyed off Product
---------------------------------------
`attribute_value` is a STRING in every case, so one column serves all four
dimensions. What goes in it per dimension:

  OD_WT        The canonical OD/WT key. "OD/WT" spans two Product columns
               (`size` and `weight`), and there is no third column to hold the
               pair, so it is keyed by the canonical rendering of the pair --
               ``f"{size} {weight:g}"``, e.g. "4-1/2 12.6". Products with no
               weight recorded key on the size alone ("4-1/2"). The alternative,
               two columns (`attribute_value` + `attribute_value_2`), would be
               dead and NULL for the other three dimensions and would need its
               own matching branch; the alternative of keying on size only would
               make wall thickness -- half of what the spec names -- silently
               irrelevant. `app.engines.lead_time.od_wt_key` is the single
               canonical formatter, used by the resolver and by any seeding code.
  GRADE        `Product.grade_type`, the existing grade FAMILY column. Not
               `grade`: grade_type exists precisely to group grades for
               lead-time lookup, and it is what the old rows already held, so
               historical Grade rows convert without invention.
  CONNECTION   `Product.connection`, e.g. "VAM TOP".
  LOGISTICS    `Product.grade_type`, because sourcing region (and therefore the
               sailing leg) correlates with the grade family in the source
               workbook -- AND, far more usually, `ANY_ATTRIBUTE_VALUE` (see
               below), because one sailing allowance normally applies to
               everything.

The wildcard, and why the shared component needs one
----------------------------------------------------
`attribute_value == ANY_ATTRIBUTE_VALUE` ("*") means "this row applies to every
product on this dimension". It is what lets a Logistics/Shipping allowance that
genuinely applies to everything be stored ONCE rather than duplicated per grade.
The old code had no such concept: it filtered on `grade_type ==
product.grade_type`, so a single shared Sailing row seeded with a sentinel
grade_type was found by NOTHING -- both `total_lead_time_months` and
`order_dates.transit_months` would have returned 0 for it and
`required_ship_date` would have silently collapsed to `== ROS`.

A specific row beats a wildcard row on the same dimension (most-specific-wins),
so a global default plus a handful of exceptions is expressible. Two rows with
the SAME (dimension, attribute_value) are a data error and make the resolver
raise -- see `AmbiguousLeadTimeComponent`. A unique constraint enforces it in the
database too.

`label` is presentation only
----------------------------
The optional `label` carries the planner's own name for the term ("Ex-mill",
"Sailing", "Threading") for display in the breakdown. It is never matched on and
never affects arithmetic; the dimension is what the engine reads.
"""

import enum

from sqlalchemy import Column, Enum as SAEnum, Float, String, UniqueConstraint

from app.db import Base
from app.models.customer import _uuid


class LeadTimeDimension(str, enum.Enum):
    """The four attribute dimensions the spec builds a lead time from."""

    OD_WT = "OD/WT"
    GRADE = "Grade"
    CONNECTION = "Connection"
    LOGISTICS = "Logistics"


# Sentinel `attribute_value` meaning "every product matches this row on this
# dimension". Chosen as "*" rather than NULL so the unique constraint below
# actually constrains it (in SQL, NULLs do not collide).
ANY_ATTRIBUTE_VALUE = "*"


class LeadTimeComponent(Base):
    """One additive lead-time term for one attribute value. See module docstring."""

    __tablename__ = "lead_time_components"
    __table_args__ = (
        UniqueConstraint(
            "dimension",
            "attribute_value",
            name="uq_lead_time_component_dimension_value",
        ),
    )

    id = Column(String(36), primary_key=True, default=_uuid)
    dimension = Column(
        SAEnum(LeadTimeDimension, name="leadtimedimension"), nullable=False
    )
    # The attribute value this term applies to, or ANY_ATTRIBUTE_VALUE for a row
    # that applies to every product on its dimension.
    attribute_value = Column(String, nullable=False)
    months = Column(Float, nullable=False)
    # Display-only planner name for the term. Never matched on.
    label = Column(String, nullable=True)
