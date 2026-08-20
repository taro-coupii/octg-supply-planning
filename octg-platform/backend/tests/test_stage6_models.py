import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Scenario, ScenarioOverride, ScenarioOverrideKind, ScenarioStatus


def _scenario(db, name="Scenario1"):
    s = Scenario(name=name, created_at=datetime.datetime.now(datetime.timezone.utc))
    db.add(s)
    db.commit()
    return s


# --- scenarios ---


def test_scenario_status_defaults_draft(db):
    s = _scenario(db)
    assert s.status == ScenarioStatus.DRAFT
    assert s.applied_at is None


def test_scenario_status_values_match_spec():
    assert ScenarioStatus.DRAFT.value == "Draft"
    assert ScenarioStatus.APPLIED.value == "Applied"


def test_scenario_can_be_applied(db):
    s = _scenario(db)
    s.status = ScenarioStatus.APPLIED
    s.applied_at = datetime.datetime.now(datetime.timezone.utc)
    db.commit()
    assert s.status == ScenarioStatus.APPLIED
    assert s.applied_at is not None


# --- scenario_overrides ---


def test_scenario_override_kind_values_match_spec():
    assert ScenarioOverrideKind.QUANTITY.value == "quantity"
    assert ScenarioOverrideKind.ROS_DATE.value == "ros_date"
    assert ScenarioOverrideKind.WELL_STATUS.value == "well_status"
    assert ScenarioOverrideKind.PO_ARRIVAL.value == "po_arrival"
    assert ScenarioOverrideKind.HARD_RELEASE.value == "hard_release"
    assert ScenarioOverrideKind.APPROVAL_FLIP.value == "approval_flip"


def test_scenario_override_created_and_readable(db):
    s = _scenario(db)
    ov = ScenarioOverride(
        scenario_id=s.id,
        kind=ScenarioOverrideKind.QUANTITY,
        target_id="some-demand-line-uuid",
        payload='{"new": 42.0}',
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db.add(ov)
    db.commit()
    assert ov.id is not None
    assert ov.kind == ScenarioOverrideKind.QUANTITY
    assert ov.target_id == "some-demand-line-uuid"
    assert ov.payload == '{"new": 42.0}'


def test_scenario_override_foreign_key_enforced(db):
    db.add(
        ScenarioOverride(
            scenario_id="nonexistent",
            kind=ScenarioOverrideKind.ROS_DATE,
            target_id="some-uuid",
            payload="{}",
            created_at=datetime.datetime.now(datetime.timezone.utc),
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


def test_scenario_override_multiple_per_scenario(db):
    s = _scenario(db)
    db.add(
        ScenarioOverride(
            scenario_id=s.id,
            kind=ScenarioOverrideKind.WELL_STATUS,
            target_id="well-1",
            payload="{}",
            created_at=datetime.datetime.now(datetime.timezone.utc),
        )
    )
    db.add(
        ScenarioOverride(
            scenario_id=s.id,
            kind=ScenarioOverrideKind.PO_ARRIVAL,
            target_id="po-1",
            payload="{}",
            created_at=datetime.datetime.now(datetime.timezone.utc),
        )
    )
    db.commit()
    assert db.query(ScenarioOverride).filter_by(scenario_id=s.id).count() == 2
