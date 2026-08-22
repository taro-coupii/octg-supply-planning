"""The PERSISTED platform-wide coverage scope default.

`app.engines.coverage` decides which demand it evaluates at all from two filters:
a STATUS filter (which selects whole wells, because demand status lives on
`Well.demand_status`) and a PROFILE filter (which selects lines, cutting inside a
well). Those two sets were Python constants -- `DEFAULT_STATUS_FILTER` and
`DEFAULT_PROFILE_FILTER` -- editable only by a code change and a deploy. The
product owner asked for them to be adjustable from the Administration screen, so
they now have a row.

ONE ROW, PLATFORM-WIDE. NOT PER CUSTOMER.
-----------------------------------------
This table holds AT MOST ONE row, pinned to `SINGLETON_ID` by a CHECK constraint.
That is a deliberate modelling decision and the alternatives were both considered:

  A nullable override column pair on `Customer`
      Rejected. It spreads one platform-wide decision across every customer row,
      so "what is the default" becomes a query with N answers and a
      reconciliation rule, and adding a customer would mean deciding its scope
      again. Worse, `Customer` is a record about a counterparty; the coverage
      scope is a property of the PLATFORM's evaluation policy, and mixing the two
      would put a policy knob in the same table a customer-master sync would
      later own.

  A per-customer settings table
      Rejected for now, and the reason is the request rather than laziness. The
      owner said "coverage scope default" -- singular. Per-customer scope would
      also make the Coverage Workspace's grid incoherent: it renders every
      customer's wells in ONE table under ONE filter label, and with per-customer
      scope that label would be a lie for every row it did not apply to. If
      per-customer scope is ever genuinely wanted, this table gains a nullable
      `customer_id` and the singleton row becomes the fallback -- an additive
      migration, not a rewrite. Nothing here forecloses it.

  A generic key/value settings table
      Rejected. `("coverage.status_filter", "Confirmed")` is a string pair with no
      schema, so a typo'd key is a silently missing setting and a typo'd value is
      a silently invalid filter. Named columns let `app.engines.coverage_scope`
      validate against the real enums on the way in and out.

THE ROW IS OPTIONAL, AND ITS ABSENCE IS NOT AN ERROR
----------------------------------------------------
No migration back-fills it and the seed does not write it. An empty table means
"nobody has adjusted the scope", and `app.engines.coverage_scope` then resolves
the ORIGINAL module constants. That is what keeps a database created before this
feature -- and every test that never touches this table -- behaving exactly as it
did, with no data invented on its behalf. The constants are the FALLBACK, not a
duplicate copy of a row that must be kept in sync.

WHY THE FILTERS ARE STRINGS AND NOT ENUM COLUMNS
------------------------------------------------
Each column stores a SET, not a value: "Confirmed" or "Planned,Confirmed". A
SQLAlchemy enum column holds one member, so it cannot express the thing being
stored at all, and an association table of (setting, status) rows would be three
tables and a join for a fact that is two short lists.

They are therefore comma-separated `DemandStatus` / `DemandProfile` VALUES in a
`String`, parsed and validated in exactly one place
(`app.engines.coverage_scope`), which raises on an unknown token rather than
skipping it. A side benefit worth naming because this project has been bitten by
it repeatedly: no new database enum type means no `sa.Enum(..., name=...)`
emitted per table and therefore no postgres `DuplicateObject` trap in the
migration (see `alembic/versions/7ebf91b5526c_initial_schema`).

An empty string is NOT allowed by the engine, and that is a rule worth stating
here as well: an empty status filter would put every well out of scope and every
verdict on the platform would silently become "not evaluated". The engine refuses
it; see `app.engines.coverage_scope.parse_status_filter`.

CHANGING THIS ROW CHANGES EVERY VERDICT ON THE PLATFORM
-------------------------------------------------------
It is not a display preference. The filters decide which lines COMPETE for the
same steel, so widening or narrowing them moves verdicts for lines that were in
scope all along. `app.engines.coverage_scope.set_coverage_scope_defaults` is the
only sanctioned writer and it recomputes every customer synchronously in the same
transaction, because a stored `CoverageResult` computed under the old scope is the
same class of defect as the stale-row bug `recompute_customer` deletes rows to
avoid.
"""

from sqlalchemy import CheckConstraint, Column, DateTime, String, func

from app.db import Base

#: The one primary-key value any row of this table may hold. A fixed, readable
#: sentinel rather than a uuid, for two reasons: the accessor can fetch the row
#: with `db.get(CoverageScopeDefault, SINGLETON_ID)` -- a primary-key load, which
#: SQLAlchemy serves from the session identity map after the first hit, so
#: resolving the scope inside a per-line loop costs one query per session rather
#: than one per call -- and a human reading the table sees what the row is.
SINGLETON_ID = "platform"


class CoverageScopeDefault(Base):
    """The platform-wide coverage scope default. At most one row. See the module docstring."""

    __tablename__ = "coverage_scope_defaults"
    __table_args__ = (
        # The singleton guarantee, in the DATABASE and not merely in the accessor.
        # Two rows here would mean two platform-wide defaults, and whichever one a
        # query happened to return first would decide every coverage verdict --
        # unexplainably, and differently between two servers reading the same data.
        CheckConstraint(
            f"id = '{SINGLETON_ID}'",
            name="ck_coverage_scope_defaults_singleton",
        ),
    )

    id = Column(String(36), primary_key=True, default=SINGLETON_ID)
    #: Comma-separated `DemandStatus` values, e.g. "Confirmed" or "Planned,Confirmed".
    status_filter = Column(String, nullable=False)
    #: Comma-separated `DemandProfile` values, e.g. "Primary,Contingency".
    profile_filter = Column(String, nullable=False)
    #: When the scope was last changed. Surfaced by the API so the Administration
    #: screen can say whether the platform is running on an adjusted scope or on
    #: the shipped fallback -- the two are indistinguishable from the filter values
    #: alone, because an administrator may legitimately set the scope BACK to the
    #: shipped values.
    updated_at = Column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
