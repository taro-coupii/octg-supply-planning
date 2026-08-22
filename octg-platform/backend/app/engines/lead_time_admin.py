"""Editing lead-time components, and reporting what the edit did.

`LeadTimeComponent` rows were seed-only data. The product owner asked for them to be
adjustable ("leadtime assumption per product components"), so `app.api.admin` now
offers real CRUD. This module is the part that is NOT plumbing: working out, and
stating, what a component edit actually changed.

WHY AN EDIT HERE NEEDS A REPORT AT ALL
======================================
A lead-time component is three columns and one of the most consequential rows in the
platform. Deleting the Grade row for "Carbon" does not make a number smaller; it
makes every carbon product's component set INCOMPLETE, which
`app.engines.lead_time` reports as `modelled = False, total_months = 0.0` -- "not
modelled", deliberately not a partial sum. That flows straight into:

  * `app.engines.order_dates.order_feasibility` -- no order date to recommend, and
    MRP labels what it does show indicative only;
  * `app.engines.order_dates.is_recoverable` -- a product with no modelled lead time
    is NEVER judged unrecoverable, because absent data means "cannot judge", not
    "hopeless";
  * and therefore `CoverageStatus.UNRECOVERABLE`, the platform's one terminal claim.

So a three-column edit can retract an Unrecoverable verdict, or create one. The
established answer in this codebase to a consequential-but-legitimate change is to
MAKE it and REPORT it -- see `app.engines.coverage.set_well_demand_status`, which
returns before/after status and coverage for every affected line, and the
customer-owned upload's `coverage_changes`, which lists every well whose rollup moved
including wells the file never mentioned. This module gives lead-time edits the same
treatment rather than inventing a confirmation step the server cannot verify.

WHAT IS AND IS NOT CACHED (checked, not assumed)
===============================================
Everything derived from lead time is computed FRESH on every read, so the edit is
visible everywhere the instant it commits, with one exception that this module
handles.

Live, nothing to invalidate:
  * `app.engines.lead_time.resolve_lead_time` queries the whole component table on
    every call (`_rows_by_dimension`) and memoises nothing -- no lru_cache, no
    module-level dict, no instance state. `total_lead_time_months`,
    `order_dates.transit_months`, `order_feasibility` and `is_recoverable` are all
    documented as VIEWS of that one resolver rather than second queries.
  * The MRP screens, the by-item analysis and the substitution "approve by" date all
    call it per request.

NOT live, which is the exception:
  * `CoverageResult.status` is PERSISTED. The choice between
    `CoverageStatus.UNCOVERED` and `CoverageStatus.UNRECOVERABLE` is made by
    `is_recoverable` at recompute time and then STORED, and `Well.coverage_status`
    rolls up from those stored rows. A lead-time edit therefore leaves a stored
    verdict that no longer describes what the engine would now say -- the same class
    of defect as the stale-`CoverageResult` bug `recompute_customer` deletes rows to
    avoid.

Hence `apply_lead_time_change` recomputes every customer in the same transaction.
Measured cost on the seeded demo database before choosing: 3 customers, 27 wells, 113
demand lines -- three `recompute_customer` calls, comfortably inside a request, for an
action an administrator performs rarely. Leaving the rows stale was rejected for the
reason the whole codebase rejects it: a superseded verdict presented as current is
worse than no verdict.
"""

from dataclasses import dataclass
from typing import Callable

from sqlalchemy.orm import Session

from app.engines.lead_time import resolve_lead_time
from app.models import Customer, Product, Well


@dataclass(frozen=True)
class ProductLeadTimeChange:
    """One product whose lead time moved because a component changed. Scalars only.

    Both the TOTAL and the MODELLED flag are reported before and after, because they
    are different kinds of change and a planner needs to tell them apart. A total
    moving from 6.5 to 7.5 is a re-estimate. `modelled` flipping to False is the
    platform retracting its ability to answer at all, and it silently converts every
    Unrecoverable verdict for that product into something softer.
    """

    product_id: str
    product_description: str | None
    total_months_before: float
    total_months_after: float
    modelled_before: bool
    modelled_after: bool
    #: Dimensions with no matching row AFTER the change, i.e. why it is not modelled.
    #: Empty when `modelled_after` is True.
    missing_dimensions_after: tuple[str, ...] = ()

    @property
    def became_unmodelled(self) -> bool:
        return self.modelled_before and not self.modelled_after

    @property
    def became_modelled(self) -> bool:
        return not self.modelled_before and self.modelled_after


@dataclass(frozen=True)
class WellCoverageChange:
    """One well whose coverage rollup moved. Scalars only."""

    well_id: str
    well_name: str
    coverage_before: str | None
    coverage_after: str | None


@dataclass(frozen=True)
class LeadTimeChangeImpact:
    """The blast radius of one lead-time component edit.

    `products_examined` is the denominator. Without it a caller cannot tell "nothing
    changed" from "nothing was looked at", which is the same distinction
    `app.api.customer_owned_inventory` keeps between "owns none" and "no data".
    """

    products_examined: int
    product_changes: tuple[ProductLeadTimeChange, ...]
    recomputed_customer_ids: tuple[str, ...]
    well_changes: tuple[WellCoverageChange, ...]


def _snapshot(db: Session, products: list[Product]) -> dict[str, tuple]:
    """{product_id: (total_months, modelled, missing_dimensions)} right now."""
    out: dict[str, tuple] = {}
    for product in products:
        breakdown = resolve_lead_time(db, product)
        out[product.id] = (
            breakdown.total_months,
            breakdown.modelled,
            tuple(breakdown.missing_dimensions),
        )
    return out


def apply_lead_time_change(
    db: Session, mutate: Callable[[], None]
) -> LeadTimeChangeImpact:
    """Run `mutate` (a create/update/delete of components), then report and repair.

    In order: snapshot every product's resolved lead time, apply the caller's
    mutation, flush, re-snapshot, recompute every customer's coverage so no stored
    verdict outlives the assumption it was computed from, and return the diff.

    Flushes; does not commit. The caller owns the transaction, so a mutation that
    raises after this point still rolls the whole thing back -- including the
    recompute -- rather than leaving components edited and verdicts half-rewritten.

    The mutation is passed IN rather than this function taking a component and an
    operation. Create, update and delete need identical before/after treatment and
    differ only in the one line that touches the row, so inlining that line as a
    callback is what keeps the three endpoints from each growing their own slightly
    different version of the reporting -- which is how two of them would eventually
    stop recomputing.
    """
    # Every product in the catalogue, not merely the ones a planner might guess are
    # affected. A component row is ATTRIBUTE-keyed and a wildcard row applies to
    # everything, so "which products does this row touch" is not answerable without
    # resolving them all -- and the wildcard case is exactly the one where a guess
    # would miss the most.
    products = db.query(Product).all()
    before = _snapshot(db, products)

    mutate()
    db.flush()

    after = _snapshot(db, products)

    product_changes: list[ProductLeadTimeChange] = []
    for product in products:
        total_before, modelled_before, _missing_before = before[product.id]
        total_after, modelled_after, missing_after = after[product.id]
        if total_before == total_after and modelled_before == modelled_after:
            continue
        product_changes.append(
            ProductLeadTimeChange(
                product_id=product.id,
                product_description=product.description,
                total_months_before=total_before,
                total_months_after=total_after,
                modelled_before=modelled_before,
                modelled_after=modelled_after,
                missing_dimensions_after=missing_after,
            )
        )
    product_changes.sort(key=lambda c: (c.product_description or c.product_id))

    # Coverage is recomputed even when NO product's lead time moved. That looks
    # wasteful and is not: `product_changes` is derived from the component table,
    # while a stored verdict could already be stale for an unrelated reason, and a
    # conditional recompute would make "did the admin screen fix it" depend on
    # whether this particular edit happened to matter. Unconditional is one rule.
    wells = db.query(Well).all()
    coverage_before = {well.id: (well.name, well.coverage_status) for well in wells}

    customers = db.query(Customer).all()
    for customer in customers:
        # No explicit filters: the platform's CURRENT coverage scope is what the
        # official verdict must be computed under. See app.engines.coverage_scope.
        from app.engines.coverage import recompute_customer

        recompute_customer(db, customer)
    db.flush()

    well_changes: list[WellCoverageChange] = []
    for well in db.query(Well).all():
        name, was = coverage_before.get(well.id, (well.name, None))
        if was != well.coverage_status:
            well_changes.append(
                WellCoverageChange(
                    well_id=well.id,
                    well_name=name,
                    coverage_before=was,
                    coverage_after=well.coverage_status,
                )
            )
    well_changes.sort(key=lambda c: c.well_name)

    return LeadTimeChangeImpact(
        products_examined=len(products),
        product_changes=tuple(product_changes),
        recomputed_customer_ids=tuple(sorted(c.id for c in customers)),
        well_changes=tuple(well_changes),
    )
