"""Inventory scope resolution -- the ONE place the Business Unit boundary is
decided.

Every consumer of an on-hand quantity (the coverage engine, the substitution
fall-through's availability seed, the surplus report) resolves
it through `on_hand_for` / `on_hand_map`. There is no second reader: a second
reader is a second policy, and a second policy is how a global scalar leaks
across the boundary.

BU-scoped rows are the ONLY source. There is no fallback.
--------------------------------------------------------
`InventoryOnHand(business_unit_id, product_id, quantity)` is the sole authority.
The legacy unscoped `Product.on_hand_qty` column is GONE -- both the reader and
the column -- so the precedence rule this module used to document no longer
exists. There is nothing to prefer between; there is one source.

Two situations therefore have no answer, and both RAISE rather than return a
number:

  * No `InventoryOnHand` row for (business_unit_id, product_id)
    -> `InventoryRowMissing`. The quantity is UNKNOWN. Returning 0 would report
       "this BU holds none of it" and returning a global scalar would report
       another BU's steel as ours; both are claims the data does not support.
       Unknown must never be silently rendered as a number.

  * `business_unit_id is None`, i.e. an unmapped customer
    -> `InventoryScopeMissing`. There is no pool to resolve against at all. The
       previous middle state -- "an unmapped customer is isolated and reads the
       legacy scalar" -- is removed: a customer with no Business Unit cannot have
       coverage computed, and failing loudly is better than half-working.

Both derive from `InventoryNotScoped`, so an API handler can catch the one type
and still distinguish the two causes (they map to different HTTP status codes --
see `app.main`).

Why an exception and not a sentinel
----------------------------------
A sentinel (None, NaN, -1) has to be checked by every caller, and the caller that
forgets is exactly the one that silently reintroduces the leak. An exception
cannot be forgotten.

Scopes
------
`scoped_customer_ids` reduces a customer to the set of customers whose inventory
facts share a pool: the whole Business Unit. An unmapped customer has no pool, so
it raises for the same reason the quantity lookups do. The old `scope_key`
helper, whose `("customer", <id>)` branch encoded the removed "unmapped customer
is a scope of one" behaviour, has been deleted along with that behaviour -- it had
no callers, and a branch that can never be reached correctly is worse than no
branch.

ON-ORDER IS RESOLVED HERE TOO, BY A DELIBERATELY DIFFERENT RULE
---------------------------------------------------------------
`on_order_map` answers the incoming-supply question from
`app.models.inventory_on_order.InventoryOnOrder`, and it does NOT raise for a
missing row. That asymmetry with on-hand is reasoned, not an oversight, and the
reasoning lives on the model: on-order is not an input to any verdict, and
"nothing is on order" is the ordinary state of most products, so a missing row is
a normal absence rather than a hole in data coverage depends on. The absence is
still never rendered as 0 -- `OnOrderPosition.known` is False and every consumer
reports the figure unavailable with a reason. An explicit `quantity = 0` row is
the way to say "nothing on order", exactly as an explicit 0 on-hand row says
"this BU holds none of it".

CUSTOMER-OWNED STOCK IS RESOLVED HERE TOO, AND THE OWNERSHIP TIER IS DECIDED HERE
--------------------------------------------------------------------------------
`app.models.customer_owned_inventory.CustomerOwnedInventory` holds material the
CUSTOMER owns -- absent from Oracle entirely, uploaded into this platform. It is
scoped to (customer, product), not to (BU, product), and it is drawn BEFORE any
company-owned stock for the same product (the product owner's ruling: "for the same
product, consuming customer-owned inventory takes priority over FIFO").

That ordering is a property of the POOL, so the pool is what this module now
resolves. `ownership_pool_for` / `ownership_pool_map` return an `OwnershipPool`,
which carries the two tiers separately, and `app.engines.allocation` draws the
customer-owned tier first. The BU boundary and the ownership tier are therefore
decided in the SAME place, which is the point: a second reader of either would be
a second policy.

WHY THE SPLIT IS A TYPE AND NOT A SECOND FLOAT
---------------------------------------------
`on_hand_map` returns `dict[str, float]` and means COMPANY-OWNED ONLY -- it reads
`InventoryOnHand`, which is Oracle's projection of OUR steel, and its meaning has
not changed by one metre. Several callers legitimately want exactly that (the
surplus report's definition, which must never offer somebody else's
property) or want a bare total across BUs (MRP).

Allocation needs the SPLIT, and the failure mode this codebase keeps finding is a
caller that took a total where it needed a breakdown. So the split is a distinct
type with no arithmetic on it: `OwnershipPool` is not a number, cannot be added,
compared or summed, and its total is only reachable by asking for `.total` by
name. A caller that wanted the split and reached for `on_hand_map` gets a float
labelled company-owned in its own signature and docstring; a caller that wanted
the total and reached for `ownership_pool_map` gets an object it cannot use as a
number until it has said which number it means.

Unknown-vs-zero, for customer-owned stock specifically
-----------------------------------------------------
A missing `CustomerOwnedInventory` row does NOT raise, for the reason
`InventoryOnOrder` does not: most products legitimately have none, and a customer
who has uploaded nothing owns none rather than an unknown amount. The two states
are still kept apart -- by the presence of a
`CustomerOwnedInventoryUpload` row for the customer, not by a sentinel quantity.
`CustomerOwnedPosition.known` carries the distinction; see the model docstring for
the full statement of the three cases.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import (
    Customer,
    CustomerOwnedInventory,
    CustomerOwnedInventoryUpload,
    InventoryOnHand,
    InventoryOnOrder,
    Product,
)


class InventoryNotScoped(Exception):
    """An on-hand quantity could not be resolved inside a Business Unit.

    Carries the identifiers so an API handler can build a message without
    re-deriving anything. Catch this base class to handle both causes; catch a
    subclass to distinguish them.
    """

    def __init__(
        self,
        message: str,
        *,
        business_unit_id: str | None = None,
        product_id: str | None = None,
    ):
        super().__init__(message)
        self.business_unit_id = business_unit_id
        self.product_id = product_id


class InventoryScopeMissing(InventoryNotScoped):
    """The customer (or caller) has no Business Unit, so there is no pool.

    A CONFIGURATION problem in this platform's own reference data: somebody must
    map the customer to a Business Unit. Nothing about the request was wrong.
    """


class InventoryRowMissing(InventoryNotScoped):
    """No `InventoryOnHand` row exists for this (Business Unit, product).

    INCOMPLETE upstream reference data: on-hand quantity is an Oracle-owned
    projection (see app.models.inventory_on_hand.InventoryOnHand) and the row for
    this pair has not arrived. The quantity is unknown, not zero.
    """


def _product_label(product: Product | None, product_id: str) -> str:
    if product is None:
        return product_id
    return product.description or product.id


def _scope_missing(product_label: str | None = None) -> InventoryScopeMissing:
    subject = (
        f"on-hand quantity of {product_label}"
        if product_label is not None
        else "an inventory scope"
    )
    return InventoryScopeMissing(
        f"Cannot resolve {subject}: no Business Unit was supplied. On-hand "
        "inventory is held per (Business Unit, product), so a customer that is "
        "not mapped to a Business Unit has no inventory pool to resolve against "
        "and no coverage can be computed for it. Map the customer to a Business "
        "Unit (Customer.business_unit_id) and retry."
    )


def _row_missing(
    product_label: str, product_id: str, business_unit_id: str
) -> InventoryRowMissing:
    return InventoryRowMissing(
        f"No InventoryOnHand row for product {product_label} "
        f"(id={product_id}) in Business Unit {business_unit_id}. The on-hand "
        "quantity is UNKNOWN for this pair -- it is deliberately not treated as "
        "0, which would assert that this BU holds none of it. Load the "
        "(Business Unit, product) row from the Oracle inventory projection (or "
        "re-seed the demo data) and retry.",
        business_unit_id=business_unit_id,
        product_id=product_id,
    )


def scoped_customer_ids(db: Session, customer: Customer) -> list[str]:
    """Every customer sharing `customer`'s inventory scope, including itself.

    That scope is the whole Business Unit. Used to bound the HARD/HYBRID
    assignment netting so a reservation made in another BU can never shrink this
    pool.

    Raises `InventoryScopeMissing` for an unmapped customer: there is no pool,
    and "the customer alone" was a made-up scope that only existed to keep the
    legacy unscoped quantity usable.
    """
    if customer.business_unit_id is None:
        raise _scope_missing()
    return [
        row_id
        for (row_id,) in db.query(Customer.id).filter(
            Customer.business_unit_id == customer.business_unit_id
        )
    ]


def on_hand_for(
    db: Session,
    business_unit_id: str | None,
    product: Product,
) -> float:
    """Resolved COMPANY-OWNED on-hand quantity of `product` in `business_unit_id`.

    Never negative. Resolves ONLY from `InventoryOnHand`, which is Oracle's
    projection of OUR OWN company's stock. It deliberately does NOT include
    customer-owned material: that is a different owner, a different table, a
    different scope (customer, not BU) and a different draw priority. A caller
    deciding COVERAGE wants `ownership_pool_for`, which carries both tiers and the
    order they are drawn in.

    Raises:
      InventoryScopeMissing  `business_unit_id` is None.
      InventoryRowMissing    no row for (business_unit_id, product).
    """
    if business_unit_id is None:
        raise _scope_missing(_product_label(product, product.id))

    row = (
        db.query(InventoryOnHand)
        .filter(
            InventoryOnHand.business_unit_id == business_unit_id,
            InventoryOnHand.product_id == product.id,
        )
        .one_or_none()
    )
    if row is None:
        raise _row_missing(
            _product_label(product, product.id), product.id, business_unit_id
        )
    return max(0.0, row.quantity or 0.0)


def on_hand_map(
    db: Session,
    business_unit_id: str | None,
    product_ids: set[str] | list[str],
) -> dict[str, float]:
    """`on_hand_for` in bulk: {product_id: COMPANY-OWNED qty} for `product_ids`.

    COMPANY-OWNED ONLY -- see `on_hand_for`. This is the right function for the
    surplus definition in `app.engines.surplus` (customer-owned stock must never be
    offered to a neighbour) and the wrong one for deciding coverage, which needs the
    ownership split from `ownership_pool_map`.

    Two queries rather than one per product, and the same rules: every requested
    product that EXISTS must have a row in `business_unit_id`, or the whole call
    raises. Product ids that do not resolve to a `Product` row at all are omitted
    (they are not an inventory question -- there is no such product).

    Raises the same two exceptions as `on_hand_for`. When several products are
    missing rows the message names all of them, so one round trip tells the
    operator everything that has to be loaded.
    """
    ids = list({pid for pid in product_ids})
    if not ids:
        # Nothing was asked, so nothing is unknown -- including for an unmapped
        # customer. The scope error belongs to whoever actually needs a quantity.
        return {}

    if business_unit_id is None:
        raise _scope_missing()

    products = {p.id: p for p in db.query(Product).filter(Product.id.in_(ids))}
    if not products:
        return {}

    rows = (
        db.query(InventoryOnHand)
        .filter(
            InventoryOnHand.business_unit_id == business_unit_id,
            InventoryOnHand.product_id.in_(sorted(products)),
        )
        .all()
    )
    resolved = {row.product_id: max(0.0, row.quantity or 0.0) for row in rows}

    missing = sorted(set(products) - set(resolved))
    if missing:
        if len(missing) == 1:
            pid = missing[0]
            raise _row_missing(
                _product_label(products.get(pid), pid), pid, business_unit_id
            )
        labels = ", ".join(
            f"{_product_label(products.get(pid), pid)} (id={pid})" for pid in missing
        )
        raise InventoryRowMissing(
            f"No InventoryOnHand rows in Business Unit {business_unit_id} for "
            f"{len(missing)} product(s): {labels}. Those quantities are UNKNOWN "
            "-- they are deliberately not treated as 0, which would assert that "
            "this BU holds none of them. Load the (Business Unit, product) rows "
            "from the Oracle inventory projection (or re-seed the demo data) and "
            "retry.",
            business_unit_id=business_unit_id,
            product_id=missing[0],
        )

    return resolved


def total_on_hand_all_bus(db: Session, product: Product) -> float:
    """Sum of `product`'s on-hand quantity across EVERY Business Unit.

    For the MRP screens ONLY, and only because MRP is itself system-wide: both
    `mrp_summary(customer_id=None)` and `by_item` aggregate demand across every
    customer of every BU, so the honest counterpart is the total the company
    holds. This is NOT a BU-scoped figure and must never be used to decide
    coverage -- coverage goes through `on_hand_for` / `on_hand_map`, always.

    Raises `InventoryRowMissing` when the product has no `InventoryOnHand` row in
    any BU at all: same rule as everywhere else, an absent row is unknown rather
    than zero, and an MRP runout curve starting from a fabricated 0 is exactly
    the kind of confident wrong number this change exists to remove.
    """
    rows = (
        db.query(InventoryOnHand)
        .filter(InventoryOnHand.product_id == product.id)
        .all()
    )
    if not rows:
        raise InventoryRowMissing(
            f"Product {_product_label(product, product.id)} (id={product.id}) has "
            "no InventoryOnHand row in ANY Business Unit, so its on-hand quantity "
            "is UNKNOWN. It is deliberately not reported as 0. Load the "
            "(Business Unit, product) rows from the Oracle inventory projection "
            "(or re-seed the demo data) and retry.",
            product_id=product.id,
        )
    return max(0.0, sum(max(0.0, row.quantity or 0.0) for row in rows))


# ---------------------------------------------------------------------------
# ON ORDER -- incoming supply, resolved by the rule the module docstring states
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OnOrderPosition:
    """Incoming supply for ONE (Business Unit, product), or the absence of any.

    `known` is the whole point of this type. It separates the two states a bare
    float cannot:

      known=True, quantity=0.0    MEASURED. An explicit `InventoryOnOrder` row (or
                                  rows) exists and totals nothing: this BU has
                                  nothing on order of this product. A fact.
      known=False, quantity=None  UNKNOWN. No row exists for the pair, so the
                                  platform holds no on-order data about it. Every
                                  consumer must render this as unavailable, never
                                  as 0 -- the same rule
                                  `app.engines.executive` applies to every figure
                                  it cannot compute.

    `source_system` is the provenance vocabulary shared with
    `app.engines.mrp.InventoryPosition.assigned_source`: "unavailable" when
    nothing is known, "synthetic" when every contributing row is seeded demo
    data, "oracle" once real feed rows back it, "mixed" if both. It is
    independent of `oracle_integrated`, which still means "the feed is live" and
    still is not.
    """

    known: bool
    quantity: float | None = None
    row_count: int = 0
    earliest_expected_arrival: datetime | None = None
    latest_expected_arrival: datetime | None = None
    #: Quantity on rows carrying NO `expected_arrival_date`. A PO with no promise
    #: date is real (raised, not acknowledged), and it must not be counted inside
    #: any arrival horizon -- so it is reported separately rather than dated to
    #: today. 0.0 when everything on order is scheduled.
    undated_quantity: float = 0.0
    source_system: str = "unavailable"


_UNKNOWN_ON_ORDER = OnOrderPosition(known=False)


def _source_label(sources: set[str]) -> str:
    """Collapse the `source_system` values of contributing rows into one label.

    Same vocabulary and same precedence as
    `app.engines.mrp._inventory_position` uses for assignments, so a UI can
    render one provenance badge for both.
    """
    if not sources:
        return "unavailable"
    if sources == {"oracle"}:
        return "oracle"
    if "oracle" in sources:
        return "mixed"
    return "synthetic"


def _position_from_rows(rows: list[InventoryOnOrder]) -> OnOrderPosition:
    if not rows:
        return _UNKNOWN_ON_ORDER
    dated = [r for r in rows if r.expected_arrival_date is not None]
    return OnOrderPosition(
        known=True,
        quantity=max(0.0, sum(max(0.0, r.quantity or 0.0) for r in rows)),
        row_count=len(rows),
        earliest_expected_arrival=(
            min(r.expected_arrival_date for r in dated) if dated else None
        ),
        latest_expected_arrival=(
            max(r.expected_arrival_date for r in dated) if dated else None
        ),
        undated_quantity=sum(
            max(0.0, r.quantity or 0.0)
            for r in rows
            if r.expected_arrival_date is None
        ),
        source_system=_source_label({(r.source_system or "synthetic") for r in rows}),
    )


def on_order_rows(
    db: Session,
    business_unit_id: str | None,
    product_ids: set[str] | list[str] | None = None,
) -> list[InventoryOnOrder]:
    """The raw projected purchase-order rows, for callers that need the dates.

    Returned rather than pre-aggregated because an arrival HORIZON is a property
    of individual rows: two POs for the same product landing four months apart
    are two different answers to "what arrives inside 3 months", and a total
    cannot be split back into them.

    `business_unit_id is None` returns `[]` rather than raising. An unmapped
    customer has no pool, which `on_hand_for` refuses over because coverage
    depends on the quantity; nothing depends on this one, and the honest answer
    for "what is on order into no Business Unit" is "no rows", which the caller
    then reports as unknown.
    """
    if business_unit_id is None:
        return []
    query = db.query(InventoryOnOrder).filter(
        InventoryOnOrder.business_unit_id == business_unit_id
    )
    if product_ids is not None:
        ids = sorted({pid for pid in product_ids})
        if not ids:
            return []
        query = query.filter(InventoryOnOrder.product_id.in_(ids))
    return query.all()


def on_order_for(
    db: Session,
    business_unit_id: str | None,
    product: Product,
) -> OnOrderPosition:
    """Incoming supply of `product` into `business_unit_id`.

    Never raises. Returns `OnOrderPosition(known=False)` when no row exists --
    see the module docstring for why this differs from `on_hand_for`.
    """
    return _position_from_rows(
        on_order_rows(db, business_unit_id, {product.id})
    )


def on_order_map(
    db: Session,
    business_unit_id: str | None,
    product_ids: set[str] | list[str],
) -> dict[str, OnOrderPosition]:
    """`on_order_for` in bulk, in ONE query.

    EVERY requested product id gets an entry, including the ones with no rows --
    they map to `known=False`. An absent key would force each caller to invent
    the unknown case, and one of them would invent it as 0.
    """
    ids = sorted({pid for pid in product_ids})
    if not ids:
        return {}
    grouped: dict[str, list[InventoryOnOrder]] = {pid: [] for pid in ids}
    for row in on_order_rows(db, business_unit_id, ids):
        grouped[row.product_id].append(row)
    return {pid: _position_from_rows(rows) for pid, rows in grouped.items()}


def total_on_order_all_bus(db: Session, product: Product) -> OnOrderPosition:
    """Sum of `product`'s on-order quantity across EVERY Business Unit.

    For the MRP screens ONLY, and for exactly the reason
    `total_on_hand_all_bus` is: MRP is itself system-wide, so the honest
    counterpart of a system-wide demand total is a system-wide supply figure. It
    is NOT a coverage figure and must never be used as one.

    Unlike `total_on_hand_all_bus` this does not raise when the product has no
    row anywhere. It returns `known=False`, and `by_item` reports that as
    unavailable rather than as 0 -- see the module docstring.
    """
    return _position_from_rows(total_on_order_rows_all_bus(db, product))


def total_on_order_rows_all_bus(db: Session, product: Product) -> list[InventoryOnOrder]:
    """The RAW on-order rows for `product` across every Business Unit.

    The row-level counterpart of `total_on_order_all_bus`, and it exists for the
    same reason `on_order_rows` exists beside `on_order_map`: an arrival SCHEDULE
    is a property of individual rows, and a total cannot be split back into the
    dates that made it. `total_on_order_all_bus` is now defined in terms of this
    function, so the two can never disagree about which rows they counted.

    ALL Business Units, deliberately, and the scope is the caller's warning to
    carry: this is an MRP-layer read (MRP is itself system-wide -- see
    `total_on_order_all_bus`), NOT a coverage-layer one. Nothing here may be used
    to decide a coverage verdict; coverage is decided from BU-scoped on-hand stock
    alone.
    """
    return (
        db.query(InventoryOnOrder)
        .filter(InventoryOnOrder.product_id == product.id)
        .all()
    )


# ---------------------------------------------------------------------------
# CUSTOMER-OWNED STOCK, and the OWNERSHIP TIER the allocation engine draws in
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CustomerOwnedPosition:
    """What ONE customer owns of ONE product, or the absence of any such data.

    `known` separates the two states a bare float cannot, exactly as
    `OnOrderPosition.known` does:

      known=True, quantity=0.0    MEASURED. The customer has uploaded a position
                                  and owns none of this product -- either by an
                                  explicit `quantity = 0` row or by the product not
                                  appearing in a file that DID arrive. A fact.
      known=False, quantity=None  UNKNOWN. No `CustomerOwnedInventoryUpload` row
                                  exists for this customer, so the platform holds no
                                  customer-owned data about them at all. Every
                                  consumer renders this unavailable, never as 0.

    `quantity` is what a caller may draw on either way -- `drawable` is 0.0 when
    nothing is known, because you cannot spend steel nobody has told you about. The
    arithmetic is the same as zero; the CLAIM is not, and that is why the flag is
    here rather than a plain float.
    """

    known: bool
    quantity: float | None = None
    source_system: str = "unavailable"
    uploaded_at: datetime | None = None

    @property
    def drawable(self) -> float:
        """The quantity allocation may consume. 0.0 when nothing is known."""
        return max(0.0, self.quantity or 0.0)


_UNKNOWN_CUSTOMER_OWNED = CustomerOwnedPosition(known=False)


@dataclass(frozen=True)
class OwnershipPool:
    """The two OWNERSHIP TIERS of one product's stock, for one customer.

    Deliberately NOT a number. It has no `__float__`, no `__add__` and no ordering,
    so it cannot be used anywhere a quantity is expected until the caller has said
    WHICH quantity it means. That is the whole design: the recurring defect class in
    this codebase is a caller taking a total where it needed the breakdown, and a
    type that refuses to be a total is the only version of the fix that a future
    edit cannot forget.

    `customer_owned` is drawn FIRST and `company_owned` second -- see
    `app.engines.allocation`. The two are ADDITIVE, not overlapping: customer-owned
    steel is not part of `InventoryOnHand` (Oracle does not know it exists), so it
    does not net off the company pool and an assignment carve-out never touches it.

    `customer_owned_known` is `CustomerOwnedPosition.known`, carried through so a
    consumer can render "owns none" differently from "no upload has happened"
    without a second query.
    """

    company_owned: float
    customer_owned: float = 0.0
    customer_owned_known: bool = False
    customer_owned_source: str = "unavailable"

    @property
    def total(self) -> float:
        """Both tiers, by name. The only way to get a single number out of this."""
        return max(0.0, self.company_owned) + max(0.0, self.customer_owned)

    def with_company_owned(self, quantity: float) -> "OwnershipPool":
        """A copy whose COMPANY tier is restated -- used for the assignment
        carve-out and for scenario inventory overrides.

        Restating only the company tier is not a convenience: an Oracle assignment
        reserves Oracle-known steel, and a scenario INVENTORY override restates an
        `InventoryOnHand` quantity. Neither can reach the customer's own property,
        and expressing that as "there is no setter for the other tier" is stronger
        than expressing it as a comment.
        """
        return OwnershipPool(
            company_owned=max(0.0, quantity),
            customer_owned=self.customer_owned,
            customer_owned_known=self.customer_owned_known,
            customer_owned_source=self.customer_owned_source,
        )


def customer_has_uploaded(db: Session, customer_id: str) -> bool:
    """Has any customer-owned inventory upload ever been accepted for `customer_id`?

    THE discriminator between "owns none" and "no data". Recorded rather than
    inferred from the presence of inventory rows: a customer whose declared position
    is genuinely all zeros must read as MEASURED, and inferring from rows would
    report that customer -- the one a planner most needs a straight answer about --
    as unknown.
    """
    return (
        db.query(CustomerOwnedInventoryUpload.id)
        .filter(CustomerOwnedInventoryUpload.customer_id == customer_id)
        .first()
        is not None
    )


def _customer_owned_source(rows: list[CustomerOwnedInventory]) -> str:
    """Provenance label for a customer-owned figure.

    Same shape as `_source_label`, DIFFERENT vocabulary, and the difference is the
    point: "oracle" is not a legal value, because Oracle does not hold this data.
    "customer-upload" means a real spreadsheet a human produced; "synthetic" means
    seeded demo data; "mixed" means both contributed.
    """
    sources = {(row.source_system or "customer-upload") for row in rows}
    if not sources:
        return "declared-none"
    if sources == {"customer-upload"}:
        return "customer-upload"
    if "customer-upload" in sources:
        return "mixed"
    return "synthetic"


def customer_owned_for(
    db: Session,
    customer: Customer,
    product: Product,
) -> CustomerOwnedPosition:
    """What `customer` owns of `product`. NEVER RAISES.

    See the module docstring for why this differs from `on_hand_for`: most products
    legitimately have none, so raising would turn the ordinary state of the world
    into an outage of coverage for every customer who has not uploaded a file.
    """
    return customer_owned_map(db, customer, {product.id})[product.id]


def customer_owned_map(
    db: Session,
    customer: Customer,
    product_ids: set[str] | list[str],
) -> dict[str, CustomerOwnedPosition]:
    """`customer_owned_for` in bulk, in at most two queries.

    EVERY requested product id gets an entry, including the ones with no row -- they
    map to `known=True, quantity=0.0` when the customer has uploaded a position and
    to `known=False` when nobody has. An absent key would force each caller to
    invent the unknown case, and one of them would invent it as 0 without saying so.
    """
    ids = sorted({pid for pid in product_ids})
    if not ids:
        return {}

    uploaded = customer_has_uploaded(db, customer.id)
    grouped: dict[str, list[CustomerOwnedInventory]] = {pid: [] for pid in ids}
    for row in (
        db.query(CustomerOwnedInventory)
        .filter(
            CustomerOwnedInventory.customer_id == customer.id,
            CustomerOwnedInventory.product_id.in_(ids),
        )
        .all()
    ):
        grouped[row.product_id].append(row)

    out: dict[str, CustomerOwnedPosition] = {}
    for pid, rows in grouped.items():
        if not rows:
            # No row. MEASURED zero if the customer has declared a position at all,
            # UNKNOWN otherwise -- the whole reason the upload record exists.
            out[pid] = (
                CustomerOwnedPosition(
                    known=True, quantity=0.0, source_system="declared-none"
                )
                if uploaded
                else _UNKNOWN_CUSTOMER_OWNED
            )
            continue
        dated = [r.uploaded_at for r in rows if r.uploaded_at is not None]
        out[pid] = CustomerOwnedPosition(
            known=True,
            quantity=max(0.0, sum(max(0.0, r.quantity or 0.0) for r in rows)),
            source_system=_customer_owned_source(rows),
            # The MOST RECENT upload behind this figure. A planner judging whether to
            # trust it wants to know how old the newest word on it is.
            uploaded_at=max(dated) if dated else None,
        )
    return out


def ownership_pool_for(
    db: Session,
    customer: Customer,
    product: Product,
) -> OwnershipPool:
    """Both ownership tiers of `product` for `customer`'s pool.

    Company-owned comes from `on_hand_for` for the customer's Business Unit and so
    raises exactly as it always did (`InventoryScopeMissing` /
    `InventoryRowMissing`); customer-owned comes from `customer_owned_for` and never
    raises. That asymmetry is the two rules stated in the module docstring, applied
    in one place so no consumer can apply them differently.
    """
    return ownership_pool_map(db, customer, {product.id})[product.id]


def ownership_pool_map(
    db: Session,
    customer: Customer,
    product_ids: set[str] | list[str],
) -> dict[str, OwnershipPool]:
    """`ownership_pool_for` in bulk. THE function a coverage decision must use.

    Keyed on exactly the products `on_hand_map` returned, so the unknown-company-
    quantity refusal is unchanged: a demanded product with no `InventoryOnHand` row
    in this BU still makes the whole call raise, and customer-owned stock does NOT
    paper over that. It could not honestly do so -- the customer owning 500 says
    nothing about what the company holds, and a pass that treated the company
    quantity as 0 because the customer happened to have declared some would be the
    same confident wrong number this module exists to refuse.
    """
    company = on_hand_map(db, customer.business_unit_id, product_ids)
    if not company:
        return {}
    owned = customer_owned_map(db, customer, set(company))
    return {
        pid: OwnershipPool(
            company_owned=qty,
            customer_owned=owned[pid].drawable,
            customer_owned_known=owned[pid].known,
            customer_owned_source=owned[pid].source_system,
        )
        for pid, qty in company.items()
    }


def total_customer_owned_all_customers(
    db: Session, product: Product
) -> CustomerOwnedPosition:
    """Sum of `product`'s CUSTOMER-OWNED quantity across every customer.

    For the MRP screens ONLY, and for exactly the reason `total_on_hand_all_bus` is:
    MRP aggregates demand across every customer of every BU, so the honest
    counterpart is everything that will be drawn against that demand -- and
    customer-owned steel WILL be drawn, first. A planner who cannot see it will
    order material the customer already has.

    It is NOT a coverage figure. Coverage goes through `ownership_pool_map`, which
    is scoped to one customer, because one customer's property can never satisfy
    another's demand.

    `known=False` when NO customer has ever uploaded anything at all, which is the
    state of a database that has not used this feature. Reported unavailable rather
    than as 0 -- the same rule everywhere else.
    """
    rows = (
        db.query(CustomerOwnedInventory)
        .filter(CustomerOwnedInventory.product_id == product.id)
        .all()
    )
    if rows:
        dated = [r.uploaded_at for r in rows if r.uploaded_at is not None]
        return CustomerOwnedPosition(
            known=True,
            quantity=max(0.0, sum(max(0.0, r.quantity or 0.0) for r in rows)),
            source_system=_customer_owned_source(rows),
            uploaded_at=max(dated) if dated else None,
        )
    any_upload = db.query(CustomerOwnedInventoryUpload.id).first() is not None
    if any_upload:
        return CustomerOwnedPosition(
            known=True, quantity=0.0, source_system="declared-none"
        )
    return _UNKNOWN_CUSTOMER_OWNED
