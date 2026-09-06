"""The demo database's SHAPE, asserted against the actual `seed.seed_demo`.

`tests/test_seed_honesty_paths.py` explains why a seed is tested by running it:
a fixture can say nothing about whether the shipped demo reaches a state. This
module pins the re-baseline of 2026-09-06 (owner ruling, HANDOFF section 25):
every verdict kind reachable, both Oracle-release stories rendered, the demo
scenario seeded and previewable with its approval story intact, and the
safety-stock dip the scenario builds on still amber rather than already red.
Each of these was broken at least once by an innocent-looking seed edit.
"""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, enforce_sqlite_foreign_keys
from app.engines.mor import order_requirements
from app.engines.scenario import preview
from app.models import (
    CoverageResult,
    CoverageStatus,
    Product,
    SafetyStock,
    Scenario,
    ScenarioOverride,
    ScenarioTargetKind,
)
from seed import seed_demo


@pytest.fixture(scope="module")
def seeded():
    engine = enforce_sqlite_foreign_keys(create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    ))
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        conn.execute(text("INSERT INTO alembic_version VALUES ('test-stamp')"))
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    original = (seed_demo.engine, seed_demo.SessionLocal)
    seed_demo.engine = engine
    seed_demo.SessionLocal = TestingSessionLocal
    try:
        assert seed_demo.main([]) == 0
    finally:
        seed_demo.engine, seed_demo.SessionLocal = original
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


def _counts(db):
    counts = {}
    for row in db.query(CoverageResult):
        counts[row.status] = counts.get(row.status, 0) + 1
    return counts


def test_every_verdict_kind_is_reachable_in_the_demo(seeded):
    counts = _counts(seeded)
    assert counts.get(CoverageStatus.COVERED, 0) >= 50
    assert counts.get(CoverageStatus.COVERED_VIA_SUBSTITUTE, 0) >= 2
    assert counts.get(CoverageStatus.PENDING_APPROVAL, 0) >= 3
    assert counts.get(CoverageStatus.UNCOVERED, 0) >= 10
    assert counts.get(CoverageStatus.UNRECOVERABLE, 0) >= 1


def test_both_oracle_release_stories_render(seeded):
    reasons = [r.reason or "" for r in seeded.query(CoverageResult)]
    # Case 1: an earlier well short because a later well's hard assignment holds
    # the steel, and releasing it would close the gap.
    assert any("releasing it would close the gap" in r for r in reasons)
    # Case 2: an approved substitute exists but is hard-assigned to another line.
    assert any("hard-assigned to another demand line" in r for r in reasons)


def test_the_demo_scenario_is_seeded_and_its_approval_story_survives_the_preview(seeded):
    scenario = seeded.query(Scenario).one()
    overrides = (
        seeded.query(ScenarioOverride)
        .filter(ScenarioOverride.scenario_id == scenario.id)
        .all()
    )
    assert len(overrides) >= 7
    assert {o.target_kind for o in overrides} >= {
        ScenarioTargetKind.DEMAND_LINE,
        ScenarioTargetKind.WELL,
        ScenarioTargetKind.PO_ARRIVAL,
        ScenarioTargetKind.ASSIGNMENT,
        ScenarioTargetKind.SUBSTITUTION_APPROVAL,
    }
    impact = preview(seeded, scenario)
    approval = next(
        o for o in overrides
        if o.target_kind == ScenarioTargetKind.SUBSTITUTION_APPROVAL
    )
    (change,) = [
        c for c in impact.line_changes if c.demand_line_id == approval.target_demand_line_id
    ]
    # The story is "the customer approves and the line is rescued". A scenario
    # whose OTHER stories consumed the substitute's residual would turn this into
    # PendingApproval -> Unrecoverable and the override would be pointless.
    assert change.status_before == CoverageStatus.PENDING_APPROVAL
    assert change.status_after == CoverageStatus.COVERED_VIA_SUBSTITUTE


def test_the_safety_stock_dip_the_scenario_builds_on_is_amber_not_red(seeded):
    """Item 2's floor is breached before the balance goes negative, so the demo
    scenario's growth story genuinely turns a dip into a runout."""
    product = next(
        p for p in seeded.query(Product)
        if p.description == seed_demo.CATALOGUE[2]["description"]
    )
    floor = seeded.query(SafetyStock).filter(SafetyStock.product_id == product.id).one()
    grid = order_requirements(seeded)
    row = next(r for r in grid.rows if r.product_id == product.id)
    balances = [c.projected_balance for c in row.cells if c.projected_balance is not None]
    first_dip = next(i for i, b in enumerate(balances) if b < floor.quantity)
    assert balances[first_dip] >= 0
