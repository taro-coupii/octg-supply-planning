"""C-17 (owner ruling 2026-09-06): when an approved substitute rescues a line,
the line's partial own-product draw goes back to the pool and the substitute
covers the whole line. The breakdown then adds up to the demand, and the
released steel can serve a later substitute draw in the same pass."""

from app.engines.coverage import recompute_customer
from app.engines.substitution import decide_approval, request_approval
from app.models import CoverageResult, CoverageStatus, CustomerSubstitutionRule, TechnicalSubstitution
from tests.test_scenario_engine import _bu, _customer, _line, _product, _stock, _well


def _allow(db, customer, frm, to):
    db.add(TechnicalSubstitution(from_product_id=frm.id, to_product_id=to.id))
    db.add(CustomerSubstitutionRule(customer_id=customer.id, from_product_id=frm.id, to_product_id=to.id, allowed=True))


def test_substitute_covers_whole_line_and_returns_the_partial_draw(db_session):
    bu = _bu(db_session)
    p = _product(db_session, grade="13CR80")
    q = _product(db_session, grade="13CR110")
    r = _product(db_session, grade="L80")
    _stock(db_session, bu, p, 2000)
    _stock(db_session, bu, q, 9000)
    _stock(db_session, bu, r, 0)
    customer, node = _customer(db_session, bu=bu)
    well = _well(db_session, node, "W-C17")
    a = _line(db_session, well, p, quantity=5000, days_out=100)  # draws 2000 of P, short 3000
    b = _line(db_session, well, r, quantity=2000, days_out=200)  # R has none; substitute is P
    _allow(db_session, customer, p, q)
    _allow(db_session, customer, r, p)
    db_session.flush()
    for line, frm, to in ((a, p, q), (b, r, p)):
        appr = request_approval(db_session, line, frm.id, to.id)
        decide_approval(db_session, appr.id, approved=True)
    recompute_customer(db_session, customer)

    ra = db_session.get(CoverageResult, a.id)
    assert ra.status == CoverageStatus.COVERED_VIA_SUBSTITUTE
    assert ra.fulfilled_by_product_id == q.id
    assert (ra.drawn_company, ra.drawn_substitute, ra.residual) == (0, 5000, 0)

    # The 2000 of P that A had partially drawn is back, so B can take it.
    rb = db_session.get(CoverageResult, b.id)
    assert rb.status == CoverageStatus.COVERED_VIA_SUBSTITUTE
    assert rb.fulfilled_by_product_id == p.id
