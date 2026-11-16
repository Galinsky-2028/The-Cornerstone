"""Sections 4(a), 4(f), 6(d), 6(e) and 8(c): larger bodies, rescission, calculation appeals, enforcement, Fund release."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app import rules
from app.models import (
    Association, Ballot, Claim, Compact, CompactProceeding, Membership, QuarterlyReport, Restoration, ShareAppeal,
)
from foundation.db import SessionLocal
from test_cornerstone import post, visitor

LONG = "Findings long enough to stand as written findings on the public record."


@pytest.fixture(autouse=True)
def fresh_world():
    import app.seed
    from app.views.preview import preview_limiter

    app.seed._warm = False
    preview_limiter._events.clear()
    yield


def test_rules():
    t = date(2030, 1, 10)
    ok = dict(ground="fraud", hearing_record="The association was heard in full.", cure_deadline=None, today=t,
              opened_by=1, decider=2, findings=LONG)
    assert rules.revocation_problem(**ok) is None
    assert "limited" in rules.revocation_problem(**{**ok, "ground": "late_paperwork"})
    assert "hearing" in rules.revocation_problem(**{**ok, "hearing_record": ""})
    assert "cure" in rules.revocation_problem(**{**ok, "cure_deadline": t})
    assert rules.revocation_problem(**{**ok, "cure_deadline": t - timedelta(days=1)}) is None
    assert "independent" in rules.revocation_problem(**{**ok, "decider": 1})
    assert "findings" in rules.revocation_problem(**{**ok, "findings": "short"})
    assert rules.fund_released(100, 100) and not rules.fund_released(99, 100) and not rules.fund_released(0, 0)
    assert rules.roll_ceiling(False) == 2500 and rules.roll_ceiling(True) is None


def _k1():
    with SessionLocal() as s:
        return s.scalar(select(Compact).where(Compact.ref == "K-001", Compact.is_demo.is_(True)))


def test_emergency_suspension_and_lift(client):
    sup = visitor("supervisor")
    post(sup, "/supervisor/compacts/K-001/proceedings", {"kind": "emergency", "ground": "material_safety", "notice": "short"}, ok=400)
    post(sup, "/supervisor/compacts/K-001/proceedings", {"kind": "emergency", "ground": "material_safety",
                                                          "notice": "Gas leak at the main site poses imminent serious harm to customers."})
    k = _k1()
    assert k.status == "suspended"
    assert "suspended" in client.get("/exchange/compacts/K-001").text
    with SessionLocal() as s:
        p = s.scalar(select(CompactProceeding).where(CompactProceeding.compact_id == k.id))
        assert p.hearing_due == date.today() + timedelta(days=rules.EMERGENCY_HEARING_DAYS)
    post(sup, "/supervisor/compacts/K-001/proceedings", {"kind": "revocation", "ground": "fraud", "notice": LONG}, ok=400)  # one at a time
    post(sup, f"/supervisor/proceedings/{p.id}/decide", {"outcome": "lifted", "findings": "Leak repaired and inspected."})
    assert _k1().status == "executed"


def test_revocation_needs_grounds_hearing_and_an_independent_adjudicator(client):
    sup, adjudicator = visitor("supervisor"), visitor("supervisor")
    post(sup, "/supervisor/compacts/K-001/proceedings", {"kind": "revocation", "ground": "unpopular_prices", "notice": LONG}, ok=400)
    post(sup, "/supervisor/compacts/K-001/proceedings", {"kind": "revocation", "ground": "loss_of_control", "notice": LONG})
    with SessionLocal() as s:
        pid = s.scalar(select(CompactProceeding.id).where(CompactProceeding.outcome == "pending"))
        r_before = s.scalar(select(Restoration.status).join(Association).where(Association.ref == "A-004", Association.is_demo.is_(True)))
    assert "Enforcement proceedings" in sup.get("/supervisor").text
    r = post(adjudicator, f"/supervisor/proceedings/{pid}/decide", {"outcome": "revoked", "findings": LONG}, ok=400)
    assert "hearing" in r.text
    post(sup, f"/supervisor/proceedings/{pid}/hearing", {"record": "Counsel for the association and the investor were heard."})
    r = post(sup, f"/supervisor/proceedings/{pid}/decide", {"outcome": "revoked", "findings": LONG}, ok=400)
    assert "independent adjudicator" in r.text
    post(adjudicator, f"/supervisor/proceedings/{pid}/decide", {"outcome": "revoked", "findings": "short"}, ok=400)
    post(adjudicator, f"/supervisor/proceedings/{pid}/decide", {"outcome": "revoked", "findings": LONG})
    assert _k1().status == "revoked"
    with SessionLocal() as s:  # enforcement for a material violation does not reopen claims
        assert s.scalar(select(Restoration.status).join(Association).where(Association.ref == "A-004", Association.is_demo.is_(True))) == r_before
    assert "revoked" in client.get("/exchange/compacts/K-001").text


def test_calculation_appeal_corrects_inputs_not_the_rate(client):
    sup = visitor("supervisor")
    k = _k1()
    with SessionLocal() as s:
        rep = s.scalar(select(QuarterlyReport).where(QuarterlyReport.compact_id == k.id).order_by(QuarterlyReport.id))
        a = ShareAppeal(report_id=rep.id, claimed_surplus=40_000, reasons="Depreciation was classified as a distribution.", is_demo=True)
        s.add(a)
        s.commit()
        aid, rid = a.id, rep.id
    post(sup, f"/supervisor/appeals/{aid}", {"decision": "corrected", "reasons": "short"}, ok=400)
    post(sup, f"/supervisor/appeals/{aid}", {"decision": "corrected", "reasons": "Depreciation is a necessary replacement allowance."})
    with SessionLocal() as s:
        rep = s.get(QuarterlyReport, rid)
        assert rep.shareable_surplus == 40_000 and rep.state_share == 40_000 * k.surplus_rate_pct // 100
        assert s.get(Compact, k.id).surplus_rate_pct == k.surplus_rate_pct
    post(sup, f"/supervisor/appeals/{aid}", {"decision": "denied", "reasons": "Already decided on the record."}, ok=404)


def test_rescission_only_after_notice_hearing_and_findings(client):
    labor = visitor("labor")
    with SessionLocal() as s:
        c = s.scalar(select(Claim).where(Claim.ref == "CL-00003", Claim.is_demo.is_(True)))
        on_roll = s.scalar(select(Membership.id).where(Membership.claim_id == c.id)) is not None
    assert c.status == "certified" and on_roll
    post(labor, f"/labor/claims/{c.ref}/rescission", {"notice": "short"}, ok=400)
    post(labor, f"/labor/claims/{c.ref}/rescission", {"notice": "Payroll records submitted for the claim appear to have been altered."})
    with SessionLocal() as s:
        from app.models import ClaimRescission

        rid = s.scalar(select(ClaimRescission.id).where(ClaimRescission.claim_id == c.id))
    assert "Rescission of fraudulently obtained eligibility" in labor.get(f"/labor/claims/{c.ref}").text
    r = post(labor, f"/labor/rescissions/{rid}/decide", {"outcome": "rescinded", "findings": LONG}, ok=400)
    assert "hearing" in r.text
    post(labor, f"/labor/rescissions/{rid}/hearing", {"record": "The worker and the employer's payroll officer testified."})
    post(labor, f"/labor/rescissions/{rid}/decide", {"outcome": "rescinded", "findings": LONG})
    with SessionLocal() as s:
        assert s.get(Claim, c.id).status == "rescinded"
        assert s.scalar(select(Membership.id).where(Membership.claim_id == c.id)) is None


def test_larger_body_needs_founders_majority_and_labor_findings(client):
    labor = visitor("labor")
    post(labor, "/labor/associations/A-003/large-body", {"findings": LONG}, ok=400)  # no founders' approval yet
    with SessionLocal() as s:
        a = s.scalar(select(Association).where(Association.ref == "A-003", Association.is_demo.is_(True)))
        s.add(Ballot(association_id=a.id, kind="large_body", title="Allow more than 2,500 founding units", status="passed",
                     closes_on=date.today(), is_demo=True))
        s.commit()
    assert "Larger founding bodies" in labor.get("/labor").text
    post(labor, "/labor/associations/A-003/large-body", {"findings": "short"}, ok=400)
    post(labor, "/labor/associations/A-003/large-body", {"findings": LONG})
    with SessionLocal() as s:
        assert s.scalar(select(Association.large_body_approved_on).where(Association.ref == "A-003", Association.is_demo.is_(True)))


def test_board_puts_the_larger_body_question_to_the_founders(client):
    from test_cohorts_consortia import _certified_event, _claim, _decide, _new_association

    labor, founder = visitor("labor"), visitor("new_worker")
    e = _certified_event(labor, founder, "Large body quarry closure")
    c = _claim(founder, e, "Large body founder")
    _decide(labor, c)
    aref = _new_association(labor, founder, c, "Large Body Association")
    post(founder, f"/app/associations/{aref}/large-body-ballot")
    post(founder, f"/app/associations/{aref}/large-body-ballot", ok=400)
    with SessionLocal() as s:
        b = s.scalar(select(Ballot).join(Association).where(Association.ref == aref, Ballot.kind == "large_body"))
        assert b is not None and b.status == "open"
