"""Lead-time resolution -- the ONE matcher for attribute-based lead times.

Lead time is built from ATTRIBUTE components and is never a stored per-SKU value
(Key Discovery #13: users trust it because the calculation is transparent and
explainable). The spec's worked example::

    OD/WT 4 + Grade 2 + Connection 0 + Logistics 2 = Total 8 months

`resolve_lead_time` is the single entry point. Everything else in this codebase --
`total_lead_time_months`, `app.engines.order_dates.transit_months`,
`order_feasibility`, `is_recoverable`, the MRP breakdown -- is a VIEW of its
result, never a second query. That is deliberate: `transit_months` used to run its
own filter, and a drifted transit calculation desynchronises
`required_ship_date` from `recommended_order_date`, publishing two dates that
contradict each other.

Matching (one rule, applied per dimension)
------------------------------------------
For each of the four `LeadTimeDimension` values, the product yields exactly one
key (see `product_attribute_key`; OD/WT is the canonical `size + weight` pair --
`od_wt_key`). A row matches that dimension if its `attribute_value` equals the
key, or if it is the `ANY_ATTRIBUTE_VALUE` wildcard "*". A specific row WINS over
a wildcard row, so a global default plus exceptions is expressible.

Two rows sharing a (dimension, attribute_value) pair raise
`AmbiguousLeadTimeComponent`. A silently-picked winner is unexplainable, and
explainability is the whole point of the model. (A unique constraint prevents this
in the database; the check exists because a session can hold unflushed duplicates
and because sqlite databases predating the constraint exist.)

INCOMPLETE MEANS NOT MODELLED (the conservative choice)
-------------------------------------------------------
All four dimensions are REQUIRED. If any dimension has no matching row -- not even
a wildcard -- the model for that product is INCOMPLETE, and then:

    total_months == 0.0  and  modelled is False

The matched months are still reported individually in `components` (and summed in
`matched_months`) so a planner can see how far the model got and exactly which
dimensions are missing, but they do NOT become the total.

Why not just sum what matched? Because a partial sum is a confidently wrong
number in the one place the platform makes an irreversible-sounding claim.
Suppose Grade (+2) is configured and OD/WT (+4) is not: summing gives a 2-month
lead time, `recommended_order_date` two months before ROS, and -- worse -- an
`is_recoverable` verdict computed from a physics figure that is 6 months too
optimistic in one direction and, for a line just inside the real lead time, a
false "recoverable" that lets a planner stop worrying. Reporting 0/"not modelled"
routes into the behaviour that already exists for the no-data case: coverage never
says UNRECOVERABLE (absent data means "cannot judge", not "hopeless" --
`app.engines.order_dates.is_recoverable`), and MRP labels the order date
indicative only. Admitting the model is incomplete is recoverable; a wrong date
that looks right is not.

The zero-months case is NOT the incomplete case. The spec's own example has
Connection at +0 months, so a row of 0 is a real, deliberate answer ("this
dimension adds nothing"). It is a ROW's presence, never its value, that makes a
dimension modelled.
"""

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models import Product
from app.models.lead_time import (
    ANY_ATTRIBUTE_VALUE,
    LeadTimeComponent,
    LeadTimeDimension,
)

# Every dimension must resolve for a product's lead time to count as modelled.
# Ordered as the spec presents them, and the breakdown is rendered in this order.
REQUIRED_DIMENSIONS: tuple[LeadTimeDimension, ...] = (
    LeadTimeDimension.OD_WT,
    LeadTimeDimension.GRADE,
    LeadTimeDimension.CONNECTION,
    LeadTimeDimension.LOGISTICS,
)

# The dimension carrying the ocean/inland leg between the mill releasing material
# and it standing at the wellsite. `order_dates.required_ship_date` strips exactly
# this off the ROS date.
TRANSIT_DIMENSIONS: frozenset[LeadTimeDimension] = frozenset(
    {LeadTimeDimension.LOGISTICS}
)

# Retained name, new meaning: the LABELS historically used for the shared
# shipping/sailing term. Kept only so seeds and fixtures can spell the display
# label consistently -- matching is by `dimension`, never by label. It is
# deliberately no longer part of any query.
SHARED_COMPONENT_LABELS = ("Sailing", "Shipping")


class AmbiguousLeadTimeComponent(Exception):
    """Two lead-time rows claim the same (dimension, attribute value).

    Raised rather than resolved by precedence: which of two equally specific rows
    won would be invisible in the published breakdown, and the breakdown is the
    reason planners trust the number.
    """


def od_wt_key(product: Product) -> str:
    """Canonical OD/WT key for `product` -- the single formatter of the pair.

    "OD/WT" spans two Product columns, so it is keyed by the canonical rendering
    of both: ``"4-1/2 12.6"``. A product with no weight recorded keys on its size
    alone (``"4-1/2"``); it is the honest key for the information present, and it
    means an OD/WT row must be seeded for it under that same key or the product
    counts as not modelled.

    `:g` formatting keeps 12.6 as "12.6" and 68.0 as "68", so seeds may write
    either 68 or 68.0 and match the same row.
    """
    size = (product.size or "").strip()
    if product.weight is None:
        return size
    return f"{size} {product.weight:g}"


def product_attribute_key(product: Product, dimension: LeadTimeDimension) -> str:
    """The one attribute value `product` presents on `dimension`.

    GRADE and LOGISTICS both key on `grade_type` (the grade FAMILY, which exists
    to group grades for lead-time lookup); LOGISTICS additionally -- and usually --
    matches a single wildcard row instead. See app.models.lead_time.
    """
    if dimension is LeadTimeDimension.OD_WT:
        return od_wt_key(product)
    if dimension is LeadTimeDimension.GRADE:
        return (product.grade_type or "").strip()
    if dimension is LeadTimeDimension.CONNECTION:
        return (product.connection or "").strip()
    if dimension is LeadTimeDimension.LOGISTICS:
        return (product.grade_type or "").strip()
    raise ValueError(f"Unknown lead-time dimension: {dimension!r}")


@dataclass(frozen=True)
class LeadTimeComponentMatch:
    """One resolved term of a product's lead time, as shown to a planner."""

    dimension: str  # LeadTimeDimension value, e.g. "OD/WT"
    attribute_value: str  # the ROW's value; "*" when it is the shared wildcard
    matched_on: str  # the PRODUCT's key on this dimension
    months: float
    shared: bool  # True when matched via the "*" wildcard row
    label: str | None = None
    component_id: str | None = None


@dataclass(frozen=True)
class LeadTimeBreakdown:
    """The explainable answer: every term, the total, and what is missing.

    `total_months` is 0.0 unless `modelled` is True -- see the module docstring on
    why an incomplete component set is reported as not modelled rather than
    summed. `matched_months` is the sum of whatever DID match and exists purely so
    the UI can show how far the model got; it must never be used as a lead time.
    """

    product_id: str
    components: tuple[LeadTimeComponentMatch, ...]
    total_months: float
    modelled: bool
    missing_dimensions: tuple[str, ...] = ()
    matched_months: float = 0.0
    note: str = ""

    @property
    def transit_months(self) -> float:
        """The shipping/sailing leg, from the SAME resolution as the total.

        0.0 when the model is incomplete, so `required_ship_date` and
        `recommended_order_date` are always derived from one consistent answer
        instead of one real number and one placeholder.
        """
        if not self.modelled:
            return 0.0
        return sum(
            c.months
            for c in self.components
            if c.dimension in {d.value for d in TRANSIT_DIMENSIONS}
        )


def _rows_by_dimension(
    db: Session,
) -> dict[LeadTimeDimension, dict[str, LeadTimeComponent]]:
    """All component rows indexed {dimension: {attribute_value: row}}.

    One query for the whole table. It is a small configuration table (a handful
    of rows per dimension), and loading it once is what lets the resolver be
    called per product inside coverage/MRP loops without N queries -- and what
    lets duplicates be detected at all.
    """
    index: dict[LeadTimeDimension, dict[str, LeadTimeComponent]] = {}
    for row in db.query(LeadTimeComponent).all():
        dimension = row.dimension
        if isinstance(dimension, str):  # defensive: raw string from a legacy row
            dimension = LeadTimeDimension(dimension)
        value = (row.attribute_value or "").strip()
        bucket = index.setdefault(dimension, {})
        if value in bucket:
            raise AmbiguousLeadTimeComponent(
                f"two lead-time components claim dimension {dimension.value!r} "
                f"attribute value {value!r} ({bucket[value].id} and {row.id}); "
                "the breakdown cannot be explained until one is removed"
            )
        bucket[value] = row
    return index


def resolve_lead_time(db: Session, product: Product) -> LeadTimeBreakdown:
    """Resolve `product`'s lead time from its attributes. THE resolver.

    Returns a full breakdown -- one entry per matched dimension, the total, and
    the names of any dimensions with no matching row. See the module docstring for
    the matching rule and for why an incomplete set totals 0.
    """
    index = _rows_by_dimension(db)

    matches: list[LeadTimeComponentMatch] = []
    missing: list[str] = []
    for dimension in REQUIRED_DIMENSIONS:
        bucket = index.get(dimension, {})
        key = product_attribute_key(product, dimension)
        row = bucket.get(key)
        shared = False
        if row is None:
            row = bucket.get(ANY_ATTRIBUTE_VALUE)
            shared = row is not None
        if row is None:
            missing.append(dimension.value)
            continue
        matches.append(
            LeadTimeComponentMatch(
                dimension=dimension.value,
                attribute_value=row.attribute_value,
                matched_on=key,
                months=float(row.months),
                shared=shared,
                label=row.label,
                component_id=row.id,
            )
        )

    matched_months = sum(m.months for m in matches)
    modelled = not missing

    if modelled:
        note = ""
    elif not matches:
        note = (
            "no lead-time components match this product on any dimension "
            "-- lead time is NOT MODELLED, not zero"
        )
    else:
        note = (
            "lead time is NOT MODELLED: no component matches "
            f"{', '.join(missing)}. The {matched_months:g} month(s) that did "
            "match are shown for information and deliberately NOT totalled -- a "
            "partial sum would be a confidently wrong date"
        )

    return LeadTimeBreakdown(
        product_id=product.id,
        components=tuple(matches),
        total_months=matched_months if modelled else 0.0,
        modelled=modelled,
        missing_dimensions=tuple(missing),
        matched_months=matched_months,
        note=note,
    )


def total_lead_time_months(db: Session, product: Product) -> float:
    """Total attribute-based lead time in months -- a VIEW of `resolve_lead_time`.

    0.0 means "not modelled" (no components, or an incomplete set), never
    "available instantly". Callers that need to tell those apart, or that need to
    explain the number, should call `resolve_lead_time` directly.
    """
    return resolve_lead_time(db, product).total_months
