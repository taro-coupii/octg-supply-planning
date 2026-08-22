"""Scenario override RESOLUTION -- the values the coverage engine reads.

Why this module exists at all
-----------------------------
Scenario preview has to answer "what would coverage be if these values were
different?" and it has to answer it with the SAME rules the official verdict
uses. There were two ways to get there:

  (a) Re-implement the coverage rules over overridden values. Rejected. That is
      a second copy of the allocation ordering, the substitution fall-through,
      the shortage wording and the well rollup, and the moment the two drift the
      preview starts lying -- confidently, and about the one thing planners are
      using it to decide. (app.engines.sharing gets away with a narrow
      re-derivation because it deliberately answers a much smaller question and
      says so; a scenario preview cannot.)

  (b) Give the ONE existing implementation a way to read different values.

This module is (b): a pure, read-only INDIRECTION LAYER between
`app.engines.coverage` and the facts it reads. The coverage engine now asks a
resolver for every value a scenario is allowed to change, and the resolver either
returns the override or returns the fact unchanged. There is exactly one coverage
algorithm, in `app.engines.coverage._compute_customer_coverage`, and the official
recompute and the scenario preview are the same call with a different resolver.

`NO_OVERRIDES` is the identity resolver
---------------------------------------
The official path is NOT a special case with the resolver bypassed -- it passes
`NO_OVERRIDES`, whose every method returns its input untouched. That is
deliberate: if the un-overridden path took a different branch, "preview and
apply agree" would rest on two code paths staying in step, which is the property
that always rots. Instead they are byte-for-byte the same path.

Everything here is READ-ONLY
----------------------------
Nothing in this module writes, flushes, or holds a Session. `LineView` wraps a
DemandLine and exposes EFFECTIVE values without ever assigning to the mapped
attributes -- overriding a quantity must not make the ORM think the row changed,
or `Session.dirty` would grow and the preview would be one commit away from
becoming real. That is why views exist instead of "mutate, compute, roll back".
"""

from dataclasses import dataclass
from datetime import date, datetime

from app.models import (
    DemandLine,
    DemandProfile,
    DemandStatus,
    ScenarioTargetKind,
    SubstitutionApprovalStatus,
)

# ----------------------------------------------------------------------------
# The override vocabulary, and what is legal in it
# ----------------------------------------------------------------------------

#: Field names legal for each target kind, and the value column each one uses.
#: Single source of truth for validation, for the API's error messages, and for
#: the frontend's field pickers (exposed via GET /scenarios/override-fields).
#:
#: "number" -> value_number, "date" -> value_date, "text" -> value_text.
OVERRIDE_FIELDS: dict[ScenarioTargetKind, dict[str, str]] = {
    ScenarioTargetKind.DEMAND_LINE: {
        "quantity": "number",
        "ros_date": "date",
        "profile": "text",
    },
    # `status` is NOT here, and `demand_status` is NOT on DEMAND_LINE. Demand
    # status is a property of the WELL (`app.models.well.Well.demand_status`), so
    # a per-line status override would let a scenario preview model one well at two
    # statuses -- the state the column move made unrepresentable in the base plan.
    # The question survives at well level, and is both previewable and applicable.
    ScenarioTargetKind.WELL: {
        "demand_status": "text",
    },
    ScenarioTargetKind.INVENTORY: {
        "quantity": "number",
    },
    # CUSTOMER-OWNED INVENTORY IS DELIBERATELY NOT AN OVERRIDE TARGET AT ALL.
    #
    # It is tempting to add one, and the tempting argument is a good one: unlike
    # `InventoryOnHand` -- Oracle-owned, which is exactly why an INVENTORY override is
    # previewable but NOT applicable (see `SUPPLY_KINDS` and
    # `app.engines.scenario.apply_to_base_plan`) -- customer-owned stock is
    # PLATFORM-OWNED (`app.models.customer_owned_inventory.CustomerOwnedInventory`),
    # so an override of it could honestly be applied. Being able to apply it is
    # precisely the problem.
    #
    # Two reasons, and the second is the decisive one:
    #
    #   1. There is already a first-class, cheap, real action for changing this
    #      number: upload a corrected spreadsheet. A scenario exists to ask "what if
    #      a fact we CANNOT change were different" -- an Oracle quantity, a mill lead
    #      time, a customer's approval. "What if the customer owned 500 more" is not
    #      that question; it is a data-entry correction wearing a what-if costume, and
    #      the honest answer to it is a new upload.
    #   2. Applying such an override would make scenario-apply a SECOND WRITER of
    #      customer-owned quantities, and one with no provenance to write: no
    #      filename, no `uploaded_at`, no upload row. The whole reason those columns
    #      exist is that this data arrives from a spreadsheet a human produced and its
    #      AGE matters to a planner. A quantity written by a scenario would sit beside
    #      genuinely uploaded ones, indistinguishable, claiming a currency it does not
    #      have -- the single worst outcome available here.
    #
    # A planner who wants to model it can still do so through the door that keeps the
    # provenance: upload the corrected file. The coverage pass reads customer-owned
    # stock through `app.engines.inventory.ownership_pool_map` and
    # `OwnershipPool.with_company_owned` is the only mutator that layer exposes, so
    # there is no resolver hook for a customer-owned quantity to be restated through
    # either. The refusal is structural, not just absent from this dict.
    # TWO FIELDS, AND THEY ARE NOT VARIANTS OF EACH OTHER.
    #
    #   arrival_date  -- "change a fact about a REAL row": shift the earliest dated
    #                    `InventoryOnOrder` row for this product (and carry the later
    #                    ones by the same offset). Product-scoped. One value: a date.
    #   new_order     -- "assert a row that DOES NOT EXIST": a hypothetical purchase
    #                    order of `value_number` units landing on `value_date`, for
    #                    (Business Unit, product). TWO values, both required.
    #
    # WHY A DISTINCT FIELD RATHER THAN A `quantity` FIELD THAT MEANS "NEW ORDER WHEN
    # NO REAL ROW MATCHES"
    # -------------------------------------------------------------------------
    # That shape was considered and rejected, and the reason is not tidiness. Its
    # meaning would depend on the STATE OF ANOTHER TABLE AT READ TIME: the very same
    # persisted row would mean "resize the PO Oracle promised" while that PO existed
    # and silently become "invent a PO that never existed" the moment an Oracle sync
    # dropped the row -- with no edit, no audit trail, and a planner reading a preview
    # that had quietly changed question. `InventoryOnOrder` is a projection that is
    # re-synced, so that is not a hypothetical failure mode; it is the normal one.
    #
    # `new_order` cannot be confused with editing a real row, and NOT merely because
    # it is spelled differently. The two are distinguishable BY SHAPE, and `validate`
    # enforces the shape:
    #
    #   * `arrival_date` takes exactly ONE value column (the date) and names no
    #     Business Unit. `new_order` REQUIRES BOTH `value_number` and `value_date`
    #     and REQUIRES a Business Unit. No row can satisfy both specifications, so a
    #     reader that lost the field name could still tell them apart -- and one that
    #     mistook a `new_order` row for an `arrival_date` row would be refused by
    #     `validate` rather than resolve to a wrong answer.
    #   * They resolve through DIFFERENT resolver members
    #     (`ScenarioOverrides._po_arrival` vs `._new_orders`) into different
    #     arguments of `app.engines.mrp.on_order_runout` (`arrival_override` shifts
    #     dated rows; `hypothetical_orders` is added beside them). Neither can reach
    #     the other's code path.
    #   * Both may exist on the same product at once and mean exactly what they say:
    #     "pull the real deliveries in by six weeks AND place an emergency order in
    #     March". Nothing has to be mutually exclusive, because nothing overlaps.
    #
    # AND IT NEVER BECOMES AN `InventoryOnOrder` ROW. Not on apply, not anywhere --
    # see `SUPPLY_KINDS` and `app.engines.scenario.apply_blockers`, which refuses it
    # for a reason of its own.
    ScenarioTargetKind.PO_ARRIVAL: {
        "arrival_date": "date",
        "new_order": "number+date",
    },
    ScenarioTargetKind.ASSIGNMENT: {
        "quantity": "number",
    },
    ScenarioTargetKind.SUBSTITUTION_APPROVAL: {
        "approval_status": "text",
    },
}

#: Legal text values, per (kind, field). Absent means "any non-empty string".
OVERRIDE_ENUM_VALUES: dict[tuple[ScenarioTargetKind, str], tuple[str, ...]] = {
    (ScenarioTargetKind.WELL, "demand_status"): tuple(s.value for s in DemandStatus),
    (ScenarioTargetKind.DEMAND_LINE, "profile"): tuple(p.value for p in DemandProfile),
    (ScenarioTargetKind.SUBSTITUTION_APPROVAL, "approval_status"): tuple(
        s.value for s in SubstitutionApprovalStatus
    ),
}

#: Kinds that touch Oracle-owned READ-ONLY PROJECTIONS -- InventoryOnHand,
#: InventoryAssignment and InventoryOnOrder. They are previewable but NOT
#: applicable -- see app.engines.scenario.apply_to_base_plan for the reasoning.
#:
#: PO_ARRIVAL stays here now that it is modelled, and the reason it stays is
#: unchanged by that: `InventoryOnOrder` is a read-only projection of Oracle's
#: purchase orders exactly as `InventoryOnHand` is of Oracle's stock, so a scenario
#: has nothing it may legitimately write. Being previewable and being applicable
#: are independent properties, and this kind is now the clearest case of the
#: first without the second.
SUPPLY_KINDS = (
    ScenarioTargetKind.INVENTORY,
    ScenarioTargetKind.PO_ARRIVAL,
    ScenarioTargetKind.ASSIGNMENT,
)

#: Kinds the preview cannot model, because nothing in this platform reads the
#: thing they describe. Reported loudly rather than ignored.
#:
#: EMPTY AS OF THE PO-ARRIVAL WORK. `PO_ARRIVAL` used to be the sole member. The
#: reason it was here -- recorded verbatim in
#: `app.models.scenario.ScenarioTargetKind` -- was:
#:
#:   "NO ENGINE READS INCOMING SUPPLY WHEN DECIDING COVERAGE. [...] there is
#:    nothing for a PO-arrival override to change. Moving a promised date would
#:    move a number on a report and no verdict anywhere."
#:
#: The first sentence is STILL TRUE and is deliberately left true: coverage,
#: substitution and allocation are still decided from BU-scoped on-hand stock
#: alone, and this change does not touch them. What was wrong was the inference
#: from it. "No verdict" was treated as "no consequence", and the platform's own
#: MRP layer already contradicts that: `app.engines.mrp` projects a monthly
#: RUNOUT BALANCE, and the month that balance goes negative is a decision figure
#: planners act on -- it is what the By Item screen draws and what
#: `runout_with_recommended_order` exists to shift. Incoming supply arriving
#: earlier or later plainly moves that month, and moving it requires no
#: coverage-rule change and no product-owner decision about whether steel landing
#: before ROS may cover a line. That decision remains UNMADE, and this override
#: does not make it.
#:
#: So the override is now consumed by `app.engines.mrp.on_order_runout` through
#: `app.engines.scenario._supply_runout_changes`, which reports the runout month
#: before and after the shift. Coverage verdicts do NOT move, and a test pins
#: that they do not. See `ScenarioOverrides.arrival_date`.
UNMODELLED_KINDS: tuple = ()


class OverrideError(ValueError):
    """An override that is not a legal override. Raised by `validate`."""


def validate(override, scenario_customer) -> None:
    """Reject an override that is malformed, mistargeted, or crosses a BU.

    The single authority on override legality -- see
    app.models.scenario.ScenarioOverride for why validation is centralised here
    rather than distributed across per-kind tables. Called by the API before an
    override is persisted, and again by the resolver when one is read, so a row
    written by an older build cannot quietly change coverage.

    `scenario_customer` is the Customer the scenario belongs to. It is needed for
    the ONE rule that is not about shape: an INVENTORY override may only touch
    that customer's own Business Unit.
    """
    kind = override.target_kind
    fields = OVERRIDE_FIELDS.get(kind)
    if fields is None:
        raise OverrideError(f"Unknown scenario override target kind: {kind!r}")

    field = override.field_name
    if field not in fields:
        allowed = ", ".join(sorted(fields))
        raise OverrideError(
            f"Field {field!r} cannot be overridden on a {kind.value} target. "
            f"Allowed: {allowed}."
        )

    # ---- the right value column(s), and no others ------------------------
    kinds_of_value = {
        "number": override.value_number,
        "date": override.value_date,
        "text": override.value_text,
    }
    expected = fields[field]
    # `"number+date"` is the ONE composite spec, and it exists for the one override
    # that describes a whole hypothetical event rather than restating a single fact:
    # a new purchase order is a QUANTITY LANDING ON A DATE, and neither half is
    # meaningful without the other. Splitting it into two rows was considered and
    # rejected -- two independent rows can be half-deleted, leaving a quantity with
    # no arrival month (unprojectable) or a date with no quantity (nothing to
    # project), and every reader would need to handle a pairing that the database
    # could not enforce. One row cannot be half-present.
    #
    # This is also what makes the shape of a `new_order` row unmistakable: it is the
    # only override in the vocabulary with two populated value columns, so it cannot
    # be read as an `arrival_date` restatement of a real PO. See `OVERRIDE_FIELDS`.
    if expected == "number+date":
        if override.value_number is None or override.value_date is None:
            raise OverrideError(
                f"Override of {kind.value}.{field} describes a hypothetical new "
                "purchase order, which is a QUANTITY LANDING ON A DATE -- it needs "
                "BOTH value_number (the quantity) and value_date (the expected "
                "arrival), and "
                + (
                    "value_number was missing."
                    if override.value_number is None
                    else "value_date was missing."
                )
                + " A quantity with no arrival date could not be projected into any "
                "month, and a date with no quantity would add nothing."
            )
        if override.value_text is not None:
            raise OverrideError(
                f"Override of {kind.value}.{field} takes a quantity and a date only; "
                "value_text must be empty."
            )
        if override.value_number <= 0:
            raise OverrideError(
                f"Override of {kind.value}.{field} must order a POSITIVE quantity "
                f"(got {override.value_number:g}). A hypothetical order of nothing "
                "is not a what-if; to model removing supply, there is nothing to "
                "remove -- this override only ever ADDS to the projection."
            )
        _assert_new_order_target(override, scenario_customer)
        return

    if kinds_of_value[expected] is None:
        raise OverrideError(
            f"Override of {kind.value}.{field} needs a {expected} value "
            f"(value_{'number' if expected == 'number' else expected}) and none was given."
        )
    for name, value in kinds_of_value.items():
        if name != expected and value is not None:
            raise OverrideError(
                f"Override of {kind.value}.{field} takes a {expected} value only; "
                f"value_{name} must be empty."
            )

    legal = OVERRIDE_ENUM_VALUES.get((kind, field))
    if legal is not None and override.value_text not in legal:
        raise OverrideError(
            f"{override.value_text!r} is not a valid {kind.value}.{field}. "
            f"Allowed: {', '.join(legal)}."
        )

    if expected == "number" and override.value_number < 0:
        raise OverrideError(
            f"Override of {kind.value}.{field} cannot be negative "
            f"(got {override.value_number:g})."
        )

    # ---- the right target ------------------------------------------------
    if kind == ScenarioTargetKind.DEMAND_LINE:
        if override.target_demand_line_id is None:
            raise OverrideError("A demand override must name a demand line.")

    elif kind == ScenarioTargetKind.WELL:
        if override.target_well_id is None:
            raise OverrideError(
                "A well override must name a well (target_well_id). Demand status "
                "is a property of the well, so there is no demand line to point at "
                "-- confirming a well confirms every line of it."
            )
        if override.target_demand_line_id is not None:
            raise OverrideError(
                "A well override must not also name a demand line: demand status "
                "applies to the whole well, and naming one of its lines would "
                "suggest the others were unaffected."
            )

    elif kind == ScenarioTargetKind.INVENTORY:
        if override.target_product_id is None:
            raise OverrideError("An inventory override must name a product.")
        _assert_same_bu(override, scenario_customer)

    elif kind == ScenarioTargetKind.PO_ARRIVAL:
        if override.target_product_id is None:
            raise OverrideError("A PO arrival override must name a product.")

    elif kind == ScenarioTargetKind.ASSIGNMENT:
        if override.target_demand_line_id is None or override.target_product_id is None:
            raise OverrideError(
                "An assignment override must name both a demand line and a product "
                "-- an assignment is a quantity of ONE product reserved to ONE line."
            )

    elif kind == ScenarioTargetKind.SUBSTITUTION_APPROVAL:
        if override.target_demand_line_id is None:
            raise OverrideError(
                "A substitution-approval override must name the demand line the "
                "approval applies to -- well-layer approval is per demand line."
            )
        if override.target_to_product_id is None:
            raise OverrideError(
                "A substitution-approval override must name the substitute "
                "(target_to_product_id)."
            )


def _assert_new_order_target(override, scenario_customer) -> None:
    """A hypothetical new order is scoped to (Business Unit, product).

    The SAME scope a real `app.models.inventory_on_order.InventoryOnOrder` row has,
    and stated for the same reason: an order is placed by a Business Unit for a
    product, and a hypothetical one that named neither would not describe anything a
    planner could go and actually do.

    The BU is REQUIRED, unlike on an `arrival_date` override -- which is the second
    half of what makes the two rows structurally distinguishable (see
    `OVERRIDE_FIELDS`). It is restricted to the scenario customer's own BU by
    `_assert_same_bu`, exactly as an INVENTORY override is: "what if BU Gulf placed
    an order for us" is not this question.

    HONEST LIMIT ON WHAT THE BU DOES. It is recorded as the fact "this is who would
    place the order", and it does NOT narrow the projection the order lands in:
    `app.engines.mrp.on_order_runout` reads on-order across every Business Unit
    (`app.engines.inventory.total_on_order_rows_all_bus`), so both the real rows and
    this hypothetical one are all-BU figures in that curve. Refusing a cross-BU
    target anyway is not decoration -- it stops a scenario from RECORDING an
    intention outside its own boundary, which is the boundary the whole override
    vocabulary respects.
    """
    if override.target_product_id is None:
        raise OverrideError(
            "A hypothetical new-order override must name the product being ordered."
        )
    if override.target_demand_line_id is not None:
        raise OverrideError(
            "A hypothetical new-order override must not name a demand line. "
            "Incoming supply is not reserved to a line -- it is material arriving "
            "for a product -- and naming one would imply an allocation that does "
            "not exist."
        )
    _assert_same_bu(override, scenario_customer, what="hypothetical new-order override")


def _assert_same_bu(override, scenario_customer, what: str = "inventory override") -> None:
    """A supply override may not reach outside the scenario customer's BU.

    The Business Unit is the outermost inventory boundary and it is never crossed
    by anything (see app.models.business_unit.BusinessUnit). A scenario is not an
    exception: "what if BU Gulf lent us its stock" is not a supply override, it is
    a different question, and the platform's answer to it is the read-only
    cross-customer sharing analysis -- which is itself intra-BU.

    Refusing here rather than clamping at read time is deliberate: a clamped
    override would sit in the scenario looking as though it had been taken into
    account.
    """
    declared = override.target_business_unit_id
    own = scenario_customer.business_unit_id

    if own is None:
        # An unmapped customer has NO inventory pool: on-hand exists only per
        # (Business Unit, product), and the legacy unscoped quantity an override
        # could once have restated is gone. So there is nothing to override
        # whether a BU is named or not, and both forms are refused. Refusing at
        # creation is better than letting the override sit in the scenario looking
        # accounted-for, only for the preview to raise InventoryScopeMissing.
        raise OverrideError(
            f"Customer {scenario_customer.name!r} is not mapped to a Business "
            f"Unit, so it has no inventory pool and an {what} has "
            "nothing to apply to"
            + (
                f" (it named BU {declared!r})."
                if declared is not None
                else ". Map the customer to a Business Unit first."
            )
        )

    if declared is None:
        raise OverrideError(
            f"An {what} must state the Business Unit it applies to "
            f"(expected {own!r}, the scenario customer's own BU)."
        )
    if declared != own:
        raise OverrideError(
            f"Refusing an {what} for Business Unit {declared!r}: "
            f"scenario customer {scenario_customer.name!r} belongs to BU {own!r}. "
            "The Business Unit is a hard inventory boundary and a scenario may "
            "not reach across it. To ask whether another party's stock could "
            "help, use the cross-customer sharing analysis (which is itself "
            "restricted to one BU)."
        )


# ----------------------------------------------------------------------------
# Line views -- effective demand values without touching the ORM row
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class LineView:
    """A demand line as the coverage engine should SEE it, override applied.

    Duck-typed against `DemandLine` for every attribute the engines actually
    read: `id`, `well_id`, `product_id`, `quantity`, `ros_date`, `status`,
    `profile`, `product`, `well`. That is what lets
    `app.engines.allocation.allocate_detailed`,
    `app.engines.substitution.find_candidates`,
    `app.engines.order_dates.is_recoverable` and
    `app.engines.mrp._recommendations_for_lines` all be reused verbatim.

    Frozen, and it never assigns to `line`'s mapped attributes. Overriding a
    quantity by writing `line.quantity = x` and rolling back would be the obvious
    shortcut and it is the wrong one: it makes the object dirty, so an unrelated
    autoflush -- or any later commit on the same Session -- could persist a
    what-if as fact. The whole no-write guarantee would then rest on nobody
    committing at the wrong moment.

    `line` is exposed so the persisting caller (recompute_customer) can reach
    the real row it must write against. Read-only consumers must not use it to
    read values, or they would bypass the override; `_effective` fields are the
    contract.
    """

    line: DemandLine
    quantity: float
    ros_date: datetime
    #: The effective demand status OF THIS LINE'S WELL, not of the line.
    #:
    #: `DemandLine` has no status column any more (see `app.models.well.Well`), so
    #: this is `line.well.demand_status` unless a WELL override restates it. The
    #: name is kept as `status` on purpose: `app.engines.coverage
    #: .compute_customer_coverage` applies the status filter as
    #: `view.status in status_filter`, and having the WELL's value arrive through
    #: that attribute is precisely what makes the status filter select whole wells
    #: with no extra branch -- every line of a well necessarily reports the same
    #: value here, so they enter and leave scope together, by construction.
    status: DemandStatus
    profile: DemandProfile
    #: True when at least one field differs from the persisted row -- including a
    #: status that differs from the WELL's stored status.
    overridden: bool = False

    @property
    def id(self) -> str:
        return self.line.id

    @property
    def well_id(self) -> str:
        return self.line.well_id

    @property
    def product_id(self) -> str:
        return self.line.product_id

    @property
    def product(self):
        return self.line.product

    @property
    def well(self):
        return self.line.well

    @property
    def current_revision_no(self) -> int:
        return self.line.current_revision_no


# ----------------------------------------------------------------------------
# Resolvers
# ----------------------------------------------------------------------------


class OverrideResolver:
    """Base resolver: returns every fact unchanged.

    `NO_OVERRIDES` (an instance of this class) is what the OFFICIAL coverage pass
    uses, so the official pass and the scenario preview run the identical code
    path. Subclass `ScenarioOverrides` supplies the scenario's values.
    """

    #: Kinds present but not modelled, for the preview to report. Empty here.
    unmodelled: tuple = ()

    def view(self, line: DemandLine) -> LineView:
        """The demand line as coverage should see it.

        `status` comes from the line's WELL. The relationship access is cheap in
        the pass that matters: `compute_customer_coverage` loads every well of the
        customer before building views, so each `line.well` is an identity-map hit
        rather than a query.
        """
        return LineView(
            line=line,
            quantity=line.quantity,
            ros_date=line.ros_date,
            status=line.well.demand_status,
            profile=line.profile,
            overridden=False,
        )

    def on_hand(self, product_id: str, base_qty: float) -> float:
        """Resolved BU on-hand quantity for `product_id`.

        `base_qty` has ALREADY been through `app.engines.inventory.on_hand_map`
        for the scenario customer's Business Unit, so an override here can only
        restate a quantity inside that BU. The boundary is upstream of this call
        and cannot be reached from it -- and since `on_hand_map` raises rather than
        returning a number it could not resolve, `base_qty` is always a real
        BU-scoped quantity rather than a fallback.
        """
        return base_qty

    def assigned(self, line_id: str, product_id: str, base_qty: float) -> float:
        """Quantity assigned to (`line_id`, `product_id`)."""
        return base_qty

    def assignment_overrides(self) -> dict[tuple[str, str], float]:
        """Every assignment override, so the engine can also fix up the
        reserved-per-product totals an override changes. Empty here."""
        return {}

    def inventory_override_product_ids(self) -> frozenset[str]:
        """Products carrying an on-hand override, so the engine resolves a
        quantity for them even when nothing demands them. Empty here."""
        return frozenset()

    def arrival_date(self, product_id: str, base_earliest):
        """Restated EARLIEST expected arrival for `product_id`'s incoming supply.

        Returns `base_earliest` unchanged here. `ScenarioOverrides` returns the
        PO_ARRIVAL override's date when one names this product.

        DELIBERATELY NOT CONSULTED BY THE COVERAGE PASS. Unlike every other
        method on this class, no call to this one exists anywhere in
        `app.engines.coverage`, `app.engines.allocation` or
        `app.engines.substitution`, and that is the point: coverage is decided
        from on-hand stock alone (`app.engines.executive.ON_ORDER_NOTE` -- on-order
        is read BESIDE coverage figures, never inside them). The single consumer is
        `app.engines.mrp.on_order_runout`, via
        `app.engines.scenario._supply_runout_changes`, which projects a monthly
        balance rather than issuing a verdict.

        It lives on the resolver anyway, rather than being read off the override
        rows directly at the call site, for the reason the whole module exists: the
        override VOCABULARY has exactly one reader, so a row that `validate`
        rejects cannot reach a projection through a side door.
        """
        return base_earliest

    def po_arrival_product_ids(self) -> frozenset[str]:
        """Products whose incoming-supply arrival this scenario restates."""
        return frozenset()

    def hypothetical_orders(self, product_id: str) -> tuple[tuple[float, date], ...]:
        """HYPOTHETICAL purchase orders this scenario asserts for `product_id`.

        `((quantity, expected_arrival), ...)`, empty here. `ScenarioOverrides`
        returns one entry per `PO_ARRIVAL.new_order` override naming this product.

        THESE ARE NOT `InventoryOnOrder` ROWS AND NEVER BECOME ANY. There is no
        writer for them: `app.engines.scenario_apply.apply_to_base_plan` refuses the
        whole kind (`app.engines.scenario.apply_blockers`), and the single consumer
        is `app.engines.mrp.on_order_runout`, which folds the quantity into a
        month-walk it computes and throws away. Nothing in this platform fabricates
        an on-order row, and this is deliberately not the exception -- a synthetic
        row sitting beside Oracle-projected ones, indistinguishable, would be the
        worst outcome available (the same argument
        `OVERRIDE_FIELDS` makes about customer-owned quantities).

        DELIBERATELY NOT CONSULTED BY THE COVERAGE PASS, for the same reason
        `arrival_date` is not: no caller exists in `app.engines.coverage`,
        `app.engines.allocation` or `app.engines.substitution`, so a hypothetical
        order cannot move a Covered/Uncovered verdict. It is incoming supply, and
        this platform reads incoming supply BESIDE coverage figures, never inside
        them (`app.engines.executive.ON_ORDER_NOTE`). A large enough hypothetical
        order will absorb a runout gap and still leave every verdict where it was,
        which is the honest answer rather than a bug.
        """
        return ()

    def new_order_product_ids(self) -> frozenset[str]:
        """Products this scenario asserts a hypothetical new order for."""
        return frozenset()

    def approval(
        self,
        line_id: str,
        from_product_id: str,
        to_product_id: str,
        base_status: SubstitutionApprovalStatus | None,
    ) -> SubstitutionApprovalStatus | None:
        """Well-layer approval status for one substitution pair on one line."""
        return base_status

    @property
    def active(self) -> bool:
        """True when this resolver can change anything at all."""
        return False


#: The identity resolver used by the official coverage pass. See the module
#: docstring: the un-overridden path is not a special case, it is this object.
NO_OVERRIDES = OverrideResolver()


class ScenarioOverrides(OverrideResolver):
    """A scenario's overrides, indexed for lookup by the coverage engine.

    Built from validated rows only. `validate` is re-run on read so a row
    persisted by an older build (or hand-edited in the database) cannot slip a
    cross-BU inventory override or a nonsense field into a coverage pass.
    """

    def __init__(self, overrides, scenario_customer):
        self._demand: dict[str, dict[str, object]] = {}
        #: {well_id: DemandStatus} from WELL overrides. Keyed by WELL, so every
        #: line of that well is seen at the overridden status and the preview
        #: cannot model a well at two statuses.
        self._well_status: dict[str, DemandStatus] = {}
        self._on_hand: dict[str, float] = {}
        self._assigned: dict[tuple[str, str], float] = {}
        self._approval: dict[tuple[str, str | None, str], SubstitutionApprovalStatus] = {}
        #: {product_id: restated earliest arrival date} from PO_ARRIVAL overrides.
        #: Keyed by PRODUCT because that is the granularity the override targets and
        #: the granularity incoming supply is REPORTED at everywhere (the Executive
        #: Dashboard's `incoming_supply`, `InventoryPosition.on_order`, and the
        #: timeline's own band are all per-product totals). A per-PO-row target would
        #: need a `target_on_order_id` column and a UI that drew one block per row;
        #: neither exists, and inventing a row-level target the screens cannot show
        #: would let a planner "move" a PO they were never able to see.
        self._po_arrival: dict[str, datetime] = {}
        #: {product_id: [(quantity, expected_arrival), ...]} from
        #: `PO_ARRIVAL.new_order` overrides -- purchase orders that DO NOT EXIST,
        #: which the planner is asking the platform to imagine.
        #:
        #: A LIST, not one entry per product, and that is not symmetry with
        #: `_po_arrival` being a dict. `arrival_date` restates ONE fact (the earliest
        #: promised arrival), so a second row for the same product would be an
        #: ambiguous scenario and the timeline overwrites rather than appends. A
        #: hypothetical order is an EVENT, and asserting two of them ("2000 in March
        #: and 3000 in June, split across two mills") is a coherent, ordinary
        #: question -- collapsing them onto one product key would silently discard
        #: the planner's second order.
        self._new_orders: dict[str, list[tuple[float, datetime]]] = {}
        unmodelled = []

        for override in overrides:
            validate(override, scenario_customer)
            kind = override.target_kind

            if kind in UNMODELLED_KINDS:
                unmodelled.append(override)
                continue

            if kind == ScenarioTargetKind.DEMAND_LINE:
                slot = self._demand.setdefault(override.target_demand_line_id, {})
                slot[override.field_name] = _demand_value(override)

            elif kind == ScenarioTargetKind.WELL:
                self._well_status[override.target_well_id] = DemandStatus(
                    override.value_text
                )

            elif kind == ScenarioTargetKind.INVENTORY:
                self._on_hand[override.target_product_id] = override.value_number

            elif kind == ScenarioTargetKind.PO_ARRIVAL:
                # The two fields land in DIFFERENT members and can never be read as
                # each other -- see `OVERRIDE_FIELDS`. Both may be present for one
                # product, and then both apply: the real deliveries shift AND the
                # hypothetical order is added beside them.
                if override.field_name == "new_order":
                    self._new_orders.setdefault(override.target_product_id, []).append(
                        (float(override.value_number), override.value_date)
                    )
                else:
                    self._po_arrival[override.target_product_id] = override.value_date

            elif kind == ScenarioTargetKind.ASSIGNMENT:
                key = (override.target_demand_line_id, override.target_product_id)
                self._assigned[key] = override.value_number

            elif kind == ScenarioTargetKind.SUBSTITUTION_APPROVAL:
                key = (
                    override.target_demand_line_id,
                    override.target_from_product_id,
                    override.target_to_product_id,
                )
                self._approval[key] = SubstitutionApprovalStatus(override.value_text)

        self.unmodelled = tuple(unmodelled)

    @property
    def active(self) -> bool:
        return bool(
            self._demand
            or self._well_status
            or self._on_hand
            or self._assigned
            or self._approval
            or self._po_arrival
            or self._new_orders
        )

    def view(self, line: DemandLine) -> LineView:
        """The line's effective values, with the WELL's effective status.

        A WELL override is applied to every line of that well, which is what makes
        "what if we confirmed this well?" a coherent what-if: the whole well moves
        into or out of coverage scope at once, exactly as it would if somebody
        called `app.engines.coverage.set_well_demand_status` for real.
        """
        base_status = line.well.demand_status
        status = self._well_status.get(line.well_id, base_status)
        fields = self._demand.get(line.id)
        if not fields:
            if status == base_status:
                return super().view(line)
            return LineView(
                line=line,
                quantity=line.quantity,
                ros_date=line.ros_date,
                status=status,
                profile=line.profile,
                overridden=True,
            )
        quantity = fields.get("quantity", line.quantity)
        ros_date = fields.get("ros_date", line.ros_date)
        profile = fields.get("profile", line.profile)
        return LineView(
            line=line,
            quantity=quantity,
            ros_date=ros_date,
            status=status,
            profile=profile,
            overridden=(
                quantity != line.quantity
                or ros_date != line.ros_date
                or status != base_status
                or profile != line.profile
            ),
        )

    def well_status_overrides(self) -> dict[str, DemandStatus]:
        """{well_id: overridden DemandStatus}, for the applier to write through
        `app.engines.coverage.set_well_demand_status`."""
        return dict(self._well_status)

    def on_hand(self, product_id: str, base_qty: float) -> float:
        return self._on_hand.get(product_id, base_qty)

    def assigned(self, line_id: str, product_id: str, base_qty: float) -> float:
        return self._assigned.get((line_id, product_id), base_qty)

    def assignment_overrides(self) -> dict[tuple[str, str], float]:
        return dict(self._assigned)

    def inventory_override_product_ids(self) -> frozenset[str]:
        return frozenset(self._on_hand)

    def arrival_date(self, product_id: str, base_earliest):
        return self._po_arrival.get(product_id, base_earliest)

    def po_arrival_product_ids(self) -> frozenset[str]:
        return frozenset(self._po_arrival)

    def hypothetical_orders(self, product_id: str) -> tuple[tuple[float, date], ...]:
        # Sorted by arrival date so the order the planner created them in cannot
        # change the projection -- `on_order_runout` sums per month, but a stable
        # order also keeps the reported `hypothetical_orders` list stable between
        # reads, which the UI lists verbatim.
        return tuple(sorted(self._new_orders.get(product_id, ()), key=lambda p: p[1]))

    def new_order_product_ids(self) -> frozenset[str]:
        return frozenset(self._new_orders)

    def approval(self, line_id, from_product_id, to_product_id, base_status):
        # A pair may be named with or without the from-product. Prefer the exact
        # match, then the wildcard, so "what if this substitute were approved"
        # works without the planner having to restate the from side.
        for key in (
            (line_id, from_product_id, to_product_id),
            (line_id, None, to_product_id),
        ):
            if key in self._approval:
                return self._approval[key]
        return base_status

    # -- introspection used by the preview's explanation strings ------------

    def overridden_line_ids(self) -> frozenset[str]:
        """Lines named DIRECTLY by an override.

        A WELL status override is NOT expanded into its lines here -- this resolver
        has no session and cannot enumerate them. `overridden_well_ids` reports it
        instead, and `app.engines.scenario._line_changes` treats a line whose well
        is overridden as directly affected, which is the honest reading: the
        planner did point at that line's status, via its well.
        """
        ids = set(self._demand)
        ids.update(line_id for line_id, _pid in self._assigned)
        ids.update(line_id for line_id, _f, _t in self._approval)
        return frozenset(ids)

    def overridden_well_ids(self) -> frozenset[str]:
        """Wells whose demand status this scenario restates."""
        return frozenset(self._well_status)


def _demand_value(override):
    """The typed value of a DEMAND_LINE override, coerced to the model's type.

    `status` is deliberately absent: it is not a DEMAND_LINE field any more. A
    WELL override's value is read in `ScenarioOverrides.__init__` instead.
    """
    field = override.field_name
    if field == "quantity":
        return float(override.value_number)
    if field == "ros_date":
        return override.value_date
    if field == "profile":
        return DemandProfile(override.value_text)
    raise OverrideError(f"Unhandled demand override field {field!r}")
