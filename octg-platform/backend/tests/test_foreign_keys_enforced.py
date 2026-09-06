"""The database refuses an orphan, on every connection, in tests and in the app.

SQLite ships with foreign-key enforcement OFF and the setting is per connection.
Before app.db.enforce_sqlite_foreign_keys existed, a row pointing at a parent
that does not exist committed silently -- reproduced in the 2026-09-06 review
(F10). PostgreSQL never allowed this, so the two databases disagreed about which
rows are legal; now they agree.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import db as db_module
from app.models import PlanningNode


def test_the_pragma_is_on_for_the_test_engine(db_session):
    assert db_session.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_the_pragma_is_on_for_the_application_engine():
    with db_module.engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_an_orphan_planning_node_cannot_commit(db_session):
    """The F10 reproduction, inverted: this used to succeed."""
    db_session.add(
        PlanningNode(id="n-orphan", name="Nowhere", node_type="Campaign", customer_id="cust-does-not-exist")
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
