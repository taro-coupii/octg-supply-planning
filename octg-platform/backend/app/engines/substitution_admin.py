"""Editing substitution master data, and reporting what the edit did.

`TechnicalSubstitution` (layer 1) and `CustomerSubstitutionRule` (layer 2) were
seed-only rows. A planner asked where a substitution item can be registered and the
honest answer was nowhere, so `app.api.admin` now offers real CRUD. This module is
the part that is NOT plumbing: working out, and stating, what such an edit changed.

It is the deliberate sibling of `app.engines.lead_time_admin` and follows its shape
on purpose -- snapshot, mutate, recompute, diff -- so the two administrative
surfaces cannot drift into reporting their blast radius differently.

WHY THESE EDITS NEED A REPORT
=============================
Both tables GATE coverage rather than describe it. `app.engines.substitution.find_candidates`
reads layer 1 to decide which products are even OFFERED as substitutes for a demand
line, and layer 2 to decide whether the customer permits each one. Above them,
`app.engines.coverage` turns a cleared substitute into the stored verdicts
`CoverageStatus.COVERED_VIA_SUBSTITUTE` and `CoverageStatus.PENDING_APPROVAL`.

So a two-column insert can move a well from Uncovered to CoveredViaSubstitute, and a
delete can take that back. In particular:

  * Deleting a `TechnicalSubstitution` removes the pair from `find_candidates`
    entirely. Any line standing at CoveredViaSubstitute on that pair loses its
    coverage, and any line at PendingApproval on it stops pursuing anything.
  * Deleting an `allowed=True` `CustomerSubstitutionRule` reverts the pair to BLOCKED,
    because layer 2 is a true allow-list -- absence is not neutrality. See
    `app.models.substitution.CustomerSubstitutionRule`.
  * Flipping `allowed` with PATCH does the same in whichever direction, and is a
    normal commercial change rather than data cleanup, which is why it is a separate
    verb from DELETE.

WHAT IS AND IS NOT CACHED (checked, not assumed)
================================================
Live, nothing to invalidate: `find_candidates` queries `technical_substitutions`,
`customer_substitution_rules` and `well_substitution_approvals` on every call and
memoises nothing. `GET /demand-lines/{id}/substitution-candidates` therefore reflects
an edit immediately.

NOT live: `CoverageResult.status` is PERSISTED, and `Well.coverage_status` rolls up
from those stored rows. An edit here would otherwise leave a stored
CoveredViaSubstitute verdict resting on a technical row that no longer exists --
precisely the "superseded assumption presented as current" defect the rest of this
codebase refuses to ship. Hence `apply_substitution_change` recomputes in the same
transaction, on the same measured basis `lead_time_admin` documents.

A MASTER-DATA WRITE IS NOT BLOCKED BY ANOTHER BU'S MISSING INVENTORY FEED
=========================================================================
This was found by exercising the endpoints against the seeded demo database, not
predicted. `TechnicalSubstitution` is BU-AGNOSTIC -- one row applies to every Business
Unit -- but the recompute it triggers is BU-scoped, and
`app.engines.inventory` deliberately RAISES `InventoryRowMissing` rather than
inventing an on-hand quantity for a (BU, product) pair whose Oracle row has not
arrived. Registering a pair adds a new substitute TARGET to resolve, so the pass would
raise for any BU that demands the primary product but holds no inventory row for the
substitute -- and the whole POST failed with 424, saving nothing. Two seeded products
reproduce it.

Refusing the write for that reason is the tail wagging the dog. The engineering claim
is true regardless of whose warehouse feed is complete, the refusal names a product the
operator did not mention and cannot fix from this screen, and the effect was that a
legitimate registration could not be performed at all.

So each customer is recomputed inside its OWN SAVEPOINT. A customer whose pass cannot
resolve its inventory is rolled back to exactly its prior stored state -- never left
with half-written verdicts -- and recorded in `recompute_failures` with the engine's
own message. The write proceeds and the response says which customers could NOT be
re-derived and why. That is the same stance the rest of the codebase takes: the refusal
to invent a number is preserved in full (no zero is substituted, no verdict is
guessed), and what cannot be answered is REPORTED rather than either hidden or allowed
to veto an unrelated change.

`app.engines.lead_time_admin` has the same theoretical exposure and is deliberately not
changed here: its recompute introduces no new products to resolve, so it can only fail
on a database that was already failing every coverage read.

DANGLING WELL-LAYER APPROVALS ARE LEFT ALONE, ON PURPOSE
=======================================================
Deleting a technical row can leave `WellSubstitutionApproval` rows whose pair has no
layer-1 claim behind them. They are NOT deleted and NOT flagged, and nothing needs to
be: `find_candidates` derives `to_ids` from the technical rows and both queries and
iterates within that set, so an approval outside it is simply never visited. Verified
by reading the function, and pinned by a test that deletes a technical row under an
Approved approval and then re-reads the candidates. Keeping the rows is the better
answer of the two available -- an approval is a record of a decision a customer
actually made, on a date, and deleting the evidence because the engineering claim was
later withdrawn would destroy history to tidy a table. If the pair is ever re-created
the old approval becomes live again, which is correct: the customer's decision did not
expire, and the alternative (silently invalidating it) would make a re-registration
quietly harsher than the original.
"""

from dataclasses import dataclass
from typing import Callable

from sqlalchemy.orm import Session

from app.models import (
    Customer,
    CustomerSubstitutionRule,
    SubstitutionApprovalStatus,
    TechnicalSubstitution,
    Well,
    WellSubstitutionApproval,
)


@dataclass(frozen=True)
class WellCoverageChange:
    """One well whose coverage rollup moved. Scalars only.

    Same shape as `app.engines.lead_time_admin.WellCoverageChange` and serialised by
    the same `WellCoverageRollupChangeOut`, so the Administration screen renders one
    kind of before/after row whichever section produced it.
    """

    well_id: str
    well_name: str
    coverage_before: str | None
    coverage_after: str | None


@dataclass(frozen=True)
class PairDependencies:
    """What currently HANGS OFF one (from_product, to_product) pair.

    Gathered BEFORE a delete, because afterwards the question cannot be asked. Every
    field is a count of rows that exist right now, never an estimate.
    """

    #: Well-layer approvals naming this pair, in any status.
    well_approval_count: int
    #: ...of which Approved. These are the ones a delete actually takes coverage from:
    #: a Pending or Rejected approval was not providing coverage to begin with.
    approved_well_approval_count: int
    pending_well_approval_count: int
    #: Customer rules naming this pair (both permissions and vetoes).
    customer_rule_count: int
    #: ...of which permit it.
    allowing_customer_rule_count: int


def pair_dependencies(
    db: Session, from_product_id: str, to_product_id: str
) -> PairDependencies:
    """Count what references one ordered pair. Reads only; writes nothing."""
    approvals = (
        db.query(WellSubstitutionApproval)
        .filter(
            WellSubstitutionApproval.from_product_id == from_product_id,
            WellSubstitutionApproval.to_product_id == to_product_id,
        )
        .all()
    )
    rules = (
        db.query(CustomerSubstitutionRule)
        .filter(
            CustomerSubstitutionRule.from_product_id == from_product_id,
            CustomerSubstitutionRule.to_product_id == to_product_id,
        )
        .all()
    )
    return PairDependencies(
        well_approval_count=len(approvals),
        approved_well_approval_count=sum(
            1 for a in approvals if a.status == SubstitutionApprovalStatus.APPROVED
        ),
        pending_well_approval_count=sum(
            1 for a in approvals if a.status == SubstitutionApprovalStatus.PENDING
        ),
        customer_rule_count=len(rules),
        allowing_customer_rule_count=sum(1 for r in rules if r.allowed),
    )


def technical_substitution_exists(
    db: Session, from_product_id: str, to_product_id: str
) -> bool:
    """Is there a layer-1 claim for this ordered pair?

    The one place this question is answered, so the list payload's
    `technical_substitution_exists` flag, the POST warning and the tests cannot
    disagree about what "inert rule" means.
    """
    return (
        db.query(TechnicalSubstitution)
        .filter(
            TechnicalSubstitution.from_product_id == from_product_id,
            TechnicalSubstitution.to_product_id == to_product_id,
        )
        .first()
        is not None
    )


@dataclass(frozen=True)
class RecomputeFailure:
    """One customer whose coverage could NOT be re-derived, and why.

    Never a silent skip. The customer's stored verdicts were rolled back to exactly
    what they were, so they are now KNOWN to predate this change -- which is a fact the
    response has to carry, because a reader who saw only `recomputed_customer_ids`
    would reasonably assume the omission meant "nothing to do".
    """

    customer_id: str
    customer_name: str
    #: The engine's own message. It names the product and the Business Unit and states
    #: the corrective action, so it is passed through verbatim rather than summarised.
    reason: str


@dataclass(frozen=True)
class SubstitutionChangeImpact:
    """The blast radius of one substitution master-data edit."""

    recomputed_customer_ids: tuple[str, ...]
    well_changes: tuple[WellCoverageChange, ...]
    #: How many wells were VISITED. Without it a reader cannot tell "nothing moved"
    #: from "nothing was looked at" -- the same distinction `products_examined` keeps
    #: on the lead-time report.
    wells_examined: int
    #: Customers whose recompute could not complete. See `RecomputeFailure` and the
    #: module docstring.
    recompute_failures: tuple[RecomputeFailure, ...] = ()


def apply_substitution_change(
    db: Session, mutate: Callable[[], None]
) -> SubstitutionChangeImpact:
    """Run `mutate` (a create/patch/delete of substitution master data), then report
    and repair.

    In order: snapshot every well's stored coverage rollup, apply the caller's
    mutation, flush, recompute every customer so no stored verdict outlives the
    permission it was computed from, and return the wells that moved.

    Flushes; does not commit. The caller owns the transaction, so a failure after this
    point rolls back the recompute along with the edit rather than leaving master data
    changed and verdicts half-rewritten.

    EVERY customer is recomputed, not merely the one a rule names. Layer 1 is
    customer-agnostic, so a technical row touches every customer at once; and even for
    a layer-2 rule the recompute is unconditional for the reason `lead_time_admin`
    states -- a conditional one would make "did the admin screen repair the stored
    verdicts" depend on which edit happened to matter.
    """
    wells = db.query(Well).all()
    coverage_before = {well.id: (well.name, well.coverage_status) for well in wells}

    mutate()
    db.flush()

    # Imported locally: `app.engines.coverage` imports `app.engines.substitution`, and
    # a top-level import here would put this module in the middle of that chain for no
    # benefit. Same reason `lead_time_admin` does it.
    from app.engines.coverage import recompute_customer
    from app.engines.inventory import InventoryRowMissing, InventoryScopeMissing

    customers = db.query(Customer).all()
    recomputed: list[str] = []
    failures: list[RecomputeFailure] = []
    for customer in customers:
        try:
            # SAVEPOINT per customer. `recompute_customer` deletes and rewrites rows as
            # it goes, so a failure part-way through would otherwise leave that
            # customer's stored verdicts half-erased -- strictly worse than the stale
            # ones it was replacing. The savepoint makes the per-customer outcome
            # all-or-nothing, which is what lets the failure be reported instead of
            # having to abort the whole request.
            with db.begin_nested():
                # No explicit filters: the platform's CURRENT coverage scope is what
                # the official verdict must be computed under. See
                # app.engines.coverage_scope.
                recompute_customer(db, customer)
            recomputed.append(customer.id)
        except (InventoryRowMissing, InventoryScopeMissing) as exc:
            # The two refusals `app.engines.inventory` raises rather than inventing a
            # quantity. Caught HERE and nowhere wider: they are the only exceptions
            # whose meaning is "this customer's inventory facts are incomplete", which
            # is a data-feed gap that must not veto an unrelated master-data write. Any
            # other exception still propagates -- a bug in the coverage rules must not
            # be laundered into a per-customer footnote.
            failures.append(
                RecomputeFailure(
                    customer_id=customer.id,
                    customer_name=customer.name,
                    reason=str(exc),
                )
            )
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

    return SubstitutionChangeImpact(
        recomputed_customer_ids=tuple(sorted(recomputed)),
        well_changes=tuple(well_changes),
        wells_examined=len(wells),
        recompute_failures=tuple(
            sorted(failures, key=lambda f: f.customer_name)
        ),
    )
