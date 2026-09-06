"""Scenario Planning -- Phase 4.

What a scenario IS
-----------------
From the discovery workshop:

    Scenarios are not strategic forecasts. Scenarios support active customer
    conversations. "What if this demand becomes confirmed?" "What if this ROS
    moves out 30 days?" "What if mill delivery accelerates?"

So a scenario is a named, reviewable BUNDLE OF OVERRIDES against one customer's
plan, plus a lifecycle that ends either in "Applied to base plan" or in nothing
at all. It is a conversation artefact, not a second plan of record: until it is
applied, NOTHING in it affects the official coverage verdict (see
app.engines.scenario, which is a read-only projection built the same way
app.engines.sharing is).

Scenarios are SHARED
--------------------
There is deliberately no per-user ownership or visibility model. Anyone with
project access sees every scenario and may review it. `created_by` is an
attribution string for the UI ("who put this on the table"), NOT an access
control field -- no code branches on it, and it must not start to.

Lifecycle
---------
    Draft -> Review -> Discussion -> Applied

matching the spec's "Create -> Modify -> Review -> Customer Discussion -> Apply
To Base Plan". Create/Modify collapse into DRAFT because they are the same state
from the data's point of view -- a scenario being built -- and a separate
"Modified" status would carry no information the timestamps do not already give.

APPLIED IS TERMINAL AND IMMUTABLE
---------------------------------
Once applied, a scenario is the record of WHAT WAS AGREED with the customer and
what was consequently written into production data. It cannot be re-applied, and
its name, description, status and overrides cannot be changed. Enforced in
app.engines.scenario.assert_mutable and in the API layer, not merely documented.

Two reasons, both about honesty rather than tidiness:

  * Re-applying is not idempotent. Demand changes go through
    `app.engines.coverage.apply_revision`, which appends a DemandRevision and an
    ImpactRecord every time it is called. A second apply would manufacture a
    second revision recording a change of nothing, polluting the revision
    history and the Home Dashboard's "Demand Changes" card.
  * Editing an applied scenario would silently rewrite history: the row would
    then describe overrides that were never applied while still claiming
    `applied_at`. A planner reading it later could not tell what was agreed.

To revisit an agreement, create a new scenario. That leaves both the original
agreement and the revision to it visible.
"""

import enum

from sqlalchemy import (
    Column,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    String,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db import Base
from app.models.customer import _uuid


class ScenarioStatus(str, enum.Enum):
    """The spec's lifecycle, with Create/Modify collapsed into DRAFT.

    APPLIED is terminal -- see the module docstring.
    """

    DRAFT = "Draft"
    REVIEW = "Review"
    DISCUSSION = "Discussion"
    APPLIED = "Applied"


#: Statuses a scenario may still be edited in. Everything except APPLIED.
EDITABLE_SCENARIO_STATUSES = (
    ScenarioStatus.DRAFT,
    ScenarioStatus.REVIEW,
    ScenarioStatus.DISCUSSION,
)


class ScenarioTargetKind(str, enum.Enum):
    """WHAT an override points at. The discriminator of the polymorphic table.

    The three override FAMILIES the spec allows map onto five kinds because the
    supply family has three physically different targets and the demand family has
    two (the line, and the well whose status covers all of its lines):

      Demand        DEMAND_LINE           quantity / ros_date / profile
                    WELL                  demand_status for one well
      Supply        INVENTORY             on-hand quantity for (BU, product)
                    PO_ARRIVAL            arrival_date of incoming supply, OR a
                                          whole hypothetical new_order
                    ASSIGNMENT            quantity assigned to a demand line
      Substitution  SUBSTITUTION_APPROVAL approval_status for one demand line

    PO_ARRIVAL IS NOW MODELLED -- IN THE RUNOUT PROJECTION, AND NOWHERE ELSE
    ------------------------------------------------------------------------
    The spec lists PO Arrival among the allowed supply overrides, and planners do
    ask the question ("what if mill delivery accelerates?").

    This kind used to be listed in `app.engines.overrides.UNMODELLED_KINDS`. The
    reason given was:

        NO ENGINE READS INCOMING SUPPLY WHEN DECIDING COVERAGE. Coverage,
        substitution and allocation are decided from on-hand stock alone, so
        there is nothing for a PO-arrival override to change. Moving a promised
        date would move a number on a report and no verdict anywhere.

    The first sentence remains TRUE, and deliberately so -- see below. The error
    was the last one. "No verdict" was taken to mean "no consequence", but this
    platform's MRP layer projects a monthly RUNOUT BALANCE
    (`app.engines.mrp._runout_series`), and the month that balance goes negative is
    a figure planners act on: it is what the By Item screen draws, and
    `ByItemAnalysis.runout_with_recommended_order` exists precisely to show a
    hypothetical arrival shifting it. Material landing earlier or later obviously
    moves that month. So there WAS something honest for this override to change,
    and it is now changed: `app.engines.mrp.on_order_runout` folds the real
    `InventoryOnOrder` rows into the month-walk at their expected arrival dates,
    and `app.engines.scenario._supply_runout_changes` runs it twice -- promised
    dates versus overridden -- and reports the difference in
    `ScenarioImpact.supply_runout_changes`.

    WHAT IT STILL DOES NOT DO, AND WHY THAT IS NOT A GAP
      * IT MOVES NO COVERAGE VERDICT. Coverage is still decided from BU-scoped
        on-hand stock alone, and the resolver method the override flows through
        (`app.engines.overrides.OverrideResolver.arrival_date`) has no caller
        anywhere in app.engines.coverage. That is the platform's stated policy --
        `app.engines.executive.ON_ORDER_NOTE`: on-order is read BESIDE coverage
        figures, never inside them. Whether material arriving before ROS may COVER
        a line is a coverage-rule change with the product owner's name on it. That
        decision is still UNMADE, and an override is not the place to make it.
      * IT MOVES NO MRP RECOMMENDATION ROW, because those are derived from the
        coverage verdicts that did not move.
      * The Oracle feed is still not integrated (`oracle_integrated` remains
        False), so the rows a scenario reasons against may be seeded demo data.
        That is reported per row by `InventoryOnOrder.source_system` and by
        `InventoryPosition.on_order_source` rather than by refusing to compute --
        the same treatment every other on-order consumer already gives it.

    Both halves are stated in `preview`'s notes, so a planner sees which figure
    responded and which deliberately did not. The override is previewable and, like
    every other supply kind, NOT applicable: `InventoryOnOrder` is a read-only
    projection of Oracle-owned purchase orders, so `apply_to_base_plan` refuses it
    with the same wording INVENTORY and ASSIGNMENT get.

    PO_ARRIVAL CARRIES TWO FIELDS, AND THE SECOND ASSERTS A ROW THAT DOES NOT EXIST
    ------------------------------------------------------------------------------
    `arrival_date` (above) restates a FACT ABOUT A REAL ROW: the earliest promised
    arrival among this product's `InventoryOnOrder` rows, with the later ones carried
    by the same offset. It answers "what if the mill delivers earlier?"

    `new_order` answers a different question that the above could not express at all:
    "what if we placed an emergency order for 5000 metres arriving next March?" There
    was no way to say that, because every other override in the vocabulary RESTATES
    an existing value and this one ASSERTS AN EVENT. So it is its own field, taking
    BOTH `value_number` (the quantity) and `value_date` (the expected arrival) in one
    row -- the only field in the vocabulary that uses two value columns -- and
    requiring `target_business_unit_id` as well as `target_product_id`, matching the
    (BU, product) scope a real `app.models.inventory_on_order.InventoryOnOrder` row
    has.

    WHY NOT A `quantity` FIELD THAT MEANS "NEW ORDER WHEN NO REAL ROW MATCHES".
    Because that row's MEANING would depend on the state of another table at read
    time: it would say "resize the promised PO" while Oracle's row existed and
    silently become "invent a PO" the moment a sync dropped it, with no edit and no
    audit trail. The full argument, and the three structural reasons a `new_order`
    row cannot be confused with an `arrival_date` row, are in
    `app.engines.overrides.OVERRIDE_FIELDS`.

    WHAT IT DOES: `app.engines.mrp.on_order_runout` takes the quantity as
    `hypothetical_orders` and folds it into its month-walk through the SAME
    `incoming_by_month` hook the real rows use -- additively beside them, not instead
    of them -- and `app.engines.scenario._supply_runout_changes` reports the
    before/after curves. The real on-order totals stay real: the invented quantity is
    reported separately as `hypothetical_quantity` and never merged into
    `on_order_total`.

    WHAT IT DOES NOT DO: it creates NO `InventoryOnOrder` row, ever, anywhere -- there
    is no writer for one and `apply_to_base_plan` refuses the whole kind. It moves NO
    coverage verdict, for exactly the reason `arrival_date` does not: coverage is
    decided from on-hand stock alone. And it moves no MRP recommendation row, since
    those derive from those same verdicts -- what the preview reports instead is
    whether the hypothetical quantity is SIZED to cover the recommendation MRP has
    already made, which is a comparison of two figures rather than a claim that a row
    disappeared.

    ITS APPLY REFUSAL IS ITS OWN, AND STRONGER THAN THE SHARED SUPPLY ONE. The other
    supply kinds are refused because this platform holds a read-only COPY of a row
    Oracle owns. Here there is no row in either system, so "applying" could not mean
    updating a projection -- it could only mean PLACING AN ORDER. Per the spec, MRP
    here is a recommendation process and mill ordering is a human last resort, decided
    against lead times, mill slots and commercial terms this platform does not model.
    A saved what-if must not trigger it. See `app.engines.scenario.apply_blockers`,
    which appends a second, separately-worded blocker for this field.

    WHY `WELL` EXISTS AS ITS OWN KIND
    ---------------------------------
    "What if we confirmed this well?" is one of the most ordinary planning
    questions there is, and it used to be asked as a DEMAND_LINE override of
    `status`. It cannot be any more: demand status is a property of the WELL
    (`app.models.well.Well.demand_status`), and a per-line status override would
    reintroduce -- inside a scenario -- exactly the invalid state that column move
    eliminated, with the preview happily modelling a well at two statuses.

    The question was NOT dropped instead. Dropping it would have made the platform
    unable to preview the single change a planner most often wants to test, and
    "recorded but unmodelled" (the PO_ARRIVAL treatment below) would have been a
    worse answer still, since this one is perfectly modellable: a well-level status
    override resolves through
    `app.engines.overrides.ScenarioOverrides.view`, so every line of the well is
    seen at the overridden status by the ONE coverage implementation, and
    `apply_to_base_plan` writes it through
    `app.engines.coverage.set_well_demand_status`. So `WELL` is previewable AND
    applicable, and nothing in the override vocabulary became inexpressible.
    """

    DEMAND_LINE = "DemandLine"
    WELL = "Well"
    INVENTORY = "Inventory"
    PO_ARRIVAL = "PoArrival"
    ASSIGNMENT = "Assignment"
    SUBSTITUTION_APPROVAL = "SubstitutionApproval"


class Scenario(Base):
    """A named bundle of overrides against ONE customer's plan.

    Scoped to a customer because that is the scope coverage is computed at (see
    app.engines.coverage.recompute_customer): a scenario spanning two customers
    could not be previewed as one coherent coverage answer without pooling their
    inventory, which is exactly the boundary the platform does not cross.
    """

    __tablename__ = "scenarios"

    id = Column(String(36), primary_key=True, default=_uuid)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    customer_id = Column(String(36), ForeignKey("customers.id"), nullable=False)
    status = Column(
        SAEnum(ScenarioStatus), nullable=False, default=ScenarioStatus.DRAFT
    )
    # "On behalf of" text, typed by the caller. Attribution only, NOT an
    # access-control field, and NOT the record of who acted: that is
    # `created_by_user_id`, which the server fills from the authenticated user
    # (adversarial review 2026-09-06, F09) and the body cannot set.
    created_by = Column(String, nullable=True)
    # No FOREIGN KEY on purpose: this is the RECORD of who acted, and it must
    # survive that user later being removed. Resolved view-only.
    created_by_user_id = Column(String(36), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=True)
    # Set exactly once, by app.engines.scenario.apply_to_base_plan. Its presence
    # is what makes the scenario an immutable historical record.
    applied_at = Column(DateTime, nullable=True)

    created_by_user = relationship(
        "User",
        primaryjoin="foreign(Scenario.created_by_user_id) == User.id",
        viewonly=True,
    )

    @property
    def created_by_user_name(self) -> str | None:
        return self.created_by_user.display_name if self.created_by_user else None

    customer = relationship("Customer")
    overrides = relationship(
        "ScenarioOverride",
        back_populates="scenario",
        cascade="all, delete-orphan",
        order_by="ScenarioOverride.created_at",
    )


class ScenarioOverride(Base):
    """ONE overridden field on ONE target. The whole override vocabulary.

    Why ONE polymorphic table rather than four
    ------------------------------------------
    Four tables (demand / inventory / assignment / substitution overrides) were
    considered and rejected. An override is a genuinely homogeneous concept --
    "this target, this field, this new value" -- and every consumer treats it as
    one:

      * ONE resolver reads them (app.engines.overrides.OverrideResolver), which
        the coverage engine consults; four tables would become four queries
        feeding the same dict.
      * ONE pair of API endpoints adds and removes them, and ONE list renders
        them in the editor. Four tables need a UNION (or four round-trips) just
        to answer "what does this scenario change?", which is the first question
        the screen asks.
      * Ordering matters for display and would have to be merged client-side.

    The usual argument FOR splitting is integrity: a per-table schema can make
    its own FK non-nullable. That argument does not actually pay off here,
    because the field name still has to be validated against the target kind in
    code either way (`ros_date` is meaningless on an inventory row whichever
    table it lives in). So splitting buys one NOT NULL per table and costs the
    single-query, single-resolver, single-list property. Validation is therefore
    centralised in ONE place instead -- `app.engines.overrides.validate` -- which
    is stricter than any of the four schemas could be on its own since it checks
    kind, field and value type together.

    Typed value columns, not a JSON blob
    ------------------------------------
    `value_number` / `value_date` / `value_text` rather than one serialised
    column. A quantity stays a Float and a ROS date stays a DateTime in both
    sqlite and postgres, so no consumer parses strings and no consumer can
    disagree with another about the format. Exactly one of the three is
    populated; which one is determined by (target_kind, field_name).

    `target_business_unit_id` and the BU boundary
    --------------------------------------------
    Carried explicitly on INVENTORY overrides rather than inferred, so the
    Business Unit an override applies to is a stated FACT in the row instead of
    something reconstructed at read time. `app.engines.overrides.validate`
    refuses any inventory override whose BU is not the scenario customer's own
    BU. That is the data-level half of the guarantee; the other half is
    structural -- the preview only ever recomputes the scenario customer's pool,
    so no other BU's coverage reads these rows at all.
    """

    __tablename__ = "scenario_overrides"

    id = Column(String(36), primary_key=True, default=_uuid)
    scenario_id = Column(String(36), ForeignKey("scenarios.id"), nullable=False)

    target_kind = Column(SAEnum(ScenarioTargetKind), nullable=False)

    # Exactly which of these is set depends on target_kind. See
    # app.engines.overrides.validate, which is the single authority.
    target_demand_line_id = Column(
        String(36), ForeignKey("demand_lines.id"), nullable=True
    )
    # Set on a WELL override, and only there. Demand status is a well-level fact,
    # so the target of a status what-if is a well -- naming a line instead would
    # imply the scenario could confirm one line of a well and not another, which is
    # the state `app.models.well.Well.demand_status` exists to make impossible.
    target_well_id = Column(String(36), ForeignKey("wells.id"), nullable=True)
    target_product_id = Column(String(36), ForeignKey("products.id"), nullable=True)
    target_business_unit_id = Column(
        String(36), ForeignKey("business_units.id"), nullable=True
    )
    target_assignment_id = Column(
        String(36), ForeignKey("inventory_assignments.id"), nullable=True
    )
    target_approval_id = Column(
        String(36), ForeignKey("well_substitution_approvals.id"), nullable=True
    )
    # For a SUBSTITUTION_APPROVAL override the pair may be named directly, so a
    # planner can ask "what if this substitute WERE approved?" for a pair that has
    # no approval record at all yet -- which is the common case and the whole
    # point of the question.
    target_from_product_id = Column(
        String(36), ForeignKey("products.id"), nullable=True
    )
    target_to_product_id = Column(String(36), ForeignKey("products.id"), nullable=True)

    # Which field of the target is being overridden. A plain String, NOT an enum:
    # the set of legal values depends on target_kind, so a single database enum
    # would accept `ros_date` on an inventory row and thereby give assurance it
    # cannot deliver. The real check is `app.engines.overrides.validate`, which
    # sees kind, field and value together.
    field_name = Column(String, nullable=False)

    value_number = Column(Float, nullable=True)
    value_date = Column(DateTime, nullable=True)
    value_text = Column(String, nullable=True)

    note = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    scenario = relationship("Scenario", back_populates="overrides")
    target_demand_line = relationship("DemandLine")
    target_well = relationship("Well")
    target_product = relationship("Product", foreign_keys=[target_product_id])
    target_from_product = relationship("Product", foreign_keys=[target_from_product_id])
    target_to_product = relationship("Product", foreign_keys=[target_to_product_id])
