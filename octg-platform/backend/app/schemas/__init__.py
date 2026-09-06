from datetime import date, datetime

from pydantic import BaseModel

from app.quantities import Quantity
from app.models import (
    AllocationPolicy,
    DemandProfile,
    DemandStatus,
    SubstitutionApprovalStatus,
    UnitOfMeasure,
)


# --------------------------------------------------------------------------
# UNIT OF MEASURE -- the rule every payload below follows
#
# Every field carrying a quantity has a UNIT reachable from the same payload.
# That is the whole point: a number the UI cannot label is the defect this rule
# exists to fix, so "has a quantity but no reachable unit" is treated as an
# incomplete payload rather than a cosmetic gap. `tests/test_units_of_measure.py`
# walks these models and enforces it, so a new quantity field added without a unit
# fails the suite rather than reaching a screen.
#
# "Reachable" is deliberately weaker than "present on this model". A payload about
# ONE product may carry the unit once for the whole payload (`ByItemAnalysisOut`),
# or reach it through a nested `ProductOut` (`SubstitutionCandidateOut`); a payload
# with one row per product carries it per row. What is never acceptable is a
# quantity with no unit anywhere above it.
#
# CROSS-PRODUCT AGGREGATES DO NOT GET A SCALAR UNIT
# ------------------------------------------------
# Where a figure sums quantities across DIFFERENT products -- the Executive
# Dashboard's demand totals, its unrecoverable total, its allocation split -- there
# may be no single unit, and adding metres to tonnes is arithmetic nobody asked
# for. Those payloads therefore report `quantities_by_unit`, one entry per unit
# actually present, and set the scalar `unit_of_measure` only when every
# contributing product genuinely shares one unit. When they do not, the scalar is
# null and a note says so. See `QuantityByUnitOut`.
# --------------------------------------------------------------------------


class QuantityByUnitOut(BaseModel):
    """One unit's share of an aggregate that spans several products.

    The honest form of a cross-product total. `unit_of_measure` is a
    `UnitOfMeasure` value ("Mtr" | "PC" | "MT") and `quantity` is the sum of ONLY
    the quantities in that unit, so the entries are never added together.

    A single-entry list is the common case and is not a special one: it means every
    contributing product shares one unit, which is exactly when the neighbouring
    scalar total is safe to render.
    """

    unit_of_measure: UnitOfMeasure
    quantity: float

    class Config:
        from_attributes = True


class BusinessUnitOut(BaseModel):
    """Top of the hierarchy. A BU is a HARD inventory boundary -- never crossed."""

    id: str
    name: str

    class Config:
        from_attributes = True


class CustomerOut(BaseModel):
    """Read-only customer row; `allocation_policy` drives how the coverage
    engine allocates inventory for this customer's wells.

    `business_unit_id` is the customer's inventory boundary. NULL means "not yet
    mapped to a Business Unit", which the platform treats as ISOLATED (never as
    "shares with everything") -- see app.models.customer.Customer.
    """

    id: str
    name: str
    business_unit_id: str | None = None
    allocation_policy: AllocationPolicy

    class Config:
        from_attributes = True


class ProductOut(BaseModel):
    """Catalogue identity of a product. Carries NO quantity, deliberately.

    `on_hand_qty` used to be here, sourced from the global `Product.on_hand_qty`
    column. Both are gone. The field was reachable through
    `SubstitutionCandidateOut.product`, where it served no purpose:
    `SubstitutionCandidate.available_qty` is the number that actually drives the
    substitution decision, and it is BU-scoped. Keeping a second, BU-blind
    quantity next to it was the leak with a UI in front of it.

    Any future quantity on a product payload must be BU-qualified in its own name
    (e.g. `bu_on_hand_qty` alongside the BU it belongs to), never a bare total.
    """

    id: str
    type: str
    size: str
    grade: str
    connection: str
    description: str | None
    # The unit EVERY quantity of this product is counted in. Present on the
    # catalogue payload because it is a catalogue fact (see
    # `app.models.product.Product`), which is also what makes it reachable from
    # `SubstitutionCandidateOut.product` without duplicating it there.
    unit_of_measure: UnitOfMeasure

    class Config:
        from_attributes = True


class DemandLineOut(BaseModel):
    """One demand line of a well, as served by GET /wells/{id}.

    `product_description` is here because its absence was a reported defect: this
    payload carried `product_id` and nothing else about the product, so the Well
    Workspace had no name to render and printed the raw UUID
    ("d54b3bb2-db1b-414a-ae06-0ebdb79b00c4") in the product column. A UUID is a
    join key, not a description. It falls back to `product_id` only when the
    catalogue row genuinely has no description -- so the column is always
    populated, and the fallback is visibly an identifier rather than a blank.

    `unit_of_measure` labels `quantity`. See the module header.

    `status` is the WELL's demand status, not the line's. It has no line-level
    column any more (`app.models.well.Well.demand_status` is the authority), and it
    is repeated on every row of a well by construction -- served here because the
    Well Workspace renders a demand table and an absent status column would read as
    "unknown". To CHANGE it, call `PUT /wells/{well_id}/demand-status`; sending it
    to `POST /demand-lines/{id}/revisions` is refused with 400.
    """

    id: str
    product_id: str
    product_description: str
    quantity: float
    unit_of_measure: UnitOfMeasure
    ros_date: datetime
    status: DemandStatus
    profile: DemandProfile
    current_revision_no: int
    coverage_status: str | None = None
    coverage_reason: str | None = None

    class Config:
        from_attributes = True


class WellOut(BaseModel):
    """`customer_id` is here so a well page can reach anything scoped by customer
    -- notably a scenario preview -- without first pulling the
    whole coverage grid to find out who owns the well. `planning_node_path` is the
    same breadcrumb the coverage grid serves, so the two screens agree."""

    id: str
    name: str
    #: PLANNER-SET INPUT: how firm this well's programme is. Not derived from
    #: anything -- contrast `coverage_status`, which is the engine's OUTPUT. See
    #: `app.models.well.Well` for why the two must not be read as a pair.
    demand_status: DemandStatus
    coverage_status: str | None
    customer_id: str | None
    customer_name: str | None
    planning_node_path: str
    # See `WellDatesMixin` for what these two dates mean. Computed per request from
    # the in-scope demand lines and their coverage verdicts -- never stored.
    earliest_ros_date: datetime | None = None
    first_runout_date: datetime | None = None
    demand_lines: list[DemandLineOut] = []

    class Config:
        from_attributes = True


class WellSummary(BaseModel):
    """A well in a list -- notably the Home Dashboard's uncovered-wells card.

    The two dates were added because the card was unactionable without them: a
    list of uncovered well names says nothing about WHEN each one bites, so a well
    needed next month and one needed in two years looked identical.

      earliest_ros_date  Earliest ROS across this well's IN-SCOPE demand lines,
                         i.e. when the well first needs steel at all. Null only
                         when the well has no in-scope demand, which is the same
                         condition that makes `coverage_status` null.
      first_runout_date  The earliest ROS among the in-scope lines coverage did
                         NOT satisfy -- "this well has a problem from date X".
                         NULL means no shortage, and is an answer rather than a
                         gap; there is deliberately no sentinel date, because a
                         far-future placeholder sorts and renders like a fact.
                         `app.engines.well_dates` documents the definition in full,
                         including the projection-based reading it rejects and why.

    Lists of these arrive SORTED BY `earliest_ros_date` ASCENDING from the server
    (`app.engines.well_dates.sort_by_earliest_ros`), so every consumer shows the
    same order. Do not re-sort client-side to a different rule.
    """

    id: str
    name: str
    #: The owning customer, via the planning node. Nullable: a well whose node
    #: (or node's customer) is missing states so rather than inventing one.
    #: Cross-customer lists (Home, /wells) are unreadable without this.
    customer_id: str | None = None
    customer_name: str | None = None
    #: Planner-set demand status. See `WellOut.demand_status`.
    demand_status: DemandStatus
    coverage_status: str | None
    earliest_ros_date: datetime | None = None
    first_runout_date: datetime | None = None

    class Config:
        from_attributes = True


class DemandRevisionIn(BaseModel):
    """Revise ONE demand line: quantity, ROS date and profile.

    `status` IS ACCEPTED HERE AND THEN REFUSED WITH 400
    --------------------------------------------------
    Demand status is a property of the well, so a line-level revision has nothing
    honest to do with one -- see `app.api.demand.create_revision`, which rejects it
    and names `PUT /wells/{well_id}/demand-status` in the message.

    It is kept in the schema ON PURPOSE rather than deleted. Pydantic ignores
    unknown fields by default, so removing it would make an older client's status
    silently disappear into a 200 response -- the planner would believe the well
    had been confirmed. A declared-then-rejected field turns that into a loud,
    self-explaining 400. `extra = "forbid"` would also fail the request, but with a
    generic validation error that could not point anywhere useful.
    """

    #: Finite, bounded, non-negative at the schema; > 0 and whole-for-PC/JT are
    #: checked by the handler once the line's product (and so its unit) is known.
    quantity: Quantity
    ros_date: datetime
    profile: DemandProfile
    #: Always refused. Present so it can be refused explicitly. See above.
    status: DemandStatus | None = None


class SubstitutionCandidateOut(BaseModel):
    demand_line_id: str
    from_product_id: str
    #: What the line ORIGINALLY asked for, in the words a planner uses for it.
    #: A property of the LINE rather than of any one candidate, so it is stamped
    #: on every row by `app.api.substitution` in the same pass as
    #: `approval_by_date` -- the engine's candidate carries a full `Product` for
    #: the `to` side only. Before it existed the screen fell back to the first
    #: eight characters of the from-product UUID, which is precisely the thing
    #: this platform tells itself never to show a human: an id is not a name,
    #: and "88b8370f…" cannot be checked against a pipe tally.
    from_product_description: str | None = None
    to_product_id: str
    product: ProductOut
    customer_allowed: bool
    approval_status: SubstitutionApprovalStatus | None
    approval_id: str | None
    available_qty: float
    required_qty: float
    usable: bool
    blocking_layer: str | None
    # Quantity that physically exists but is hard-assigned to another demand line,
    # so it is excluded from `available_qty`. Non-zero together with
    # blocking_layer == "hard-assigned-elsewhere" means the material is on the
    # shelf and only Oracle can free it.
    hard_assigned_qty: float = 0.0
    # The step that would clear `blocking_layer`, in words a planner can act on.
    recommended_action: str | None = None
    # Pool-wide PendingApproval load on this substitute. `over_subscription_note`
    # is the sentence to render; it is non-null only when approving every pending
    # line could not all succeed.
    pending_line_count: int = 0
    pending_required_qty: float = 0.0
    over_subscribed: bool = False
    over_subscription_note: str | None = None
    # The line's approval-by date -- the latest date a rejection could still be
    # recovered by a mill order of the line's OWN (primary) product. It is a
    # property of the DEMAND LINE, not of this particular candidate, so it is
    # repeated identically across every row of the list -- exactly the pattern
    # `pending_line_count` etc. already follow for pool-wide facts a single
    # candidate cannot derive on its own. See
    # `app.engines.substitution.approval_by_date`; `available=False` means do
    # not render `approval_by_date`, render `approval_by_date_reason` instead.
    approval_by_date_available: bool = False
    approval_by_date: date | None = None
    # False means mill recovery is ALREADY IMPOSSIBLE (not merely narrowing) --
    # give this a more urgent treatment than the ordinary "reject by DATE" case.
    # Always None when `approval_by_date_available` is False.
    still_recoverable: bool | None = None
    approval_by_date_reason: str | None = None

    class Config:
        from_attributes = True


class SubstitutionApprovalIn(BaseModel):
    from_product_id: str
    to_product_id: str


class SubstitutionDecisionIn(BaseModel):
    approved: bool


class SubstitutionApprovalOut(BaseModel):
    id: str
    demand_line_id: str
    from_product_id: str
    to_product_id: str
    status: SubstitutionApprovalStatus
    requested_at: datetime
    decided_at: datetime | None
    #: Server-recorded actors (F09); never settable from a request body.
    requested_by_user_id: str | None = None
    requested_by_user_name: str | None = None
    decided_by_user_id: str | None = None
    decided_by_user_name: str | None = None

    class Config:
        from_attributes = True


class ImpactRecordOut(BaseModel):
    """A before/after snapshot of one demand revision -- the Home Dashboard's
    "Demand Changes" card.

    `product_description` and `unit_of_measure` are served alongside the two
    quantities because this card is where a planner first SEES a change, and
    "5000 -> 9000" against an unnamed product in an unstated unit is the same defect
    the owner reported on the Well Workspace. Both are read through the impact
    record's demand line to its product.

    One unit for both figures: a revision can restate a quantity but never the
    product a line is for, so before and after are always in the same unit.

    Both are nullable because an ImpactRecord OUTLIVES its demand line in one
    direction -- the FK has no cascade -- so a historical record whose line has since
    been deleted has no product to read. Null there is honest; a placeholder unit
    would label the two numbers with a guess.
    """

    id: str
    demand_line_id: str
    well_id: str
    #: Named so the cross-customer "Demand Changes" card can say WHOSE demand
    #: moved. Nullable like the product: an ImpactRecord can outlive its line.
    well_name: str | None = None
    customer_name: str | None = None
    product_description: str | None = None
    unit_of_measure: UnitOfMeasure | None = None
    quantity_before: float | None
    quantity_after: float | None
    ros_date_before: datetime | None
    ros_date_after: datetime | None
    status_before: str | None
    status_after: str | None
    coverage_before: str | None
    coverage_after: str | None
    created_at: datetime

    class Config:
        from_attributes = True


class PendingApprovalCardOut(BaseModel):
    """One row of the Home Dashboard's "Pending Approvals" card.

    `approval_id` / `substitute_description` are nullable: a coverage row can be
    PendingApproval while its WellSubstitutionApproval has just been decided in
    another tab (the engine recomputes on decision, but this card may render a
    snapshot taken in between). The card's buttons disable without an id rather
    than guessing one.
    """

    well_id: str
    well_name: str
    customer_name: str | None = None
    demand_line_id: str
    product_description: str
    quantity: float
    unit_of_measure: UnitOfMeasure
    ros_date: str
    approval_id: str | None = None
    substitute_description: str | None = None


class HomeDashboardOut(BaseModel):
    """GET /dashboard/home -- the three Home cards, typed.

    Previously served as a raw dict, the one JSON-bearing endpoint without a
    response_model; typed 2026-08-12 so the units-of-measure walk
    (tests/test_units_of_measure.py) covers its quantities too.
    """

    demand_changes: list[ImpactRecordOut] = []
    uncovered_wells: list[WellSummary] = []
    pending_approvals: list[PendingApprovalCardOut] = []


class LeadTimeComponentMatchOut(BaseModel):
    """One term of a product's attribute-based lead time.

    `attribute_value` is the value stored on the component row -- "*" when it is
    the SHARED row that applies to every product (that is how one Logistics /
    sailing allowance is stored once instead of duplicated per grade).
    `matched_on` is the product's own key on that dimension, so the UI can render
    "OD/WT 4-1/2 12.6 -> 4.5 months" or "Logistics (shared) -> 2 months".
    """

    dimension: str
    attribute_value: str
    matched_on: str
    months: float
    shared: bool
    label: str | None = None
    component_id: str | None = None

    class Config:
        from_attributes = True


class LeadTimeBreakdownOut(BaseModel):
    """Why a lead time is what it is -- the explainable form of the scalar.

    `total_months` is 0 whenever `modelled` is False, which covers BOTH "no
    components match" and "an incomplete set matches". In that case
    `missing_dimensions` names what to configure and `matched_months` says how far
    the model got -- and `matched_months` must NOT be rendered as a lead time,
    because a partial sum is a confidently wrong date (see
    app.engines.lead_time)."""

    product_id: str
    components: list[LeadTimeComponentMatchOut] = []
    total_months: float
    modelled: bool
    missing_dimensions: list[str] = []
    matched_months: float = 0.0
    transit_months: float = 0.0
    note: str = ""

    class Config:
        from_attributes = True


class MrpRecommendationOut(BaseModel):
    """MRP Layer 1 row -- one product needing procurement.

    `lead_time` explains `lead_time_months` and therefore
    `recommended_order_date`. Both come from one resolution, so the breakdown can
    never explain a different number than the one shown."""

    product_id: str
    product_description: str | None
    quantity: float
    # `quantity` is one product's aggregated shortfall, so ONE unit is always
    # correct here -- this row is never a cross-product total (the grouping key is
    # (product, recoverability), see `app.engines.mrp._recommendations_for_lines`).
    unit_of_measure: UnitOfMeasure
    ros_date: datetime
    required_ship_date: date
    recommended_order_date: date
    lead_time_months: float
    unrecoverable: bool
    reason: str
    demand_line_ids: list[str] = []
    lead_time: LeadTimeBreakdownOut | None = None
    #: The breakdown behind `quantity` (F04): demand less the three draws is the
    #: net shortfall. `whole_line_ids` are lines counted at their whole quantity
    #: because their verdict carries no net figures yet.
    demand_quantity: float = 0.0
    drawn_customer_owned: float = 0.0
    drawn_company: float = 0.0
    drawn_substitute: float = 0.0
    whole_line_ids: list[str] = []

    class Config:
        from_attributes = True


class ByItemDemandLineOut(BaseModel):
    """`product_id` / `product_description` are the LINE's own product, which may
    differ from the product whose By Item page this is -- coverage charges a line
    to whichever product satisfied it, so a substitute's page legitimately lists
    lines that ordered something else. Render them as a column; assuming every row
    ordered the page's product is wrong precisely where it matters."""

    demand_line_id: str
    well_id: str
    well_name: str
    customer_name: str | None = None
    profile: str
    quantity: float
    # The LINE's own product's unit, matching `product_id` beside it. It can
    # legitimately differ from the unit of the product whose By Item page this is
    # -- a substituted line is listed on the substitute's page -- which is another
    # reason the unit is per row here rather than once for the page.
    unit_of_measure: UnitOfMeasure
    ros_date: datetime
    coverage_status: str | None
    coverage_reason: str | None
    product_id: str
    product_description: str | None

    class Config:
        from_attributes = True


class InventoryPositionOut(BaseModel):
    """`on_hand` is the ALL-BUSINESS-UNIT total held of this product, because the
    MRP screens themselves are system-wide (see `app.engines.mrp
    .InventoryPosition`). It is a procurement figure, not a coverage figure, and
    must not be shown as "what is available to customer X".

    `assigned` is read from the local projection of Oracle's inventory
    assignments and is a carve-out of `on_hand`. `assigned_source` says where it
    came from ("unavailable" | "synthetic" | "oracle" | "mixed").

    `on_order` NOW HAS A LOCAL SOURCE and is no longer a hardcoded 0. It is
    resolved from `app.models.inventory_on_order.InventoryOnOrder`, the read-only
    projection of Oracle-owned purchase orders, and it is NULLABLE: null means
    UNKNOWN (no projected row for this product in any Business Unit), while 0.0
    means measured and nothing is on order. Those are different facts and are never
    conflated -- render null as "no data", not as zero.

    `on_order_source` carries this figure's provenance in the same vocabulary as
    `assigned_source`, and `oracle_integrated` is UNCHANGED at false: it means "the
    Oracle feed is live", which seeding a projection table does not make true. The
    flags are independent on purpose; collapsing them would force a lie in one
    direction or the other.

    `on_order_undated` is the part of `on_order` on rows with no promised arrival
    date -- a raised but unacknowledged purchase order. It is reported separately
    rather than dated to today, which would place unscheduled steel inside every
    arrival horizon."""

    on_hand: float
    assigned: float
    on_order: float | None = None
    oracle_integrated: bool
    assigned_source: str = "unavailable"
    on_order_source: str = "unavailable"
    on_order_earliest_arrival: date | None = None
    on_order_latest_arrival: date | None = None
    on_order_undated: float = 0.0
    # Booking-stage split of on_order (PO'ed / Booked / feed-did-not-state)
    on_order_poed: float = 0.0
    on_order_booked: float = 0.0
    on_order_unstated: float = 0.0
    #: CUSTOMER-OWNED stock of this product, summed across every customer. NOT part
    #: of `on_hand`, which is company-owned only: this material belongs to the
    #: customer, will be consumed against that customer's own demand FIRST (see
    #: `app.engines.allocation`), and can never be redirected to anybody else. A
    #: planner must see it because it reduces what has to be ordered -- and must see
    #: it SEPARATELY because we may not promise it to a third party.
    #:
    #: NULLABLE with the same meaning as `on_order`: null is UNKNOWN (no customer has
    #: ever uploaded a position), 0.0 is measured and no customer owns any. Render
    #: null as "no data", never as zero. `customer_owned_source` carries provenance in
    #: its own vocabulary -- "customer-upload" | "synthetic" | "mixed" |
    #: "declared-none" | "unavailable". "oracle" is not among them, because Oracle
    #: does not hold this data at all.
    customer_owned: float | None = None
    customer_owned_source: str = "unavailable"
    # Every figure on this model is a quantity of ONE product, so a single unit
    # labels all of them. Summing across Business Units -- which `on_hand` does --
    # is dimensionally safe for exactly the reason the class docstring gives it is
    # safe at all: it is the same SKU in every BU, and a SKU has one unit
    # (`app.models.product.Product`). The BU boundary is a scope question, not a
    # units question.
    unit_of_measure: UnitOfMeasure

    class Config:
        from_attributes = True


class RunoutPointOut(BaseModel):
    """One month of the runout projection. Every balance is in `unit_of_measure`.

    Carried per point rather than only once on the parent so a chart series can be
    axis-labelled from the data it plots, without the caller having to reach back up
    to `ByItemAnalysisOut`. It is the same value on every point of a given series --
    all three figures are quantities of the one product being analysed.
    """

    month: str
    opening_balance: float
    demand: float
    closing_balance: float
    # Profile split (Mtr vs Mtr-Cont): primary + contingency == demand
    demand_primary: float = 0.0
    demand_contingency: float = 0.0
    # Ownership split (Cust Balance / Owned Balance): customer-owned depletes
    # first; once the total goes negative the deficit lives in closing_company
    closing_customer_owned: float = 0.0
    closing_company: float = 0.0
    # Opening split, before this month's demand draws (incoming is company
    # steel, so the customer tier carries over from last month's close)
    opening_customer_owned: float = 0.0
    opening_company: float = 0.0
    # Incoming split by CERTAINTY: on_order = real InventoryOnOrder promised
    # this month; recommended = hypothetical (the engine's suggestion, or a
    # scenario's what-if order). Never blended -- a suggestion is not a promise.
    incoming_on_order: float = 0.0
    incoming_recommended: float = 0.0
    unit_of_measure: UnitOfMeasure

    class Config:
        from_attributes = True


class ByItemAnalysisOut(BaseModel):
    """MRP Layer 2 -- the modern 'By Item' sheet for one product."""

    product_id: str
    product_description: str | None
    # The analysed product's unit, once for the whole page. Every quantity directly
    # on this payload -- the inventory position, the runout balances, the
    # recommendation quantities -- is in it. The per-row units inside
    # `demand_lines` are the LINE's own product's and can differ; see
    # `ByItemDemandLineOut`.
    unit_of_measure: UnitOfMeasure
    inventory: InventoryPositionOut
    demand_lines: list[ByItemDemandLineOut] = []
    runout: list[RunoutPointOut] = []
    runout_month: str | None
    # SECOND, ADDITIVE series assuming this product's own RECOVERABLE
    # recommended order is placed and arrives -- see
    # `app.engines.mrp.ByItemAnalysis` for the arrival-date derivation and why
    # only the recoverable quantity is injected. Never merged into `runout`.
    runout_with_recommended_order: list[RunoutPointOut] = []
    runout_month_with_recommended_order: str | None = None
    recommendation: MrpRecommendationOut | None
    recommendations: list[MrpRecommendationOut] = []
    # THE MONTHLY LEDGER (2026-08-12 rework): opening (ownership-split) + real
    # on-order arrivals + the recommended order's arrival (separate columns:
    # promise vs suggestion) - demand = ending (ownership-split). Kept beside
    # the question-specific curves above, never replacing them.
    ledger: list[RunoutPointOut] = []
    ledger_runout_month: str | None = None
    # Undated on-order quantity that has no month to land in -- reported so the
    # ledger's incoming column never silently understates the order book.
    ledger_undated_on_order: float = 0.0
    # Always present, even when there is no recommendation at all: "how long to
    # order more of this" is a question about a covered item too.
    lead_time: LeadTimeBreakdownOut | None = None

    class Config:
        from_attributes = True


# --------------------------------------------------------------------------
#
# Every model below describes a READ-ONLY what-if projection. Nothing in this
# response has been persisted: the official coverage verdict stays
# customer-scoped and is written only by app.engines.coverage.recompute_customer.
# --------------------------------------------------------------------------


from app.models import (  # noqa: E402
    DemandImportBatchStatus,
    DemandImportDecision,
    DemandImportMatchType,
)


class DemandLineRowOut(BaseModel):
    """One row of GET /demand-lines.

    `status` is the WELL's demand status (see `DemandLineOut`).

    `evaluated` is False when the line falls outside the coverage engine's
    status/profile filters -- either because its WELL's status is out of scope, in
    which case every line of that well is out of scope together, or because its own
    profile is. Such a line is deliberately served
    `coverage_status = null`: the engine does not evaluate it, so there is no
    verdict, and a superseded verdict is worse than none. The UI must render
    "not evaluated" rather than treating null as covered.
    """

    id: str
    well_id: str
    well_name: str
    planning_node_id: str | None
    planning_node_path: str
    customer_id: str | None
    customer_name: str | None = None
    product_id: str
    product_description: str
    quantity: float
    unit_of_measure: UnitOfMeasure
    ros_date: datetime
    status: DemandStatus
    profile: DemandProfile
    current_revision_no: int
    evaluated: bool
    coverage_status: str | None = None
    coverage_reason: str | None = None

    class Config:
        from_attributes = True


class DemandLineListOut(BaseModel):
    total: int
    limit: int
    offset: int
    returned: int
    rows: list[DemandLineRowOut] = []

    class Config:
        from_attributes = True


class CoverageGridFiltersOut(BaseModel):
    """Which status/profile filters the grid was computed under, and how.

    `recomputed_read_only=True` means these numbers are a PROJECTION for
    non-default filters, computed on the fly and not persisted -- not the official
    stored verdicts. `explanation` is safe to show verbatim.
    """

    status: list[str]
    profile: list[str]
    are_default: bool
    recomputed_read_only: bool
    explanation: str
    #: Customers left out of a recompute because they are not mapped to a
    #: Business Unit (no inventory pool). Named so one unmapped customer can
    #: never silently vanish -- and never 409 everyone else (C-07).
    skipped_customers: list[str] = []

    class Config:
        from_attributes = True


class CoverageGridRowOut(BaseModel):
    """One well in the Coverage Workspace grid.

    `coverage_status` is the WELL ROLLUP and is null when the well has no in-scope
    demand at all. Null means UNEVALUATED -- neither covered nor uncovered -- and
    such wells are excluded from coverage percentages.

    `demand_status` is here so an unevaluated row can EXPLAIN ITSELF. The status
    filter selects whole wells, so the commonest reason a row has no verdict is that
    its demand status is outside the requested scope -- and without the status on
    the row the grid states the consequence while withholding the cause, leaving a
    planner to guess whether the well is out of scope or the engine failed on it.
    The two columns are opposite kinds of fact: planner-set input versus
    engine-written output. See `app.models.well.Well`.
    """

    well_id: str
    well_name: str
    planning_node_id: str | None
    planning_node_path: str
    customer_id: str | None
    customer_name: str | None
    #: PLANNER-SET INPUT -- how firm this well's programme is. Not derived.
    demand_status: str
    in_scope_line_count: int
    covered_line_count: int
    coverage_status: str | None
    evaluated: bool
    # Same two dates, with the same meanings, as `WellSummary` -- and computed by
    # the same `app.engines.well_dates.well_dates` call, so the Coverage Workspace
    # and the Home Dashboard can never disagree about when a well is due or when it
    # first falls short.
    #
    # Under NON-DEFAULT filters these dates are recomputed for the filters actually
    # requested, alongside the verdicts, because both are answers to the same
    # question: which lines are in scope. Serving default-filter dates beside
    # projected verdicts would reintroduce the mismatch `filters.recomputed_read_only`
    # exists to rule out.
    earliest_ros_date: datetime | None = None
    first_runout_date: datetime | None = None

    class Config:
        from_attributes = True


class CoverageGridOut(BaseModel):
    filters: CoverageGridFiltersOut
    well_count: int
    rows: list[CoverageGridRowOut] = []
    #: Oldest / newest computed_at across the verdicts shown -- how old the
    #: answers are (C-08). Null when no verdict exists in scope.
    verdicts_computed_from: datetime | None = None
    verdicts_computed_to: datetime | None = None

    class Config:
        from_attributes = True


class MeasureOut(BaseModel):
    """A figure that may not exist.

    `available=False` means DO NOT RENDER A NUMBER -- render `reason`. Substituting
    0 for an unknown is the specific failure this type exists to prevent.
    """

    available: bool
    value: float | None = None
    reason: str | None = None

    class Config:
        from_attributes = True


class HorizonDemandOut(BaseModel):
    """Demand for one forward horizon, against the same horizon seen earlier.

    `previous` is reconstructed from `DemandRevision` history at
    `previous_as_of`. When that reconstruction is not possible for every in-scope
    line it is returned unavailable with the reason -- never estimated, and never
    zero. `definition` states exactly what both figures mean.

    CROSS-PRODUCT TOTAL -- read the units rule
    -----------------------------------------
    The scalar totals below sum quantities over every in-scope product, so they are
    only meaningful when those products share a unit. `unit_of_measure` is non-null
    exactly when they do; when they do not it is null, `notes` on the parent says
    so, and `quantities_by_unit` is the figure to render. The entries of
    `quantities_by_unit` must never be added together -- metres and tonnes are not
    addable, and no conversion factor exists in this platform (see
    `app.models.product.UnitOfMeasure`).

    `change_pct` is a RATIO and so has no unit at all, but it inherits the same
    restriction for a subtler reason: a percentage change between two mixed-unit
    totals is as meaningless as the totals themselves. It is reported unavailable
    when `unit_of_measure` is null, rather than computed from numbers that should
    not have been added.
    """

    months: int
    window_start: datetime
    window_end: datetime
    current_total: float
    current_line_count: int
    unit_of_measure: UnitOfMeasure | None = None
    quantities_by_unit: list[QuantityByUnitOut] = []
    previous: MeasureOut
    previous_as_of: datetime | None
    change_pct: MeasureOut
    definition: str
    # METRIC-TONNES headline(s): display-layer conversion (MVP_COMPROMISES.md
    # C-05), floor semantics -- see the engine dataclass. Native figures above
    # remain the ground truth.
    current_tonnes: MeasureOut
    previous_tonnes: MeasureOut
    change_pct_tonnes: MeasureOut

    class Config:
        from_attributes = True


class DemandTrendOut(BaseModel):
    horizons: list[HorizonDemandOut] = []
    notes: list[str] = []

    class Config:
        from_attributes = True


class CoverageQuantityByStatusOut(BaseModel):
    """One coverage status's share of in-scope demand, in QUANTITY terms.

    The "quantity coverage snapshot". `status` is a `CoverageStatus` value, or
    `"NotEvaluated"` for an in-scope line the engine has reached no verdict on --
    which is deliberately its own bucket rather than being folded into Uncovered
    (that would assert a verdict) or dropped (that would stop the buckets summing
    to the total).

    The five real statuses need five different actions -- PendingApproval a
    customer decision, Uncovered a mill order, Unrecoverable an escalation -- so
    collapsing them into "not covered" throws away the actionable half of the
    answer.
    """

    status: str
    quantity: float
    line_count: int
    well_count: int
    counts_as_covered: bool
    unit_of_measure: UnitOfMeasure | None = None
    quantities_by_unit: list[QuantityByUnitOut] = []
    # METRIC-TONNES headline(s): display-layer conversion (MVP_COMPROMISES.md
    # C-05), floor semantics -- see the engine dataclass. Native figures above
    # remain the ground truth.
    tonnes: MeasureOut

    class Config:
        from_attributes = True


class CoverageSummaryOut(BaseModel):
    """Coverage measured by DEMAND QUANTITY, with the well counts as reference.

    `coverage_pct` is the QUANTITY ratio -- the primary measure, on the product
    owner's ruling that coverage should be based on quantity from lines rather than
    on a count of wells. The well-count ratio is still served, as
    `well_coverage_pct`, together with the counts behind it: "2 of 3 wells are
    covered" is what a planner walks the floor with, and the two measures routinely
    disagree (one well short by 50 and one short by 40 000 count alike by well and
    nothing alike by quantity).

    Wells with no in-scope demand are counted in `unevaluated_well_count` and
    excluded from `well_coverage_pct` -- they are neither covered nor uncovered.

    CROSS-PRODUCT TOTAL -- read the units rule
    -----------------------------------------
    `covered_quantity` / `total_quantity` sum over every in-scope product, so
    `unit_of_measure` is non-null exactly when those products share one unit and
    `coverage_pct` is null when they do not: a ratio of two mixed-unit sums is not
    a number. `covered_quantities_by_unit` / `total_quantities_by_unit` are always
    correct, and their entries must never be added together.

    `by_status` partitions `total_quantity` exactly, per unit, so it can be checked
    against the headline rather than trusted.
    """

    available: bool
    coverage_pct: float | None
    covered_quantity: float
    total_quantity: float
    unit_of_measure: UnitOfMeasure | None = None
    covered_quantities_by_unit: list[QuantityByUnitOut] = []
    total_quantities_by_unit: list[QuantityByUnitOut] = []
    by_status: list[CoverageQuantityByStatusOut] = []
    # REFERENCE figures, kept at the product owner's explicit request.
    well_coverage_pct: float | None = None
    covered_well_count: int
    uncovered_well_count: int
    evaluated_well_count: int
    unevaluated_well_count: int
    in_scope_line_count: int = 0
    reason: str | None = None
    # METRIC-TONNES headline(s): display-layer conversion (MVP_COMPROMISES.md
    # C-05), floor semantics -- see the engine dataclass. Native figures above
    # remain the ground truth.
    covered_tonnes: MeasureOut
    total_tonnes: MeasureOut
    coverage_pct_tonnes: MeasureOut

    class Config:
        from_attributes = True


class SupplyRiskOut(BaseModel):
    """Demand not coverable even if ordered today.

    This is the coverage engine's own `Unrecoverable` verdict, read and summed --
    not re-derived here. `note` records that nothing in this figure accounts for
    on-order material, which has no local source.

    CROSS-PRODUCT TOTAL -- read the units rule
    -----------------------------------------
    The scalar totals below sum quantities over every in-scope product, so they are
    only meaningful when those products share a unit. `unit_of_measure` is non-null
    exactly when they do; when they do not it is null, `notes` on the parent says
    so, and `quantities_by_unit` is the figure to render. The entries of
    `quantities_by_unit` must never be added together -- metres and tonnes are not
    addable, and no conversion factor exists in this platform (see
    `app.models.product.UnitOfMeasure`).
    """

    available: bool
    unrecoverable_quantity: float
    unrecoverable_line_count: int
    affected_well_count: int
    # A RATIO of two cross-product totals. Reported null when the contributing
    # products do not share one unit, for the same reason `change_pct` is: the
    # numerator and denominator would each be a sum of unlike things.
    unrecoverable_pct_of_in_scope_demand: float | None
    unit_of_measure: UnitOfMeasure | None = None
    quantities_by_unit: list[QuantityByUnitOut] = []
    reason: str | None = None
    note: str
    # METRIC-TONNES headline(s): display-layer conversion (MVP_COMPROMISES.md
    # C-05), floor semantics -- see the engine dataclass. Native figures above
    # remain the ground truth.
    unrecoverable_tonnes: MeasureOut

    class Config:
        from_attributes = True


class SoftAllocationChannelOut(BaseModel):
    """One way in-scope demand was satisfied (or was not), in quantity terms.

    The FIVE channels partition the horizon's demand exactly, in the order the steel
    is drawn. `key` is the stable machine name (`from_customer_owned` |
    `from_shared_pool` | `from_own_assignment` | `via_substitute` | `not_satisfied`);
    `label` is renderable prose.

    `from_customer_owned` is quantity drawn from the CUSTOMER'S OWN uploaded
    inventory, which is consumed first for the same product -- ahead of company stock
    and ahead of any Oracle assignment. It is deliberately its own channel rather than
    part of `from_shared_pool`, because that material is not the company's and must
    never be presented as company inventory the company allocated well.

    `pct` is a share of a cross-product total, so it is null when the horizon spans
    several units -- same rule as every other ratio on this dashboard.
    """

    key: str
    label: str
    quantity: float
    line_count: int
    pct: float | None = None
    unit_of_measure: UnitOfMeasure | None = None
    quantities_by_unit: list[QuantityByUnitOut] = []
    # METRIC-TONNES headline(s): display-layer conversion (MVP_COMPROMISES.md
    # C-05), floor semantics -- see the engine dataclass. Native figures above
    # remain the ground truth.
    tonnes: MeasureOut

    class Config:
        from_attributes = True


class SoftAllocationCoverageOut(BaseModel):
    """SOFT allocation coverage -- how in-scope demand was actually satisfied.

    REPLACES the old `allocation` block, which reported Oracle's HARD assignment
    and which the product owner asked to be reframed ("here i like to show soft
    allocation coverage not hard allocation from oracle"). It is renamed so it
    cannot be mistaken for the Oracle figure.

    WHAT IT MEANS: of the in-scope demand with an ROS inside `horizon_months`, how
    much was drawn from the shared unassigned pool, how much from a line's own
    Oracle assignment, how much satisfied via an approved substitute, and how much
    not satisfied at all.

    WHAT IT DOES NOT MEAN: it is not a reservation. This platform creates no hard
    reservation of any kind, so a quantity shown as drawn from the pool is not held
    for that line. `note` says so verbatim and is safe to render.

    The figures come from the SAME coverage pass that produced the coverage
    verdicts (`app.engines.coverage.compute_customer_coverage`), so the two blocks
    cannot disagree and no allocation rule exists twice in the codebase.

    `assignment_source` is the provenance of the assignment channel only, in the
    vocabulary `InventoryPositionOut.assigned_source` uses. The pool and substitute
    channels are computed by this platform from its own rules and have no upstream
    provenance to state.

    CROSS-PRODUCT TOTAL -- read the units rule
    -----------------------------------------
    `total_quantity` sums over every in-scope product, so `unit_of_measure` is
    non-null exactly when they share one unit; when they do not it is null, `notes`
    on the parent says so, and `quantities_by_unit` is the figure to render. Its
    entries must never be added together (see `app.models.product.UnitOfMeasure`).
    """

    horizon_months: int
    available: bool
    total_quantity: float | None
    total_line_count: int = 0
    channels: list[SoftAllocationChannelOut] = []
    unit_of_measure: UnitOfMeasure | None = None
    quantities_by_unit: list[QuantityByUnitOut] = []
    assignment_source: str = "unavailable"
    unresolved_customer_ids: list[str] = []
    reason: str | None = None
    note: str
    # METRIC-TONNES headline(s): display-layer conversion (MVP_COMPROMISES.md
    # C-05), floor semantics -- see the engine dataclass. Native figures above
    # remain the ground truth.
    total_tonnes: MeasureOut

    class Config:
        from_attributes = True


class FirstRunoutWellOut(BaseModel):
    """One well that falls short, with its customer named.

    `first_runout_date` is the earliest ROS among this well's in-scope demand lines
    that coverage did NOT satisfy -- the first date the well is let down. It is NOT
    the month an inventory balance crosses zero; see `app.engines.well_dates`.

    `shortfall_quantity` is the summed quantity of exactly those lines, so the two
    figures describe the same rows. It is a cross-product sum WITHIN one well, so
    the units rule applies: `unit_of_measure` is null when the unsatisfied lines
    span several units and `quantities_by_unit` is the answer.
    """

    well_id: str
    well_name: str
    customer_id: str | None
    customer_name: str | None
    business_unit_id: str | None
    first_runout_date: datetime
    shortfall_quantity: float
    shortfall_line_count: int
    unit_of_measure: UnitOfMeasure | None = None
    quantities_by_unit: list[QuantityByUnitOut] = []
    # METRIC-TONNES headline(s): display-layer conversion (MVP_COMPROMISES.md
    # C-05), floor semantics -- see the engine dataclass. Native figures above
    # remain the ground truth.
    shortfall_tonnes: MeasureOut

    class Config:
        from_attributes = True


class FirstRunoutDetailOut(BaseModel):
    """The wells that first fall short, earliest first, with the cap declared.

    `cap`, `total_well_count`, `returned_well_count`, `omitted_well_count` and
    `truncated` are all served so a shortened list can SAY it is shortened. A
    management screen silently showing 10 of 40 shortfalls would understate the
    problem fourfold with nothing on it to say so.

    An EMPTY list with `available=true` means no well in scope has an unsatisfied
    in-scope demand line. That is an answer, not a gap.
    """

    available: bool
    wells: list[FirstRunoutWellOut] = []
    total_well_count: int = 0
    returned_well_count: int = 0
    cap: int
    omitted_well_count: int = 0
    truncated: bool = False
    earliest_first_runout_date: datetime | None = None
    reason: str | None = None
    note: str

    class Config:
        from_attributes = True


class OnOrderArrivalOut(BaseModel):
    """On-order quantity expected to land within `months`. CUMULATIVE.

    Cumulative so it lines up with the demand-trend horizons, which are also
    cumulative -- comparing incoming supply against demand for the same window is
    the only useful thing to do with this figure, and it is only valid if both
    sides count the same way. Overdue rows are included: a late purchase order is
    still incoming supply.
    """

    months: int
    window_end: datetime
    quantity: float
    row_count: int
    unit_of_measure: UnitOfMeasure | None = None
    quantities_by_unit: list[QuantityByUnitOut] = []
    # METRIC-TONNES headline(s): display-layer conversion (MVP_COMPROMISES.md
    # C-05), floor semantics -- see the engine dataclass. Native figures above
    # remain the ground truth.
    tonnes: MeasureOut

    class Config:
        from_attributes = True


class IncomingSupplyOut(BaseModel):
    """Material already on order into the scoped Business Unit(s).

    A NEW block. There was deliberately no on-order figure on this dashboard while
    `on_order` was a hardcoded 0, because a bare 0 reads as "nothing is on order" --
    a measurement nobody had made. It exists now that there is a real local
    projection to read (`app.models.inventory_on_order.InventoryOnOrder`).

    THE THREE STATES ARE KEPT APART, which is the whole point:

      available=false     no projected purchase-order row exists for any in-scope
                          product. The quantity is UNKNOWN and none is served.
      quantity=0          rows exist and total nothing: nothing is on order. A fact.
      quantity>0          the normal case.

    `products_with_no_data` counts in-scope products with no row at all even when
    others have one, so a partly-known total says how partial it is.

    PROVENANCE, in two independent flags:
      source              "unavailable" | "synthetic" | "oracle" | "mixed" -- where
                          THESE rows came from.
      oracle_integrated   Still false. The Oracle feed is not live, and seeding a
                          projection table does not make an integration exist.

    Nothing in the coverage, supply-risk or soft-allocation figures nets this
    quantity against demand -- coverage is decided from on-hand stock alone. Read
    this block BESIDE them, never inside them.
    """

    available: bool
    quantity: float | None = None
    unit_of_measure: UnitOfMeasure | None = None
    quantities_by_unit: list[QuantityByUnitOut] = []
    row_count: int = 0
    product_count: int = 0
    products_with_no_data: int = 0
    products_with_nothing_on_order: int = 0
    earliest_expected_arrival: datetime | None = None
    latest_expected_arrival: datetime | None = None
    undated_quantity: float = 0.0
    by_arrival_horizon: list[OnOrderArrivalOut] = []
    # METRIC-TONNES headline(s): display-layer conversion (MVP_COMPROMISES.md
    # C-05), floor semantics -- see the engine dataclass. Native figures above
    # remain the ground truth.
    tonnes: MeasureOut
    undated_tonnes: MeasureOut
    source: str
    oracle_integrated: bool
    reason: str | None = None
    note: str

    class Config:
        from_attributes = True


class InventoryUtilisationProductOut(BaseModel):
    """Tied vs not-tied on-hand for ONE (Business Unit, product), this horizon.

    `tied + not_tied == on_hand_quantity` exactly. `customer_owned_quantity` is
    netted OUT of demand before the split, drawn first for the same product
    exactly as `SoftAllocationChannelOut`'s `from_customer_owned` channel is.
    """

    business_unit_id: str
    product_id: str
    product_description: str | None = None
    unit_of_measure: UnitOfMeasure
    on_hand_quantity: float
    demand_in_window: float
    #: Overdue portion of demand_in_window (ROS already passed) -- counted into
    #: the tie, labelled separately (product-owner decision, 2026-08-12).
    demand_overdue: float = 0.0
    customer_owned_quantity: float
    tied: float
    not_tied: float
    # Per-row MT conversions (display-layer, C-05); unavailable = cannot convert
    tied_tonnes: MeasureOut
    not_tied_tonnes: MeasureOut

    class Config:
        from_attributes = True


class UnknownPositionProductOut(BaseModel):
    """A product with in-scope demand but NO on-hand row in its Business Unit.
    Its inventory position is UNKNOWN -- it is in neither `tied` nor `not_tied`.
    """

    business_unit_id: str
    product_id: str
    product_description: str | None = None
    unit_of_measure: UnitOfMeasure

    class Config:
        from_attributes = True


class UnconvertibleQuantityOut(BaseModel):
    """A tied-or-not_tied quantity that could not be converted to metric tonnes
    (PC, or a product with no stated weight). Named rather than silently
    excluded from the metric-tonnes headline.
    """

    business_unit_id: str
    product_id: str
    product_description: str | None = None
    unit_of_measure: UnitOfMeasure
    side: str
    quantity: float
    reason: str

    class Config:
        from_attributes = True


class InventoryUtilisationOut(BaseModel):
    """Of company-owned steel standing in the yard, how much is tied to
    in-window demand and how much is idle.

    THE DENOMINATOR IS INVENTORY, NOT DEMAND -- the opposite of
    `SoftAllocationCoverageOut`, whose denominator is demand quantity. Do not
    read the two blocks as answering the same question. This block is demand
    NETTING against on-hand stock, and is NOT a return of the old
    `InventoryAssignment`-based allocation block that was removed by
    product-owner instruction -- see `app.engines.executive
    .INVENTORY_UTILISATION_NOTE`, served verbatim in `note` below.

    `tied_tonnes` / `not_tied_tonnes` are a metric-tonnes CONVENIENCE over
    `tied_by_unit` / `not_tied_by_unit`, computed via `app.engines.units
    .to_metric_tonnes`. `available=False` on either means nothing on that side
    could be converted; `available=True` with a `reason` means the total is a
    PARTIAL one and `unconvertible` names what was excluded and why. A product
    with no on-hand row is reported in `unknown_position`, never as a zero.
    """

    horizon_months: int
    available: bool
    tied_tonnes: MeasureOut
    not_tied_tonnes: MeasureOut
    products: list[InventoryUtilisationProductOut] = []
    tied_by_unit: list[QuantityByUnitOut] = []
    not_tied_by_unit: list[QuantityByUnitOut] = []
    unconvertible: list[UnconvertibleQuantityOut] = []
    unknown_position: list[UnknownPositionProductOut] = []
    unknown_position_count: int = 0
    reason: str | None = None
    note: str

    class Config:
        from_attributes = True


class ExecutiveScopeOut(BaseModel):
    """WHICH scope produced this payload.

    Both filters are optional and either may be supplied. A customer named together
    with a Business Unit it does not belong to is refused with a 400 rather than
    answered with an empty payload -- an empty payload is a measurement, and a
    measurement of a scope that cannot exist reads as good news. See
    `app.engines.executive.resolve_scope`.

    `label` and `description` are renderable prose, so the screen can title itself
    without re-deriving the scope from the ids.
    """

    business_unit_id: str | None = None
    business_unit_name: str | None = None
    customer_id: str | None = None
    customer_name: str | None = None
    label: str
    description: str
    customer_ids: list[str] = []

    class Config:
        from_attributes = True


class ExecutiveSummaryOut(BaseModel):
    """GET /dashboard/executive. See `app.engines.executive` for the honesty
    rules governing every `available` flag in here.

    `scope` states which Business Unit and/or customer produced these figures.
    `business_unit_id` / `business_unit_name` / `customer_name` mirror it at the top
    level beside the pre-existing `customer_id`, so a client that only wants to
    caption the screen need not descend.
    """

    generated_at: datetime
    customer_id: str | None
    business_unit_id: str | None = None
    business_unit_name: str | None = None
    customer_name: str | None = None
    scope: ExecutiveScopeOut
    demand_trend: DemandTrendOut
    coverage: CoverageSummaryOut
    supply_risk: SupplyRiskOut
    soft_allocation_coverage: SoftAllocationCoverageOut
    first_runout: FirstRunoutDetailOut
    incoming_supply: IncomingSupplyOut
    inventory_utilisation: InventoryUtilisationOut
    notes: list[str] = []
    #: The demand scope (well statuses / line profiles) EVERY block above was
    #: computed under. When `scope_is_default` is False the figures came from a
    #: read-only recompute, not from the stored official verdicts.
    status_scope: list[str] = []
    profile_scope: list[str] = []
    scope_is_default: bool = True
    #: Customers whose scoped recompute was skipped (no BU / missing on-hand
    #: row). Named so a scoped answer can never silently omit a customer.
    skipped_customers: list[str] = []

    class Config:
        from_attributes = True


class DemandImportRowOut(BaseModel):
    """One staged spreadsheet row.

    `match_type` is a SUGGESTION, `decision` is the user's answer. `raw_*` is the
    verbatim cell text, kept so an error row can show what was typed. The
    `matched_*` fields are the matched line's CURRENT values, so the review screen
    can diff against them. A row with `match_type = "Error"` can only be skipped.
    """

    id: str
    row_number: int
    raw_well: str | None
    raw_product: str | None
    raw_quantity: str | None
    raw_ros_date: str | None
    raw_status: str | None
    raw_profile: str | None
    well_id: str | None
    well_name: str | None
    product_id: str | None
    product_description: str | None
    quantity: float | None
    # The MATCHED product's unit, so the review screen can label both `quantity`
    # and `matched_quantity` while a planner is deciding whether to accept the row.
    # Null exactly when `product_id` is null -- an unmatched or error row has no
    # product, so there is no unit to state and nothing to label; `raw_quantity`
    # (verbatim cell text) is what such a row shows instead. Guessing a unit for an
    # unmatched row would put a confident label on a number that has not been
    # validated at all.
    unit_of_measure: UnitOfMeasure | None
    ros_date: datetime | None
    status: DemandStatus | None
    profile: DemandProfile | None
    match_type: DemandImportMatchType
    matched_demand_line_id: str | None
    matched_quantity: float | None
    matched_ros_date: datetime | None
    matched_status: DemandStatus | None
    matched_profile: DemandProfile | None
    match_reason: str | None
    error: str | None
    decision: DemandImportDecision

    # ---- CONFLICT: this row disagrees with the LIVE book -------------------
    #
    # Computed FRESH on every read by `app.engines.demand_import.detect_conflict`,
    # never read out of a stored flag -- live data keeps moving after staging, so a
    # persisted verdict would be the answer to a question asked at the wrong moment.
    #
    # A conflict is NOT an error. An `Error` row is one the file got wrong; a
    # conflict row is perfectly valid and merely disagrees with what it would land
    # on, in one of the two ways where the diff above is NOT the change that would
    # actually happen: the row asserts a demand status its well does not have (which
    # cascades to every line of the well), or the line it revises has been revised by
    # somebody else since this batch was staged. An ordinary revision -- the file
    # simply carrying a different quantity -- is deliberately not a conflict.
    is_conflict: bool = False
    conflict_kind: str | None = None
    conflict_field: str | None = None
    #: Rendered strings, so the screen's before -> after diff and the server's
    #: refusal message say the same thing. See `app.engines.demand_import.RowConflict`.
    conflict_current_value: str | None = None
    conflict_file_value: str | None = None
    conflict_detail: str | None = None
    #: For a status conflict: how many demand lines of the well would be revised,
    #: including lines this spreadsheet never listed.
    conflict_cascade_line_count: int = 0
    #: True when apply would REFUSE this row as things stand -- i.e. it conflicts and
    #: the override is not approved. The one field a screen needs to decide whether
    #: to gate; `is_conflict` alone would still be true after approval.
    requires_override_approval: bool = False

    # ---- The override APPROVAL: a second, explicit decision ---------------
    override_approved: bool = False
    override_approved_at: datetime | None = None
    #: "On behalf of" text as typed by the caller. No code branches on it -- see
    #: `app.models.demand_import.DemandImportRow.override_approved_by`.
    override_approved_by: str | None = None
    #: Who actually approved: the authenticated user, recorded by the server (F09).
    override_approved_by_user_id: str | None = None
    override_approved_by_user_name: str | None = None
    #: True when the approval was given against live data that has since changed
    #: (F07). The approval is still shown as having happened; it no longer
    #: authorises the write, and `requires_override_approval` is True again.
    override_approval_lapsed: bool = False

    # ---- The BASELINE: what was live when this row was staged -------------
    #
    # Served so the review screen can show the reviewer that the ground moved:
    # baseline -> current -> file is three values, and the two-column diff above only
    # has room for two. `baseline_quantity` is in the MATCHED product's unit, i.e.
    # `unit_of_measure` above -- the same line, so necessarily the same unit.
    baseline_revision_no: int | None = None
    baseline_quantity: float | None = None
    baseline_ros_date: datetime | None = None
    #: The matched line's revision number RIGHT NOW. Equal to `baseline_revision_no`
    #: unless somebody has revised the line since staging, which is exactly the
    #: condition a ConcurrentRevision conflict reports.
    current_revision_no: int | None = None

    applied: bool
    applied_demand_line_id: str | None
    apply_error: str | None

    class Config:
        from_attributes = True


class DemandImportBatchSummaryOut(BaseModel):
    id: str
    filename: str | None
    status: DemandImportBatchStatus
    row_count: int
    error_count: int
    created_at: datetime
    applied_at: datetime | None

    class Config:
        from_attributes = True


class DemandImportBatchOut(BaseModel):
    """A staged batch, for the Review step. Nothing here is demand yet."""

    id: str
    filename: str | None
    sheet_name: str | None
    status: DemandImportBatchStatus
    row_count: int
    error_count: int
    pending_count: int
    revision_suggestion_count: int
    new_suggestion_count: int
    #: Rows that disagree with the LIVE book, and how many of those still lack the
    #: override approval apply requires. `unapproved_conflict_count` counts only rows
    #: the user has ACCEPTED, because a conflicting row left Pending or Skipped blocks
    #: nothing -- apply was never going to touch it.
    conflict_count: int = 0
    unapproved_conflict_count: int = 0
    created_at: datetime
    applied_at: datetime | None
    column_contract: dict[str, list[str]]
    rows: list[DemandImportRowOut] = []

    class Config:
        from_attributes = True


class DemandImportDecisionIn(BaseModel):
    decision: DemandImportDecision


class DemandImportOverrideApprovalIn(BaseModel):
    """Approve (or withdraw approval for) overriding ONE row's conflict.

    Deliberately separate from `DemandImportDecisionIn`. Accept/skip answers "is
    this the change I want?"; this answers "I have seen that it conflicts with live
    data and I approve overriding that". Collapsing the two into one field would
    make the second decision reachable by accident.

    `approved_by` is attribution only -- see
    `app.models.demand_import.DemandImportRow.override_approved_by`.
    """

    approved: bool
    approved_by: str | None = None


class DemandImportApplyOut(BaseModel):
    """Outcome of applying a batch. Every applied row produced a
    `DemandRevision` and an `ImpactRecord`; `impact_record_ids` lists them."""

    batch_id: str
    status: DemandImportBatchStatus
    revised_count: int
    created_count: int
    skipped_count: int
    pending_count: int
    error_count: int
    failed_row_ids: list[str] = []
    impact_record_ids: list[str] = []
    #: Wells whose DEMAND STATUS this batch changed, and the number of demand lines
    #: the change cascaded a revision to. A status cell in the spreadsheet is a
    #: statement about the WELL, so applying it touches every line of that well --
    #: including lines the file never listed. Surfaced separately from
    #: `revised_count` (which counts spreadsheet rows) so the review screen can say
    #: so out loud instead of leaving the user to discover it.
    well_status_changed_ids: list[str] = []
    well_status_cascaded_line_count: int = 0
    batch: DemandImportBatchOut

    class Config:
        from_attributes = True


class ImportConflictLineChangeOut(BaseModel):
    """One demand line, before -> after, in a conflict preview. NOT persisted."""

    demand_line_id: str
    well_id: str
    well_name: str
    product_id: str
    product_description: str | None
    #: Labels both quantities. One value serves both: an import row can restate a
    #: quantity but never a product's unit.
    unit_of_measure: UnitOfMeasure | None
    coverage_before: str
    coverage_after: str
    reason_before: str | None
    reason_after: str | None
    quantity_before: float
    quantity_after: float
    ros_date_before: datetime
    ros_date_after: datetime
    changed: bool
    #: False marks a line dragged along by the well-status cascade or by inventory
    #: being reallocated -- the lines the spreadsheet row never mentioned, which is
    #: precisely what the row's own diff cannot show.
    named_by_the_row: bool

    class Config:
        from_attributes = True


class ImportConflictWellChangeOut(BaseModel):
    well_id: str
    well_name: str
    coverage_before: str | None
    coverage_after: str | None
    changed: bool

    class Config:
        from_attributes = True


class ImportConflictPreviewOut(BaseModel):
    """READ-ONLY what-if: coverage impact of approving ONE conflicting import row.

    Nothing in here has been persisted, and requesting it approves nothing.
    `is_what_if` is serialised explicitly so the screen can label the panel without
    inferring anything -- the same rule the scenario preview and the cross-customer
    scenario preview follow. A figure from this response must never be rendered as the
    coverage verdict.

    Computed by `app.engines.coverage.compute_customer_coverage` run twice, through
    the same override layer a scenario preview uses. See
    `app.engines.demand_import_preview` for why the row does not become a real
    `Scenario`.
    """

    batch_id: str
    row_id: str
    row_number: int
    conflict_kind: str
    conflict_field: str
    conflict_current_value: str
    conflict_file_value: str
    conflict_detail: str

    customer_id: str
    customer_name: str
    well_id: str | None
    well_name: str | None

    #: Lines that would receive a revision. For a status conflict, every line of the
    #: well -- the number the reviewer is really being asked to authorise.
    revised_line_ids: list[str] = []
    cascade_line_count: int = 0

    line_changes: list[ImportConflictLineChangeOut] = []
    well_changes: list[ImportConflictWellChangeOut] = []

    covered_lines_before: int = 0
    covered_lines_after: int = 0
    unrecoverable_lines_before: int = 0
    unrecoverable_lines_after: int = 0
    changed_line_count: int = 0
    changed_well_count: int = 0

    is_what_if: bool = True
    notes: list[str] = []

    class Config:
        from_attributes = True


# --------------------------------------------------------------------------
# Scenario Planning (app.engines.scenario / app.engines.scenario_apply)
#
# Two very different kinds of model live below and the distinction matters:
#
#   * Scenario / ScenarioOverride models describe PERSISTED rows -- a scenario is
#     real, shared, editable data.
#   * ScenarioImpactOut and everything under it describes a READ-ONLY WHAT-IF.
#     Nothing in it has been persisted. `is_what_if` is serialised explicitly so
#     the frontend can label the panel without inferring anything, exactly as the
#     scenario preview is labelled. A figure from that response must
#     never be shown as the official coverage verdict.
#
# Own import block, appended, following the pattern already used above.
# --------------------------------------------------------------------------

from app.models import ScenarioStatus, ScenarioTargetKind  # noqa: E402


class ScenarioOverrideIn(BaseModel):
    """Add one override. Exactly one of the value_* fields is set, and which one
    is determined by (target_kind, field_name) -- see
    `app.engines.overrides.OVERRIDE_FIELDS`, which validates every combination
    and is also served by GET /scenarios/override-fields so the UI need not
    duplicate the rules."""

    target_kind: ScenarioTargetKind
    field_name: str

    target_demand_line_id: str | None = None
    # Required on a WELL override, and forbidden on every other kind. Demand status
    # is a well-level fact, so "what if this well were Confirmed?" points at a well.
    target_well_id: str | None = None
    target_product_id: str | None = None
    # Required on an INVENTORY override and must be the scenario customer's own
    # BU. Stated rather than inferred so the Business Unit an override applies to
    # is a fact in the row; a cross-BU value is refused with 400.
    target_business_unit_id: str | None = None
    target_from_product_id: str | None = None
    target_to_product_id: str | None = None

    value_number: Quantity | None = None
    value_date: datetime | None = None
    value_text: str | None = None

    note: str | None = None


class ScenarioOverrideOut(BaseModel):
    id: str
    scenario_id: str
    target_kind: ScenarioTargetKind
    field_name: str
    target_demand_line_id: str | None
    target_well_id: str | None
    target_product_id: str | None
    target_business_unit_id: str | None
    target_from_product_id: str | None
    target_to_product_id: str | None
    value_number: float | None
    value_date: datetime | None
    value_text: str | None
    note: str | None
    created_at: datetime

    class Config:
        from_attributes = True


class ScenarioIn(BaseModel):
    """Create a scenario. Scenarios are SHARED -- there is no visibility field
    and `created_by` is attribution only, never access control."""

    name: str
    #: The Business Unit this scenario plans for -- the scope coverage is
    #: allocated at (D01), and therefore the only scope it can be previewed and
    #: applied at.
    business_unit_id: str
    description: str | None = None
    created_by: str | None = None


class ScenarioPatch(BaseModel):
    """Rename / re-describe / move through the lifecycle. Every field optional.

    Rejected with 409 on an APPLIED scenario: an applied scenario is an immutable
    record of what was agreed -- see app.models.scenario.Scenario.
    """

    name: str | None = None
    description: str | None = None
    status: ScenarioStatus | None = None


class ScenarioSummaryOut(BaseModel):
    """One row of GET /scenarios.

    `coverage_delta_wells` is the headline: how many of the customer's wells this
    scenario would move, computed by the read-only preview. It is a WHAT-IF
    figure, and the list screen labels the whole column as such.
    """

    id: str
    name: str
    description: str | None
    #: The scope a scenario is previewed and applied at (D01). It was a customer
    #: until the pool became Business-Unit-wide.
    business_unit_id: str
    business_unit_name: str | None
    status: ScenarioStatus
    #: "On behalf of" text typed at creation. The actor is `created_by_user_*`,
    #: recorded server-side from the authenticated user (F09).
    created_by: str | None
    created_by_user_id: str | None = None
    created_by_user_name: str | None = None
    created_at: datetime
    updated_at: datetime | None
    applied_at: datetime | None
    override_count: int
    #: Bumped on every change; an apply must name the version it previewed (F08).
    version: int = 1
    coverage_delta_wells: int | None = None
    coverage_delta_lines: int | None = None
    # Set when the preview for this row could not be computed. The list still
    # renders -- one broken scenario must not blank the whole screen.
    preview_error: str | None = None

    class Config:
        from_attributes = True


class ScenarioDetailOut(ScenarioSummaryOut):
    overrides: list[ScenarioOverrideOut] = []


class LineCoverageChangeOut(BaseModel):
    """One demand line, before -> after.

    `status_before` / `status_after` may be the literal "NotEvaluated", which is a
    real outcome rather than a missing value: it is what the absence of a
    CoverageResult means, and it is how an override that pushes a line out of the
    status/profile filters shows up.
    """

    demand_line_id: str
    well_id: str
    well_name: str
    product_id: str
    product_description: str | None
    # Labels `quantity_before` and `quantity_after`. A scenario can restate a
    # quantity but never a product's unit -- there is no UoM override field (see
    # `app.engines.overrides.OVERRIDE_FIELDS`) -- so before and after are always
    # in the same unit and one field is enough for both.
    unit_of_measure: UnitOfMeasure
    status_before: str
    status_after: str
    reason_before: str | None
    reason_after: str | None
    quantity_before: float
    quantity_after: float
    ros_date_before: datetime
    ros_date_after: datetime
    changed: bool
    directly_overridden: bool
    #: WHOSE it is. A scenario preview spans the whole Business Unit (D01), so a
    #: change may belong to a customer the planner did not name. A knock-on onto a
    #: neighbour is never shown anonymously.
    customer_id: str | None = None
    customer_name: str | None = None

    class Config:
        from_attributes = True


class WellCoverageChangeOut(BaseModel):
    well_id: str
    well_name: str
    status_before: str | None
    status_after: str | None
    changed: bool
    #: WHOSE it is. A scenario preview spans the whole Business Unit (D01), so a
    #: change may belong to a customer the planner did not name. A knock-on onto a
    #: neighbour is never shown anonymously.
    customer_id: str | None = None
    customer_name: str | None = None

    class Config:
        from_attributes = True


class MrpRowChangeOut(BaseModel):
    """`kind` is added | removed | changed | unchanged, keyed on the same
    (product, recoverability) pair the MRP screen groups by."""

    kind: str
    product_id: str
    product_description: str | None
    unrecoverable: bool
    # One MRP row is one product, so one unit labels both quantities. Null only for
    # a row whose product has been deleted from the catalogue between the two
    # passes, which is also when `product_description` is null.
    unit_of_measure: UnitOfMeasure | None
    quantity_before: float | None
    quantity_after: float | None
    ros_date_before: datetime | None
    ros_date_after: datetime | None
    recommended_order_date_before: date | None
    recommended_order_date_after: date | None
    reason_after: str | None

    class Config:
        from_attributes = True


class SupplyRunoutChangeOut(BaseModel):
    """What a PO_ARRIVAL override does to one product's runout projection.

    BOTH runout months come from an ON-ORDER-AWARE projection -- one at the
    promised arrival dates, one at the overridden ones -- so the difference is
    attributable to the shift alone. NEITHER equals `ByItemAnalysisOut.runout_month`,
    which is deliberately on-hand-only. Do not label these as the By Item runout.

    NO COVERAGE VERDICT MOVES BECAUSE OF THIS. Coverage is decided from on-hand
    stock alone; incoming supply is reported beside that verdict, never inside it.
    A UI rendering this block must not imply the Covered/Uncovered counts responded.
    """

    product_id: str
    product_description: str | None
    unit_of_measure: UnitOfMeasure | None
    # Null when every purchase-order row for this product is undated -- a real state
    # (raised but not acknowledged), in which case there was no schedule position to
    # shift and `shift_days` is 0.
    arrival_before: date | None
    arrival_after: date | None
    shift_days: int
    runout_month_before: str | None
    runout_month_after: str | None
    # False is a real answer, not a failure: a shift inside one month, or a product
    # with enough stock never to run out, both leave the month alone. Render it as
    # "no change to the runout month", never as though the override was ignored.
    runout_month_changed: bool
    on_order_dated_quantity: float
    on_order_undated_quantity: float
    runout_before: list[RunoutPointOut] = []
    runout_after: list[RunoutPointOut] = []

    # -- HYPOTHETICAL new orders: purchase orders that DO NOT EXIST -----------
    #
    # Set by `PoArrival.new_order` overrides. `runout_after` includes them and
    # `runout_before` does not, which is what makes the two curves a readable diff.
    #
    # `(quantity, expected_arrival)` pairs rather than a nested object, because the
    # pair IS the whole content of a hypothetical order and there is no third
    # attribute a real `InventoryOnOrder` row would contribute -- no PO number, no
    # source system, no acknowledgement, because there is no purchase order.
    hypothetical_orders: list[tuple[float, date]] = []
    # The invented total. DELIBERATELY NOT PART OF `on_order_dated_quantity`, which
    # stays a sum of real Oracle-projected rows a planner can go and verify. A UI must
    # render this one as a hypothesis and never add the two into a single "on order".
    hypothetical_quantity: float = 0.0
    # What MRP already recommends ordering for this product (after pass, both
    # recoverability rows summed). Null when it recommends nothing.
    mrp_recommended_quantity: float | None = None
    # "The order you drew is big enough to cover that recommendation." NOT "the MRP
    # recommendation row went away" -- it did not and cannot. MRP rows derive from
    # coverage verdicts, coverage is on-hand-only, and incoming supply is invisible to
    # it. Render this as a sizing check, never as a resolved gap.
    hypothetical_covers_recommendation: bool = False

    class Config:
        from_attributes = True


class RiskImpactOut(BaseModel):
    """Change in UNRECOVERABLE demand -- what a mill order can no longer save."""

    unrecoverable_lines_before: int
    unrecoverable_lines_after: int
    # CROSS-PRODUCT TOTALS. These sum unrecoverable demand over every product in the
    # scenario's customer pool, so they follow the aggregate rule in the module
    # header: `unit_of_measure` is non-null ONLY when every contributing product
    # shares one unit, and `quantities_by_unit_*` is the answer that is always
    # correct. When the scalar is null, render the breakdown -- do not add the
    # entries together.
    unrecoverable_quantity_before: float
    unrecoverable_quantity_after: float
    unit_of_measure: UnitOfMeasure | None = None
    quantities_by_unit_before: list[QuantityByUnitOut] = []
    quantities_by_unit_after: list[QuantityByUnitOut] = []
    became_unrecoverable: list[str] = []
    no_longer_unrecoverable: list[str] = []

    class Config:
        from_attributes = True


class ScenarioImpactOut(BaseModel):
    """Result of GET /scenarios/{id}/preview -- a READ-ONLY WHAT-IF.

    Nothing here has been persisted. The official coverage verdict is written
    only by the coverage engine and is unchanged by this call. `is_what_if` is
    always true and exists so the UI can say so unmistakably.

    `applicable` / `apply_blockers` tell the editor in advance whether
    POST /scenarios/{id}/apply would succeed, and if not, why -- so a planner
    learns that supply overrides cannot be written to Oracle-owned projections
    before pressing the button, not after.
    """

    scenario_id: str
    scenario_name: str
    scenario_status: str
    business_unit_id: str | None
    business_unit_name: str | None
    override_count: int
    unmodelled_override_count: int
    line_changes: list[LineCoverageChangeOut] = []
    well_changes: list[WellCoverageChangeOut] = []
    mrp_changes: list[MrpRowChangeOut] = []
    # Empty unless the scenario restates a purchase-order arrival date.
    supply_runout_changes: list[SupplyRunoutChangeOut] = []
    risk: RiskImpactOut | None
    covered_lines_before: int
    covered_lines_after: int
    covered_wells_before: int
    covered_wells_after: int
    changed_line_count: int
    changed_well_count: int
    applicable: bool
    apply_blockers: list[str] = []
    is_what_if: bool
    notes: list[str] = []
    #: The scenario version this impact was computed for. Send it back as
    #: `expected_version` on apply (F08).
    scenario_version: int = 1

    class Config:
        from_attributes = True


class ScenarioApplyIn(BaseModel):
    """POST /scenarios/{id}/apply body (F08).

    `expected_version` is the `scenario_version` of the preview the planner is
    confirming. A scenario whose version has moved since that preview is refused
    with 409 and nothing is written -- the impact they reviewed is no longer the
    impact they would get.
    """

    expected_version: int


class ScenarioApplyOut(BaseModel):
    """Result of POST /scenarios/{id}/apply -- this one DID write.

    `line_status_after` / `well_status_after` are the coverage answer after the
    write, in the same vocabulary the preview reports, so a client can verify the
    promise was kept. Demand changes went through the revision machinery, so
    `demand_revision_ids` and `impact_record_ids` are non-empty whenever a demand
    line actually moved.
    """

    scenario_id: str
    scenario_name: str
    applied_at: datetime
    revised_demand_line_ids: list[str] = []
    demand_revision_ids: list[str] = []
    impact_record_ids: list[str] = []
    decided_approval_ids: list[str] = []
    line_status_after: list[tuple[str, str]] = []
    well_status_after: list[tuple[str, str | None]] = []
    notes: list[str] = []

    class Config:
        from_attributes = True


class OverrideFieldsOut(BaseModel):
    """GET /scenarios/override-fields: the override vocabulary, from the engine.

    Served so the editor's field pickers cannot drift from
    `app.engines.overrides.OVERRIDE_FIELDS`, which is the single authority on
    what may be overridden and what value type each field takes.
    """

    # {target_kind: {field_name: number | date | text}}
    fields: dict[str, dict[str, str]]
    # {"<kind>.<field>": [allowed, values]} for the fields that take an enum.
    enum_values: dict[str, list[str]]
    # Kinds that can be previewed but never applied (Oracle-owned or unmodelled).
    supply_kinds: list[str]
    # Kinds the preview cannot model at all.
    unmodelled_kinds: list[str]


class WellDemandStatusIn(BaseModel):
    """Body of `PUT /wells/{well_id}/demand-status`.

    One field, because there is one thing to say. The write is well-level by
    nature: `app.engines.coverage.set_well_demand_status` fans a revision out to
    every demand line of the well.
    """

    demand_status: DemandStatus


class WellDemandStatusChangeOut(BaseModel):
    """What the status change actually wrote.

    The three id lists are returned rather than a count so a caller can VERIFY the
    fan-out instead of trusting it: one `DemandRevision` and one `ImpactRecord` per
    demand line of the well, which is what keeps each line's history complete and
    what the Home Dashboard's "Demand Changes" card reads.

    `unchanged` is True when the requested status equalled the stored one, in which
    case nothing was written at all and the three lists are empty -- appending a
    revision to every line to record a change of nothing would pollute exactly the
    history this endpoint exists to keep.

    `coverage_before` / `coverage_after` are the well's rollup either side of the
    recompute. They can differ even though no quantity moved: the status filter
    selects wells, so entering or leaving scope changes which wells compete for the
    same inventory.
    """

    well_id: str
    well_name: str
    status_before: str
    status_after: str
    coverage_before: str | None
    coverage_after: str | None
    demand_line_ids: list[str] = []
    revision_ids: list[str] = []
    impact_record_ids: list[str] = []
    revised_line_count: int = 0
    unchanged: bool = False

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# CUSTOMER-OWNED INVENTORY -- the one inventory table this platform OWNS
# ---------------------------------------------------------------------------


class CustomerOwnedInventoryRowOut(BaseModel):
    """One customer's declared position in one product.

    `uploaded_at` is the AS-OF date the spreadsheet stated, or the upload time when it
    stated none. It is served on every row and should be RENDERED on every row: this
    figure is only as current as the last file somebody sent, which can be months,
    and a planner deciding on it has to be able to see that. `source_reference` names
    the file (and yard, if given) it came from.
    """

    product_id: str
    product_description: str | None = None
    quantity: float
    unit_of_measure: UnitOfMeasure
    source_system: str = "customer-upload"
    source_reference: str | None = None
    uploaded_at: datetime | None = None

    class Config:
        from_attributes = True


class CustomerOwnedInventoryOut(BaseModel):
    """A customer's whole declared customer-owned position.

    `has_uploaded` IS THE PAYLOAD'S MOST IMPORTANT FIELD and a UI must branch on it
    before rendering anything else. It separates the two states an empty `positions`
    list cannot:

      has_uploaded = false   Nobody has ever uploaded a position for this customer, so
                             the platform holds NO customer-owned data about them.
                             Render "no data", never "owns none" and never "0".
      has_uploaded = true    They have declared a position. A product absent from
                             `positions` is one they own NONE of -- a measured fact.

    That is the same unknown-vs-zero rule the rest of the platform applies to on-order
    material and on-hand quantities, and it is the reason the upload record exists at
    all. See `app.models.customer_owned_inventory.CustomerOwnedInventory`.

    There is deliberately NO total quantity field. The positions can span several
    units of measure and a bare sum across them is undefined, not imprecise -- the
    same rule `app.engines.executive.quantity_by_unit` enforces everywhere else.
    """

    customer_id: str
    customer_name: str
    business_unit_id: str | None = None
    has_uploaded: bool = False
    last_uploaded_at: datetime | None = None
    positions: list[CustomerOwnedInventoryRowOut] = []
    note: str | None = None


class CustomerOwnedUploadRowOut(BaseModel):
    """What happened to ONE spreadsheet row.

    `action` is "Created" | "Replaced" | "Error".

    "Replaced" is not a conflict awaiting a decision, unlike a demand import's
    revision-vs-new suggestion: an inventory position has a current value and a newer
    count supersedes it. `previous_quantity` is reported because it is a number the
    user has just lost sight of.

    `unit_of_measure` is the MATCHED product's unit, and is null exactly when no
    product matched -- an error row has no product and therefore no unit, and
    labelling an unvalidated cell with a guessed unit is the mistake the units guard
    exists to prevent. Such a row shows `raw_quantity` instead.
    """

    row_number: int
    action: str
    raw_product: str | None = None
    raw_quantity: str | None = None
    product_id: str | None = None
    product_description: str | None = None
    quantity: float | None = None
    previous_quantity: float | None = None
    unit_of_measure: UnitOfMeasure | None = None
    error: str | None = None

    class Config:
        from_attributes = True


class CustomerOwnedUploadOut(BaseModel):
    """The result of one upload. There is no separate Apply step -- see
    `app.engines.customer_owned_import` for why an inventory upload honestly has no
    review flow while a demand import must have one.

    `coverage_changes` lists the wells whose rollup MOVED, as
    {well_id: [before, after]}. It can be non-empty for wells the file never
    mentioned: customer-owned stock is consumed first for its product, so an uploaded
    quantity reallocates the whole customer's pool. Showing the consequence at the
    moment the user caused it is the point.

    `column_contract` is served with every response so a UI can render the expected
    columns and generate a template without a second endpoint or a duplicated list --
    the same pattern `DemandImportBatchOut` uses.
    """

    upload_id: str
    customer_id: str
    filename: str | None = None
    sheet_name: str | None = None
    row_count: int = 0
    created_count: int = 0
    replaced_count: int = 0
    applied_count: int = 0
    error_count: int = 0
    rows: list[CustomerOwnedUploadRowOut] = []
    coverage_changes: dict[str, list[str | None]] = {}
    column_contract: dict[str, list[str]] = {}


class CustomerOwnedUploadSummaryOut(BaseModel):
    """One historical upload, for the "when did this data last arrive" list."""

    id: str
    customer_id: str
    filename: str | None = None
    sheet_name: str | None = None
    row_count: int = 0
    applied_count: int = 0
    created_count: int = 0
    replaced_count: int = 0
    error_count: int = 0
    uploaded_at: datetime
    source_system: str = "customer-upload"

    class Config:
        from_attributes = True


# --------------------------------------------------------------------------
# COMPANY-OWNED INVENTORY MAINTENANCE -- serves app.api.company_inventory
#
# MVP-COMPROMISE[C-03]: these schemas describe writes onto what design principle
# #5 calls a read-only Oracle projection. See app.engines.company_inventory's
# module docstring and MVP_COMPROMISES.md C-03 for the full reasoning; not
# repeated on every schema below, only on this section header and the write
# request bodies, where the compromise is actually load-bearing.
#
# Every row-level payload carries `source_system` / `synced_at` / `editable` /
# `not_editable_reason` -- the four fields a planner needs to know WHETHER a
# figure can be corrected here before they try. Absence of an on-hand row is
# `known=False`, never a zero; an absent on-order row is likewise never rendered.
# --------------------------------------------------------------------------


class CompanyOnHandRowOut(BaseModel):
    """On-hand for one (Business Unit, product). `known=False` means no
    `InventoryOnHand` row exists -- the quantity is UNKNOWN, never zero.
    """

    business_unit_id: str
    product_id: str
    product_description: str | None = None
    unit_of_measure: UnitOfMeasure
    known: bool
    row_id: str | None = None
    quantity: float | None = None
    source_system: str | None = None
    synced_at: datetime | None = None
    editable: bool = True
    not_editable_reason: str | None = None

    class Config:
        from_attributes = True


class CompanyOnOrderRowOut(BaseModel):
    """One `InventoryOnOrder` row (one PO line). Several may legitimately exist
    for the same product -- see `app.engines.company_inventory` for why this is
    per-row rather than per-product, and why it has no template/upload path.
    """

    row_id: str
    business_unit_id: str
    product_id: str
    product_description: str | None = None
    unit_of_measure: UnitOfMeasure
    quantity: float
    expected_arrival_date: datetime | None = None
    source_system: str
    source_reference: str | None = None
    synced_at: datetime | None = None
    editable: bool
    not_editable_reason: str | None = None

    class Config:
        from_attributes = True


class CompanyAssignmentLineOut(BaseModel):
    """One `InventoryAssignment` row: a quantity tied to ONE demand line."""

    row_id: str
    demand_line_id: str
    well_id: str
    well_name: str | None = None
    customer_id: str
    quantity: float
    #: Carried on the LINE too, not only on the parent group, so this row is
    #: self-describing wherever a UI renders it out of the group's context (a
    #: single-line edit form, for instance) -- same unit as the group's, because
    #: every line in a group is the same product.
    unit_of_measure: UnitOfMeasure
    source_system: str
    source_reference: str | None = None
    synced_at: datetime | None = None
    editable: bool
    not_editable_reason: str | None = None

    class Config:
        from_attributes = True


class CompanyAssignmentGroupOut(BaseModel):
    """Oracle assignment quantity for one product, aggregated, with its lines.

    `total_quantity` sums plain floats because every line is the SAME product and
    therefore the same unit -- this is not a cross-product aggregate, so it is not
    a `quantities_by_unit` case.
    """

    product_id: str
    product_description: str | None = None
    unit_of_measure: UnitOfMeasure
    total_quantity: float
    lines: list[CompanyAssignmentLineOut] = []

    class Config:
        from_attributes = True


class CompanyInventoryPositionOut(BaseModel):
    """The company-owned inventory position for one Business Unit."""

    business_unit_id: str
    business_unit_name: str
    on_hand: list[CompanyOnHandRowOut] = []
    on_order: list[CompanyOnOrderRowOut] = []
    assignments: list[CompanyAssignmentGroupOut] = []


class RecomputeFailureOut(BaseModel):
    """One customer whose coverage could not be re-derived after this write.

    Same shape and same reason as `CoverageRecomputeFailureOut` -- a second name
    only because this module does not import `app.api.admin`'s schema namespace,
    not because the meaning differs. Its stored verdicts were rolled back to
    exactly their prior state (a per-customer SAVEPOINT), so they now PREDATE this
    write.
    """

    customer_id: str
    customer_name: str
    reason: str

    class Config:
        from_attributes = True


class CoverageWellChangeOut(BaseModel):
    """One well whose coverage rollup moved because of this write."""

    well_id: str
    coverage_before: str | None = None
    coverage_after: str | None = None


class CompanyInventoryWriteOut(BaseModel):
    """Before/after for one row write, plus the coverage consequences it caused.

    `coverage_changes` lists every well whose rollup MOVED, including wells this
    write never named directly -- an on-hand or on-order edit can move every
    customer sharing that Business Unit's pool. `recompute_failures` names any
    customer whose pass could not be re-derived (see `RecomputeFailureOut`); the
    write itself still succeeded.
    """

    row_id: str | None = None
    before: dict = {}
    after: dict | None = None
    recomputed_customer_ids: list[str] = []
    coverage_changes: dict[str, list[str | None]] = {}
    recompute_failures: list[RecomputeFailureOut] = []


class CompanyOnHandEditIn(BaseModel):
    """PATCH body for `/company-inventory/on-hand/{row_id}`.

    MVP-COMPROMISE[C-03]: this request restates a figure design principle #5
    treats as Oracle-owned. See MVP_COMPROMISES.md C-03.
    """

    quantity: Quantity


class CompanyOnHandCreateIn(BaseModel):
    """POST body for `/company-inventory/on-hand`, creating a row where none
    exists.

    MVP-COMPROMISE[C-03]: see `CompanyOnHandEditIn`.
    """

    business_unit_id: str
    product_id: str
    quantity: Quantity


class CompanyOnOrderEditIn(BaseModel):
    """PATCH body for `/company-inventory/on-order/{row_id}`.

    MVP-COMPROMISE[C-03]: see `CompanyOnHandEditIn`.
    """

    quantity: Quantity
    expected_arrival_date: datetime | None = None


class CompanyOnOrderCreateIn(BaseModel):
    """POST body for `/company-inventory/on-order`.

    MVP-COMPROMISE[C-03]: see `CompanyOnHandEditIn`.
    """

    business_unit_id: str
    product_id: str
    quantity: Quantity
    expected_arrival_date: datetime | None = None


class CompanyAssignmentEditIn(BaseModel):
    """PATCH body for `/company-inventory/assignments/{row_id}`.

    MVP-COMPROMISE[C-03]: see `CompanyOnHandEditIn`.
    """

    quantity: Quantity


class CompanyAssignmentCreateIn(BaseModel):
    """POST body for `/company-inventory/assignments`.

    MVP-COMPROMISE[C-03]: see `CompanyOnHandEditIn`.
    """

    demand_line_id: str
    product_id: str
    quantity: Quantity


class CompanyInventoryUploadRowOut(BaseModel):
    """What happened to ONE spreadsheet row of the on-hand template/upload.

    Same shape as `CustomerOwnedUploadRowOut`, applied to on-hand instead of
    customer-owned stock.
    """

    row_number: int
    action: str
    raw_product: str | None = None
    raw_quantity: str | None = None
    product_id: str | None = None
    product_description: str | None = None
    quantity: float | None = None
    previous_quantity: float | None = None
    unit_of_measure: UnitOfMeasure | None = None
    error: str | None = None


class CompanyInventoryUploadOut(BaseModel):
    """The result of one on-hand upload for a Business Unit.

    MVP-COMPROMISE[C-03]: this endpoint restates Oracle-owned on-hand figures via
    a spreadsheet. See MVP_COMPROMISES.md C-03.

    `coverage_changes` lists every well whose rollup moved, `[before, after]`,
    including wells the file never named -- on-hand quantity is the foundation of
    every coverage verdict for every customer in this Business Unit.
    """

    upload_id: str
    business_unit_id: str
    filename: str | None = None
    sheet_name: str | None = None
    row_count: int = 0
    created_count: int = 0
    replaced_count: int = 0
    applied_count: int = 0
    error_count: int = 0
    rows: list[CompanyInventoryUploadRowOut] = []
    recomputed_customer_ids: list[str] = []
    coverage_changes: dict[str, list[str | None]] = {}
    recompute_failures: list[RecomputeFailureOut] = []
    column_contract: dict = {}


class CompanyInventoryUploadSummaryOut(BaseModel):
    """One historical company on-hand upload, newest first."""

    id: str
    business_unit_id: str
    filename: str | None = None
    sheet_name: str | None = None
    row_count: int = 0
    applied_count: int = 0
    created_count: int = 0
    replaced_count: int = 0
    error_count: int = 0
    uploaded_at: datetime
    source_system: str = "manual"

    class Config:
        from_attributes = True


# --------------------------------------------------------------------------
# ADMINISTRATION -- the two adjustable settings
#
# Everything below serves `app.api.admin`, which is where the product owner asked
# for "all adjustable elements ... leadtime assumption per product components ...
# also coverage scope default to be adjustable here".
#
# NO QUANTITY OF MATERIAL APPEARS IN THIS SECTION, and that is worth stating
# because `tests/test_units_of_measure.py` walks every model here demanding a unit
# for any field that carries one. `months` is a DURATION, not a quantity of steel --
# "months" is already in that test's `NON_MATERIAL_TOKENS` -- and the product/well
# counts below are dimensionless counts of rows. There is deliberately no
# `unit_of_measure` on any of these models, because there is no material to measure:
# a lead-time component says how long something takes, and a coverage scope says
# which demand is looked at.
# --------------------------------------------------------------------------


class LeadTimeComponentOut(BaseModel):
    """One additive lead-time term, as the Administration screen lists it.

    `shared` is DERIVED, not stored: it is True exactly when `attribute_value` is the
    `ANY_ATTRIBUTE_VALUE` wildcard "*", meaning the row applies to every product on
    its dimension. It is served rather than left for the client to infer, so the UI's
    "shared" convention -- the same one the lead-time breakdown already uses for a
    wildcard-matched term -- is decided in one place and cannot come out differently
    on the admin screen than on the breakdown screen.
    """

    id: str
    #: One of the four `LeadTimeDimension` values: "OD/WT", "Grade", "Connection",
    #: "Logistics". A closed set -- see `app.models.lead_time`.
    dimension: str
    attribute_value: str
    months: float
    label: str | None = None
    shared: bool = False

    class Config:
        from_attributes = True


class LeadTimeComponentsOut(BaseModel):
    """Every component row, plus the vocabulary a client needs to add one.

    `dimensions` and `wildcard` are served rather than hard-coded in the frontend for
    the reason the rest of this API states its own contracts (see
    `CustomerOwnedUploadOut.column_contract`): a client with its own copy of a closed
    enum offers options the server would reject, and a client with its own idea of the
    wildcard sentinel would write a literal row that matched nothing if the sentinel
    ever changed.

    `incomplete_note` is not decoration. All four dimensions are REQUIRED for a
    product to be modelled at all, so a table listing three of them looks healthy and
    means every product on the platform is unmodelled. The note names the dimensions
    with no rows whatsoever, which is the one thing a screen cannot work out from the
    rows it was given.
    """

    components: list[LeadTimeComponentOut]
    #: The four permitted `dimension` values, in the order the breakdown is rendered.
    dimensions: list[str]
    #: The wildcard sentinel that means "applies to every product on this dimension".
    wildcard: str
    #: Dimensions with NO row at all. Every product is "not modelled" while this is
    #: non-empty, whatever the other rows say.
    dimensions_with_no_rows: list[str]
    incomplete_note: str | None = None


class LeadTimeComponentIn(BaseModel):
    """Create one component.

    `dimension` is typed `str`, NOT the `LeadTimeDimension` enum, and that is
    deliberate. Pydantic's own enum failure is a 422 whose body describes a schema
    violation; this codebase's convention is that a refusal states the REASON and the
    corrective action in prose (see `app.engines.inventory`'s refusals and the 409/424
    handlers in `app.main`). Taking a string here lets `app.api.admin` answer with a
    400 that names the four valid dimensions and says why the set is closed.
    """

    dimension: str
    attribute_value: str
    months: float
    label: str | None = None


class LeadTimeComponentPatch(BaseModel):
    """Update a component's MONTHS and/or LABEL. Deliberately nothing else.

    `dimension` and `attribute_value` are absent on purpose, and the omission is the
    design rather than an unimplemented feature.

    That pair IS the row's identity: it is the only thing
    `app.engines.lead_time.resolve_lead_time` matches on. Changing it does not edit a
    term, it re-points the term at a DIFFERENT set of products -- so one PATCH would
    silently remove the term from every product that used to match (potentially
    flipping them to "not modelled", which retracts an Unrecoverable verdict) and add
    it to every product that now does. That is a delete and a create wearing the
    clothes of an edit, and the least honest possible shape for it is a field called
    `attribute_value`.

    Requiring DELETE + POST instead buys three things: the destructive half is
    performed by the verb that means destruction and reports its own blast radius; the
    unique-constraint conflict on (dimension, attribute_value) is reachable from
    exactly ONE endpoint, so there is one place to get the 409 right; and nothing is
    lost, because the two-call sequence expresses precisely what the single call would
    have done.

    Both fields are optional so a caller may change the months without restating the
    label. A body with neither is refused -- see `app.api.admin`.
    """

    months: float | None = None
    label: str | None = None


class ProductLeadTimeChangeOut(BaseModel):
    """One product whose resolved lead time moved because a component changed.

    Both the total and the MODELLED flag are reported before and after. They are
    different kinds of change: a total moving is a re-estimate, whereas `modelled`
    going False is the platform retracting its ability to answer -- which converts
    every `Unrecoverable` verdict for that product into something softer, because
    `app.engines.order_dates.is_recoverable` never judges an unmodelled product
    hopeless.
    """

    product_id: str
    product_description: str | None = None
    total_months_before: float
    total_months_after: float
    modelled_before: bool
    modelled_after: bool
    #: Why it is not modelled any more. Empty when `modelled_after` is True.
    missing_dimensions_after: list[str] = []
    became_unmodelled: bool = False
    became_modelled: bool = False

    class Config:
        from_attributes = True


class WellCoverageRollupChangeOut(BaseModel):
    """One well whose coverage rollup moved as a result of an administrative change."""

    well_id: str
    well_name: str
    coverage_before: str | None = None
    coverage_after: str | None = None

    class Config:
        from_attributes = True


class LeadTimeComponentChangeOut(BaseModel):
    """What a component create / update / delete actually did.

    Follows `WellDemandStatusChangeOut` and the customer-owned upload's
    `coverage_changes`: this codebase performs a consequential-but-legitimate change
    and REPORTS its blast radius rather than refusing it or hiding it behind a
    confirmation the server cannot verify.

    `component` is null for a DELETE -- the row is gone, and echoing it back would
    invite a client to render it as though it still existed.

    `products_examined` is the denominator for `product_changes`. Without it a reader
    cannot tell "no product was affected" from "no product was checked".
    """

    action: str  # "created" | "updated" | "deleted"
    component: LeadTimeComponentOut | None = None
    products_examined: int = 0
    product_changes: list[ProductLeadTimeChangeOut] = []
    #: Coverage was recomputed for these customers as part of this call.
    recomputed_customer_ids: list[str] = []
    well_changes: list[WellCoverageRollupChangeOut] = []
    #: Plain-language statement of what happened and what it means downstream, so a
    #: screen that renders only this string is still honest.
    note: str = ""


class CoverageScopeDefaultsOut(BaseModel):
    """The coverage scope the platform is evaluating under, right now.

    `persisted` is the fact the filter values alone cannot convey: an administrator
    may legitimately save the shipped values back, so "adjusted, and happens to match
    what shipped" is a different state from "never adjusted". The screen needs the
    difference to say whether it is showing a setting or a default.

    `shipped_*` is served alongside so the UI can offer "restore the shipped default"
    without carrying its own copy of two enum sets -- the same reason `dimensions` is
    served on `LeadTimeComponentsOut`.
    """

    status_filter: list[str]
    profile_filter: list[str]
    #: Every value each filter may be chosen from, so the UI renders the checkbox set
    #: the server will actually accept.
    available_statuses: list[str]
    available_profiles: list[str]
    #: What the platform ships with when nothing is persisted.
    shipped_status_filter: list[str]
    shipped_profile_filter: list[str]
    #: True when a row exists, i.e. somebody has adjusted the scope.
    persisted: bool
    #: When it was adjusted. Null exactly when `persisted` is False.
    updated_at: datetime | None = None
    #: True when the effective scope equals the shipped one, however it got there.
    matches_shipped_default: bool = True
    note: str = ""


class CoverageScopeDefaultsIn(BaseModel):
    """Set the platform-wide coverage scope.

    Both filters are required and both are lists of `DemandStatus` / `DemandProfile`
    VALUES. Typed as `list[str]` rather than as the enums for the same reason
    `LeadTimeComponentIn.dimension` is a string: the refusal has to be able to say
    what the valid values are and why an empty list is not one of them, and a Pydantic
    422 cannot.

    There is no partial update. Sending only the status filter and leaving the profile
    filter to whatever is stored would make the blast radius of a request depend on
    state the caller did not send, and this is the one setting on the platform that
    changes every coverage verdict at once.
    """

    status_filter: list[str]
    profile_filter: list[str]


class CoverageScopeDefaultsChangeOut(BaseModel):
    """What saving the coverage scope did, before and after.

    `well_changes` lists only the wells whose rollup MOVED; `recomputed_well_count` is
    how many were visited. Both are needed: a change that moved nothing and a change
    that was never applied look identical from the list alone.
    """

    status_filter_before: list[str]
    profile_filter_before: list[str]
    status_filter_after: list[str]
    profile_filter_after: list[str]
    #: True => the requested scope was already in effect; nothing was written and
    #: nothing was recomputed.
    unchanged: bool = False
    #: True => every customer's coverage was recomputed inside this request. See
    #: `app.engines.coverage_scope` for why deferring was rejected.
    recomputed_synchronously: bool = False
    recomputed_customer_ids: list[str] = []
    recomputed_well_count: int = 0
    well_changes: list[WellCoverageRollupChangeOut] = []
    note: str = ""


# --------------------------------------------------------------------------
# Substitution master data  (/admin/technical-substitutions,
#                            /admin/customer-substitution-rules)
#
# The two tables `app.engines.substitution` GATES on, and the only two rows in the
# three-layer model that are MASTER DATA rather than a per-line decision. They were
# seed-only; a planner asking "where do I register a substitution item?" had no
# answer. See app.api.admin and app.engines.substitution_admin.
#
# Every payload below carries product DESCRIPTIONS alongside the ids. That is a
# standing rule in this API rather than a courtesy here (see `ByItemDemandLineOut`,
# `ProductSurplusOut`, `ProductLeadTimeChangeOut`): a bare uuid on a screen is
# unreadable, and a picker built from ids alone cannot be checked by the person
# using it. `*_description` is nullable because `Product.description` is.
# --------------------------------------------------------------------------


class CoverageRecomputeFailureOut(BaseModel):
    """One customer whose coverage could NOT be re-derived after a substitution edit.

    `app.engines.inventory` refuses to invent an on-hand quantity, and a technical
    substitution is BU-agnostic while the recompute is BU-scoped -- so registering a
    pair can require an inventory row some Business Unit has never received. Blocking
    the write for that reason would let one warehouse's missing feed veto an
    engineering fact, so the write proceeds and the customer is reported here instead.

    Its stored verdicts were rolled back to exactly their prior state (a per-customer
    SAVEPOINT, never half-written) and therefore now PREDATE this change. That is the
    fact this object exists to carry: a reader seeing only `recomputed_customer_ids`
    would take the omission for "nothing to do".

    `reason` is the engine's own message, passed through verbatim because it names the
    product, the Business Unit and the corrective action.
    """

    customer_id: str
    customer_name: str
    reason: str

    class Config:
        from_attributes = True


class TechnicalSubstitutionOut(BaseModel):
    """One layer-1 engineering claim: from_product MAY be substituted by to_product.

    DIRECTIONAL. A -> B does not imply B -> A, and both rows may legitimately exist,
    so the list never collapses a pair into one bidirectional row.

    The counts are not decoration. This row is a GATE, and what a reader cannot see
    from the pair alone is what is currently standing on it:

      * `customer_rule_count` / `allowing_customer_rule_count` -- how many customers
        have an opinion on this pair, and how many permit it. A technical row with no
        allowing customer rule grants nobody anything yet, because layer 2 is a true
        allow-list and silence never permits.
      * `approved_well_approval_count` -- demand lines whose customer has already
        approved this exact substitution. These are what a DELETE takes coverage from.
    """

    id: str
    from_product_id: str
    from_product_description: str | None = None
    to_product_id: str
    to_product_description: str | None = None
    customer_rule_count: int = 0
    allowing_customer_rule_count: int = 0
    well_approval_count: int = 0
    approved_well_approval_count: int = 0

    class Config:
        from_attributes = True


class TechnicalSubstitutionsOut(BaseModel):
    """Every layer-1 claim on the platform, plus what the layers mean.

    `note` states the allow-list semantics because a screen showing only this table
    would otherwise imply that registering a technical substitution makes a
    substitution usable. It does not: layers 2 and 3 must clear as well.
    """

    substitutions: list[TechnicalSubstitutionOut]
    #: Layer-1 rows that no customer rule permits, so they currently gate nothing.
    #: Served rather than counted client-side for the reason
    #: `LeadTimeComponentsOut.dimensions_with_no_rows` is: the table looks configured
    #: and is not.
    unpermitted_count: int = 0
    note: str = ""


class TechnicalSubstitutionIn(BaseModel):
    """Register one layer-1 claim.

    Both ids are required and must differ; `app.api.admin` refuses `from == to` with a
    400 that says why rather than letting a self-substitution be stored. There is no
    PATCH counterpart: the pair IS the row -- it holds no other data at all -- so
    "editing" it is a delete plus a create, and the destructive half must be performed
    by the verb that reports what it broke. Same reasoning as `LeadTimeComponentPatch`.
    """

    from_product_id: str
    to_product_id: str


class TechnicalSubstitutionChangeOut(BaseModel):
    """What registering or removing a layer-1 claim actually did.

    Follows `LeadTimeComponentChangeOut` and `WellDemandStatusChangeOut`: the change is
    performed and its blast radius REPORTED, never refused and never hidden behind a
    confirmation the server cannot verify.

    `substitution` is null for a DELETE -- the row is gone, and echoing it back would
    invite a client to render it as though it still existed. The pair that was removed
    is named in `note` instead.

    `approved_well_approvals_affected` is the field that makes a delete honest. Those
    approvals are customer decisions on specific demand lines that were relying on this
    pair; the rows are deliberately KEPT (see `app.engines.substitution_admin` for why,
    and for the verified reason a dangling approval cannot crash `find_candidates`) but
    they stop having any effect the moment the technical claim goes.
    """

    action: str  # "created" | "deleted"
    substitution: TechnicalSubstitutionOut | None = None
    #: Well-layer approvals naming the pair, gathered BEFORE the change.
    well_approvals_affected: int = 0
    approved_well_approvals_affected: int = 0
    pending_well_approvals_affected: int = 0
    #: Customer rules that named the pair. After a DELETE they are inert: layer 2 is
    #: only ever consulted for pairs layer 1 offers. They are not deleted -- a customer
    #: veto is a recorded commercial fact, and cascading it away would be this endpoint
    #: discarding somebody else's data.
    customer_rules_affected: int = 0
    allowing_customer_rules_affected: int = 0
    #: Coverage was recomputed for these customers inside this request. A customer
    #: ABSENT from this list is either in `coverage_recompute_failures` or does not
    #: exist -- absence never means "nothing to do".
    recomputed_customer_ids: list[str] = []
    wells_examined: int = 0
    well_changes: list[WellCoverageRollupChangeOut] = []
    #: Customers whose stored verdicts could not be re-derived and now predate this
    #: change. Non-empty is a real caveat on everything above.
    coverage_recompute_failures: list[CoverageRecomputeFailureOut] = []
    #: Plain-language statement of what happened, so a screen rendering only this
    #: string is still honest.
    note: str = ""


class CustomerSubstitutionRuleOut(BaseModel):
    """One layer-2 rule: whether THIS customer permits THIS pair.

    `allowed=False` and "no row at all" both block the substitution, and they are
    different facts: the first is a recorded customer decision, the second is silence.
    `find_candidates` treats them identically (a true allow-list), which is exactly why
    the difference has to be visible HERE -- deleting an allowing rule is not a
    cleanup, it reverts the pair to blocked.

    `technical_substitution_exists` is served because a rule for a pair with no layer-1
    claim is INERT: `find_candidates` derives its candidate set from the technical rows
    and never looks at a rule outside it. Such a rule is not wrong and not refused (see
    `app.api.admin` for why), but a screen must be able to say it is doing nothing yet
    rather than showing it as live configuration.
    """

    id: str
    customer_id: str
    customer_name: str | None = None
    from_product_id: str
    from_product_description: str | None = None
    to_product_id: str
    to_product_description: str | None = None
    allowed: bool
    #: False => this rule currently has no effect whatsoever. See the class docstring.
    technical_substitution_exists: bool = True
    #: Why it is inert, in words. Null exactly when `technical_substitution_exists`.
    inert_reason: str | None = None

    class Config:
        from_attributes = True


class CustomerSubstitutionRulesOut(BaseModel):
    """Layer-2 rules, for one customer or for all of them."""

    rules: list[CustomerSubstitutionRuleOut]
    #: Echoed back so a screen can prove it is showing a filtered list. Null => all
    #: customers.
    customer_id: str | None = None
    #: Rules whose pair has no technical substitution, so they gate nothing today.
    inert_count: int = 0
    note: str = ""


class CustomerSubstitutionRuleIn(BaseModel):
    """Create one layer-2 rule.

    `allowed` is REQUIRED, with no default. A defaulted permission is the one mistake
    this payload must not make possible in either direction: defaulting to True would
    let a dropped field grant a customer permission nobody agreed, and defaulting to
    False would write a recorded VETO -- a stronger statement than silence -- for a
    caller that merely forgot the field.
    """

    customer_id: str
    from_product_id: str
    to_product_id: str
    allowed: bool


class CustomerSubstitutionRulePatch(BaseModel):
    """Flip `allowed`. Deliberately the only editable field.

    A customer that used to disallow a substitution and now permits it (or the
    reverse) is a NORMAL commercial change, not data cleanup, so it gets its own verb
    instead of being expressed as delete-then-create. The two are not equivalent:
    delete-then-create passes through "no row", which is a third state, and it loses
    the row's identity along the way.

    `customer_id`, `from_product_id` and `to_product_id` are absent for the same reason
    `LeadTimeComponentPatch` omits its key columns -- that triple is the row's identity,
    and re-pointing a permission at a different customer or a different pair is a
    delete plus a create wearing the clothes of an edit.
    """

    allowed: bool


class CustomerSubstitutionRuleChangeOut(BaseModel):
    """What creating, flipping or removing a layer-2 rule actually did.

    `allowed_before` / `allowed_after` are the whole point of the PATCH response, and
    they use null to mean "the row did not exist on that side of the change" -- so a
    create reads `null -> true` and a delete reads `true -> null`. Null is NOT a third
    permission value; it says the rule was absent, which the engine treats as not
    permitted.

    `warning` is populated, and the write still PERFORMED, when the rule names a pair
    with no technical substitution. See `app.api.admin` for why that is a warning
    rather than a refusal.
    """

    action: str  # "created" | "updated" | "deleted"
    rule: CustomerSubstitutionRuleOut | None = None
    #: Null when the row did not exist on that side of the change. See the docstring.
    allowed_before: bool | None = None
    allowed_after: bool | None = None
    #: Non-null => the change was made AND something about it needs saying.
    warning: str | None = None
    #: A customer ABSENT from this list is either in `coverage_recompute_failures` or
    #: does not exist -- absence never means "nothing to do".
    recomputed_customer_ids: list[str] = []
    wells_examined: int = 0
    well_changes: list[WellCoverageRollupChangeOut] = []
    #: Customers whose stored verdicts could not be re-derived and now predate this
    #: change. Non-empty is a real caveat on everything above.
    coverage_recompute_failures: list[CoverageRecomputeFailureOut] = []
    note: str = ""


# ---------------------------------------------------------------------------
# Business Unit creation, and customer configuration (BU mapping + policy)
#
# The Administration screen said these were "maintained upstream". That was true of
# the customer MASTER and stays true -- nothing here creates or renames a customer --
# but it was never true of the two fields below. `Customer.business_unit_id` is THIS
# platform's mapping of a customer onto an inventory pool whose rows are its own, and
# `allocation_policy` is a commercial modelling choice Oracle holds no column for.
# See `app.engines.customer_admin` for the full argument and for the three refusals.
# ---------------------------------------------------------------------------


class BusinessUnitIn(BaseModel):
    """Create one Business Unit. Name only -- a BU has nothing else to configure.

    Uniqueness is enforced (see `app.models.business_unit.BusinessUnit`), and the
    endpoint's pre-check is case-insensitive: the name is the only handle a human has
    on a BU, and two indistinguishable rows in a remap dropdown would put an operator
    one click from moving a customer into the wrong inventory pool.
    """

    name: str


class BusinessUnitPatch(BaseModel):
    """Rename one Business Unit. The name is the only field this accepts.

    There is deliberately no parent field. A BusinessUnit in this platform is FLAT --
    it has no `parent_id` at all, and `app.engines.coverage` states why: the pool a
    customer draws from is a flat join and no recursive walk of a hierarchy is needed.
    A sibling implementation of this platform models BUs as a tree and offers a
    parent remap; that capability was reviewed and deliberately NOT adopted, because
    a hierarchy that no engine reads is a field an operator can set and be misled by.

    Renaming changes no coverage verdict: the BU id is what every pool, assignment and
    scope resolves through, and the name is presentation. It is still guarded like the
    create path, because the name is the only handle a human has on a BU.
    """

    name: str


class BusinessUnitDeleteBlockedOut(BaseModel):
    """Why a delete was refused, itemised by what still points at the BU.

    Served as the 409 body rather than a sentence alone so the screen can list the
    obstacles instead of asking the operator to go hunting. Every count is of rows
    that would be orphaned -- the BU is an absolute inventory boundary, so a dangling
    `business_unit_id` is not untidiness, it is a customer whose coverage can no
    longer be computed at all.
    """

    business_unit: BusinessUnitOut
    customer_count: int = 0
    inventory_on_hand_row_count: int = 0
    inventory_on_order_row_count: int = 0
    company_inventory_upload_count: int = 0
    user_count: int = 0
    scenario_override_count: int = 0
    note: str = ""


class CoverageRecomputeIn(BaseModel):
    """Ask the engine to rewrite the stored verdicts. Optionally for one customer.

    `customer_id` omitted means every customer. This is the explicit trigger that
    C-08 says the platform lacks: stored `CoverageResult` rows are written only when
    something happens (a revision applied, an approval decided, a well status changed,
    inventory edited), so after an out-of-band data change -- or an engine fix -- the
    grid can be correct in code and stale in the database. `computed_at` already makes
    that staleness visible on the Coverage screen; this makes it fixable without
    inventing a write just to provoke a recompute.

    It does NOT take status/profile filters. A recompute writes the OFFICIAL verdict,
    and the official verdict is the one computed under the platform's default scope --
    see `app.engines.coverage_view.scoped_verdicts` for the read-only projection that
    answers "what if the scope were different", which never persists anything.
    """

    customer_id: str | None = None


class CoverageRecomputeSkippedOut(BaseModel):
    """One customer the recompute could not evaluate, named with the reason."""

    customer_id: str
    name: str
    reason: str


class CoverageRecomputeOut(BaseModel):
    """What the recompute wrote, and who it could not write for.

    Failures are isolated PER CUSTOMER and each customer is committed on its own, so
    one customer with incomplete inventory facts cannot cost every other customer its
    recompute -- the same C-07 lesson the read path learned, applied to the write
    path. A skipped customer keeps whatever verdicts it already had; nothing is
    half-written and nothing is silently dropped.
    """

    #: Customers whose verdicts were rewritten and committed.
    computed_customers: int
    #: Demand lines those customers wrote a verdict for.
    computed_lines: int
    skipped_customers: list[CoverageRecomputeSkippedOut] = []
    note: str = ""


class BusinessUnitCreatedOut(BaseModel):
    """The created Business Unit, plus what it can and cannot do yet.

    A brand-new BU is INERT and looks configured, which is the state worth stating: it
    has no customers, and -- more importantly -- no `InventoryOnHand` rows. Mapping a
    customer into it before the inventory feed has delivered those rows is exactly the
    remap `PATCH /customers/{id}` refuses, so the note says so here rather than letting
    the operator discover it from a 409 two clicks later.
    """

    business_unit: BusinessUnitOut
    #: `InventoryOnHand` rows already present for this BU. Always 0 on creation; served
    #: so the field means the same thing here as everywhere else the screen reads it.
    inventory_on_hand_row_count: int = 0
    note: str = ""


class CustomerConfigPatch(BaseModel):
    """Change a customer's Business Unit and/or its allocation policy.

    ONE endpoint for both fields, deliberately. They feed the SAME coverage pass -- the
    BU decides which quantities `app.engines.inventory.ownership_pool_map` resolves, the
    policy decides how `app.engines.allocation` divides them -- so a combined change
    must be recomputed ONCE, after both mutations. Two endpoints would either recompute
    twice (briefly storing an intermediate state, new BU under the old policy, that
    nobody asked for) or leave the caller to sequence two consequential writes and
    handle a failure between them.

    Both fields are OPTIONAL and `model_fields_set` distinguishes "not sent" from
    "sent as null". That distinction is load-bearing for `business_unit_id`: null is a
    REAL value there (the unmapped state), so a schema default of None would make
    omitting the field indistinguishable from asking to un-map -- which is refused
    outright, and must not be reachable by accident.

    `allocation_policy` arrives as a plain string rather than as the enum so the refusal
    can name the three values and say what each MEANS for the verdict, instead of
    Pydantic's generic 422. Same stance as `app.api.admin._parse_dimension`.
    """

    business_unit_id: str | None = None
    allocation_policy: str | None = None


class CustomerConfigChangeOut(BaseModel):
    """What a customer-configuration PATCH actually did.

    Follows `LeadTimeComponentChangeOut` and `TechnicalSubstitutionChangeOut`: the
    change is performed and its blast radius REPORTED, using the same
    `WellCoverageRollupChangeOut` rows so the Administration screen renders one kind of
    before/after list whichever section produced it.

    `recomputes_performed` is 1 for a change that moved BOTH fields, not 2. It is
    reported rather than implied so "recomputed exactly once" is checkable.

    `coverage_resolvable_before` / `unresolved_reason` carry the one caveat this report
    can need. A policy change on a customer whose inventory facts were ALREADY
    incomplete is performed anyway (a policy cannot cause a missing on-hand row -- the
    products a pass must resolve are identical under all three policies -- and refusing
    would make the field permanently uneditable for the customers that most need it),
    but its stored verdicts were rolled back to exactly what they were and therefore
    still predate this change. Without these two fields an empty `well_changes` would
    read as "nothing moved".
    """

    customer: CustomerOut
    unchanged: bool = False

    business_unit_changed: bool = False
    business_unit_id_before: str | None = None
    business_unit_id_after: str | None = None
    business_unit_name_before: str | None = None
    business_unit_name_after: str | None = None

    allocation_policy_changed: bool = False
    allocation_policy_before: str = ""
    allocation_policy_after: str = ""

    #: Denominator for `well_changes`: how many of this customer's wells were visited.
    wells_examined: int = 0
    well_changes: list[WellCoverageRollupChangeOut] = []
    #: 1 for any real change including a combined one; 0 for a no-op.
    recomputes_performed: int = 0
    neighbour_recompute_failures: list[str] = []

    coverage_resolvable_before: bool = True
    #: The engine's own message when coverage still cannot be resolved. See docstring.
    unresolved_reason: str | None = None

    note: str = ""

# ---------------------------------------------------------------------------
# Material Order Requirements -- the monthly order grid (app.engines.mor)
# ---------------------------------------------------------------------------


class MorCellOut(BaseModel):
    """One (product, month) cell; every quantity is in `unit_of_measure`."""

    unit_of_measure: UnitOfMeasure
    month: date
    demand: float
    arrivals: float
    projected_balance: float
    order_requirement: float
    #: Month the covering order must be placed (need month minus total lead
    #: time). Null when nothing is required this month or the lead time is not
    #: modelled.
    order_by_month: date | None = None

    class Config:
        from_attributes = True


class MorRowOut(BaseModel):
    """One product's order-requirement row.

    `available=False` means the inventory position is UNKNOWN: render `reason`,
    never figures -- the row exists so the missing data is visible, not so it
    can be netted from a fabricated 0.
    """

    product_id: str
    product_description: str | None
    unit_of_measure: UnitOfMeasure
    available: bool
    #: Planner-set safety stock (product's unit). None = not set -- distinct
    #: from an explicit 0. When set, requirements trigger below this level.
    safety_stock: float | None = None
    position: InventoryPositionOut | None = None
    lead_time_months: float | None = None
    lead_time_modelled: bool = False
    lead_time_note: str = ""
    total_demand: float = 0.0
    #: Overdue portion of total_demand (ROS month already passed) -- folded
    #: into the first month of the grid, labelled here.
    total_overdue_demand: float = 0.0
    total_order_requirement: float = 0.0
    order_flag: bool = False
    first_order_by: date | None = None
    already_late: bool = False
    cells: list[MorCellOut] = []
    reason: str | None = None

    class Config:
        from_attributes = True


class MorGridOut(BaseModel):
    months: list[date]
    horizon_months: int
    generated_for_customer_id: str | None
    rows: list[MorRowOut] = []
    unavailable_count: int = 0
    notes: list[str] = []

    class Config:
        from_attributes = True

# ---------------------------------------------------------------------------
# Surplus List (app.engines.surplus) -- quantity-only decomposition
# ---------------------------------------------------------------------------


class SurplusRowOut(BaseModel):
    """One (BU, product): allocated + surplus + obsolete == on_hand exactly."""

    business_unit_id: str
    business_unit_name: str | None
    product_id: str
    product_description: str | None
    unit_of_measure: UnitOfMeasure
    on_hand: float
    allocated: float
    surplus: float
    obsolete: float
    demand_in_window: float
    #: Overdue portion of demand_in_window (ROS already passed) -- counted,
    #: labelled separately.
    demand_overdue: float = 0.0
    customer_owned: float

    class Config:
        from_attributes = True


class SurplusReportOut(BaseModel):
    horizon_months: int
    generated_at: datetime
    rows: list[SurplusRowOut] = []
    # MT headlines (display-layer, C-05; floor semantics)
    allocated_tonnes: MeasureOut
    surplus_tonnes: MeasureOut
    obsolete_tonnes: MeasureOut
    unknown_position_count: int = 0
    note: str
    #: The demand scope the allocation was computed under. When
    #: `scope_is_default` is False the figures came from a read-only recompute.
    status_scope: list[str] = []
    profile_scope: list[str] = []
    scope_is_default: bool = True
    #: Customers whose scoped recompute was skipped (no BU / missing on-hand
    #: row). Named so a scoped answer can never silently omit a customer.
    skipped_customers: list[str] = []

    class Config:
        from_attributes = True

# ---------------------------------------------------------------------------
# Safety stock administration (app.models.safety_stock)
# ---------------------------------------------------------------------------


class ApprovalQueueRowOut(BaseModel):
    """One row of the substitution-approval queue (GET /substitution-approvals).

    `quantity` carries `unit_of_measure` beside it, per the platform-wide
    contract; both are None together when the demand line no longer exists
    (an approval outlives a deleted line -- reported, not hidden)."""

    approval_id: str
    demand_line_id: str
    status: str
    requested_at: datetime
    decided_at: datetime | None = None
    #: Server-recorded decider (F09); None for rows decided before it existed.
    decided_by_user_name: str | None = None
    well_id: str | None = None
    well_name: str | None = None
    customer_name: str | None = None
    from_product_description: str
    to_product_description: str
    quantity: float | None = None
    unit_of_measure: UnitOfMeasure | None = None
    ros_date: datetime | None = None

    class Config:
        from_attributes = True


class SafetyStockRowOut(BaseModel):
    """One product's safety stock setting. `quantity` is None when NOT SET --
    a real state, distinct from an explicit 0."""

    product_id: str
    product_description: str | None
    unit_of_measure: UnitOfMeasure
    quantity: float | None = None
    note: str | None = None

    class Config:
        from_attributes = True


class SafetyStockListOut(BaseModel):
    rows: list[SafetyStockRowOut] = []
    note: str


class SafetyStockIn(BaseModel):
    quantity: Quantity
    note: str | None = None
