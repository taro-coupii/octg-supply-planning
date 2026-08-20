"""Minimal UAT seed: stage 1 masters plus stage 2/3 rows.

Run from octg-platform/backend:  .venv/bin/python -m seed.seed_minimal
Idempotent: wipes and re-inserts only these master/stage-2/stage-3 tables.
"""

import json
from datetime import date, datetime, timedelta, timezone

from app.db import SessionLocal
from app.engines.coverage import recompute_all
from app.models import (
    BookingStatus,
    BusinessUnit,
    Customer,
    CustomerOwnedInventory,
    CustomerOwnedUpload,
    CustomerSubstitutionRule,
    CoverageVerdict,
    DemandImport,
    DemandImportRow,
    DemandImportStatus,
    DemandLine,
    DemandProfile,
    DemandRevision,
    DemandRevisionSource,
    DemandStatus,
    InventoryAssignment,
    InventoryOnHand,
    InventoryOnOrder,
    LeadTime,
    Product,
    Scenario,
    ScenarioOverride,
    ScenarioOverrideKind,
    ScenarioStatus,
    SafetyStock,
    Setting,
    SubstitutionApproval,
    SubstitutionApprovalStatus,
    TechnicalSubstitution,
    UnitOfMeasure,
    User,
    Well,
)
from app.services.demand_apply import apply_import_rows
from seed.seed_users import seed_users


def _prev_month_1st(today: date) -> date:
    """First day of the month before `today`'s month (always overdue: month < today's month)."""
    first_this_month = today.replace(day=1)
    return (first_this_month - timedelta(days=1)).replace(day=1)


def _add_months(d: date, months: int) -> date:
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)


def _seed_stage5_demo(db, norway: BusinessUnit, equinor: Customer) -> None:
    """Stage-5 Task 6 demo cases for the MOR/MRP/Surplus screens (all dates
    computed relative to date.today(), never hardcoded):

    - p_mor_future: deficit starts months out -> a requirement whose
      ex_mill_month is still in the future (no on_hand/PO, one demand line
      several months out, a short product-specific lead time).
    - p_mor_overdue: need month is soon but lead_time is long enough that
      ex_mill_month falls before this month -> overdue=True requirement.
    - p_safety_breach: on_hand + safety_stock sized so the safety_breach
      marker fires in an earlier month than physical_runout (two demand
      lines: a small one that dips below safety_stock while balance is
      still >=0, a larger later one that finally takes balance negative).
    - p_obsolete: company on_hand with zero scoped demand anywhere in the
      36-month Surplus horizon -> Surplus engine reports it fully obsolete.
    """
    today = date.today()

    p_mor_future = Product(name="MOR Demo-FutureExMill", unit_of_measure=UnitOfMeasure.MTR, weight_kg=None)
    p_mor_overdue = Product(name="MOR Demo-OverdueExMill", unit_of_measure=UnitOfMeasure.MTR, weight_kg=None)
    p_safety_breach = Product(name="MOR Demo-SafetyBreach", unit_of_measure=UnitOfMeasure.MTR, weight_kg=None)
    p_obsolete = Product(name="Surplus Demo-Obsolete", unit_of_measure=UnitOfMeasure.MTR, weight_kg=None)
    db.add_all([p_mor_future, p_mor_overdue, p_safety_breach, p_obsolete])
    db.flush()

    # p_mor_future: no on_hand/PO. Product-specific lead time of 3 months.
    # Demand of 100 lands 8 months out, so the deficit's ex_mill_month
    # (need_month - 3) is still 5 months out from today -> future, not overdue.
    db.add(LeadTime(business_unit_id=None, product_id=p_mor_future.id, months=3))
    future_need = _add_months(today, 8)
    well_mor_future = Well(customer_id=equinor.id, name="Equinor-Demo-MorFuture", demand_status=DemandStatus.CONFIRMED)
    db.add(well_mor_future)
    db.flush()
    db.add(
        DemandLine(
            well_id=well_mor_future.id,
            product_id=p_mor_future.id,
            quantity=100.0,
            unit=UnitOfMeasure.MTR,
            ros_date=future_need,
            profile=DemandProfile.PRIMARY,
        )
    )

    # p_mor_overdue: no on_hand/PO. Product-specific lead time of 8 months,
    # but the need lands only 1 month out, so ex_mill_month (need - 8) is
    # already in the past relative to today -> overdue=True.
    db.add(LeadTime(business_unit_id=None, product_id=p_mor_overdue.id, months=8))
    overdue_need = _add_months(today, 1)
    well_mor_overdue = Well(
        customer_id=equinor.id, name="Equinor-Demo-MorOverdue", demand_status=DemandStatus.CONFIRMED
    )
    db.add(well_mor_overdue)
    db.flush()
    db.add(
        DemandLine(
            well_id=well_mor_overdue.id,
            product_id=p_mor_overdue.id,
            quantity=40.0,
            unit=UnitOfMeasure.MTR,
            ros_date=overdue_need,
            profile=DemandProfile.PRIMARY,
        )
    )

    # p_safety_breach: on_hand=100, safety_stock=80. A small demand of 30 two
    # months out drops the running balance to 70 (< safety_stock=80, but
    # still >= 0) -> safety_breach fires there. A later, larger demand of
    # 100 six months out then takes the balance negative -> physical_runout
    # fires several months after safety_breach.
    db.add(
        InventoryOnHand(
            business_unit_id=norway.id,
            product_id=p_safety_breach.id,
            quantity=100.0,
            unit=UnitOfMeasure.MTR,
            source_system="manual",
        )
    )
    db.add(
        SafetyStock(business_unit_id=norway.id, product_id=p_safety_breach.id, quantity=80.0, unit=UnitOfMeasure.MTR)
    )
    breach_month = _add_months(today, 2)
    runout_month = _add_months(today, 6)
    well_safety = Well(customer_id=equinor.id, name="Equinor-Demo-SafetyBreach", demand_status=DemandStatus.CONFIRMED)
    db.add(well_safety)
    db.flush()
    db.add_all(
        [
            DemandLine(
                well_id=well_safety.id,
                product_id=p_safety_breach.id,
                quantity=30.0,
                unit=UnitOfMeasure.MTR,
                ros_date=breach_month,
                profile=DemandProfile.PRIMARY,
            ),
            DemandLine(
                well_id=well_safety.id,
                product_id=p_safety_breach.id,
                quantity=100.0,
                unit=UnitOfMeasure.MTR,
                ros_date=runout_month,
                profile=DemandProfile.PRIMARY,
            ),
        ]
    )

    # p_obsolete: company on_hand, no demand lines at all (so zero scoped
    # demand across the fixed 36-month Surplus horizon) -> fully obsolete.
    db.add(
        InventoryOnHand(
            business_unit_id=norway.id,
            product_id=p_obsolete.id,
            quantity=60.0,
            unit=UnitOfMeasure.MTR,
            source_system="manual",
        )
    )


def run() -> None:
    db = SessionLocal()
    try:
        # Delete stage-3 demand rows first (leaves before wells before
        # stage-2/stage-1 roots), then stage-2 rows, so this script stays
        # idempotent across reruns.
        from app.models import CoverageResult

        db.query(CoverageResult).delete()
        db.query(ScenarioOverride).delete()
        db.query(Scenario).delete()
        db.query(SubstitutionApproval).delete()
        db.query(DemandImportRow).delete()
        db.query(DemandImport).delete()
        db.query(DemandRevision).delete()
        db.query(DemandLine).delete()
        db.query(Well).delete()

        db.query(CustomerSubstitutionRule).delete()
        db.query(TechnicalSubstitution).delete()
        db.query(SafetyStock).delete()
        db.query(LeadTime).delete()
        db.query(CustomerOwnedInventory).delete()
        db.query(CustomerOwnedUpload).delete()
        db.query(InventoryAssignment).delete()
        db.query(InventoryOnOrder).delete()
        db.query(InventoryOnHand).delete()
        db.query(Setting).delete()

        db.query(Customer).delete()
        # users are wipe-exempt (spec §seed) but hold an FK to business_units;
        # nullify before the BU wipe below so the delete doesn't violate the
        # FK constraint, then seed_users() re-associates by BU name once the
        # (regenerated-id) business units exist again.
        db.query(User).update({User.business_unit_id: None})
        db.query(BusinessUnit).delete()
        db.query(Product).delete()

        root = BusinessUnit(name="SC Global")
        norway = BusinessUnit(name="SCEU Norway", parent=root)
        db.add_all([root, norway])
        db.flush()

        seed_users(db)

        equinor = Customer(name="Equinor Norway", business_unit_id=norway.id)
        akerbp = Customer(name="AkerBP Norway", business_unit_id=norway.id)
        db.add_all([equinor, akerbp])

        casing = Product(name='Casing 9-5/8" 53.5# L80', unit_of_measure=UnitOfMeasure.MTR, weight_kg=79.6)
        tubing = Product(name='Tubing 5-1/2" 17# 13Cr', unit_of_measure=UnitOfMeasure.PC, weight_kg=None)
        casing2 = Product(name='Casing 13-3/8" 68# K55', unit_of_measure=UnitOfMeasure.MT, weight_kg=101.2)
        db.add_all([casing, tubing, casing2])
        db.flush()

        # On-hand: 2 products, one sourced from the Oracle feed (COMPROMISE[C-03R]
        # target: source_system="oracle" rows are read-only via the API).
        db.add_all(
            [
                InventoryOnHand(
                    business_unit_id=norway.id,
                    product_id=casing.id,
                    quantity=420.0,
                    unit=UnitOfMeasure.MTR,
                    source_system="oracle",
                ),
                InventoryOnHand(
                    business_unit_id=norway.id,
                    product_id=tubing.id,
                    quantity=150.0,
                    unit=UnitOfMeasure.PC,
                    source_system="manual",
                ),
            ]
        )

        # On-order: one dated, one date-TBD (spec §不変条件4).
        db.add_all(
            [
                InventoryOnOrder(
                    business_unit_id=norway.id,
                    product_id=casing.id,
                    quantity=200.0,
                    unit=UnitOfMeasure.MTR,
                    expected_date=date.today() + timedelta(days=90),
                    booking_status=BookingStatus.POED,
                ),
                InventoryOnOrder(
                    business_unit_id=norway.id,
                    product_id=casing2.id,
                    quantity=80.0,
                    unit=UnitOfMeasure.MT,
                    expected_date=None,
                    booking_status=BookingStatus.BOOKED,
                ),
            ]
        )

        # Hard allocation.
        db.add(
            InventoryAssignment(
                business_unit_id=norway.id,
                product_id=casing.id,
                customer_id=equinor.id,
                quantity=100.0,
                unit=UnitOfMeasure.MTR,
                reference="PO-2026-0417",
            )
        )

        # Customer-owned upload + positions for Equinor Norway (the only
        # platform-writable inventory — spec §データモデル).
        db.add(
            CustomerOwnedUpload(
                customer_id=equinor.id,
                filename="equinor_norway_owned_stock.xlsx",
                row_count=1,
            )
        )
        db.add(
            CustomerOwnedInventory(
                customer_id=equinor.id,
                product_id=tubing.id,
                quantity=25.0,
                unit=UnitOfMeasure.PC,
            )
        )

        # Lead times: BU-specific and product-specific.
        db.add_all(
            [
                LeadTime(business_unit_id=norway.id, product_id=None, months=4),
                LeadTime(business_unit_id=None, product_id=casing.id, months=6),
            ]
        )

        # Safety stocks: one explicit zero, one set quantity (row absent = unset).
        db.add_all(
            [
                SafetyStock(
                    business_unit_id=norway.id, product_id=tubing.id, quantity=0.0, unit=UnitOfMeasure.PC
                ),
                SafetyStock(
                    business_unit_id=norway.id, product_id=casing.id, quantity=50.0, unit=UnitOfMeasure.MTR
                ),
            ]
        )

        # Technical substitution + customer rule.
        sub = TechnicalSubstitution(from_product_id=casing.id, to_product_id=casing2.id)
        db.add(sub)
        db.flush()
        db.add(
            CustomerSubstitutionRule(
                customer_id=equinor.id,
                technical_substitution_id=sub.id,
                allowed=True,
            )
        )

        # Coverage scope defaults (裁定SC-1).
        db.add_all(
            [
                Setting(key="coverage_scope_statuses", value="Confirmed"),
                Setting(key="coverage_scope_profiles", value="Primary,Contingency"),
            ]
        )

        # --- Stage 3: wells + demand lines + one applied import (裁定D-1/D-2) ---
        today = date.today()
        overdue_ros = _prev_month_1st(today)
        future_ros_1 = _add_months(today, 2)
        future_ros_2 = _add_months(today, 5)
        future_ros_3 = _add_months(today, 9)

        eq_well_1 = Well(customer_id=equinor.id, name="Equinor-A1", demand_status=DemandStatus.PLANNED)
        eq_well_2 = Well(customer_id=equinor.id, name="Equinor-A2", demand_status=DemandStatus.CONFIRMED)
        akbp_well_1 = Well(customer_id=akerbp.id, name="AkerBP-B1", demand_status=DemandStatus.BUDGETED)
        akbp_well_2 = Well(customer_id=akerbp.id, name="AkerBP-B2", demand_status=DemandStatus.CONFIRMED)
        db.add_all([eq_well_1, eq_well_2, akbp_well_1, akbp_well_2])
        db.flush()

        # Demand lines across products/profiles/ROS dates for the 3 wells that
        # are NOT going through the import flow below (eq_well_2 is created
        # fresh by the import apply).
        db.add_all(
            [
                # Equinor-A1: overdue casing line (Primary) + future tubing line (Contingency).
                DemandLine(
                    well_id=eq_well_1.id,
                    product_id=casing.id,
                    quantity=60.0,
                    unit=UnitOfMeasure.MTR,
                    ros_date=overdue_ros,
                    profile=DemandProfile.PRIMARY,
                ),
                DemandLine(
                    well_id=eq_well_1.id,
                    product_id=tubing.id,
                    quantity=30.0,
                    unit=UnitOfMeasure.PC,
                    ros_date=future_ros_1,
                    profile=DemandProfile.CONTINGENCY,
                ),
                # AkerBP-B1: two future lines, different products/profiles.
                DemandLine(
                    well_id=akbp_well_1.id,
                    product_id=casing2.id,
                    quantity=40.0,
                    unit=UnitOfMeasure.MT,
                    ros_date=future_ros_2,
                    profile=DemandProfile.PRIMARY,
                ),
                DemandLine(
                    well_id=akbp_well_1.id,
                    product_id=tubing.id,
                    quantity=15.0,
                    unit=UnitOfMeasure.PC,
                    ros_date=future_ros_1,
                    profile=DemandProfile.PRIMARY,
                ),
                # AkerBP-B2: overdue + far-future line.
                DemandLine(
                    well_id=akbp_well_2.id,
                    product_id=casing.id,
                    quantity=25.0,
                    unit=UnitOfMeasure.MTR,
                    ros_date=overdue_ros,
                    profile=DemandProfile.CONTINGENCY,
                ),
                DemandLine(
                    well_id=akbp_well_2.id,
                    product_id=casing2.id,
                    quantity=50.0,
                    unit=UnitOfMeasure.MT,
                    ros_date=future_ros_3,
                    profile=DemandProfile.PRIMARY,
                ),
            ]
        )

        # Manual-source revision for eq_well_1 so revisions aren't 100% import.
        db.add(
            DemandRevision(
                well_id=eq_well_1.id,
                revision_no=1,
                source=DemandRevisionSource.MANUAL,
                summary="Initial seed lines",
            )
        )
        # Status-change revision for akbp_well_2 (Budgeted -> Confirmed on seed).
        db.add(
            DemandRevision(
                well_id=akbp_well_2.id,
                revision_no=1,
                source=DemandRevisionSource.STATUS_CHANGE,
                summary="Planned -> Confirmed",
            )
        )
        db.flush()

        # One applied import for Equinor, creating eq_well_2 fresh via
        # apply's replace-all-lines-per-well logic (裁定D-1), producing a
        # source=import DemandRevision. Reuse the API's own apply logic by
        # replicating it directly against ORM rows (staging + apply), since
        # calling through the HTTP layer would require a running server.
        imp = DemandImport(
            customer_id=equinor.id,
            filename="equinor_demand_seed_import.xlsx",
            status=DemandImportStatus.PENDING,
        )
        db.add(imp)
        db.flush()

        import_rows = [
            {
                "well_name": eq_well_2.name,
                "product_id": casing.id,
                "quantity": 90.0,
                "unit": UnitOfMeasure.MTR,
                "ros_date": future_ros_1,
                "profile": DemandProfile.PRIMARY,
            },
            {
                "well_name": eq_well_2.name,
                "product_id": tubing.id,
                "quantity": 45.0,
                "unit": UnitOfMeasure.PC,
                "ros_date": future_ros_3,
                "profile": DemandProfile.PRIMARY,
            },
            {
                "well_name": eq_well_2.name,
                "product_id": casing2.id,
                "quantity": 20.0,
                "unit": UnitOfMeasure.MT,
                "ros_date": overdue_ros,
                "profile": DemandProfile.CONTINGENCY,
            },
        ]
        for row_no, r in enumerate(import_rows, start=1):
            db.add(
                DemandImportRow(
                    import_id=imp.id,
                    row_no=row_no,
                    well_name=r["well_name"],
                    product_id=r["product_id"],
                    quantity=r["quantity"],
                    unit=r["unit"],
                    ros_date=r["ros_date"],
                    profile=r["profile"],
                )
            )
        db.flush()

        # Apply via the shared apply-import logic (app.services.demand_apply),
        # the same function app.api.demand_imports.apply_import calls.
        apply_import_rows(db, imp)

        imp.status = DemandImportStatus.APPLIED

        # --- Stage 4 Task 8: five-verdict demo cases (all Confirmed wells,
        # Primary profile, in-scope) — one isolated product per case so pools
        # never interact across cases. ---
        p_covered = Product(name="Casing Demo-Covered", unit_of_measure=UnitOfMeasure.MTR, weight_kg=None)
        p_sub_approved = Product(name="Casing Demo-SubApproved", unit_of_measure=UnitOfMeasure.MTR, weight_kg=None)
        p_sub_approved_target = Product(
            name="Casing Demo-SubApproved-Target", unit_of_measure=UnitOfMeasure.MTR, weight_kg=None
        )
        p_sub_pending = Product(name="Casing Demo-SubPending", unit_of_measure=UnitOfMeasure.MTR, weight_kg=None)
        p_sub_pending_target = Product(
            name="Casing Demo-SubPending-Target", unit_of_measure=UnitOfMeasure.MTR, weight_kg=None
        )
        p_hard_other = Product(name="Casing Demo-HardOther", unit_of_measure=UnitOfMeasure.MTR, weight_kg=None)
        p_bare = Product(name="Casing Demo-Bare", unit_of_measure=UnitOfMeasure.MTR, weight_kg=None)
        db.add_all(
            [
                p_covered,
                p_sub_approved,
                p_sub_approved_target,
                p_sub_pending,
                p_sub_pending_target,
                p_hard_other,
                p_bare,
            ]
        )
        db.flush()

        # Ample on-hand for the Covered case; free stock for the two substitute
        # targets; nothing at all for hard-other/bare (their coverage must come
        # from elsewhere or not at all).
        db.add_all(
            [
                InventoryOnHand(
                    business_unit_id=norway.id,
                    product_id=p_covered.id,
                    quantity=200.0,
                    unit=UnitOfMeasure.MTR,
                    source_system="manual",
                ),
                InventoryOnHand(
                    business_unit_id=norway.id,
                    product_id=p_sub_approved_target.id,
                    quantity=80.0,
                    unit=UnitOfMeasure.MTR,
                    source_system="manual",
                ),
                InventoryOnHand(
                    business_unit_id=norway.id,
                    product_id=p_sub_pending_target.id,
                    quantity=80.0,
                    unit=UnitOfMeasure.MTR,
                    source_system="manual",
                ),
            ]
        )

        # Hard assignment to AkerBP (the "other customer" relative to
        # Equinor's demand below) large enough to cover the shortfall —
        # the Uncovered / Oracle-release demo case.
        db.add(
            InventoryAssignment(
                business_unit_id=norway.id,
                product_id=p_hard_other.id,
                customer_id=akerbp.id,
                quantity=60.0,
                unit=UnitOfMeasure.MTR,
                reference="PO-2026-DEMO-HARDOTHER",
            )
        )

        # Two technical substitutions with customer rules allowed for Equinor.
        sub_approved = TechnicalSubstitution(
            from_product_id=p_sub_approved.id, to_product_id=p_sub_approved_target.id
        )
        sub_pending = TechnicalSubstitution(
            from_product_id=p_sub_pending.id, to_product_id=p_sub_pending_target.id
        )
        db.add_all([sub_approved, sub_pending])
        db.flush()
        db.add_all(
            [
                CustomerSubstitutionRule(
                    customer_id=equinor.id, technical_substitution_id=sub_approved.id, allowed=True
                ),
                CustomerSubstitutionRule(
                    customer_id=equinor.id, technical_substitution_id=sub_pending.id, allowed=True
                ),
            ]
        )

        # Five Confirmed wells (Equinor holds four of the demo lines; AkerBP's
        # existing hard assignment above supplies the fifth's coverage).
        demo_well_covered = Well(customer_id=equinor.id, name="Equinor-Demo-Covered", demand_status=DemandStatus.CONFIRMED)
        demo_well_sub_approved = Well(
            customer_id=equinor.id, name="Equinor-Demo-SubApproved", demand_status=DemandStatus.CONFIRMED
        )
        demo_well_sub_pending = Well(
            customer_id=equinor.id, name="Equinor-Demo-SubPending", demand_status=DemandStatus.CONFIRMED
        )
        demo_well_hard_other = Well(
            customer_id=equinor.id, name="Equinor-Demo-HardOther", demand_status=DemandStatus.CONFIRMED
        )
        demo_well_bare = Well(customer_id=equinor.id, name="Equinor-Demo-Bare", demand_status=DemandStatus.CONFIRMED)
        db.add_all(
            [
                demo_well_covered,
                demo_well_sub_approved,
                demo_well_sub_pending,
                demo_well_hard_other,
                demo_well_bare,
            ]
        )
        db.flush()

        demand_line_covered = DemandLine(
            well_id=demo_well_covered.id,
            product_id=p_covered.id,
            quantity=50.0,
            unit=UnitOfMeasure.MTR,
            ros_date=future_ros_1,
            profile=DemandProfile.PRIMARY,
        )
        demand_line_sub_approved = DemandLine(
            well_id=demo_well_sub_approved.id,
            product_id=p_sub_approved.id,
            quantity=50.0,
            unit=UnitOfMeasure.MTR,
            ros_date=future_ros_1,
            profile=DemandProfile.PRIMARY,
        )
        demand_line_sub_pending = DemandLine(
            well_id=demo_well_sub_pending.id,
            product_id=p_sub_pending.id,
            quantity=50.0,
            unit=UnitOfMeasure.MTR,
            ros_date=future_ros_1,
            profile=DemandProfile.PRIMARY,
        )
        demand_line_hard_other = DemandLine(
            well_id=demo_well_hard_other.id,
            product_id=p_hard_other.id,
            quantity=50.0,
            unit=UnitOfMeasure.MTR,
            ros_date=future_ros_1,
            profile=DemandProfile.PRIMARY,
        )
        demand_line_bare = DemandLine(
            well_id=demo_well_bare.id,
            product_id=p_bare.id,
            quantity=50.0,
            unit=UnitOfMeasure.MTR,
            ros_date=future_ros_1,
            profile=DemandProfile.PRIMARY,
        )
        db.add_all(
            [
                demand_line_covered,
                demand_line_sub_approved,
                demand_line_sub_pending,
                demand_line_hard_other,
                demand_line_bare,
            ]
        )
        db.flush()

        now = datetime.now(timezone.utc)

        # Approved request — with p_sub_approved_target's free stock, the
        # coverage engine yields CoveredViaSubstitute for this line.
        db.add(
            SubstitutionApproval(
                customer_id=equinor.id,
                well_id=demo_well_sub_approved.id,
                demand_line_id=demand_line_sub_approved.id,
                technical_substitution_id=sub_approved.id,
                status=SubstitutionApprovalStatus.APPROVED,
                customer_approved=True,
                well_approved=True,
                requested_at=now,
                decided_at=now,
                note="Demo: approved substitution",
            )
        )
        # Pending request — with p_sub_pending_target's free stock but no
        # decision yet, the coverage engine yields PendingApproval.
        db.add(
            SubstitutionApproval(
                customer_id=equinor.id,
                well_id=demo_well_sub_pending.id,
                demand_line_id=demand_line_sub_pending.id,
                technical_substitution_id=sub_pending.id,
                status=SubstitutionApprovalStatus.PENDING,
                customer_approved=False,
                well_approved=False,
                requested_at=now,
                decided_at=None,
            )
        )

        db.commit()

        # Coverage engine writes CoverageResult, not this script — recompute
        # so dev.db ships with stored verdicts (spec §3-6: engine is the only
        # writer). Also serves as the five-verdict audit for the stage gate.
        summary = recompute_all(db)
        rollup: dict[str, int] = {}
        for cr in db.query(CoverageResult).all():
            rollup[cr.verdict.value] = rollup.get(cr.verdict.value, 0) + 1
        missing = [v.value for v in CoverageVerdict if v.value not in rollup]

        print(
            "seeded: 2 business units, 2 customers, 10 products, "
            "5 on-hand rows (1 oracle), 2 on-order (1 undated), 2 assignments, "
            "1 customer-owned upload+position, 2 lead times, 2 safety stocks, "
            "3 technical substitutions + 3 customer rules, coverage scope defaults, "
            "9 wells (mixed status, 6 Confirmed), 11 manual demand lines (overdue+future), "
            "1 applied import creating 1 well with 3 lines + revisions, "
            "2 substitution approvals (1 Approved, 1 Pending)"
        )
        print(f"coverage recompute: {summary['computed']} results, skipped={summary['skipped_customers']}")
        print(f"verdict audit: {rollup}")
        if missing:
            print(f"WARNING: verdicts missing from seed audit: {missing}")
        else:
            print("verdict audit OK: all five verdicts present")

        # --- Stage 5 Task 6: MOR/MRP/Surplus demo cases (all dates relative
        # to date.today(), no hardcoded months) ---
        _seed_stage5_demo(db, norway, equinor)
        db.commit()

        # --- Stage 6 Task 6: one Draft scenario demoing the What-if flow
        # ("What-if: Equinor uplift"). Targets the five-verdict demo lines
        # above (both Equinor, both isolated single-product pools) so the
        # preview delta is deterministic and doesn't disturb any other case:
        #   - quantity override: demand_line_covered 50 -> 250 MTR, exceeding
        #     p_covered's 200 MTR on_hand -> Covered flips to Uncovered.
        #   - ros_date override: demand_line_sub_approved pulled one month
        #     earlier (no coverage dependency on date; exercises the MRP
        #     preview path and the date-override UI/apply path together).
        scenario = Scenario(
            name="What-if: Equinor uplift",
            created_at=datetime.now(timezone.utc),
            status=ScenarioStatus.DRAFT,
        )
        db.add(scenario)
        db.flush()
        db.add_all(
            [
                ScenarioOverride(
                    scenario_id=scenario.id,
                    kind=ScenarioOverrideKind.QUANTITY,
                    target_id=demand_line_covered.id,
                    payload=json.dumps({"value": 250.0}),
                    created_at=datetime.now(timezone.utc),
                ),
                ScenarioOverride(
                    scenario_id=scenario.id,
                    kind=ScenarioOverrideKind.ROS_DATE,
                    target_id=demand_line_sub_approved.id,
                    payload=json.dumps({"value": _add_months(future_ros_1, -1).isoformat()}),
                    created_at=datetime.now(timezone.utc),
                ),
            ]
        )
        db.commit()

        # Coverage engine writes CoverageResult, not this script — rerun the
        # recompute so the new demo demand's coverage rows exist too, and
        # re-audit that all five verdicts still survive after the stage-5
        # additions (spec: existing five coverage verdicts must survive).
        summary2 = recompute_all(db)
        rollup2: dict[str, int] = {}
        for cr in db.query(CoverageResult).all():
            rollup2[cr.verdict.value] = rollup2.get(cr.verdict.value, 0) + 1
        missing2 = [v.value for v in CoverageVerdict if v.value not in rollup2]
        print(f"coverage recompute (post stage-5 demo): {summary2['computed']} results")
        print(f"verdict audit (post stage-5 demo): {rollup2}")
        if missing2:
            print(f"WARNING: verdicts missing after stage-5 demo seed: {missing2}")
        else:
            print("verdict audit OK (post stage-5 demo): all five verdicts present")
    finally:
        db.close()


if __name__ == "__main__":
    run()
