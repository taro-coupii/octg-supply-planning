import enum
import uuid

from sqlalchemy import Column, ForeignKey, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import relationship

from app.db import Base


class AllocationPolicy(str, enum.Enum):
    SOFT = "soft"
    HARD = "hard"
    HYBRID = "hybrid"


def _uuid() -> str:
    return str(uuid.uuid4())


class Customer(Base):
    """A customer, sitting one level below Business Unit in the hierarchy.

    Hierarchy:  Business Unit -> Customer -> PlanningNode(s) -> Well -> DemandLine

    Both of the top two levels are inventory boundaries, but they are not the
    same KIND of boundary:

      Business Unit  ALWAYS separates inventory, and is never crossed. On-hand
                     quantity is held per (BU, product) -- see
                     app.models.inventory_on_hand.InventoryOnHand. No code path
                     may let one BU's stock affect another BU's coverage.
      Customer       Separates inventory BY DEFAULT. `recompute_customer` pools a
                     customer's own wells and stops there, so customer B's demand
                     never moves customer A's official verdict. Planners may ask,
                     as a read-only WHAT-IF, whether uncovered demand could be met
                     from another customer's surplus WITHIN the same BU -- that is
                     the pool is divided across every customer of the BU
                     together, earliest need first (D01).

    `business_unit_id` is NULLABLE, and NULL has one precise meaning: "not yet
    mapped to a Business Unit". It does NOT mean "shares with everything".

    An unmapped customer has NO INVENTORY POOL, so coverage FAILS LOUDLY
    -------------------------------------------------------------------
    This used to be a middle state: an unmapped customer was "isolated" and read
    the legacy unscoped `Product.on_hand_qty`, which let it be judged against a
    global scalar. That column is gone (see
    app.models.inventory_on_hand.InventoryOnHand), and with it the middle state.
    On-hand inventory exists only per (Business Unit, product), so a customer
    outside every BU has nothing to be judged against:

      * inventory resolution raises `InventoryScopeMissing`
        (app.engines.inventory) -- no quantity, no coverage verdict, no MRP figure;
      * the HARD/HYBRID assignment netting raises for the same reason, since
        "the customer alone" was a scope invented to make the legacy scalar usable;
      * the cross-customer sharing analysis still returns an empty, EXPLAINED
        result rather than raising -- it is a read-only what-if whose honest answer
        is "there is no BU to share within", and saying so is more useful than a
        stack trace.

    Half-working was the worse outcome: a screen showing a confident Covered badge
    computed from another BU's steel is harder to notice than an error telling the
    operator to map the customer.

    The nullable FK was chosen over auto-creating a shared "Default BU" precisely
    because a shared default is the PERMISSIVE failure mode: every unmapped
    customer would land in the same bucket and would then be a legitimate sharing
    partner for every other unmapped customer. Refusal is the conservative failure
    mode, so that is what absence buys you.

    The column is deliberately still NULLABLE. The failure is enforced at the
    POINT OF USE, loudly, rather than by a schema constraint -- so an operator can
    still create a customer, see exactly which screens refuse to compute, and fix
    the mapping. Making it NOT NULL is a defensible follow-up, not this change.
    """

    __tablename__ = "customers"

    id = Column(String(36), primary_key=True, default=_uuid)
    name = Column(String, nullable=False)
    # See the class docstring: NULL means "unmapped", which has no inventory pool
    # at all -- coverage refuses to compute rather than guessing a quantity.
    business_unit_id = Column(
        String(36), ForeignKey("business_units.id"), nullable=True
    )
    allocation_policy = Column(
        SAEnum(AllocationPolicy), nullable=False, default=AllocationPolicy.SOFT
    )

    business_unit = relationship("BusinessUnit")
