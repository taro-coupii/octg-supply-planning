"""MRP -> .xlsx export.

Every test here OPENS the produced workbook with openpyxl and reads real cell
values. Asserting the status code and the content type would pass on a zero-byte
body, and asserting "a file came back" is not the property the feature claims:
the claim is that the cells contain the SAME numbers `mrp_summary` and `by_item`
produce for the screens. So each test computes the engine answer itself and
compares it against the cells, rather than against a hardcoded expectation --
which is what makes these tests fail if the export ever starts recalculating
anything instead of reusing the engines.
"""

import io
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.db import get_db
from app.engines.coverage import recompute_well
from app.engines.mrp import by_item, mrp_summary
from app.engines.mrp_export import (
    _item_sheet_name,
    EXCEL_SHEET_NAME_ILLEGAL,
    EXCEL_SHEET_NAME_MAX,
    NO_DATA,
    NOT_MODELLED,
    SERIES_BASELINE,
    SERIES_WITH_ORDER,
    SHEET_INVENTORY,
    SHEET_LEAD_TIME,
    SHEET_NAMES,
    SHEET_NOTES,
    SHEET_RUNOUT,
    SHEET_SUMMARY,
    build_mrp_export,
)
from app.main import app
from app.models import Product, UnitOfMeasure

from tests.test_mrp_engine import (
    TOTAL_MONTHS,
    _customer,
    _default_bu,
    _lead_times,
    _line,
    _product,
    _stock,
    _well,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _http(db_session):
    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def client(db_session):
    yield _http(db_session)
    app.dependency_overrides.clear()


def _described_product(db_session, description, on_hand_qty=0.0, grade_type="13CR"):
    """A product with a caller-chosen DESCRIPTION, stocked in the file's BU.

    `test_mrp_engine._product` hardcodes its description, and the sheet-name
    collision test is specifically about descriptions, so it needs its own
    builder. The on-hand row is written even at 0 because `by_item` refuses a
    product with no row in any BU (unknown is not zero).

    `grade_type` defaults to the one `_lead_times` seeds a Grade component for, so
    these products get a COMPLETE lead-time model. That matters: an incomplete set
    resolves to "not modelled", and a test asserting the export prints a real
    number would then be asserting the NOT MODELLED path by accident.
    """
    product = Product(
        unit_of_measure=UnitOfMeasure.MTR,
        type="CSG",
        size="10-3/4",
        weight=60.7,
        grade=f"{grade_type}80",
        grade_type=grade_type,
        connection="VAM 21",
        description=description,
    )
    db_session.add(product)
    db_session.flush()
    _stock(db_session, _default_bu(db_session), product, on_hand_qty)
    return product


def _open(content: bytes):
    return load_workbook(io.BytesIO(content))


def _as_date(value):
    """openpyxl hands a date cell back as a midnight `datetime`.

    Normalised here rather than by writing dates as text in the export: they are
    written as real date cells on purpose, so Excel formats, sorts and filters
    them as dates for the customer reading the file.
    """
    return value.date() if isinstance(value, datetime) else value


def _rows(sheet):
    """Every row below the header, as a list of cell-value lists."""
    return [list(r) for r in sheet.iter_rows(min_row=2, values_only=True)]


def _header(sheet):
    return [c.value for c in sheet[1]]


def _data_rows(sheet, action_col=0):
    """Summary data rows only -- banner rows and note rows filtered out.

    A banner row carries a group title in the Action column ("ORDERABLE (2)"),
    and a data row carries exactly "Order" or the escalate label, so the two are
    told apart by the Product ID column being populated rather than by string
    matching on the banner text.
    """
    return [r for r in _rows(sheet) if r[2]]


def _two_group_world(db_session):
    """One orderable product and one unrecoverable one, so the export has both.

    The unrecoverable row exists because its ROS is inside the lead time, which is
    the LIVE verdict `mrp_summary` reaches -- the test does not assert it, it reads
    it back off the engine, so this fixture cannot silently stop producing the
    split it exists to produce.
    """
    _cust, node = _customer(db_session)
    _lead_times(db_session)
    late = _product(db_session, on_hand_qty=0)
    fine = _described_product(db_session, "CSG 9-5/8 53.5 L80 VAM 21 SMLS", 0)

    late_well = _well(db_session, node, "Well Late-01")
    _line(db_session, late_well, late, quantity=1000, days_out=30)
    fine_well = _well(db_session, node, "Well Fine-01")
    _line(db_session, fine_well, fine, quantity=2500, days_out=600)

    for well in (late_well, fine_well):
        recompute_well(db_session, well)
    db_session.flush()
    return late, fine


# ---------------------------------------------------------------------------
# Shape: a real, openable workbook with the documented sheets
# ---------------------------------------------------------------------------


def test_export_is_a_real_openable_xlsx_with_the_documented_sheets(db_session):
    _two_group_world(db_session)

    export = build_mrp_export(db_session)
    book = _open(export.content)

    # Tab 1 is the summary, and the supporting detail is on tabs 2 onward --
    # the product-owner's actual request, pinned.
    assert book.sheetnames[0] == SHEET_SUMMARY
    # Per-item demand sheets ("D01 ...") sit between the summary and the fixed
    # supporting sheets; the fixed set itself is unchanged and in order.
    item_tabs = [n for n in book.sheetnames if n[:1] == "D" and n[1:3].isdigit()]
    assert item_tabs, book.sheetnames
    fixed = [n for n in book.sheetnames if n not in item_tabs]
    assert fixed == list(SHEET_NAMES)
    assert len(book.sheetnames) >= 2
    assert export.filename.startswith("mrp-all-customers-")
    assert export.filename.endswith(".xlsx")
    # Not a default-width description column. 8.43 would truncate every product.
    assert book[SHEET_SUMMARY].column_dimensions["B"].width > 20
    assert book[SHEET_SUMMARY].freeze_panes == "A2"


def test_every_sheet_name_obeys_excel_limits(db_session):
    """The constants themselves, not a hopeful assumption about them."""
    _two_group_world(db_session)
    book = _open(build_mrp_export(db_session).content)
    for name in book.sheetnames:
        assert 0 < len(name) <= EXCEL_SHEET_NAME_MAX, name
        assert not (set(name) & EXCEL_SHEET_NAME_ILLEGAL), name
    assert len(set(book.sheetnames)) == len(book.sheetnames)


# ---------------------------------------------------------------------------
# Tab 1 matches mrp_summary EXACTLY
# ---------------------------------------------------------------------------


def test_tab1_rows_match_mrp_summary_exactly(db_session):
    _two_group_world(db_session)
    today = date.today()

    expected = mrp_summary(db_session, today=today)
    export = build_mrp_export(db_session, today=today)
    sheet = _open(export.content)[SHEET_SUMMARY]
    header = _header(sheet)
    rows = _data_rows(sheet)

    assert len(rows) == len(expected) == 2
    idx = {name: i for i, name in enumerate(header)}

    # Compared in the SHEET's order against the engine's rows regrouped the way
    # the sheet groups them (orderable first, then unrecoverable, each keeping
    # the engine's own order-date sort).
    ordered = [r for r in expected if not r.unrecoverable] + [
        r for r in expected if r.unrecoverable
    ]
    for row, rec in zip(rows, ordered):
        assert row[idx["Product ID"]] == rec.product_id
        assert row[idx["Product"]] == (rec.product_description or rec.product_id)
        assert row[idx["Net Shortfall"]] == pytest.approx(rec.quantity)
        # F04: the breakdown columns reconcile to the net figure, row by row.
        assert row[idx["Demand"]] == pytest.approx(rec.demand_quantity)
        assert row[idx["Drawn Company"]] == pytest.approx(rec.drawn_company)
        assert (
            row[idx["Demand"]]
            - row[idx["Drawn Customer-owned"]]
            - row[idx["Drawn Company"]]
            - row[idx["Drawn Substitute"]]
        ) == pytest.approx(rec.quantity)
        assert row[idx["Unit"]] == rec.unit_of_measure.value
        assert _as_date(row[idx["ROS Date"]]) == rec.ros_date.date()
        assert _as_date(row[idx["Required Ship Date"]]) == rec.required_ship_date
        assert (
            _as_date(row[idx["Recommended Order Date"]]) == rec.recommended_order_date
        )
        # The scalar when the model is complete; NOT MODELLED (never "0") when it
        # is not -- see `test_not_modelled_lead_time_is_never_written_as_zero_months`.
        if rec.lead_time is not None and rec.lead_time.modelled:
            assert row[idx["Lead Time (months)"]] == pytest.approx(
                rec.lead_time_months
            )
        else:
            assert row[idx["Lead Time (months)"]] == NOT_MODELLED
        assert row[idx["Demand Lines"]] == len(rec.demand_line_ids)
        assert row[idx["Reason"]] == rec.reason
        assert row[idx["Unrecoverable"]] == ("YES" if rec.unrecoverable else "no")
        assert row[idx["Action"]].startswith(
            "ESCALATE" if rec.unrecoverable else "Order"
        )


def test_recoverable_and_unrecoverable_are_visibly_separated(db_session):
    """Two different actions -- order vs escalate -- must not blur back together.

    Three independent carriers of the distinction are checked, because a reader
    WILL re-sort this sheet and two of the three survive that.
    """
    _two_group_world(db_session)
    expected = mrp_summary(db_session)
    sheet = _open(build_mrp_export(db_session).content)[SHEET_SUMMARY]
    all_rows = _rows(sheet)
    data = _data_rows(sheet)

    n_order = sum(1 for r in expected if not r.unrecoverable)
    n_escalate = len(expected) - n_order
    assert (n_order, n_escalate) == (1, 1)

    # 1. Banner rows naming each group and its count.
    banners = [r[0] for r in all_rows if r[0] and not r[2]]
    assert f"ORDERABLE ({n_order})" in banners
    assert f"UNRECOVERABLE - ESCALATE ({n_escalate})" in banners

    # 2. Grouped sort: every orderable row precedes every unrecoverable one.
    actions = [r[0] for r in data]
    assert actions == ["Order", actions[-1]]
    assert actions[-1].startswith("ESCALATE")

    # 3. A per-row Action column, so the split survives re-sorting.
    assert all(r[0] in {"Order"} or r[0].startswith("ESCALATE") for r in data)


def test_not_modelled_lead_time_is_never_written_as_zero_months(db_session):
    """0 months reads as instant delivery -- the least-known product would look
    like the safest row. No lead-time components are seeded here at all."""
    _cust, node = _customer(db_session)
    product = _product(db_session, on_hand_qty=0)
    well = _well(db_session, node, "Well NoLT-01")
    _line(db_session, well, product, quantity=500, days_out=200)
    recompute_well(db_session, well)

    rec = mrp_summary(db_session)[0]
    assert rec.lead_time_months == 0  # the engine's scalar really is 0

    book = _open(build_mrp_export(db_session).content)
    sheet = book[SHEET_SUMMARY]
    idx = {n: i for i, n in enumerate(_header(sheet))}
    row = _data_rows(sheet)[0]
    assert row[idx["Lead Time (months)"]] == NOT_MODELLED
    assert row[idx["Lead Time (months)"]] != 0
    assert "missing lead-time components" in row[idx["Lead Time Basis"]]

    # And the supporting sheet says which dimensions are missing, not "0".
    lt_rows = _rows(book[SHEET_LEAD_TIME])
    assert any(NOT_MODELLED in str(r[3]) for r in lt_rows)
    assert any("NOT MODELLED" in str(r[9]) for r in lt_rows)


# ---------------------------------------------------------------------------
# Supporting tabs match by_item EXACTLY
# ---------------------------------------------------------------------------


def test_supporting_sheets_match_by_item_exactly(db_session):
    _late, fine = _two_group_world(db_session)
    today = date.today()

    analysis = by_item(db_session, fine.id, today=today)
    book = _open(build_mrp_export(db_session, today=today).content)

    # -- Per-item demand sheet (By Item layout) ----------------------------
    sanitized = (analysis.product_description or "").replace("/", "-")[:8]
    item_sheet = next(
        book[n] for n in book.sheetnames
        if n[:1] == "D" and n[1:3].isdigit() and sanitized in n
    )
    all_rows = list(item_sheet.iter_rows(values_only=True))
    # The sheet is: title, banner, ledger header, ledger rows, banner,
    # line header, line rows. Anchor on the two headers, not on absolute
    # row numbers.
    ledger_header_idx = next(
        i for i, r in enumerate(all_rows) if r[0] == "Month"
    )
    line_banner_idx = next(
        i for i, r in enumerate(all_rows)
        if str(r[0]).startswith("DEMAND LINES")
    )
    line_header_idx = next(
        i for i, r in enumerate(all_rows) if r[0] == "ROS Date"
    )
    # The ledger block mirrors the By Item screen's monthly table.
    ledger = [
        r for r in all_rows[ledger_header_idx + 1:line_banner_idx] if r[0]
    ]
    assert len(ledger) == len(analysis.runout)
    line_rows = [r for r in all_rows[line_header_idx + 1:] if r[0]]
    assert len(line_rows) == len(analysis.demand_lines) == 1
    for row, line in zip(line_rows, analysis.demand_lines):
        assert row[1] == line.well_name
        assert row[3] == line.profile
        assert row[4] == pytest.approx(line.quantity)
        assert row[5] == line.unit_of_measure.value
        assert _as_date(row[0]) == line.ros_date.date()
        assert row[6] == line.coverage_status

    # -- Inventory Position ------------------------------------------------
    inv_sheet = book[SHEET_INVENTORY]
    idx = {n: i for i, n in enumerate(_header(inv_sheet))}
    inv_row = next(r for r in _rows(inv_sheet) if r[1] == fine.id)
    inv = analysis.inventory
    assert inv_row[idx["On Hand (all Business Units)"]] == pytest.approx(inv.on_hand)
    assert inv_row[idx["Assigned (carve-out of On Hand)"]] == pytest.approx(
        inv.assigned
    )
    assert inv_row[idx["Assigned Source"]] == inv.assigned_source
    assert inv_row[idx["On Order Source"]] == inv.on_order_source
    assert inv_row[idx["Unit"]] == analysis.unit_of_measure.value
    assert inv_row[idx["Oracle Feed Live"]] == ("yes" if inv.oracle_integrated else "no")
    assert inv_row[idx["Runout Month"]] == (
        analysis.runout_month or "does not run out in the projected window"
    )

    # -- Runout Projection -------------------------------------------------
    runout = book[SHEET_RUNOUT]
    baseline = [
        r for r in _rows(runout) if r[1] == fine.id and r[2] == SERIES_BASELINE
    ]
    assert len(baseline) == len(analysis.runout)
    for row, point in zip(baseline, analysis.runout):
        assert row[3] == point.month
        assert row[4] == pytest.approx(point.opening_balance)
        assert row[5] == pytest.approx(point.demand)
        assert row[6] == pytest.approx(point.closing_balance)
        assert row[7] == point.unit_of_measure.value
    # The two series are labelled and never merged.
    with_order = [
        r for r in _rows(runout) if r[1] == fine.id and r[2] == SERIES_WITH_ORDER
    ]
    assert len(with_order) == len(analysis.runout_with_recommended_order)
    for row, point in zip(with_order, analysis.runout_with_recommended_order):
        assert row[3] == point.month
        assert row[6] == pytest.approx(point.closing_balance)

    # -- Lead Time Breakdown -----------------------------------------------
    lt = book[SHEET_LEAD_TIME]
    lines = [r for r in _rows(lt) if r[1] == fine.id]
    assert analysis.lead_time.modelled
    assert analysis.lead_time.total_months == pytest.approx(TOTAL_MONTHS)
    assert len(lines) == len(analysis.lead_time.components)
    assert all(r[3] == pytest.approx(TOTAL_MONTHS) for r in lines)
    for row, component in zip(lines, analysis.lead_time.components):
        assert row[4] == component.dimension
        assert row[5] == component.attribute_value
        assert row[6] == component.matched_on
        assert row[7] == pytest.approx(component.months)
        assert row[8] == ("yes" if component.shared else "no")


def test_unknown_on_order_is_blank_or_explicit_never_zero(db_session):
    """The honesty rule, in a cell. `on_order` null means no purchase-order row
    exists anywhere -- which is NOT the same fact as "nothing is on order", and a
    0 in a customer-facing spreadsheet would assert the wrong one of the two."""
    _late, fine = _two_group_world(db_session)

    analysis = by_item(db_session, fine.id)
    # Precondition, asserted rather than assumed: the engine really does report
    # UNKNOWN here (no InventoryOnOrder / CustomerOwnedInventory rows seeded).
    assert analysis.inventory.on_order is None
    assert analysis.inventory.customer_owned is None

    sheet = _open(build_mrp_export(db_session).content)[SHEET_INVENTORY]
    idx = {n: i for i, n in enumerate(_header(sheet))}
    row = next(r for r in _rows(sheet) if r[1] == fine.id)

    for column in ("On Order", "Customer-Owned (all customers)"):
        value = row[idx[column]]
        assert value == NO_DATA, (column, value)
        assert value != 0
        assert value != 0.0
        assert not isinstance(value, (int, float))
    assert row[idx["On Order Source"]] == "unavailable"
    assert "UNKNOWN" in row[idx["Notes"]]
    # A measured 0 stays a number, so the two are still distinguishable.
    on_hand = row[idx["On Hand (all Business Units)"]]
    assert on_hand == 0
    assert isinstance(on_hand, (int, float)) and not isinstance(on_hand, bool)


def test_every_quantity_column_names_its_unit(db_session):
    """Not exempted just because the medium is a cell instead of a schema field."""
    _late, fine = _two_group_world(db_session)
    book = _open(build_mrp_export(db_session).content)

    for name in (SHEET_SUMMARY, SHEET_INVENTORY, SHEET_RUNOUT):
        header = _header(book[name])
        assert "Unit" in header, name
        unit_values = {r[header.index("Unit")] for r in _rows(book[name]) if r[2]}
        unit_values.discard(None)
        assert unit_values, name
        assert unit_values <= {u.value for u in UnitOfMeasure}, (name, unit_values)


# ---------------------------------------------------------------------------
# Sheet-name collisions -- constructed, not hoped away
# ---------------------------------------------------------------------------


def test_products_colliding_after_31_char_truncation_still_export(db_session):
    """The exact case one-sheet-per-product could not survive.

    Both descriptions are longer than Excel's 31-character sheet-name cap, share
    their first 31 characters, AND contain '/', which is illegal in a sheet name.
    Sanitise-and-truncate would map them to the SAME legal name. The
    per-item sheets survive it because every name begins with a unique ordinal
    prefix ("D01 ", "D02 ", ...), so uniqueness is by construction and the
    colliding descriptions are merely truncated context after it.
    """
    _cust, node = _customer(db_session)
    _lead_times(db_session)
    a = _described_product(db_session, "CSG 10-3/4 60.7 L80 VAM 21 SMLS AAAA", 0)
    b = _described_product(db_session, "CSG 10-3/4 60.7 L80 VAM 21 SMLS BBBB", 0)

    # The collision is real, and asserted rather than asserted-about-in-a-comment.
    assert a.description[:EXCEL_SHEET_NAME_MAX] == b.description[
        :EXCEL_SHEET_NAME_MAX
    ]
    assert len(a.description) > EXCEL_SHEET_NAME_MAX
    assert set(a.description) & EXCEL_SHEET_NAME_ILLEGAL

    for i, product in enumerate((a, b)):
        well = _well(db_session, node, f"Well Collide-{i}")
        _line(db_session, well, product, quantity=100 * (i + 1), days_out=600)
        recompute_well(db_session, well)
    db_session.flush()

    export = build_mrp_export(db_session)
    book = _open(export.content)

    # No duplicate, over-long or illegal sheet name -- the ordinal prefixes
    # make the two near-identical products land on distinct tabs.
    assert len(set(book.sheetnames)) == len(book.sheetnames)
    for name in book.sheetnames:
        assert len(name) <= EXCEL_SHEET_NAME_MAX
        assert not (set(name) & EXCEL_SHEET_NAME_ILLEGAL)
    assert len([n for n in book.sheetnames if n[:1] == "D" and n[1:3].isdigit()]) == 2

    # Both products present, distinguishable, with their FULL descriptions intact
    # (a column has no 31-character cap).
    summary_rows = _data_rows(book[SHEET_SUMMARY])
    by_id = {r[2]: r for r in summary_rows}
    assert set(by_id) == {a.id, b.id}
    assert by_id[a.id][1] == a.description
    assert by_id[b.id][1] == b.description
    assert by_id[a.id][3] == pytest.approx(100.0)
    assert by_id[b.id][3] == pytest.approx(200.0)

    # And on every supporting sheet too; the demand detail is now one sheet
    # per product, so both products must have their own tab instead.
    for name in (SHEET_INVENTORY, SHEET_RUNOUT, SHEET_LEAD_TIME):
        ids = {r[1] for r in _rows(book[name])}
        assert {a.id, b.id} <= ids, name
    item_tabs = [n for n in book.sheetnames if n[:1] == "D" and n[1:3].isdigit()]
    assert len(item_tabs) == 2


# ---------------------------------------------------------------------------
# Empty scope
# ---------------------------------------------------------------------------


def test_empty_summary_still_produces_an_openable_workbook_with_an_explanation(
    db_session,
):
    """Nothing to order is a real MRP answer, and the file must say so.

    A header row with no rows under it is indistinguishable from a broken export,
    so every sheet carries prose instead.
    """
    _customer(db_session)
    _lead_times(db_session)
    assert mrp_summary(db_session) == []

    export = build_mrp_export(db_session)
    assert export.recommendation_count == 0
    book = _open(export.content)
    # No recommendations -> no per-item sheets; the fixed set remains.
    assert book.sheetnames == list(SHEET_NAMES)

    summary_rows = _rows(book[SHEET_SUMMARY])
    assert len(summary_rows) == 1
    assert "NOTHING TO REPORT" in summary_rows[0][0]
    assert "no MRP recommendations" in summary_rows[0][0]

    for name in (SHEET_INVENTORY, SHEET_RUNOUT, SHEET_LEAD_TIME):
        rows = _rows(book[name])
        assert len(rows) == 1, name
        assert "NOTHING TO REPORT" in rows[0][0], name

    # The Notes sheet is always substantive.
    assert len(_rows(book[SHEET_NOTES])) > 5


def test_product_whose_by_item_refuses_keeps_its_summary_row_and_is_reported(
    db_session,
):
    """`by_item` RAISES for a product with no InventoryOnHand row in any BU
    (unknown is not zero). One such product must not 500 the whole workbook and
    hide every other recommendation -- nor vanish silently."""
    _cust, node = _customer(db_session)
    _lead_times(db_session)
    # Deliberately NO on-hand row anywhere: built directly, bypassing _stock.
    orphan = Product(
        unit_of_measure=UnitOfMeasure.MTR,
        type="CSG",
        size="7",
        weight=29.0,
        grade="13CR80",
        grade_type="13CR",
        connection="VAM 21",
        description="CSG 7 29.0 13CR80 VAM 21 SMLS",
    )
    db_session.add(orphan)
    db_session.flush()
    well = _well(db_session, node, "Well Orphan-01")
    _line(db_session, well, orphan, quantity=750, days_out=600)
    db_session.flush()  # no recompute_well: an unevaluated line is still unresolved

    with pytest.raises(Exception):
        by_item(db_session, orphan.id)

    export = build_mrp_export(db_session)
    assert export.recommendation_count == 1
    assert [label for label, _r in export.unavailable_products] == [orphan.description]

    book = _open(export.content)
    assert [r[2] for r in _data_rows(book[SHEET_SUMMARY])] == [orphan.id]
    notes = [str(r[0]) for r in _rows(book[SHEET_NOTES])]
    assert any("SUPPORTING DETAIL UNAVAILABLE" in n for n in notes)
    assert any(orphan.description in n for n in notes)


# ---------------------------------------------------------------------------
# The HTTP route
# ---------------------------------------------------------------------------


def test_route_returns_an_xlsx_attachment_whose_cells_match_the_engine(
    db_session, client
):
    _two_group_world(db_session)

    response = client.get("/mrp/export")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert "attachment" in response.headers["content-disposition"]
    assert ".xlsx" in response.headers["content-disposition"]
    assert response.headers["x-mrp-recommendation-count"] == "2"
    assert response.headers["x-mrp-orderable-count"] == "1"
    assert response.headers["x-mrp-unrecoverable-count"] == "1"

    book = _open(response.content)
    assert book.sheetnames[0] == SHEET_SUMMARY
    expected = {r.product_id: r.quantity for r in mrp_summary(db_session)}
    got = {r[2]: r[3] for r in _data_rows(book[SHEET_SUMMARY])}
    assert got.keys() == expected.keys()
    for pid, quantity in expected.items():
        assert got[pid] == pytest.approx(quantity)


def test_route_respects_the_customer_filter(db_session, client):
    """Two customers, one export each: a customer-scoped file must not contain
    the other customer's demand."""
    _cust_a, node_a = _customer(db_session, name="Export Co A")
    _cust_b, node_b = _customer(db_session, name="Export Co B")
    _lead_times(db_session)
    product_a = _product(db_session, on_hand_qty=0)
    product_b = _described_product(db_session, "CSG 7 32.0 L80 VAM 21 SMLS", 0)

    for node, product, qty, label in (
        (node_a, product_a, 300, "A"),
        (node_b, product_b, 400, "B"),
    ):
        well = _well(db_session, node, f"Well Scoped-{label}")
        _line(db_session, well, product, quantity=qty, days_out=600)
        recompute_well(db_session, well)
    db_session.flush()

    response = client.get(f"/mrp/export?customer_id={_cust_a.id}")
    assert response.status_code == 200, response.text
    assert "Export-Co-A" in response.headers["content-disposition"]
    rows = _data_rows(_open(response.content)[SHEET_SUMMARY])
    assert [r[2] for r in rows] == [product_a.id]
    assert rows[0][3] == pytest.approx(300.0)

    # And the unscoped export contains both, matching the screen's default.
    both = _data_rows(_open(client.get("/mrp/export").content)[SHEET_SUMMARY])
    assert {r[2] for r in both} == {product_a.id, product_b.id}


def test_route_404s_on_an_unknown_customer(db_session, client):
    response = client.get("/mrp/export?customer_id=nope")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]
