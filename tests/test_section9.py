"""Section 9: launch deadlines and preopening failures, reopening grounds, annual audits, conflicts and recusal."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app import rules
from app.models import Association, AuditReport, Compact, FundAdvance, Offer, Restoration
from foundation.db import SessionLocal
from test_act_procedures import LONG, _own_draft, _to_verified, _through_objections
from test_cohorts_consortia import _certified_event, _two_unit_association
from test_cornerstone import post, visitor


@pytest.fixture(autouse=True)
def fresh_world():
    import app.seed
    from app.views.preview import preview_limiter

    app.seed._warm = False
    preview_limiter._events.clear()
    yield


FUTURE = (date.today() + timedelta(days=200)).isoformat()


def test_rules():
    assert rules.reopen_problem("judicial_invalidation") is None
    assert "does not reopen" in rules.reopen_problem("commercial_failure")
    assert "does not reopen" in rules.reopen_problem("material_violation")
    assert rules.reopen_problem("anything")
    assert rules.preopening_outcome(0, "revise") == "revise" and rules.preopening_outcome(0, "return") == "return"
    assert rules.preopening_outcome(1, "") == "second_failure"
    with pytest.raises(ValueError):
        rules.preopening_outcome(2, "revise")
    with pytest.raises(ValueError):
        rules.preopening_outcome(0, "")
    assert rules.cooling_off_ends(date(2030, 3, 1)) == date(2032, 3, 1)


def _a4(s):
    a = s.scalar(select(Association).where(Association.ref == "A-004", Association.is_demo.is_(True)))
    return a, s.scalar(select(Restoration).where(Restoration.association_id == a.id))


def test_provisional_finding_needs_launch_deadline(client):
    labor = visitor("labor")
    checks = {k: "1" for k in rules_checks()}
    r = post(labor, "/labor/restorations/A-004/provisional", {**checks, "findings": "All met."}, ok=400)
    assert "launch deadline" in r.text
    post(labor, "/labor/restorations/A-004/provisional", {**checks, "findings": "All met.", "launch_deadline": FUTURE})
    with SessionLocal() as s:
        assert _a4(s)[1].launch_deadline.isoformat() == FUTURE


def rules_checks():
    from app.models import RESTORATION_CHECKS

    return RESTORATION_CHECKS


def _provisional(labor):
    post(labor, "/labor/restorations/A-004/provisional", {**{k: "1" for k in rules_checks()}, "findings": "All met.",
                                                         "launch_deadline": FUTURE})


def test_revise_then_second_failure_leaves_claims_open_without_automatic_advance(client):
    labor = visitor("labor")
    _provisional(labor)
    assert "Capital not secured" in labor.get("/labor/restorations/A-004").text
    post(labor, "/labor/restorations/A-004/preopening-failure", {"choice": "revise", "reasons": LONG}, ok=400)  # no plan
    post(labor, "/labor/restorations/A-004/preopening-failure", {"choice": "revise", "reasons": LONG,
                                                                 "revised_plan": "Smaller first site, same offer terms.",
                                                                 "new_launch_deadline": FUTURE})
    with SessionLocal() as s:
        a, r = _a4(s)
        assert r.status == "provisional" and r.preopening_failures == 1 and not r.no_automatic_advance
        claims_before = {m.claim.status for m in a.members}
    post(labor, "/labor/restorations/A-004/preopening-failure", {"reasons": LONG})
    with SessionLocal() as s:
        a, r = _a4(s)
        k = s.get(Compact, r.compact_id)
        assert r.status == "failed_preopening" and r.preopening_failures == 2 and r.no_automatic_advance
        assert a.status == "roll_closed" and k.status == "returned" and k.offer.status == "verified"
        assert {m.claim.status for m in a.members} == claims_before  # the claims stay open
        f = FundAdvance(association_id=a.id, category="training", amount=10_000, is_demo=True)
        s.add(f)
        s.commit()
        fid = f.id
    post(labor, "/labor/restorations/A-004/preopening-failure", {"choice": "revise", "reasons": LONG}, ok=400)
    r = post(labor, f"/labor/fund/{fid}", {"action": "approve"}, ok=400)
    assert "no automatic right" in r.text
    post(labor, f"/labor/fund/{fid}", {"action": "approve", "reasons": "A new State offer justifies renewed training."})


def test_first_failure_may_return_to_exchange(client):
    labor = visitor("labor")
    _provisional(labor)
    post(labor, "/labor/restorations/A-004/preopening-failure", {"choice": "return", "reasons": LONG})
    with SessionLocal() as s:
        a, r = _a4(s)
        assert r.status == "returned" and a.status == "roll_closed" and s.get(Compact, r.compact_id).status == "returned"


def test_only_governmental_defeat_reopens(client):
    labor = visitor("labor")
    _provisional(labor)
    for ground in ("commercial_failure", "costs_or_demand", "material_violation", ""):
        post(labor, "/labor/restorations/A-004/reopen", {"ground": ground, "reasons": LONG}, ok=400)
    post(labor, "/labor/restorations/A-004/reopen", {"ground": "judicial_invalidation", "reasons": LONG})
    with SessionLocal() as s:
        r = _a4(s)[1]
        assert r.status == "reopened" and r.reopen_ground == "judicial_invalidation" and r.no_automatic_advance


def test_annual_audit_is_published(client):
    labor, starter = visitor("labor"), visitor("new_worker")
    e = _certified_event(labor, starter, "Audit quarry closure")
    aref, board, member = _two_unit_association(labor, e, "Audited")
    year = str(date.today().year - 1)
    good = {"year": year, "auditor": "Independent Audit LLP", "financial_opinion": "unqualified",
            "beneficiary_control": "1", "no_political_contributions": "1", "beneficial_ownership": "Members, one unit each.",
            "summary": "Accounts present fairly; members hold all voting control; no outside board seats."}
    post(member, f"/app/associations/{aref}/audits", good, ok=403)  # board only
    post(board, f"/app/associations/{aref}/audits", {**good, "auditor": "Audited Association"}, ok=400)  # not independent
    post(board, f"/app/associations/{aref}/audits", {**good, "summary": "short"}, ok=400)
    post(board, f"/app/associations/{aref}/audits", {**good, "year": str(date.today().year + 1)}, ok=400)
    post(board, f"/app/associations/{aref}/audits", good)
    post(board, f"/app/associations/{aref}/audits", good, ok=400)  # one per year
    page = client.get("/exchange").text
    assert "Independent Audit LLP" in page and "Members, one unit each." in page
    post(board, f"/app/associations/{aref}/audits", {**good, "year": str(int(year) - 1), "beneficiary_control": ""})
    assert "NOT confirmed" in client.get("/exchange").text
    with SessionLocal() as s:
        assert len(s.scalars(select(AuditReport).join(Association).where(Association.ref == aref)).all()) == 2


def test_disclosed_conflict_recuses_the_official(client):
    labor, other_labor, worker = visitor("labor"), visitor("labor"), visitor("new_worker")
    assert worker.get("/app/conflicts").status_code == 403
    assert labor.get("/app/conflicts").status_code == 200
    post(labor, "/app/conflicts", {"matter": "A-004", "role": "labor", "nature": "short"}, ok=400)
    post(labor, "/app/conflicts", {"matter": "A-999", "role": "labor", "nature": LONG}, ok=400)
    post(labor, "/app/conflicts", {"matter": "A-004", "role": "commerce", "nature": LONG}, ok=403)  # not your role
    post(labor, "/app/conflicts", {"matter": "A-004", "role": "labor", "nature": "My brother is on the board of A-004."})
    assert "My brother is on the board" in client.get("/exchange").text
    r = post(labor, "/labor/restorations/A-004/remedied", {"operations_began_on": "2026-01-01", "financing_secured": "1"}, ok=403)
    assert "recused" in r.text
    post(other_labor, "/labor/restorations/A-004/remedied", {"operations_began_on": "2026-01-01", "financing_secured": "1"})


def test_commerce_official_recused_from_offer(client):
    state, commerce, other = visitor("state"), visitor("commerce"), visitor("commerce")
    oref = _own_draft(state)
    _through_objections(state, oref)
    _to_verified(state, commerce, oref)
    post(other, "/app/conflicts", {"matter": oref, "role": "commerce", "nature": "I advised the State on this offer."})
    from test_act_procedures import _open_round

    post(other, f"/commerce/offers/{oref}/publish", {"round_id": str(_open_round())}, ok=403)
    post(commerce, f"/commerce/offers/{oref}/publish", {"round_id": str(_open_round())})
    with SessionLocal() as s:
        assert s.scalar(select(Offer.status).where(Offer.ref == oref)) == "published"


def test_compact_page_lists_deciders_with_cooling_off(client):
    sup = visitor("supervisor")
    from app.models import SupervisorDecision

    with SessionLocal() as s:
        d = s.scalar(select(SupervisorDecision).where(SupervisorDecision.decision == "pending"))
        kref = d.compact.ref
    post(sup, f"/supervisor/decisions/{d.id}", {"decision": "approved", "reasons": "Within the published pricing band."})
    page = client.get(f"/exchange/compacts/{kref}").text
    assert "restricted until" in page
