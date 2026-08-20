import datetime
import io

from openpyxl import load_workbook

from app.models import (
    AllocationPolicy,
    BusinessUnit,
    Customer,
    DemandLine,
    DemandProfile,
    DemandStatus,
    InventoryOnHand,
    Product,
    UnitOfMeasure,
    Well,
)
from app.services.dates import today


def _bu(db, name="BU1"):
    bu = BusinessUnit(name=name)
    db.add(bu)
    db.commit()
    return bu


def _customer(db, name="Cust1", bu=None, policy=AllocationPolicy.SOFT):
    c = Customer(name=name, business_unit_id=bu.id if bu else None, allocation_policy=policy)
    db.add(c)
    db.commit()
    return c


def _product(db, name="Casing"):
    p = Product(name=name, unit_of_measure=UnitOfMeasure.MTR)
    db.add(p)
    db.commit()
    return p


def _well(db, customer, name="Well1", status=DemandStatus.CONFIRMED):
    w = Well(customer_id=customer.id, name=name, demand_status=status)
    db.add(w)
    db.commit()
    return w


def _line(db, well, product, quantity, ros_date, unit=UnitOfMeasure.MTR, profile=DemandProfile.PRIMARY):
    line = DemandLine(
        well_id=well.id, product_id=product.id, quantity=quantity, unit=unit, ros_date=ros_date, profile=profile
    )
    db.add(line)
    db.commit()
    return line


def _on_hand(db, bu, product, quantity, unit=UnitOfMeasure.MTR):
    row = InventoryOnHand(business_unit_id=bu.id, product_id=product.id, quantity=quantity, unit=unit)
    db.add(row)
    db.commit()
    return row


def _find_header_row(ws, label):
    for row in ws.iter_rows():
        for cell in row:
            if cell.value == label:
                return cell.row
    return None


def test_export_has_summary_and_product_tabs(client, db):
    bu = _bu(db)
    product = _product(db, name="Casing A")
    _on_hand(db, bu, product, 10)  # deliberately small -> triggers a runout
    cust = _customer(db, bu=bu)
    well = _well(db, cust)
    now = today()
    _line(db, well, product, 20, datetime.date(now.year, now.month, min(now.day, 27)))

    resp = client.get("/mrp/export", params={"horizon": 6})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

    wb = load_workbook(io.BytesIO(resp.content))
    assert "Summary" in wb.sheetnames
    assert "Casing A" in wb.sheetnames

    summary = wb["Summary"]
    header_cells = [c.value for c in summary[1]]
    assert "Product" in header_cells
    assert "Unit" in header_cells

    ws = wb["Casing A"]
    ledger_header_row = _find_header_row(ws, "MONTHLY LEDGER")
    assert ledger_header_row is not None
    col_header_row = ledger_header_row + 1
    col_labels = [c.value for c in ws[col_header_row]]
    assert col_labels == [
        "Month",
        "Opening company",
        "Opening owned",
        "Receipts booked",
        "Receipts recommended",
        "Issues",
        "Closing company",
        "Closing owned",
    ]

    demand_header_row = _find_header_row(ws, "DEMAND LINES")
    assert demand_header_row is not None
    assert demand_header_row > ledger_header_row


def test_export_runout_month_cell_is_red_filled(client, db):
    bu = _bu(db)
    product = _product(db, name="Runout Item")
    _on_hand(db, bu, product, 5)
    cust = _customer(db, bu=bu)
    well = _well(db, cust)
    now = today()
    _line(db, well, product, 10, datetime.date(now.year, now.month, min(now.day, 27)))

    resp = client.get("/mrp/export", params={"horizon": 3})
    wb = load_workbook(io.BytesIO(resp.content))
    ws = wb["Runout Item"]

    ledger_header_row = _find_header_row(ws, "MONTHLY LEDGER")
    col_header_row = ledger_header_row + 1
    month_col = 1

    found_fill = False
    for r in range(col_header_row + 1, ws.max_row + 1):
        cell = ws.cell(row=r, column=month_col)
        if cell.value is None:
            break
        fill = cell.fill
        if fill is not None and fill.fgColor and fill.fgColor.rgb == "00FFC7CE":
            found_fill = True
            break
    assert found_fill
