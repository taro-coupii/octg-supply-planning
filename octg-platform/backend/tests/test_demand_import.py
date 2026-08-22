"""Excel demand import, end to end.

Fixtures are generated here with openpyxl rather than committed as binaries, so
the column contract under test is visible in the test source. No real customer
workbook is read by anything in this suite.
"""

import io
from datetime import datetime, timedelta

import pytest
from openpyxl import Workbook

from app.main import app
from app.models import (
    CoverageResult,
    CoverageStatus,
    DemandImportBatch,
    DemandLine,
    DemandRevision,
    ImpactRecord,
    Well,
)
from tests.phase5_fixtures import NOW, build_client

HEADER = ["Well", "Product", "Quantity", "ROS Date", "Status", "Profile"]


def make_xlsx(rows, header=HEADER, sheet_title="Demand") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title
    if header is not None:
        ws.append(header)
    for row in rows:
        ws.append(row)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


@pytest.fixture()
def client_world():
    client, session_factory, world = build_client()
    try:
        yield client, session_factory, world
    finally:
        app.dependency_overrides.clear()



def _revision_count(session_factory) -> int:
    """How many DemandRevision rows exist right now.

    Used to assert the DELTA an import caused. A bare ``count() == 0`` used to
    work because nothing wrote revision history at creation; now every demand line
    records its initial state as revision 1 when it is created (see
    app.models.demand._write_initial_revision), so the fixture arrives with one
    revision per line. Comparing before/after is what these assertions always
    meant -- "the import wrote N revisions" -- and it stays exact.
    """
    db = session_factory()
    try:
        return db.query(DemandRevision).count()
    finally:
        db.close()


def _upload(client, payload, filename="demand.xlsx"):
    return client.post(
        "/demand-imports",
        files={
            "file": (
                filename,
                payload,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )


def _stage(client, rows, **kw):
    resp = _upload(client, make_xlsx(rows, **kw))
    assert resp.status_code == 201, resp.text
    return resp.json()


def _rows_by_number(batch):
    return {r["row_number"]: r for r in batch["rows"]}


def _decide(client, batch_id, row_id, decision):
    resp = client.patch(
        f"/demand-imports/{batch_id}/rows/{row_id}", json={"decision": decision}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# --------------------------------------------------------------------------
# Staging suggests; it never mutates demand
# --------------------------------------------------------------------------


def test_upload_stages_and_matches_without_touching_demand(client_world):
    client, session_factory, w = client_world
    revisions_before = _revision_count(session_factory)
    ros = (NOW + timedelta(days=30)).date().isoformat()
    batch = _stage(
        client,
        [
            # Same well/product/ROS as L1 but a different quantity -> revision.
            ["WELL-1", w.p_a_desc, 7000, ros, "Confirmed", "Primary"],
            # A product WELL-1 has never ordered -> new demand.
            ["WELL-1", w.p_b_desc, 2500, ros, "Confirmed", "Primary"],
        ],
    )
    assert batch["status"] == "Staged"
    assert batch["row_count"] == 2
    assert batch["error_count"] == 0
    assert batch["pending_count"] == 2
    assert batch["revision_suggestion_count"] == 1
    assert batch["new_suggestion_count"] == 1
    assert batch["column_contract"]["required"] == [
        "well",
        "product",
        "quantity",
        "ros_date",
    ]

    rows = _rows_by_number(batch)
    revision = rows[2]
    assert revision["match_type"] == "Revision"
    assert revision["matched_demand_line_id"] == w.l1_id
    assert revision["matched_quantity"] == 5000
    assert "quantity differs" in revision["match_reason"]
    assert revision["decision"] == "Pending"

    new_row = rows[3]
    assert new_row["match_type"] == "New"
    assert new_row["matched_demand_line_id"] is None
    assert "No existing demand" in new_row["match_reason"]

    # Nothing was written to the demand model.
    db = session_factory()
    try:
        assert db.get(DemandLine, w.l1_id).quantity == 5000
        assert db.query(DemandLine).count() == 6
        assert db.query(DemandRevision).count() == revisions_before
        assert db.query(ImpactRecord).count() == 0
    finally:
        db.close()


def test_get_batch_returns_the_staged_batch_for_review(client_world):
    client, _sf, w = client_world
    ros = (NOW + timedelta(days=30)).date().isoformat()
    staged = _stage(client, [["WELL-1", w.p_a_desc, 7000, ros, "Confirmed", "Primary"]])
    fetched = client.get(f"/demand-imports/{staged['id']}").json()
    assert fetched["id"] == staged["id"]
    assert fetched["rows"][0]["matched_demand_line_id"] == w.l1_id
    assert client.get("/demand-imports/nope").status_code == 404


def test_batch_list(client_world):
    client, _sf, w = client_world
    ros = (NOW + timedelta(days=30)).date().isoformat()
    _stage(client, [["WELL-1", w.p_a_desc, 7000, ros, "Confirmed", "Primary"]])
    listed = client.get("/demand-imports").json()
    assert len(listed) == 1
    assert listed[0]["status"] == "Staged"
    assert listed[0]["filename"] == "demand.xlsx"


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------


def test_exact_match_on_all_four_keys_is_a_no_change_revision(client_world):
    client, _sf, w = client_world
    ros = (NOW + timedelta(days=30)).date().isoformat()
    batch = _stage(client, [["WELL-1", w.p_a_desc, 5000, ros, "Confirmed", "Primary"]])
    row = batch["rows"][0]
    assert row["match_type"] == "Revision"
    assert row["matched_demand_line_id"] == w.l1_id
    assert "Exact match" in row["match_reason"]


def test_same_quantity_different_ros_is_a_revision(client_world):
    client, _sf, w = client_world
    ros = (NOW + timedelta(days=75)).date().isoformat()
    batch = _stage(client, [["WELL-1", w.p_a_desc, 5000, ros, "Confirmed", "Primary"]])
    row = batch["rows"][0]
    assert row["match_type"] == "Revision"
    assert row["matched_demand_line_id"] == w.l1_id
    assert "ROS date differs" in row["match_reason"]


def test_neither_ros_nor_quantity_matching_suggests_new_but_offers_the_candidate(
    client_world,
):
    """The suggestion is New, yet a candidate line is still attached so the user
    can override and treat it as a re-plan of existing demand."""
    client, _sf, w = client_world
    ros = (NOW + timedelta(days=75)).date().isoformat()
    batch = _stage(client, [["WELL-1", w.p_a_desc, 9999, ros, "Confirmed", "Primary"]])
    row = batch["rows"][0]
    assert row["match_type"] == "New"
    assert row["matched_demand_line_id"] is not None
    assert "neither its ROS date nor its quantity matches" in row["match_reason"]


def test_wells_and_products_resolve_by_id_and_case_insensitively(client_world):
    client, _sf, w = client_world
    ros = (NOW + timedelta(days=30)).date().isoformat()
    batch = _stage(
        client,
        [
            ["well-1", w.p_a_desc.lower(), 7000, ros, "confirmed", "primary"],
            [w.w1_id, w.p_a_id, 8000, ros, None, None],
        ],
    )
    assert batch["error_count"] == 0
    for row in batch["rows"]:
        assert row["well_id"] == w.w1_id
        assert row["product_id"] == w.p_a_id


def test_omitted_status_inherits_from_the_matched_line_on_a_revision(client_world):
    """Importing a quantity change must not silently demote a Confirmed line to
    Planned and drop it out of coverage scope."""
    client, _sf, w = client_world
    ros = (NOW + timedelta(days=30)).date().isoformat()
    batch = _stage(
        client,
        [["WELL-1", w.p_a_desc, 7000, ros]],
        header=["Well", "Product", "Quantity", "ROS Date"],
    )
    row = batch["rows"][0]
    assert row["match_type"] == "Revision"
    assert row["status"] == "Confirmed"
    assert row["profile"] == "Primary"


def test_omitted_status_inherits_the_wells_status_on_new_demand(client_world):
    """An omitted status cell asserts NOTHING, so it must change nothing.

    REPURPOSED, and the safety property it existed to protect is STRONGER than
    before, not weaker. The old rule defaulted new demand to Planned so that "a
    fresh import cannot move anybody's coverage until a planner confirms it". That
    worked when status was a LINE column: a new Planned line was harmlessly out of
    scope beside its well's Confirmed demand.

    Demand status is a property of the WELL now, so the old default would no longer
    be safe -- it would assert that the WELL is Planned, dropping every existing
    line of that well out of coverage scope, which is a far larger and quieter
    change than the one it was written to prevent. Inheriting the well's current
    status makes the guarantee absolute instead of approximate: an import with no
    status column cannot move ANY well's coverage scope at all.

    WELL-2 is Confirmed, so that is what the staged row carries.
    """
    client, _sf, w = client_world
    ros = (NOW + timedelta(days=30)).date().isoformat()
    batch = _stage(
        client,
        [["WELL-2", w.p_a_desc, 1500, ros]],
        header=["Well", "Product", "Quantity", "ROS Date"],
    )
    row = batch["rows"][0]
    assert row["match_type"] == "New"
    assert row["status"] == "Confirmed"
    assert row["profile"] == "Primary"


def test_omitted_status_on_a_budgeted_well_inherits_budgeted(client_world):
    """The complement, so the rule is shown to READ the well rather than to hardcode
    Confirmed. WELL-4 is Budgeted."""
    client, _sf, w = client_world
    ros = (NOW + timedelta(days=30)).date().isoformat()
    batch = _stage(
        client,
        [["WELL-4", w.p_a_desc, 1500, ros]],
        header=["Well", "Product", "Quantity", "ROS Date"],
    )
    assert batch["rows"][0]["status"] == "Budgeted"


def test_header_aliases_and_whitespace(client_world):
    client, _sf, w = client_world
    ros = (NOW + timedelta(days=30)).date().isoformat()
    batch = _stage(
        client,
        [["WELL-1", w.p_a_desc, 7000, ros]],
        header=[" well_name ", "ITEM", "Qty", "ros"],
    )
    assert batch["error_count"] == 0
    assert batch["rows"][0]["match_type"] == "Revision"


# --------------------------------------------------------------------------
# Malformed input: per-row errors, never a 500, never a blocked batch
# --------------------------------------------------------------------------


def test_bad_rows_produce_per_row_errors_without_failing_the_batch(client_world):
    client, session_factory, w = client_world
    revisions_before = _revision_count(session_factory)
    good_ros = (NOW + timedelta(days=30)).date().isoformat()
    batch = _stage(
        client,
        [
            ["WELL-1", w.p_a_desc, 7000, good_ros, "Confirmed", "Primary"],  # r2 good
            ["WELL-NOPE", w.p_a_desc, 100, good_ros, None, None],            # r3 well
            ["WELL-1", "Not A Product", 100, good_ros, None, None],          # r4 product
            ["WELL-1", w.p_a_desc, "many", good_ros, None, None],            # r5 qty
            ["WELL-1", w.p_a_desc, -5, good_ros, None, None],                # r6 qty<=0
            ["WELL-1", w.p_a_desc, 100, "03/04/2027", None, None],           # r7 date
            ["WELL-1", w.p_a_desc, 100, None, None, None],                   # r8 no date
            [None, None, None, None, None, None],                            # blank: skipped
            ["WELL-1", w.p_a_desc, 100, good_ros, "Maybe", None],            # r10 status
            ["WELL-1", w.p_a_desc, 100, good_ros, None, "Sideways"],         # r11 profile
            ["WELL-1", w.p_a_desc, 8000, good_ros, None, None],              # r12 good
            ["WELL-1", w.p_a_desc, 8000, good_ros, None, None],              # r13 dup of r12
        ],
    )
    rows = _rows_by_number(batch)
    assert batch["row_count"] == 11  # the fully blank row is not staged at all
    assert batch["error_count"] == 9

    assert rows[2]["error"] is None and rows[2]["match_type"] == "Revision"
    assert "unknown well 'WELL-NOPE'" in rows[3]["error"]
    assert "unknown product 'Not A Product'" in rows[4]["error"]
    assert "not a number" in rows[5]["error"]
    assert "greater than zero" in rows[6]["error"]
    assert "day and month cannot be told apart" in rows[7]["error"]
    assert "ROS date is empty" in rows[8]["error"]
    assert "status 'Maybe' is not recognised" in rows[10]["error"]
    assert "profile 'Sideways' is not recognised" in rows[11]["error"]
    assert rows[12]["error"] is None
    assert "duplicate of row 12 in this file" in rows[13]["error"]

    for number in (3, 4, 5, 6, 7, 8, 10, 11, 13):
        assert rows[number]["match_type"] == "Error"
        # The verbatim cell text survives, so the review screen can show it.
        assert rows[number]["raw_well"] is not None or number == 8

    # Absolutely no demand was touched.
    db = session_factory()
    try:
        assert db.query(DemandRevision).count() == revisions_before
    finally:
        db.close()


def test_multiple_problems_in_one_row_are_all_reported(client_world):
    client, _sf, _w = client_world
    batch = _stage(client, [["WELL-NOPE", "Nope", "lots", "not-a-date", None, None]])
    error = batch["rows"][0]["error"]
    assert "unknown well" in error
    assert "unknown product" in error
    assert "not a number" in error
    assert "is not a date" in error


def test_bare_number_in_the_ros_column_is_refused_not_guessed(client_world):
    """45000 could be an Excel serial or a quantity in the wrong column. Guessing
    would silently produce a 2023 ROS date."""
    client, _sf, w = client_world
    batch = _stage(client, [["WELL-1", w.p_a_desc, 100, 45000, None, None]])
    assert "bare number" in batch["rows"][0]["error"]


def test_real_excel_date_cells_and_thousands_separators_are_accepted(client_world):
    client, _sf, w = client_world
    when = NOW + timedelta(days=30)
    batch = _stage(
        client,
        [["WELL-1", w.p_a_desc, "7,000", datetime(when.year, when.month, when.day)]],
        header=["Well", "Product", "Quantity", "ROS Date"],
    )
    row = batch["rows"][0]
    assert row["error"] is None
    assert row["quantity"] == 7000
    assert row["match_type"] == "Revision"


def test_file_level_failures_are_400s(client_world):
    client, _sf, _w = client_world

    # Not a workbook at all.
    resp = _upload(client, b"this is not a zip file")
    assert resp.status_code == 400
    assert "could not be read as an .xlsx workbook" in resp.json()["detail"]

    # Empty sheet.
    resp = _upload(client, make_xlsx([], header=None))
    assert resp.status_code == 400
    assert "empty" in resp.json()["detail"]

    # Missing required columns.
    resp = _upload(client, make_xlsx([["WELL-1", 100]], header=["Well", "Quantity"]))
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "Missing required column(s): product, ros_date" in detail

    # Header row only, no data: valid but empty.
    resp = _upload(client, make_xlsx([]))
    assert resp.status_code == 201
    assert resp.json()["row_count"] == 0

    # Wrong extension, and a zero-byte upload.
    assert _upload(client, b"x", filename="demand.csv").status_code == 400
    assert _upload(client, b"").status_code == 400


# --------------------------------------------------------------------------
# Decisions
# --------------------------------------------------------------------------


def test_error_rows_can_only_be_skipped(client_world):
    client, _sf, _w = client_world
    batch = _stage(client, [["WELL-NOPE", "Nope", 1, "2030-01-01", None, None]])
    row_id = batch["rows"][0]["id"]
    for decision in ("AcceptRevision", "AcceptNew"):
        resp = client.patch(
            f"/demand-imports/{batch['id']}/rows/{row_id}",
            json={"decision": decision},
        )
        assert resp.status_code == 400
        assert "failed validation" in resp.json()["detail"]
    assert _decide(client, batch["id"], row_id, "Skip")["decision"] == "Skip"


def test_a_row_with_no_match_cannot_be_accepted_as_a_revision(client_world):
    client, _sf, w = client_world
    ros = (NOW + timedelta(days=30)).date().isoformat()
    batch = _stage(client, [["WELL-1", w.p_b_desc, 2500, ros, "Confirmed", "Primary"]])
    row_id = batch["rows"][0]["id"]
    resp = client.patch(
        f"/demand-imports/{batch['id']}/rows/{row_id}",
        json={"decision": "AcceptRevision"},
    )
    assert resp.status_code == 400
    assert "cannot be accepted as a revision" in resp.json()["detail"]


def test_unknown_row_is_a_404(client_world):
    client, _sf, _w = client_world
    batch = _stage(client, [])
    resp = client.patch(
        f"/demand-imports/{batch['id']}/rows/nope", json={"decision": "Skip"}
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# Apply
# --------------------------------------------------------------------------


def test_apply_happy_path_end_to_end(client_world):
    client, session_factory, w = client_world
    ros = (NOW + timedelta(days=30)).date().isoformat()
    new_ros = (NOW + timedelta(days=45)).date().isoformat()
    batch = _stage(
        client,
        [
            ["WELL-1", w.p_a_desc, 6500, ros, "Confirmed", "Primary"],   # revision of L1
            ["WELL-1", w.p_b_desc, 200, new_ros, "Confirmed", "Primary"],  # new demand
            ["WELL-3", w.p_a_desc, 4242, ros, "Confirmed", "Primary"],  # to be skipped
        ],
    )
    rows = _rows_by_number(batch)
    _decide(client, batch["id"], rows[2]["id"], "AcceptRevision")
    _decide(client, batch["id"], rows[3]["id"], "AcceptNew")
    _decide(client, batch["id"], rows[4]["id"], "Skip")

    resp = client.post(f"/demand-imports/{batch['id']}/apply")
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["status"] == "Applied"
    assert result["revised_count"] == 1
    assert result["created_count"] == 1
    assert result["skipped_count"] == 1
    assert result["pending_count"] == 0
    assert len(result["impact_record_ids"]) == 2
    assert result["failed_row_ids"] == []
    assert result["batch"]["applied_at"] is not None

    db = session_factory()
    try:
        # The revision landed on the existing line, through apply_revision.
        line = db.get(DemandLine, w.l1_id)
        assert line.quantity == 6500
        assert line.current_revision_no == 2
        revisions = (
            db.query(DemandRevision)
            .filter(DemandRevision.demand_line_id == w.l1_id)
            .all()
        )
        # Revision 1 is the line's state AS CREATED by the fixture (5000); the
        # import appended revision 2. Asserting the whole history rather than only
        # the appended row is strictly more than the old `== [2]` checked.
        assert [r.revision_no for r in revisions] == [1, 2]
        assert revisions[0].quantity == 5000
        assert revisions[1].quantity == 6500

        # ImpactRecords exist for both -- the Home Dashboard card depends on them.
        impacts = db.query(ImpactRecord).all()
        assert len(impacts) == 2
        revised = next(i for i in impacts if i.demand_line_id == w.l1_id)
        assert revised.quantity_before == 5000
        assert revised.quantity_after == 6500

        # The new line was created and given revision 1 by apply_revision, with an
        # impact record showing it appearing from nothing.
        created_id = next(
            r["applied_demand_line_id"]
            for r in client.get(f"/demand-imports/{batch['id']}").json()["rows"]
            if r["row_number"] == 3
        )
        created = db.get(DemandLine, created_id)
        assert created.quantity == 200
        assert created.current_revision_no == 1
        created_revs = (
            db.query(DemandRevision)
            .filter(DemandRevision.demand_line_id == created_id)
            .all()
        )
        assert [r.revision_no for r in created_revs] == [1]
        created_impact = next(i for i in impacts if i.demand_line_id == created_id)
        assert created_impact.quantity_before == 0.0
        assert created_impact.quantity_after == 200

        # Coverage was recomputed: the raised quantity now exceeds the 6000 on hand.
        assert db.get(CoverageResult, w.l1_id).status == CoverageStatus.UNCOVERED
    finally:
        db.close()


def test_skipped_rows_change_nothing(client_world):
    client, session_factory, w = client_world
    revisions_before = _revision_count(session_factory)
    ros = (NOW + timedelta(days=30)).date().isoformat()
    batch = _stage(client, [["WELL-1", w.p_a_desc, 9500, ros, "Confirmed", "Primary"]])
    _decide(client, batch["id"], batch["rows"][0]["id"], "Skip")

    result = client.post(f"/demand-imports/{batch['id']}/apply").json()
    assert result["skipped_count"] == 1
    assert result["revised_count"] == 0
    assert result["created_count"] == 0

    db = session_factory()
    try:
        assert db.get(DemandLine, w.l1_id).quantity == 5000
        assert db.query(DemandRevision).count() == revisions_before
        assert db.query(ImpactRecord).count() == 0
        assert db.get(CoverageResult, w.l1_id).status == CoverageStatus.COVERED
    finally:
        db.close()


def test_pending_rows_are_left_alone_and_counted(client_world):
    """The system never decides on the user's behalf."""
    client, session_factory, w = client_world
    revisions_before = _revision_count(session_factory)
    ros = (NOW + timedelta(days=30)).date().isoformat()
    batch = _stage(
        client,
        [
            ["WELL-1", w.p_a_desc, 5500, ros, "Confirmed", "Primary"],
            ["WELL-1", w.p_a_desc, 5600, ros, "Confirmed", "Primary"],
        ],
    )
    rows = _rows_by_number(batch)
    _decide(client, batch["id"], rows[2]["id"], "AcceptRevision")

    result = client.post(f"/demand-imports/{batch['id']}/apply").json()
    assert result["revised_count"] == 1
    assert result["pending_count"] == 1

    db = session_factory()
    try:
        assert db.get(DemandLine, w.l1_id).quantity == 5500
        # Exactly ONE revision written by the import -- the accepted row.
        assert db.query(DemandRevision).count() == revisions_before + 1
    finally:
        db.close()


def test_error_rows_are_never_applied(client_world):
    client, session_factory, _w = client_world
    revisions_before = _revision_count(session_factory)
    batch = _stage(client, [["WELL-NOPE", "Nope", 1, "2030-01-01", None, None]])
    result = client.post(f"/demand-imports/{batch['id']}/apply").json()
    assert result["error_count"] == 1
    assert result["revised_count"] == 0
    assert result["created_count"] == 0

    db = session_factory()
    try:
        assert db.query(DemandRevision).count() == revisions_before
    finally:
        db.close()


def test_applying_twice_is_a_409(client_world):
    client, _sf, w = client_world
    ros = (NOW + timedelta(days=30)).date().isoformat()
    batch = _stage(client, [["WELL-1", w.p_a_desc, 5500, ros, "Confirmed", "Primary"]])
    _decide(client, batch["id"], batch["rows"][0]["id"], "AcceptRevision")
    assert client.post(f"/demand-imports/{batch['id']}/apply").status_code == 200
    resp = client.post(f"/demand-imports/{batch['id']}/apply")
    assert resp.status_code == 409
    assert "already been applied" in resp.json()["detail"]


def test_decisions_are_frozen_after_apply(client_world):
    client, _sf, w = client_world
    ros = (NOW + timedelta(days=30)).date().isoformat()
    batch = _stage(client, [["WELL-1", w.p_a_desc, 5500, ros, "Confirmed", "Primary"]])
    row_id = batch["rows"][0]["id"]
    _decide(client, batch["id"], row_id, "AcceptRevision")
    client.post(f"/demand-imports/{batch['id']}/apply")
    resp = client.patch(
        f"/demand-imports/{batch['id']}/rows/{row_id}", json={"decision": "Skip"}
    )
    assert resp.status_code == 409


def test_a_deleted_match_target_fails_only_that_row(client_world):
    client, session_factory, w = client_world
    ros = (NOW + timedelta(days=30)).date().isoformat()
    new_ros = (NOW + timedelta(days=45)).date().isoformat()
    batch = _stage(
        client,
        [
            ["WELL-1", w.p_a_desc, 5500, ros, "Confirmed", "Primary"],
            ["WELL-1", w.p_b_desc, 200, new_ros, "Confirmed", "Primary"],
        ],
    )
    rows = _rows_by_number(batch)
    _decide(client, batch["id"], rows[2]["id"], "AcceptRevision")
    _decide(client, batch["id"], rows[3]["id"], "AcceptNew")

    # The matched line disappears between review and apply.
    db = session_factory()
    try:
        cr = db.get(CoverageResult, w.l1_id)
        if cr is not None:
            db.delete(cr)
        db.query(DemandImportBatch)  # keep the session honest about flush order
        db.delete(db.get(DemandLine, w.l1_id))
        db.commit()
    finally:
        db.close()

    result = client.post(f"/demand-imports/{batch['id']}/apply").json()
    assert result["created_count"] == 1
    assert result["revised_count"] == 0
    assert len(result["failed_row_ids"]) == 1
    failed = next(
        r for r in result["batch"]["rows"] if r["id"] in result["failed_row_ids"]
    )
    assert "no longer exists" in failed["apply_error"]


# --------------------------------------------------------------------------
# A `status` cell is a statement about the ROW'S WELL
# --------------------------------------------------------------------------


def test_two_rows_disagreeing_about_one_wells_status_are_per_row_errors(client_world):
    """A file cannot put one well at two statuses, and it is refused ROW BY ROW.

    Demand status is a property of the well, so "WELL-1 is Confirmed" and "WELL-1 is
    Budgeted" in the same upload is an assertion the platform does not represent.
    There is no defensible tie-break -- "last row wins" and "most committed wins" are
    both a guess about which spreadsheet row the planner meant, and either one would
    silently move every line of the well -- so the LATER row becomes an error naming
    the row it contradicts, exactly as an in-file duplicate does. The rest of the file
    stages normally.
    """
    client, _sf, w = client_world
    ros = (NOW + timedelta(days=30)).date().isoformat()
    batch = _stage(
        client,
        [
            ["WELL-1", w.p_a_desc, 7000, ros, "Confirmed", "Primary"],   # row 2
            ["WELL-1", w.p_b_desc, 300, ros, "Budgeted", "Primary"],     # row 3, clash
            ["WELL-2", w.p_a_desc, 400, ros, "Budgeted", "Primary"],     # row 4, fine
        ],
    )
    rows = {r["row_number"]: r for r in batch["rows"]}
    assert rows[2]["match_type"] != "Error"
    assert rows[4]["match_type"] != "Error"

    clash = rows[3]
    assert clash["match_type"] == "Error"
    assert "contradicts row 2" in clash["error"]
    assert "property of the WELL" in clash["error"]
    assert batch["error_count"] == 1


def test_two_rows_agreeing_about_one_wells_status_are_fine(client_world):
    """The complement, so the check is shown to compare VALUES rather than to refuse
    any well mentioned twice -- which would break the ordinary multi-stage upload."""
    client, _sf, w = client_world
    ros = (NOW + timedelta(days=30)).date().isoformat()
    batch = _stage(
        client,
        [
            ["WELL-1", w.p_a_desc, 7000, ros, "Confirmed", "Primary"],
            ["WELL-1", w.p_b_desc, 300, ros, "Confirmed", "Primary"],
        ],
    )
    assert batch["error_count"] == 0


def test_a_blank_status_cell_never_joins_a_disagreement(client_world):
    """A blank asserts nothing, so it cannot contradict anything.

    Without this the ordinary case -- one row states the status, the others leave the
    column empty -- would be reported as a conflict with itself.
    """
    client, _sf, w = client_world
    ros = (NOW + timedelta(days=30)).date().isoformat()
    batch = _stage(
        client,
        [
            ["WELL-1", w.p_a_desc, 7000, ros, "Budgeted", "Primary"],
            ["WELL-1", w.p_b_desc, 300, ros, None, "Primary"],
        ],
    )
    assert batch["error_count"] == 0
    rows = {r["row_number"]: r for r in batch["rows"]}
    assert rows[2]["status"] == "Budgeted"
    # The blank row inherits the WELL's CURRENT status (WELL-1 is Confirmed today),
    # not the sibling row's assertion. A blank changes nothing, and reading it as
    # "whatever another row said" would make it change something.
    assert rows[3]["status"] == "Confirmed"


def test_applying_a_row_whose_status_differs_moves_the_WHOLE_well(client_world):
    """Applying an imported status is a WELL-level write, and says so.

    The file listed one line; the status it asserted belongs to the well, so every
    line of that well gets a revision -- including lines the spreadsheet never
    mentioned. Pretending otherwise would leave those lines' history claiming a status
    their well no longer has. The result reports the fan-out separately from the
    row count so the user is told rather than left to discover it.

    A SECOND DECISION IS NOW REQUIRED FIRST, and this test walks it
    --------------------------------------------------------------
    A status cell that disagrees with the well's current status is exactly the
    file-vs-live CONFLICT that `app.engines.demand_import.detect_conflict` gates: the
    fan-out this test exists to pin is the reason it is gated, because it is strictly
    more than the diff the reviewer was shown. So the row now needs an explicit
    override approval as well as an accept, and apply refuses the whole batch without
    it.

    Every original assertion below is unchanged -- the write, the ordering of the two
    revisions, the fan-out counts and the well leaving coverage scope are all still
    pinned exactly as before. What is added is the refusal ahead of them, so this test
    proves both that the gate exists AND that passing through it produces the
    identical outcome the direct path always produced. There is one apply path, not
    two.
    """
    client, session_factory, w = client_world
    ros = (NOW + timedelta(days=30)).date().isoformat()
    batch = _stage(
        client,
        [["WELL-1", w.p_a_desc, 6500, ros, "Budgeted", "Primary"]],
    )
    row = batch["rows"][0]
    assert row["match_type"] == "Revision"
    # The row is valid -- a conflict is not an error -- and it is flagged.
    assert row["error"] is None
    assert row["is_conflict"] is True
    assert row["conflict_kind"] == "WellDemandStatus"
    assert row["requires_override_approval"] is True

    _decide(client, batch["id"], row["id"], "AcceptRevision")

    # Apply REFUSES, and writes nothing at all.
    blocked = client.post(f"/demand-imports/{batch['id']}/apply")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["blocked_row_ids"] == [row["id"]]
    db = session_factory()
    try:
        assert db.get(Well, w.w1_id).demand_status.value == "Confirmed"
    finally:
        db.close()

    approved = client.post(
        f"/demand-imports/{batch['id']}/rows/{row['id']}/override-approval",
        json={"approved": True, "approved_by": "planner"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["requires_override_approval"] is False

    result = client.post(f"/demand-imports/{batch['id']}/apply").json()

    assert result["revised_count"] == 1
    assert result["well_status_changed_ids"] == [w.w1_id]
    # WELL-1 has one line, so the cascade revised exactly that one.
    assert result["well_status_cascaded_line_count"] == 1

    db = session_factory()
    try:
        well = db.get(Well, w.w1_id)
        assert well.demand_status.value == "Budgeted"
        line = db.get(DemandLine, w.l1_id)
        # Revision 1 at creation, revision 2 from the status change, revision 3 from
        # the imported quantity -- in that order, so the history never records the new
        # quantity against the status the well had just stopped having.
        revisions = sorted(line.revisions, key=lambda r: r.revision_no)
        assert [r.revision_no for r in revisions] == [1, 2, 3]
        assert revisions[1].status.value == "Budgeted"
        assert revisions[1].quantity == 5000        # quantity not yet changed
        assert revisions[2].status.value == "Budgeted"
        assert revisions[2].quantity == 6500        # ...then the import
        # The well left scope, so its verdict is gone.
        assert db.get(CoverageResult, w.l1_id) is None
        assert well.coverage_status is None
    finally:
        db.close()


def test_a_row_left_pending_does_not_move_its_wells_status(client_world):
    """A PENDING row is not a decision, so its status cell is not applied either.

    Same principle the whole flow rests on: uploading a file changes nothing until the
    user accepts a row.
    """
    client, session_factory, w = client_world
    ros = (NOW + timedelta(days=30)).date().isoformat()
    batch = _stage(
        client,
        [["WELL-1", w.p_a_desc, 6500, ros, "Budgeted", "Primary"]],
    )
    result = client.post(f"/demand-imports/{batch['id']}/apply").json()
    assert result["pending_count"] == 1
    assert result["well_status_changed_ids"] == []

    db = session_factory()
    try:
        assert db.get(Well, w.w1_id).demand_status.value == "Confirmed"
    finally:
        db.close()
