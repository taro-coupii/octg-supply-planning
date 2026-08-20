from sqlalchemy.orm import Session

from app.models import (
    DemandImport,
    DemandImportRow,
    DemandLine,
    DemandRevision,
    DemandRevisionSource,
    Well,
)


def apply_import_rows(db: Session, imp: DemandImport) -> tuple[list[str], list[str]]:
    """Apply an import's staged rows to wells: replace each well's demand
    lines wholesale, bump its revision number, and record a source=import
    DemandRevision. Does not commit or set imp.status — callers own that.

    Returns (applied_wells, created_wells).
    """
    staged_rows = db.query(DemandImportRow).filter_by(import_id=imp.id).all()
    by_well: dict[str, list[DemandImportRow]] = {}
    for row in staged_rows:
        by_well.setdefault(row.well_name, []).append(row)

    applied_wells: list[str] = []
    created_wells: list[str] = []

    for well_name, rows in by_well.items():
        well = db.query(Well).filter_by(customer_id=imp.customer_id, name=well_name).first()
        if well is None:
            well = Well(customer_id=imp.customer_id, name=well_name)
            db.add(well)
            db.flush()
            created_wells.append(well_name)

        db.query(DemandLine).filter_by(well_id=well.id).delete()
        for r in rows:
            db.add(
                DemandLine(
                    well_id=well.id,
                    product_id=r.product_id,
                    quantity=r.quantity,
                    unit=r.unit,
                    ros_date=r.ros_date,
                    profile=r.profile,
                )
            )

        max_rev = (
            db.query(DemandRevision)
            .filter_by(well_id=well.id)
            .order_by(DemandRevision.revision_no.desc())
            .first()
        )
        next_rev = (max_rev.revision_no + 1) if max_rev else 1
        db.add(
            DemandRevision(
                well_id=well.id,
                revision_no=next_rev,
                source=DemandRevisionSource.IMPORT,
                summary=f"Import applied ({len(rows)} lines)",
            )
        )
        applied_wells.append(well_name)

    return applied_wells, created_wells
