from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db import Base
from app.models.customer import _uuid


class CustomerOwnedInventory(Base):
    """Stock of ONE product that the CUSTOMER owns, held for ONE customer.

    THE SYSTEM BOUNDARY IS THE OPPOSITE WAY ROUND FROM EVERY OTHER INVENTORY TABLE
    -----------------------------------------------------------------------------
    READ THIS FIRST, because a reader who has just come from
    `app.models.inventory_on_hand.InventoryOnHand`,
    `app.models.inventory_on_order.InventoryOnOrder` or
    `app.models.inventory_assignment.InventoryAssignment` will assume the wrong
    thing, and the assumption is load-bearing.

    Those three are READ-ONLY LOCAL PROJECTIONS of Oracle-owned data. They expose
    no mutation endpoint, on purpose: Oracle owns the fact and a platform that let
    a planner edit its own copy would be claiming ownership of something it does
    not own.

    This table is not that. The product owner's statement is explicit --
    "what is in Oracle is strictly our own company's inventory": Oracle holds only OUR
    OWN company's inventory. Customer-owned material is ABSENT FROM ORACLE
    ENTIRELY. There is no feed to project, no upstream row to defer to, and no
    other system that could be asked. It arrives by a human uploading a
    spreadsheet into THIS platform, so THIS PLATFORM OWNS IT, and it therefore
    legitimately gets real write endpoints (`POST /customer-owned-inventory`, see
    `app.api.customer_owned_inventory`).

    The practical consequence, spelled out because it is the one a future reader
    will get backwards: `downgrade()` of the migration that creates this table
    DESTROYS DATA THAT HAS NO OTHER SOURCE. Dropping `inventory_on_order` loses
    nothing -- re-run the feed. Dropping this loses the customer's declared
    position until somebody finds the spreadsheet again.

    SCOPE IS (customer, product) -- AND THE BUSINESS UNIT IS DELIBERATELY NOT HERE
    -----------------------------------------------------------------------------
    A customer belongs to exactly one Business Unit
    (`app.models.customer.Customer.business_unit_id`), so the BU is DERIVABLE and
    is deliberately not denormalised onto this row. Two reasons, and the second is
    the decisive one:

      * The fact this row states is "customer C owns Q of product P". The BU is a
        property of C, not of the ownership. On `InventoryOnHand` the BU IS the
        fact -- it is which shelf the steel stands on -- which is exactly why the
        BU is a column there and the uniqueness constraint is on the pair.
      * IF A CUSTOMER IS EVER REMAPPED to another Business Unit, a denormalised
        `business_unit_id` here goes stale the instant the remap is written, and
        there is no defensible resolution of the disagreement: a row saying "BU
        Gulf" under a customer saying "BU North" describes steel in a BU that has
        no claim on it, and either reading is a guess. Deriving it means the two
        can never disagree, because there is only one of them. Customer-owned
        steel MOVES WITH ITS OWNER; company steel does not move at all.

    So the unique constraint is on (customer_id, product_id): one declared
    position per customer per product, which is what makes an upload a REPLACEMENT
    rather than an append (see `app.engines.customer_owned_import`).

    "NONE OWNED" IS DISTINGUISHABLE FROM "NO UPLOAD HAS HAPPENED"
    ------------------------------------------------------------
    Same tension `InventoryOnOrder` resolved, resolved the same way and for the
    same reason. Absence must not be read as zero -- but it also must not RAISE,
    the way a missing `InventoryOnHand` row does.

    Why not raise: `on_hand_for` raises because coverage cannot be decided without
    the company quantity, so every partial verdict would still be a verdict
    computed from an invented number. Customer-owned stock is different in kind.
    MOST PRODUCTS LEGITIMATELY HAVE NONE, and a customer who has never uploaded
    anything owns none rather than an unknown amount -- demanding a row per
    (customer, product) pair would mean seeding tens of thousands of rows whose
    only content is "no", and raising would convert the ordinary state of the
    world into an outage of coverage for every customer who has not uploaded yet.

    So the two states are kept apart by the UPLOAD RECORD, not by a sentinel
    quantity:

      no `CustomerOwnedInventoryUpload` row for this customer
          The platform holds NO customer-owned data about that customer.
          `app.engines.inventory.customer_owned_for` reports
          `known=False, quantity=None`, every consumer renders it unavailable, and
          allocation draws nothing (which is arithmetically the same as zero and
          semantically not the same claim, which is why the flag exists).

      an upload exists, and no row for this product
          The customer DECLARED its position and this product was not in it, so it
          owns none. A measured fact: `known=True, quantity=0.0`.

      an explicit `quantity = 0` row
          The same fact, stated by name in the spreadsheet. Both spellings mean
          "none owned", exactly as an explicit 0 on-hand row says "this BU holds
          none of it".

    IT CAN NEVER BE OFFERED TO ANOTHER CUSTOMER
    ------------------------------------------
    A boundary TIGHTER than the Business Unit boundary, and the one wall that
    survived the pool being shared across the BU (D01). The surplus report
    answers "could another customer's surplus in this BU cover my gap"; this
    material is that customer's PROPERTY, so it is excluded from `shareable`
    entirely and in both directions. That exclusion is structural -- the sharing
    module computes surplus from `on_hand_map` alone, which reads
    `InventoryOnHand` and nothing else -- and it is asserted there rather than
    merely commented.
    """

    __tablename__ = "customer_owned_inventory"
    __table_args__ = (
        UniqueConstraint(
            "customer_id",
            "product_id",
            name="uq_customer_owned_inventory_customer_product",
        ),
    )

    id = Column(String(36), primary_key=True, default=_uuid)
    customer_id = Column(String(36), ForeignKey("customers.id"), nullable=False)
    product_id = Column(String(36), ForeignKey("products.id"), nullable=False)
    #: Quantity the customer owns, in the product's own unit
    #: (`app.models.product.Product.unit_of_measure` -- there is no second unit for
    #: customer-owned steel; it is the same SKU). An explicit 0 states "none
    #: owned", which is a fact.
    quantity = Column(Float, nullable=False, default=0)

    #: Provenance. NOT the same vocabulary as the Oracle projections, and
    #: deliberately so: "oracle" is not a legal value here, because Oracle does not
    #: hold this data and a row claiming it did would be false by construction. The
    #: values in use are "customer-upload" (a real spreadsheet a human produced) and
    #: "synthetic" (seeded demo data).
    source_system = Column(String, nullable=False, default="customer-upload")
    #: The filename of the spreadsheet this figure came from, when known. A planner
    #: reading a surprising number needs to be able to go and look at the file.
    source_reference = Column(String, nullable=True)
    #: WHEN the spreadsheet was uploaded. Named `uploaded_at` rather than
    #: `synced_at` because nothing is being synced: there is no upstream system to
    #: be in step with. Its AGE is the whole point -- this data is as current as the
    #: last time somebody sent a file, which can be months, and a planner deciding
    #: on it has to be able to see that.
    uploaded_at = Column(DateTime, nullable=True)
    #: The upload that last wrote this row, so a row can be traced to its batch.
    upload_id = Column(
        String(36), ForeignKey("customer_owned_inventory_uploads.id"), nullable=True
    )

    customer = relationship("Customer")
    product = relationship("Product")
    upload = relationship("CustomerOwnedInventoryUpload")


class CustomerOwnedInventoryUpload(Base):
    """One accepted spreadsheet of customer-owned inventory, for ONE customer.

    Two jobs, and the first is not bookkeeping.

    1. IT IS THE FACT "AN UPLOAD HAS HAPPENED"
       Without it, "this customer owns none of product P" and "nobody has ever told
       us what this customer owns" are the same absence, and `on_hand_map`'s own
       lesson is that an absence rendered as a number is how a platform states a
       claim its data does not support. The existence of a row here is what makes
       `known=True` legal for a product with no `CustomerOwnedInventory` row. It is
       recorded rather than derived from "does the customer have any inventory row
       at all" because a customer whose declared position is genuinely all zeros
       would otherwise be indistinguishable from one that never uploaded -- and
       that customer is precisely the one a planner must not be told "no data" about.

    2. It is the upload's own audit trail: which file, when, how many rows were
       accepted and how many were rejected. Rejected rows do NOT block the batch
       (see `app.engines.customer_owned_import`), so the counts are how a user finds
       out that some of their file did not land.

    NO STAGING, NO PER-ROW DECISION TABLE -- and that is a deliberate difference
    from `app.models.demand_import`. A demand import must ask the user "is this a
    revision of existing demand or new demand?", because both readings are
    plausible and the wrong one rewrites history. An inventory upload has no such
    question: a position REPLACES the previous position for that (customer,
    product), which is the only thing a stock count can mean. See
    `app.engines.customer_owned_import` for the full reasoning.
    """

    __tablename__ = "customer_owned_inventory_uploads"

    id = Column(String(36), primary_key=True, default=_uuid)
    customer_id = Column(String(36), ForeignKey("customers.id"), nullable=False)
    filename = Column(String, nullable=True)
    sheet_name = Column(String, nullable=True)
    #: Rows the file contained, rows that produced a position, and rows refused
    #: with a per-row error. `row_count == applied_count + error_count` always.
    row_count = Column(Integer, nullable=False, default=0)
    applied_count = Column(Integer, nullable=False, default=0)
    error_count = Column(Integer, nullable=False, default=0)
    #: Positions this upload REPLACED (a row already existed for the pair) versus
    #: created. Reported because replacement is the one thing an inventory upload
    #: does that a user should be shown rather than left to infer -- their previous
    #: declared quantity is gone.
    replaced_count = Column(Integer, nullable=False, default=0)
    created_count = Column(Integer, nullable=False, default=0)
    uploaded_at = Column(DateTime, nullable=False, server_default=func.now())
    source_system = Column(String, nullable=False, default="customer-upload")

    customer = relationship("Customer")
