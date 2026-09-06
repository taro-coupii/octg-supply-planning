"""The coverage SCOPE: the shipped fallback, the persisted default, and the setter.

This module owns the two filters that decide which demand
`app.engines.coverage` evaluates at all. It exists as a module of its own -- rather
than as more code in `app.engines.coverage` -- because `app.api.wells`,
`app.api.demand`, `app.api.dashboard`, `app.engines.mrp`, `app.engines.executive`,
`app.engines.well_dates` and `app.engines.coverage_view` all
need to resolve the scope, and importing the whole coverage engine to ask "which
statuses are in scope" would tie eight modules to it for one fact.

THE CONSTANT / SETTING SPLIT, AND WHY IT IS SHAPED THIS WAY
==========================================================
`DEFAULT_STATUS_FILTER` and `DEFAULT_PROFILE_FILTER` are still module-level
constants and still hold exactly the values they always held. What changed is
their JOB: they are now the SHIPPED FALLBACK, consulted when nothing is persisted,
rather than the answer.

They could not have become the answer by any other shape. A constant is bound once
at import time, before any `Session` exists, so a module-level constant CANNOT read
a database -- an attempt would either fail at import or, worse, capture whatever a
throwaway connection said at process start and then serve it forever, ignoring every
later edit. Nor may they be reassigned at runtime: `DEFAULT_STATUS_FILTER` is
imported BY VALUE into a dozen modules and is used as a DEFAULT ARGUMENT in several
signatures (default arguments are evaluated once, at `def` time), so rebinding the
name here would change the answer in some places and not others, which is worse
than not changing it anywhere.

So the resolution happens where a `Session` exists: at CALL time, through
`effective_status_filter(db)` / `effective_profile_filter(db)`. Every consumer that
previously read the constant now calls the accessor, and every engine entry point
that previously DEFAULTED an argument to the constant now defaults it to `None` and
resolves inside. The constants remain exported (and re-exported by
`app.engines.coverage`, so existing imports and
`tests/test_coverage_engine.py::test_default_filters_are_confirmed_only_and_primary_plus_contingency`
are untouched) as the fallback and as documentation of what the platform ships with.

Cost of resolving per call, and why a memo is REQUIRED rather than an optimisation
---------------------------------------------------------------------------------
`_row` fetches the singleton by PRIMARY KEY (`db.get`). That is served from the
session identity map once the row has been loaded -- but the identity map only
remembers HITS. On the ordinary platform, where nobody has adjusted the scope and
there IS no row, every single `db.get` re-issues the SELECT. `_in_scope` in
`app.engines.executive` is called once per demand line, so the naive version turned
one predicate into hundreds of round trips on the densest screen in the product.
That was measured, not theorised: it broke
`tests/test_well_dates.py::test_well_dates_costs_a_constant_number_of_queries`, which
pins that function at two statements.

So the resolved scope is memoised in `Session.info` -- per session, never in a
module-level dict that would outlive one and serve a stale scope to unrelated
requests. Invalidation is explicit and narrow:

  * `after_commit` / `after_rollback` / `after_soft_rollback` clear it, so a session
    reused across transaction boundaries re-reads. These are the same boundaries the
    identity map itself is invalidated at, so the memo's lifetime does not exceed
    SQLAlchemy's own.
  * `set_coverage_scope_defaults` clears it directly, because it must see its own
    write immediately: the full recompute it performs runs in the SAME session and
    has to evaluate under the NEW scope.

`after_flush` deliberately does NOT clear it. A flush of unrelated work -- which
happens constantly inside `recompute_customer` -- cannot change this setting, and
invalidating there would restore exactly the per-line query storm the memo exists to
remove.

WHAT SAVING DOES: A FULL SYNCHRONOUS RECOMPUTE
==============================================
`set_coverage_scope_defaults` recomputes EVERY customer in the same transaction.
The alternative -- leave stored `CoverageResult` rows alone until something else
happens to recompute each well -- was rejected on this codebase's own established
stance: a derived row that no longer describes what the engine would say is not
merely stale, it is misinformation (`recompute_customer` DELETES rows for lines that
leave scope for precisely this reason, and `app.engines.coverage_view` refuses to
answer a non-default filter request from stored rows at all). Deferring would leave
the coverage grid serving verdicts computed under the OLD scope, under a label
claiming the NEW one.

The cost was measured before choosing, on the seeded demo database: 3 customers,
27 wells, 113 demand lines. A full pass is 3 calls to `recompute_customer` and
completes well inside a request. This is a rare administrative action, not a hot
path. If the platform ever grows to a size where this is not true, the honest fix is
to make the endpoint asynchronous and report progress -- NOT to serve verdicts
computed under a scope nobody is using any more.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.models import Customer, DemandProfile, DemandStatus
from app.models.coverage_scope import SINGLETON_ID, CoverageScopeDefault

# ---------------------------------------------------------------------------
# The shipped fallback
# ---------------------------------------------------------------------------
#
# Coverage Calculation Sequence (per spec): Demand -> Status Filter -> Profile
# Filter -> Allocation Policy -> Coverage Result. Lines excluded by a filter
# are not evaluated and do not affect the well rollup.
#
# THE TWO FILTERS SELECT DIFFERENT THINGS
# --------------------------------------
# The STATUS filter selects WELLS. Demand status lives on `Well.demand_status`
# (see `app.models.well.Well` for the product owner's ruling), so a status filter
# either takes a well entirely or leaves it out entirely -- there is no such thing
# as one line of a well being in scope on status grounds and another not.
#
# The PROFILE filter selects LINES. A Confirmed well routinely holds both Primary
# and Contingency demand, so profile varies within a well and the profile filter
# cuts inside it.
#
# That asymmetry is what makes the coverage grid's counts readable. Before the
# move, filtering to Confirmed could drop a LINE while leaving its WELL (one well
# held a Confirmed line and a Budgeted line), so the well count and the line count
# moved by unrelated amounts and neither could be explained. Now a status filter
# moves whole wells and their whole line complements together.
#
# THE SHIPPED SCOPE, and what changing it does
# --------------------------------------------
# Set by the product owner: "default configuration should be; demand status =
# confirmed only, profile = primary and contingency."
#
# Both halves moved at once, in opposite directions, and the pair is coherent:
#
#   Status  Confirmed ONLY. Budgeted demand is money set aside, not a commitment
#           to drill, so it must not compete for steel against demand somebody has
#           actually confirmed. It was previously in scope, which meant a Budgeted
#           line with an early ROS could draw a pool out from under a Confirmed
#           line -- the allocation is ROS-ordered and does not care why a line is
#           in scope (see `app.engines.allocation`). Budgeted lines are still
#           LISTED everywhere; they simply get no verdict, which is what "not
#           evaluated" has always meant here.
#   Profile Primary AND Contingency. A contingency string is steel the well may
#           genuinely need, and planning as though it will not be called off is
#           how a contingency arrives unreserved. It was previously out of scope,
#           so contingency demand consumed nothing and was invisible to coverage.
#
# This is NOT a display filter. It decides which lines compete for the same
# inventory, so widening or narrowing it moves verdicts for lines that were in
# scope all along -- which is exactly why `app.engines.coverage_view` refuses to
# answer a non-default filter request from the stored rows, and why
# `tests/test_coverage_engine.py::test_default_filters_are_confirmed_only_and_primary_plus_contingency`
# pins these two sets.
#
# These two sets are now the FALLBACK rather than the last word: an administrator
# may persist a different scope (`set_coverage_scope_defaults`), in which case
# `effective_status_filter` / `effective_profile_filter` return that instead. What
# the constants still guarantee is that a platform where nobody has adjusted
# anything behaves exactly as the owner specified, with no row required to say so.
DEFAULT_STATUS_FILTER = {DemandStatus.CONFIRMED}
DEFAULT_PROFILE_FILTER = {DemandProfile.PRIMARY, DemandProfile.CONTINGENCY}


class InvalidCoverageScope(ValueError):
    """A requested coverage scope is not one the engine can evaluate.

    Raised rather than coerced. Both failure modes it covers -- an unrecognised
    status/profile name, and an EMPTY filter -- would otherwise be absorbed
    silently and change every verdict on the platform:

      * an unknown token skipped would narrow the scope by whatever the caller
        misspelled, and the response would confirm a scope that was not saved;
      * an empty status filter puts every well out of scope, so every line on the
        platform becomes "not evaluated" and the whole coverage grid empties. That
        is a legal set and a catastrophic setting, and no UI accident should be
        able to reach it.
    """


# ---------------------------------------------------------------------------
# Parsing / rendering the stored form
# ---------------------------------------------------------------------------
#
# The stored form is comma-separated enum VALUES ("Confirmed", "Primary,Contingency").
# Both directions live here so the column can never hold a token the engine cannot
# read back. See `app.models.coverage_scope` for why a String holds a set.


def _parse(raw: str, enum_cls, what: str) -> set:
    """Parse one stored/requested filter into a non-empty set of `enum_cls` members."""
    tokens = [token.strip() for token in (raw or "").split(",") if token.strip()]
    if not tokens:
        raise InvalidCoverageScope(
            f"the {what} filter is empty. An empty {what} filter puts EVERY demand "
            f"line on the platform out of scope, so nothing would be evaluated and "
            f"every coverage verdict would disappear. Select at least one {what}."
        )
    by_value = {member.value: member for member in enum_cls}
    # Case-insensitive, so a client sending "confirmed" is understood -- but the
    # canonical enum value is what gets stored, so the column never accumulates
    # spellings that `_render` would then have to normalise.
    by_lower = {value.lower(): member for value, member in by_value.items()}
    out: set = set()
    for token in tokens:
        member = by_value.get(token) or by_lower.get(token.lower())
        if member is None:
            raise InvalidCoverageScope(
                f"{token!r} is not a demand {what}. The {what} filter must be chosen "
                f"from {sorted(by_value)}. Nothing was saved."
            )
        out.add(member)
    return out


def parse_status_filter(raw: str) -> set[DemandStatus]:
    """Parse a status filter, raising `InvalidCoverageScope` on anything unusable."""
    return _parse(raw, DemandStatus, "status")


def parse_profile_filter(raw: str) -> set[DemandProfile]:
    """Parse a profile filter, raising `InvalidCoverageScope` on anything unusable."""
    return _parse(raw, DemandProfile, "profile")


def render_filter(members) -> str:
    """The stored form of a filter set: canonical enum values, comma separated.

    Sorted, so the column is stable between two saves of the same set and a diff of
    the table is readable. The order carries no meaning -- both filters are sets.
    """
    return ",".join(sorted(member.value for member in members))


# ---------------------------------------------------------------------------
# Reading the effective scope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoverageScope:
    """The scope the engine is actually using right now, and where it came from.

    `persisted` is what the Administration screen needs and what the filter values
    alone cannot tell it: an administrator may legitimately set the scope back to
    the shipped values, and "adjusted, and happens to match the shipped default" is
    a different fact from "never adjusted".
    """

    status_filter: frozenset
    profile_filter: frozenset
    #: True when a row exists, i.e. somebody has adjusted the scope.
    persisted: bool
    #: When it was adjusted. None exactly when `persisted` is False.
    updated_at: datetime | None = None
    #: True only while `scope_override` is serving a caller-requested scope --
    #: never for a resolved persisted/shipped scope.
    overridden: bool = False

    @property
    def matches_shipped_default(self) -> bool:
        return (
            set(self.status_filter) == DEFAULT_STATUS_FILTER
            and set(self.profile_filter) == DEFAULT_PROFILE_FILTER
        )


#: Key under which the resolved `CoverageScope` is memoised in `Session.info`.
#: Namespaced so it cannot collide with anything else stashed on a session.
_MEMO_KEY = "app.engines.coverage_scope:resolved"


def _forget(session: Session, *_args) -> None:
    """Drop the memoised scope for `session`. See the module docstring on lifetime."""
    session.info.pop(_MEMO_KEY, None)


# Registered on the Session CLASS, so every session in the process (application and
# test alike) gets the same invalidation rules. Registering per-session would mean
# any code path that built a session without going through `app.db` silently kept a
# scope forever.
event.listen(Session, "after_commit", _forget)
event.listen(Session, "after_rollback", _forget)
event.listen(Session, "after_soft_rollback", _forget)


def _row(db: Session) -> CoverageScopeDefault | None:
    """The singleton row, or None when nobody has adjusted the scope.

    A PRIMARY-KEY load on purpose: when the row DOES exist, `db.get` returns the
    identity-mapped instance with no round trip. When it does not -- the ordinary
    state of the platform -- every call re-queries, which is why `coverage_scope`
    memoises the parsed result rather than relying on this. See the module docstring.
    """
    return db.get(CoverageScopeDefault, SINGLETON_ID)


def coverage_scope(db: Session) -> CoverageScope:
    """THE effective coverage scope. Persisted row if there is one, shipped fallback if not.

    Memoised per session; see the module docstring for the lifetime and for why a
    memo is required rather than merely nice. The memoised value is an immutable
    frozen dataclass, so a caller cannot mutate what the next caller reads.
    """
    memo = db.info.get(_MEMO_KEY)
    if memo is not None:
        return memo
    resolved = _resolve(db)
    db.info[_MEMO_KEY] = resolved
    return resolved


def _resolve(db: Session) -> CoverageScope:
    row = _row(db)
    if row is None:
        return CoverageScope(
            status_filter=frozenset(DEFAULT_STATUS_FILTER),
            profile_filter=frozenset(DEFAULT_PROFILE_FILTER),
            persisted=False,
        )
    # Parsed, not trusted. A row written by an older build, by a hand-edited
    # database or by a future column change must not silently redefine the scope --
    # if it cannot be read back as real enum members, that is a loud failure, the
    # same stance `app.engines.lead_time` takes on a duplicate component.
    return CoverageScope(
        status_filter=frozenset(parse_status_filter(row.status_filter)),
        profile_filter=frozenset(parse_profile_filter(row.profile_filter)),
        persisted=True,
        updated_at=row.updated_at,
    )


def effective_status_filter(db: Session) -> set[DemandStatus]:
    """The demand statuses currently in coverage scope. Use this, never the constant."""
    return set(coverage_scope(db).status_filter)


def effective_profile_filter(db: Session) -> set[DemandProfile]:
    """The demand profiles currently in coverage scope. Use this, never the constant."""
    return set(coverage_scope(db).profile_filter)


@contextmanager
def scope_override(
    db: Session,
    status_filter: set[DemandStatus],
    profile_filter: set[DemandProfile],
):
    """Serve a REQUESTED scope through the accessors for one read-only computation.

    THE one sanctioned way to make `effective_status_filter` /
    `effective_profile_filter` answer with something other than the persisted
    default. It exists so that a multi-block computation (the Executive
    dashboard, the Surplus report) can honour a caller-supplied scope while
    every one of its blocks keeps resolving the scope through the SAME
    accessors -- threading explicit filter arguments through eight call sites
    is exactly the block-to-block drift `app.engines.executive._in_scope`'s
    docstring warns against.

    Mechanics: the memo slot in `Session.info` is pre-filled with the requested
    scope, so every accessor call inside the block reads it. On exit the slot
    is dropped. The transaction-boundary listeners (`after_commit` /
    `after_rollback`) also drop it, which is correct, not a hazard: an override
    is only valid for the read-only computation it wraps, and a computation
    that crosses a transaction boundary has ended either way.

    This changes WHICH LINES the readers consider in scope; it does not touch
    stored `CoverageResult` rows. A caller whose blocks read stored verdicts
    must pair this with a read-only recompute under the same filters -- see
    `app.engines.coverage_view.scoped_verdicts`, which is the composition most
    callers actually want.
    """
    if not status_filter:
        raise InvalidCoverageScope(
            "the status filter is empty. An empty status filter puts EVERY well "
            "out of scope, so nothing would be evaluated. Select at least one "
            "status."
        )
    if not profile_filter:
        raise InvalidCoverageScope(
            "the profile filter is empty. An empty profile filter puts EVERY "
            "demand line out of scope. Select at least one profile."
        )
    if _MEMO_KEY in db.info:
        # A nested override (or an override on top of an already-resolved memo)
        # would silently swap the scope out from under the outer computation.
        # The memo is dropped rather than refused -- the outer value re-resolves
        # on next access -- but two LIVE overrides is a programming error.
        current = db.info[_MEMO_KEY]
        if getattr(current, "overridden", False):
            raise RuntimeError(
                "scope_override does not nest: an override is already active "
                "on this session."
            )
    db.info[_MEMO_KEY] = CoverageScope(
        status_filter=frozenset(status_filter),
        profile_filter=frozenset(profile_filter),
        persisted=False,
        overridden=True,
    )
    try:
        yield
    finally:
        _forget(db)


def is_default_scope(
    db: Session,
    status_filter: set[DemandStatus],
    profile_filter: set[DemandProfile],
) -> bool:
    """Are these filters the ones the platform's CURRENT default resolves to?

    `app.engines.coverage_view` asks this to decide whether the stored
    `CoverageResult` rows answer the caller's question or whether it must recompute
    read-only. It has to compare against the EFFECTIVE default, not the shipped
    constant: after an administrator widens the scope, the stored rows are the ones
    written under the NEW scope, and treating the old constant as "default" would
    make the workspace recompute the very request the table already answers while
    serving the ad-hoc label for it.
    """
    scope = coverage_scope(db)
    return (
        set(status_filter) == set(scope.status_filter)
        and set(profile_filter) == set(scope.profile_filter)
    )


# ---------------------------------------------------------------------------
# Writing it
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WellVerdictChange:
    """One well whose coverage rollup moved because the scope changed. Scalars only."""

    well_id: str
    well_name: str
    coverage_before: str | None
    coverage_after: str | None


@dataclass(frozen=True)
class CoverageScopeChange:
    """What `set_coverage_scope_defaults` did, reported before/after.

    Follows `app.engines.coverage.WellStatusChange` and the customer-owned upload's
    `coverage_changes`: this codebase's answer to a consequential change is to make
    the change and REPORT its blast radius, not to refuse it or to hide it behind a
    confirmation the server cannot verify happened. A caller can therefore prove the
    recompute occurred rather than trust that it did.
    """

    status_filter_before: tuple[str, ...]
    profile_filter_before: tuple[str, ...]
    status_filter_after: tuple[str, ...]
    profile_filter_after: tuple[str, ...]
    #: True when the requested scope equalled the effective one, in which case
    #: nothing was written and nothing was recomputed.
    unchanged: bool
    #: Every customer the recompute covered, whether or not any verdict moved.
    recomputed_customer_ids: tuple[str, ...] = ()
    #: How many wells the recompute visited -- the denominator for `well_changes`.
    recomputed_well_count: int = 0
    #: Only the wells whose rollup actually MOVED.
    well_changes: tuple[WellVerdictChange, ...] = ()
    #: True when the recompute happened in this call (it always does; the field
    #: exists so the payload states the policy rather than leaving a client to
    #: assume it, and so a future asynchronous variant is a value change here
    #: rather than a silent behaviour change).
    recomputed_synchronously: bool = True


def set_coverage_scope_defaults(
    db: Session,
    status_filter: set[DemandStatus],
    profile_filter: set[DemandProfile],
) -> CoverageScopeChange:
    """Persist a new platform-wide coverage scope and recompute EVERY customer.

    THE one sanctioned writer of `CoverageScopeDefault`. Flushes; does not commit --
    the caller owns the transaction, matching every other engine here.

    A NO-OP REQUEST WRITES NOTHING AND RECOMPUTES NOTHING
    ----------------------------------------------------
    Saving the scope that is already in effect returns `unchanged=True` and touches
    neither the row nor a single verdict. The same rule
    `app.engines.coverage.set_well_demand_status` applies, for a sharper reason
    here: the recompute is platform-wide, and running it to write back the numbers
    that were already there would burn the cost of the blast radius without the
    change.

    Note that "already in effect" is compared against the EFFECTIVE scope, so saving
    the shipped values on a platform that has never been adjusted is correctly a
    no-op -- it does not create a row that changes nothing.

    WHY THE RECOMPUTE IS NOT OPTIONAL
    --------------------------------
    See the module docstring. In short: every stored `CoverageResult` was computed
    against a pool built from the OLD include-set, so after this change every one of
    them answers a question nobody asked. The recompute is a full pass per customer
    because coverage cannot honestly be recomputed one well at a time -- inventory is
    pooled across a customer's wells.
    """
    # Imported here, not at module scope. `app.engines.coverage` imports the two
    # fallback constants FROM this module (and re-exports them so a dozen existing
    # imports keep working), so a module-level import back into it would be a cycle.
    # This is the only direction that needs breaking and it needs breaking in exactly
    # one function.
    from app.engines.coverage import recompute_all_business_units

    before = coverage_scope(db)
    requested_status = set(status_filter)
    requested_profile = set(profile_filter)

    # Re-validated here rather than trusted from the caller. This is the ONLY
    # writer, so this is the only place that can guarantee no unusable scope reaches
    # the column -- an empty set arriving from a caller that skipped the parser would
    # otherwise empty the entire coverage grid.
    if not requested_status:
        raise InvalidCoverageScope(
            "the status filter is empty. An empty status filter puts EVERY well out "
            "of scope, so no demand line on the platform would be evaluated. Select "
            "at least one status."
        )
    if not requested_profile:
        raise InvalidCoverageScope(
            "the profile filter is empty. An empty profile filter puts EVERY demand "
            "line out of scope, so no line would be evaluated. Select at least one "
            "profile."
        )

    status_before = tuple(sorted(s.value for s in before.status_filter))
    profile_before = tuple(sorted(p.value for p in before.profile_filter))
    status_after = tuple(sorted(s.value for s in requested_status))
    profile_after = tuple(sorted(p.value for p in requested_profile))

    if status_before == status_after and profile_before == profile_after:
        return CoverageScopeChange(
            status_filter_before=status_before,
            profile_filter_before=profile_before,
            status_filter_after=status_after,
            profile_filter_after=profile_after,
            unchanged=True,
            recomputed_synchronously=False,
        )

    # The before-picture is harvested BEFORE the row is written, because the
    # recompute below reads the new scope through the accessor and there is no second
    # chance to see what the platform used to say.
    from app.models import Well  # local: only needed for the before/after snapshot

    wells = db.query(Well).all()
    coverage_before = {well.id: (well.name, well.coverage_status) for well in wells}

    row = _row(db)
    if row is None:
        row = CoverageScopeDefault(
            id=SINGLETON_ID,
            status_filter=render_filter(requested_status),
            profile_filter=render_filter(requested_profile),
            # Set explicitly rather than left to the server default, so the value is
            # present on the instance this call is about to report on -- a
            # server_default is only visible after a refresh.
            updated_at=datetime.utcnow(),
        )
        db.add(row)
    else:
        row.status_filter = render_filter(requested_status)
        row.profile_filter = render_filter(requested_profile)
        row.updated_at = datetime.utcnow()
    db.flush()

    # The memo still holds the OLD scope, and this session is about to recompute the
    # whole platform through it. Dropped explicitly rather than left to a commit
    # boundary: `set_coverage_scope_defaults` does not commit (the caller owns the
    # transaction), so without this the recompute below would faithfully rewrite
    # every verdict under the scope that was just replaced. See the module docstring.
    _forget(db)

    # Now every consumer resolving the scope sees the new one, including
    # `recompute_customer` -- which is why the row is written first and the recompute
    # passes no explicit filters. Passing them explicitly would work today and would
    # silently stop tracking the setting the moment a caller forgot.
    sweep = recompute_all_business_units(db)
    db.flush()

    changes: list[WellVerdictChange] = []
    for well in db.query(Well).all():
        name, was = coverage_before.get(well.id, (well.name, None))
        if was != well.coverage_status:
            changes.append(
                WellVerdictChange(
                    well_id=well.id,
                    well_name=name,
                    coverage_before=was,
                    coverage_after=well.coverage_status,
                )
            )
    changes.sort(key=lambda c: c.well_name)

    return CoverageScopeChange(
        status_filter_before=status_before,
        profile_filter_before=profile_before,
        status_filter_after=status_after,
        profile_filter_after=profile_after,
        unchanged=False,
        recomputed_customer_ids=tuple(sorted(sweep.recomputed_customer_ids)),
        recomputed_well_count=len(wells),
        well_changes=tuple(changes),
    )
