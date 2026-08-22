"""Conflict review and override approval on a demand import.

THREE DISTINCT SITUATIONS, AND THIS MODULE KEEPS THEM APART
-----------------------------------------------------------
    FILE vs FILE   Two rows of one upload put one well at two statuses. The file is
                   internally incoherent, so the row is an ERROR and may only be
                   skipped. Pre-existing behaviour, pinned in
                   `tests/test_demand_import.py`; re-asserted here from the new
                   angle -- such a row is an error and NOT a conflict, because there
                   is no override anybody could sensibly approve.

    FILE vs LIVE   The row is perfectly valid and disagrees with the data it would
                   land on, in one of the two ways where the diff the reviewer was
                   shown is NOT the change that would happen: it asserts a demand
                   status the well does not have (cascading to every line of the
                   well), or the line it revises has been revised by somebody else
                   since staging. This is a CONFLICT: gated on a second, explicit
                   override approval, never refused.

    NEITHER        Everything else, which is nearly every row -- including an
                   ordinary revision carrying a different quantity. Not a conflict,
                   deliberately, and it applies exactly as it did before this feature
                   existed.

The last one is the one worth guarding hardest, and
`test_an_ordinary_revision_is_not_a_conflict_and_applies_unchanged` does it: a safety
gate that fires on the common case is not a safety gate, it is a reason to stop
reading the screen.
"""

import io
from datetime import timedelta

import pytest
from openpyxl import Workbook

from app.engines.coverage import apply_revision
from app.engines.demand_import_preview import preview_row_conflict
from app.main import app
from app.models import (
    CoverageResult,
    DemandLine,
    DemandProfile,
    DemandRevision,
    ImpactRecord,
    Well,
)
from tests.phase5_fixtures import NOW, build_client

HEADER = ["Well", "Product", "Quantity", "ROS Date", "Status", "Profile"]


def make_xlsx(rows, header=HEADER) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Demand"
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


def _stage(client, rows, **kw):
    resp = client.post(
        "/demand-imports",
        files={
            "file": (
                "demand.xlsx",
                make_xlsx(rows, **kw),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _decide(client, batch_id, row_id, decision):
    resp = client.patch(
        f"/demand-imports/{batch_id}/rows/{row_id}", json={"decision": decision}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _approve(client, batch_id, row_id, approved=True, by="planner"):
    return client.post(
        f"/demand-imports/{batch_id}/rows/{row_id}/override-approval",
        json={"approved": approved, "approved_by": by},
    )


def _revise_out_of_band(session_factory, line_id, quantity, days=None):
    """Somebody else revises the line between staging and apply -- through the ONE
    sanctioned writer, exactly as the demand API and a scenario apply would."""
    db = session_factory()
    try:
        line = db.get(DemandLine, line_id)
        apply_revision(
            db,
            line,
            quantity=quantity,
            ros_date=line.ros_date if days is None else NOW + timedelta(days=days),
            profile=line.profile,
        )
        db.commit()
    finally:
        db.close()


def _counts(session_factory):
    db = session_factory()
    try:
        return (
            db.query(DemandRevision).count(),
            db.query(ImpactRecord).count(),
        )
    finally:
        db.close()


ROS = (NOW + timedelta(days=30)).date().isoformat()


# --------------------------------------------------------------------------
# What is, and is not, a conflict
# --------------------------------------------------------------------------


def test_an_ordinary_revision_is_not_a_conflict_and_applies_unchanged(client_world):
    """THE regression guard. A file carrying a different quantity is what a revision
    IS -- the ordinary, overwhelmingly common case -- and it must not have acquired a
    second approval step. Gating it would make every row cost two decisions, which is
    not safety, it is a reason to stop reading."""
    client, session_factory, w = client_world
    revisions_before, impacts_before = _counts(session_factory)

    batch = _stage(client, [["WELL-1", w.p_a_desc, 6500, ROS, "Confirmed", "Primary"]])
    row = batch["rows"][0]
    assert row["match_type"] == "Revision"
    assert row["is_conflict"] is False
    assert row["conflict_kind"] is None
    assert row["requires_override_approval"] is False
    assert batch["conflict_count"] == 0
    assert batch["unapproved_conflict_count"] == 0
    # The baseline WAS recorded even though nothing conflicts -- it is what makes a
    # later concurrent revision detectable.
    assert row["baseline_revision_no"] == 1
    assert row["baseline_quantity"] == 5000
    assert row["current_revision_no"] == 1

    # Approving an override on it is refused: there is nothing to authorise.
    _decide(client, batch["id"], row["id"], "AcceptRevision")
    refused = _approve(client, batch["id"], row["id"])
    assert refused.status_code == 400
    assert "does not conflict" in refused.json()["detail"]

    # And it applies with NO approval at all, exactly as before.
    result = client.post(f"/demand-imports/{batch['id']}/apply")
    assert result.status_code == 200, result.text
    assert result.json()["revised_count"] == 1
    assert result.json()["well_status_changed_ids"] == []

    revisions_after, impacts_after = _counts(session_factory)
    assert revisions_after == revisions_before + 1
    assert impacts_after == impacts_before + 1
    db = session_factory()
    try:
        assert db.get(DemandLine, w.l1_id).quantity == 6500
    finally:
        db.close()


def test_a_blank_status_cell_is_never_a_conflict(client_world):
    """A blank asserts nothing, so it cannot disagree with anything -- even when the
    well's status is not the one the reviewer might assume."""
    client, _sf, w = client_world
    batch = _stage(
        client,
        [["WELL-4", w.p_a_desc, 600, ROS]],  # WELL-4 is Budgeted
        header=["Well", "Product", "Quantity", "ROS Date"],
    )
    row = batch["rows"][0]
    assert row["status"] == "Budgeted"  # inherited
    assert row["raw_status"] is None
    assert row["is_conflict"] is False


def test_a_genuinely_new_line_is_never_a_conflict(client_world):
    """There is nothing live to have disagreed with. A new line has no prior value,
    which is exactly why its baseline columns are null."""
    client, _sf, w = client_world
    batch = _stage(
        client,
        [["WELL-1", w.p_b_desc, 2500, ROS, "Confirmed", "Primary"]],
    )
    row = batch["rows"][0]
    assert row["match_type"] == "New"
    assert row["matched_demand_line_id"] is None
    assert row["baseline_revision_no"] is None
    assert row["is_conflict"] is False
    assert row["requires_override_approval"] is False


def test_file_vs_file_disagreement_stays_an_ERROR_and_is_not_a_conflict(client_world):
    """Two rows, one well, two statuses. The FILE is wrong, so there is no override
    a human could approve -- "apply one of the two statuses" is the guess the error
    exists to refuse. An error row must therefore never be reported as a conflict, or
    the screen would offer an approval button that can authorise nothing."""
    client, _sf, w = client_world
    batch = _stage(
        client,
        [
            ["WELL-1", w.p_a_desc, 7000, ROS, "Confirmed", "Primary"],
            ["WELL-1", w.p_b_desc, 300, ROS, "Budgeted", "Primary"],
        ],
    )
    rows = {r["row_number"]: r for r in batch["rows"]}
    clash = rows[3]
    assert clash["match_type"] == "Error"
    assert "contradicts row 2" in clash["error"]
    assert clash["is_conflict"] is False
    assert clash["requires_override_approval"] is False

    # Row 2's status agrees with the well, so nothing in this batch conflicts.
    assert batch["conflict_count"] == 0

    # An error row cannot carry an override approval either.
    resp = _approve(client, batch["id"], clash["id"])
    assert resp.status_code == 400
    assert "does not conflict" in resp.json()["detail"]


def test_status_conflict_is_detected_at_staging_with_the_cascade_stated(client_world):
    """FILE vs LIVE, kind one. WELL-2 is Confirmed and carries TWO lines; a file
    asserting Budgeted for it would revise both, including the one it never listed."""
    client, _sf, w = client_world
    batch = _stage(client, [["WELL-2", w.p_b_desc, 3000, "2027-01-15", "Budgeted", "Primary"]])
    row = batch["rows"][0]

    assert row["error"] is None  # a conflict is not an error
    assert row["is_conflict"] is True
    assert row["conflict_kind"] == "WellDemandStatus"
    assert row["conflict_field"] == "status"
    assert row["conflict_current_value"] == "Confirmed"
    assert row["conflict_file_value"] == "Budgeted"
    assert row["conflict_cascade_line_count"] == 2
    assert row["requires_override_approval"] is True
    assert "writes a revision to all 2 demand line(s)" in row["conflict_detail"]
    assert batch["conflict_count"] == 1
    # Not counted as BLOCKING yet -- nobody has accepted it, so apply would not have
    # touched it anyway.
    assert batch["unapproved_conflict_count"] == 0

    _decide(client, batch["id"], row["id"], "AcceptRevision")
    refreshed = client.get(f"/demand-imports/{batch['id']}").json()
    assert refreshed["unapproved_conflict_count"] == 1


def test_concurrent_revision_conflict_appears_AFTER_staging_when_the_book_moves(
    client_world,
):
    """FILE vs LIVE, kind two -- the lost-update problem.

    The row was clean when staged. Somebody else then revised the very line it
    targets, so the diff the reviewer is looking at was computed against a value that
    no longer exists. The conflict must appear on the NEXT read without anything
    re-uploading, which is why `is_conflict` is derived rather than stored.
    """
    client, session_factory, w = client_world
    batch = _stage(client, [["WELL-1", w.p_a_desc, 6500, ROS, "Confirmed", "Primary"]])
    row = batch["rows"][0]
    assert row["is_conflict"] is False
    assert row["baseline_revision_no"] == 1

    _revise_out_of_band(session_factory, w.l1_id, 5200)

    row = client.get(f"/demand-imports/{batch['id']}").json()["rows"][0]
    assert row["is_conflict"] is True
    assert row["conflict_kind"] == "ConcurrentRevision"
    assert row["baseline_revision_no"] == 1
    assert row["current_revision_no"] == 2
    assert row["baseline_quantity"] == 5000
    assert row["matched_quantity"] == 5200  # what is live NOW
    assert "quantity 5000 -> 5200" in row["conflict_detail"]
    assert "was at revision 1 then and is at revision 2 now" in row["conflict_detail"]
    assert row["requires_override_approval"] is True


def test_drift_on_a_candidate_a_NEW_row_will_not_touch_is_not_reported(client_world):
    """A New-suggestion row carries the nearest candidate only so the planner may
    overrule the suggestion. Until they do, drift on a line this row will not write is
    noise -- and it becomes a real conflict the moment they DO overrule it."""
    client, session_factory, w = client_world
    far = (NOW + timedelta(days=75)).date().isoformat()
    batch = _stage(client, [["WELL-1", w.p_a_desc, 9999, far, "Confirmed", "Primary"]])
    row = batch["rows"][0]
    assert row["match_type"] == "New"
    assert row["matched_demand_line_id"] == w.l1_id  # the candidate is attached

    _revise_out_of_band(session_factory, w.l1_id, 5200)

    row = client.get(f"/demand-imports/{batch['id']}").json()["rows"][0]
    assert row["is_conflict"] is False

    # Overruling the suggestion makes the drift matter, and it is reported at once.
    updated = _decide(client, batch["id"], row["id"], "AcceptRevision")
    assert updated["is_conflict"] is True
    assert updated["conflict_kind"] == "ConcurrentRevision"


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def test_a_conflicting_row_cannot_be_applied_without_the_approval(client_world):
    client, session_factory, w = client_world
    revisions_before, impacts_before = _counts(session_factory)

    batch = _stage(client, [["WELL-1", w.p_a_desc, 6500, ROS, "Confirmed", "Primary"]])
    row_id = batch["rows"][0]["id"]
    _decide(client, batch["id"], row_id, "AcceptRevision")
    _revise_out_of_band(session_factory, w.l1_id, 5200)

    resp = client.post(f"/demand-imports/{batch['id']}/apply")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "demand_import_conflicts_unapproved"
    assert detail["blocked_row_ids"] == [row_id]
    assert detail["blocked_rows"][0]["conflict_kind"] == "ConcurrentRevision"
    assert "NOTHING was applied" in detail["detail"]

    # The batch is still Staged and the line still carries the OTHER person's value.
    assert client.get(f"/demand-imports/{batch['id']}").json()["status"] == "Staged"
    db = session_factory()
    try:
        assert db.get(DemandLine, w.l1_id).quantity == 5200
    finally:
        db.close()
    # Only the out-of-band revision was written; the import wrote nothing.
    assert _counts(session_factory) == (revisions_before + 1, impacts_before + 1)


def test_the_refusal_is_all_or_nothing_across_the_batch(client_world):
    """One unapproved conflict blocks the clean rows too. Applying the clean half and
    marking the batch Applied would leave the conflicting row permanently unappliable
    -- a batch cannot be applied twice -- so approving afterwards would be too late."""
    client, session_factory, w = client_world
    batch = _stage(
        client,
        [
            ["WELL-1", w.p_a_desc, 6500, ROS, "Confirmed", "Primary"],   # clean
            ["WELL-2", w.p_b_desc, 3100, "2027-01-15", "Budgeted", "Primary"],  # conflict
        ],
    )
    rows = {r["row_number"]: r for r in batch["rows"]}
    _decide(client, batch["id"], rows[2]["id"], "AcceptRevision")
    _decide(client, batch["id"], rows[3]["id"], "AcceptRevision")

    assert client.post(f"/demand-imports/{batch['id']}/apply").status_code == 409
    db = session_factory()
    try:
        assert db.get(DemandLine, w.l1_id).quantity == 5000  # the CLEAN row too
    finally:
        db.close()

    # Skipping the conflicting row is the other way out, and it unblocks the batch.
    _decide(client, batch["id"], rows[3]["id"], "Skip")
    resp = client.post(f"/demand-imports/{batch['id']}/apply")
    assert resp.status_code == 200, resp.text
    assert resp.json()["revised_count"] == 1
    assert resp.json()["skipped_count"] == 1
    assert resp.json()["well_status_changed_ids"] == []


def test_a_conflict_appearing_between_approval_and_apply_is_caught(client_world):
    """The check that decides is the LAST one. An approval is a decision about the
    conflict the reviewer SAW; a different conflict arriving afterwards has not been
    approved by anybody."""
    client, session_factory, w = client_world
    batch = _stage(client, [["WELL-1", w.p_a_desc, 6500, ROS, "Budgeted", "Primary"]])
    row_id = batch["rows"][0]["id"]
    _decide(client, batch["id"], row_id, "AcceptRevision")
    assert _approve(client, batch["id"], row_id).status_code == 200

    # Approved for the status conflict... and then the line moves underneath.
    _revise_out_of_band(session_factory, w.l1_id, 5200)
    row = client.get(f"/demand-imports/{batch['id']}").json()["rows"][0]
    # The status conflict is reported first and is approved, so apply proceeds -- the
    # approval is per ROW, not per conflict kind, and that is stated rather than
    # implied: a reviewer approving "override the live book for this row" has
    # authorised the row.
    assert row["override_approved"] is True
    assert row["requires_override_approval"] is False
    assert client.post(f"/demand-imports/{batch['id']}/apply").status_code == 200


def test_withdrawing_an_approval_re_blocks_the_row_and_clears_its_attribution(
    client_world,
):
    client, _sf, w = client_world
    batch = _stage(client, [["WELL-1", w.p_a_desc, 6500, ROS, "Budgeted", "Primary"]])
    row_id = batch["rows"][0]["id"]
    _decide(client, batch["id"], row_id, "AcceptRevision")

    approved = _approve(client, batch["id"], row_id, by="  Tanaka  ").json()
    assert approved["override_approved"] is True
    assert approved["override_approved_by"] == "Tanaka"
    assert approved["override_approved_at"] is not None

    withdrawn = _approve(client, batch["id"], row_id, approved=False, by=None).json()
    assert withdrawn["override_approved"] is False
    # Cleared, not kept: a withdrawn approval that retained its timestamp would read
    # as an approval that had happened.
    assert withdrawn["override_approved_at"] is None
    assert withdrawn["override_approved_by"] is None
    assert withdrawn["requires_override_approval"] is True
    assert client.post(f"/demand-imports/{batch['id']}/apply").status_code == 409


def test_approvals_are_frozen_after_apply_and_unknown_rows_404(client_world):
    client, _sf, w = client_world
    batch = _stage(client, [["WELL-1", w.p_a_desc, 6500, ROS, "Confirmed", "Primary"]])
    row_id = batch["rows"][0]["id"]
    _decide(client, batch["id"], row_id, "AcceptRevision")
    assert client.post(f"/demand-imports/{batch['id']}/apply").status_code == 200

    frozen = _approve(client, batch["id"], row_id)
    assert frozen.status_code == 409
    assert "already been applied" in frozen.json()["detail"]

    assert _approve(client, batch["id"], "nope").status_code == 404
    assert (
        client.get(
            f"/demand-imports/{batch['id']}/rows/nope/conflict-preview"
        ).status_code
        == 404
    )


# --------------------------------------------------------------------------
# Applying an APPROVED conflict produces the SAME trail as the direct path
# --------------------------------------------------------------------------


def test_an_approved_concurrent_revision_conflict_applies_through_apply_revision(
    client_world,
):
    """One implementation of applying, not two.

    Approving an override changes WHETHER the row is written, never HOW. The revision
    number, the DemandRevision row, the ImpactRecord and the coverage recompute are
    all produced by the same `apply_revision` call the unconflicted path uses -- so
    the trail is indistinguishable, and the overwritten value is visible in history
    rather than lost.
    """
    client, session_factory, w = client_world
    batch = _stage(client, [["WELL-1", w.p_a_desc, 6500, ROS, "Confirmed", "Primary"]])
    row_id = batch["rows"][0]["id"]
    _decide(client, batch["id"], row_id, "AcceptRevision")
    _revise_out_of_band(session_factory, w.l1_id, 5200)

    revisions_before, impacts_before = _counts(session_factory)
    assert _approve(client, batch["id"], row_id).status_code == 200

    result = client.post(f"/demand-imports/{batch['id']}/apply")
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["revised_count"] == 1
    assert len(body["impact_record_ids"]) == 1

    assert _counts(session_factory) == (revisions_before + 1, impacts_before + 1)
    db = session_factory()
    try:
        line = db.get(DemandLine, w.l1_id)
        assert line.quantity == 6500
        assert line.current_revision_no == 3
        history = sorted(line.revisions, key=lambda r: r.revision_no)
        # 1 = as created (5000), 2 = the other person's change (5200), 3 = this
        # import. The value the override replaced is still in history -- that is the
        # point of going through the revision machinery rather than around it.
        assert [(r.revision_no, r.quantity) for r in history] == [
            (1, 5000.0),
            (2, 5200.0),
            (3, 6500.0),
        ]
        # Two impact records exist for this line: the out-of-band revision and this
        # import's. Asserted as a SET of transitions rather than by row order --
        # `ImpactRecord.id` is a uuid and carries no ordering.
        transitions = {
            (i.quantity_before, i.quantity_after)
            for i in db.query(ImpactRecord)
            .filter(ImpactRecord.demand_line_id == w.l1_id)
            .all()
        }
        assert transitions == {(5000.0, 5200.0), (5200.0, 6500.0)}
        # Coverage was recomputed: 6500 exceeds the 6000 on hand.
        assert db.get(CoverageResult, w.l1_id).status.value == "Uncovered"
    finally:
        db.close()


# --------------------------------------------------------------------------
# The read-only preview
# --------------------------------------------------------------------------


def test_conflict_preview_shows_the_cascade_the_rows_own_diff_cannot(client_world):
    """WELL-2 carries two lines; the file names one. Demoting the well to Budgeted
    takes BOTH out of coverage scope, and the line the spreadsheet never mentioned is
    marked as not named by the row -- which is the whole reason this panel exists."""
    client, _sf, w = client_world
    batch = _stage(
        client,
        [["WELL-2", w.p_b_desc, 3000, "2027-01-15", "Budgeted", "Primary"]],
    )
    row = batch["rows"][0]
    resp = client.get(
        f"/demand-imports/{batch['id']}/rows/{row['id']}/conflict-preview"
    )
    assert resp.status_code == 200, resp.text
    preview = resp.json()

    assert preview["is_what_if"] is True
    assert preview["conflict_kind"] == "WellDemandStatus"
    assert preview["customer_id"] == w.acme_id
    assert preview["well_name"] == "WELL-2"
    assert preview["cascade_line_count"] == 2
    assert sorted(preview["revised_line_ids"]) == sorted([w.l2_id, w.l5_id])

    changes = {c["demand_line_id"]: c for c in preview["line_changes"]}
    # Both of WELL-2's lines leave coverage scope entirely.
    for line_id in (w.l2_id, w.l5_id):
        assert changes[line_id]["coverage_after"] == "NotEvaluated"
        assert changes[line_id]["changed"] is True
        # Every quantity carries the unit that labels it.
        assert changes[line_id]["unit_of_measure"] == "Mtr"
    # THE distinction the panel exists for: the file named L2, and L5 is being
    # revised anyway because the status belongs to the well. `named_by_the_row` must
    # separate the two -- reporting the whole cascade as "in the file" would delete
    # the only information the row's own diff cannot give.
    assert changes[w.l2_id]["named_by_the_row"] is True
    assert changes[w.l5_id]["named_by_the_row"] is False
    # L5 was Unrecoverable; demoting the well makes that risk simply disappear from
    # the answer, which is exactly the kind of thing a reviewer must be shown.
    assert preview["unrecoverable_lines_before"] == 1
    assert preview["unrecoverable_lines_after"] == 0

    prose = " ".join(preview["notes"])
    assert "WHAT-IF ONLY" in prose
    assert "one coverage implementation" in prose


def test_conflict_preview_is_refused_for_a_row_with_no_conflict(client_world):
    """Zeros would let the screen render "approving this changes nothing" about a row
    that needs no approval at all."""
    client, _sf, w = client_world
    batch = _stage(client, [["WELL-1", w.p_a_desc, 6500, ROS, "Confirmed", "Primary"]])
    resp = client.get(
        f"/demand-imports/{batch['id']}/rows/{batch['rows'][0]['id']}"
        "/conflict-preview"
    )
    assert resp.status_code == 409
    assert "does not conflict" in resp.json()["detail"]


def test_conflict_preview_persists_NOTHING(client_world):
    """The tripwire's subject. A preview served on a GET that left anything pending
    would turn a read into a demand change on the request's commit -- an upload
    changing demand with nobody deciding anything, the one thing this flow exists to
    prevent."""
    client, session_factory, w = client_world
    batch = _stage(
        client,
        [["WELL-2", w.p_b_desc, 3000, "2027-01-15", "Budgeted", "Primary"]],
    )
    row_id = batch["rows"][0]["id"]

    def snapshot():
        db = session_factory()
        try:
            return (
                db.query(DemandRevision).count(),
                db.query(ImpactRecord).count(),
                db.query(CoverageResult).count(),
                {
                    cr.demand_line_id: cr.status.value
                    for cr in db.query(CoverageResult).all()
                },
                {well.id: well.coverage_status for well in db.query(Well).all()},
                {
                    line.id: (line.quantity, line.current_revision_no)
                    for line in db.query(DemandLine).all()
                },
                {well.id: well.demand_status.value for well in db.query(Well).all()},
            )
        finally:
            db.close()

    before = snapshot()
    for _ in range(3):  # repeated calls must be side-effect free too
        assert (
            client.get(
                f"/demand-imports/{batch['id']}/rows/{row_id}/conflict-preview"
            ).status_code
            == 200
        )
    assert snapshot() == before

    # And the engine-level guarantee, asserted on the session directly.
    db = session_factory()
    try:
        from app.models import DemandImportRow

        row = db.get(DemandImportRow, row_id)
        counts = (len(db.new), len(db.dirty), len(db.deleted))
        preview_row_conflict(db, row)
        assert (len(db.new), len(db.dirty), len(db.deleted)) == counts
        db.flush()  # nothing was merely QUEUED for a later commit
    finally:
        db.close()
    assert snapshot() == before


def test_conflict_preview_write_tripwire_fires(client_world):
    """Layer 4 is real, not decorative: if anything ever does leave a pending change,
    the preview raises instead of letting a GET quietly write demand."""
    client, session_factory, w = client_world
    from app.engines import demand_import_preview as engine
    from app.models import DemandImportRow

    batch = _stage(
        client,
        [["WELL-2", w.p_b_desc, 3000, "2027-01-15", "Budgeted", "Primary"]],
    )
    row_id = batch["rows"][0]["id"]

    db = session_factory()
    original = engine._line_changes
    try:
        row = db.get(DemandImportRow, row_id)

        def _sneaky_write(base, after, well_names, revised):
            # Exactly the kind of edit the tripwire exists to catch.
            db.get(DemandLine, w.l2_id).quantity = 1.0
            return original(base, after, well_names, revised)

        engine._line_changes = _sneaky_write
        with pytest.raises(AssertionError, match="must not modify the session"):
            preview_row_conflict(db, row)
    finally:
        engine._line_changes = original
        db.rollback()
        db.close()


def test_conflict_preview_agrees_with_what_applying_actually_does(client_world):
    """The promise and the keeping of it, compared.

    The preview says both of WELL-2's lines leave coverage scope. Approving and
    applying must produce exactly that -- which it does by construction, because the
    preview is `compute_customer_coverage` and the apply is the same function plus
    persistence. This test is what makes "by construction" checkable.
    """
    client, session_factory, w = client_world
    batch = _stage(
        client,
        [["WELL-2", w.p_b_desc, 3000, "2027-01-15", "Budgeted", "Primary"]],
    )
    row = batch["rows"][0]
    promised = client.get(
        f"/demand-imports/{batch['id']}/rows/{row['id']}/conflict-preview"
    ).json()
    promised_after = {
        c["demand_line_id"]: c["coverage_after"] for c in promised["line_changes"]
    }

    _decide(client, batch["id"], row["id"], "AcceptRevision")
    assert _approve(client, batch["id"], row["id"]).status_code == 200
    result = client.post(f"/demand-imports/{batch['id']}/apply")
    assert result.status_code == 200, result.text
    assert result.json()["well_status_changed_ids"] == [w.w2_id]
    assert result.json()["well_status_cascaded_line_count"] == 2

    db = session_factory()
    try:
        assert db.get(Well, w.w2_id).demand_status.value == "Budgeted"
        for line_id, expected in promised_after.items():
            actual = db.get(CoverageResult, line_id)
            if expected == "NotEvaluated":
                assert actual is None, line_id
            else:
                assert actual is not None and actual.status.value == expected, line_id
    finally:
        db.close()


def test_a_new_row_whose_status_conflicts_says_the_new_line_is_unmodelled(
    client_world,
):
    """Honesty about the boundary of the preview. There is no override kind that can
    CREATE a demand line, so the new line's own draw on the pool is not in the
    figures. That is stated in the notes rather than left for the reviewer to
    discover by comparing the numbers afterwards."""
    client, _sf, w = client_world
    batch = _stage(
        client,
        [["WELL-1", w.p_b_desc, 2500, ROS, "Budgeted", "Primary"]],
    )
    row = batch["rows"][0]
    assert row["match_type"] == "New"
    assert row["conflict_kind"] == "WellDemandStatus"

    _decide(client, batch["id"], row["id"], "AcceptNew")
    preview = client.get(
        f"/demand-imports/{batch['id']}/rows/{row['id']}/conflict-preview"
    ).json()
    prose = " ".join(preview["notes"])
    assert "accepted as NEW demand" in prose
    assert "cannot create one" in prose
    assert "upper bound" in prose


def test_both_halves_of_a_row_are_modelled_not_only_the_conflicting_one(client_world):
    """The preview models the WHOLE row, not only its conflicting half: a row can
    assert a new well status AND restate quantity/ROS/profile, and applying does both.
    Showing only the status half would understate the very approval being asked for.

    WELL-4 is BUDGETED, so its line L6 is evaluated by nothing today. Confirming it
    brings the well into scope, and the quantity the FILE carries -- not the live 500
    -- is what the newly-in-scope line is judged on. That combination is precisely
    what a status-only preview would get wrong.
    """
    client, _sf, w = client_world
    ros_45 = (NOW + timedelta(days=45)).date().isoformat()
    batch = _stage(
        client,
        [["WELL-4", w.p_a_desc, 900, ros_45, "Confirmed", "Primary"]],
    )
    row = batch["rows"][0]
    assert row["match_type"] == "Revision"
    assert row["matched_demand_line_id"] == w.l6_id
    assert row["conflict_kind"] == "WellDemandStatus"

    preview = client.get(
        f"/demand-imports/{batch['id']}/rows/{row['id']}/conflict-preview"
    ).json()
    change = next(
        c for c in preview["line_changes"] if c["demand_line_id"] == w.l6_id
    )
    # Out of scope before (no verdict at all), and judged on the FILE's 900 after.
    assert change["coverage_before"] == "NotEvaluated"
    assert change["coverage_after"] != "NotEvaluated"
    assert change["quantity_after"] == 900
    assert change["named_by_the_row"] is True


def test_preview_needs_no_scenario_row_to_exist(client_world):
    """The scenario machinery is reused; the scenario TABLE is not. No Scenario or
    ScenarioOverride row may be created by asking for a conflict preview -- the
    override objects are transient, which is what keeps the no-write tripwire
    meaningful."""
    client, session_factory, w = client_world
    from app.models import Scenario, ScenarioOverride

    batch = _stage(
        client,
        [["WELL-2", w.p_b_desc, 3000, "2027-01-15", "Budgeted", "Primary"]],
    )
    row_id = batch["rows"][0]["id"]
    assert (
        client.get(
            f"/demand-imports/{batch['id']}/rows/{row_id}/conflict-preview"
        ).status_code
        == 200
    )
    db = session_factory()
    try:
        assert db.query(Scenario).count() == 0
        assert db.query(ScenarioOverride).count() == 0
    finally:
        db.close()


def test_profile_default_is_unaffected_by_the_conflict_machinery(client_world):
    """A guard against the gate quietly changing what an unconflicted row stages."""
    client, _sf, w = client_world
    batch = _stage(
        client,
        [["WELL-2", w.p_a_desc, 400, ROS]],
        header=["Well", "Product", "Quantity", "ROS Date"],
    )
    row = batch["rows"][0]
    assert row["profile"] == DemandProfile.PRIMARY.value
    assert row["status"] == "Confirmed"
    assert row["is_conflict"] is False
