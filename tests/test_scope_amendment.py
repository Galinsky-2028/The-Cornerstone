"""Section 2(e): Labor amends a certification's scope on new evidence, with notice, an evidence period and findings."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.models import Event, EventAmendment, EventComment
from foundation.db import SessionLocal
from test_cohorts_consortia import _certified_event
from test_cornerstone import post, visitor

LONG = "Reasoned findings on the new evidence, stated for the public record in full."
EVIDENCE = "Newly released agency memoranda show the order also targeted the county's crushing plants."


@pytest.fixture(autouse=True)
def fresh_world():
    import app.seed
    from app.views.preview import preview_limiter

    app.seed._warm = False
    preview_limiter._events.clear()
    yield


def _scope(**kw):
    base = {"occupations": "Quarry workers and crusher operators", "worksites": "Quarry 1; crushing plants",
            "geography": "County", "causal_mechanism": "Order", "acceptable_proof": "Payroll",
            "causal_start": "2020-01-01", "causal_end": "", "first_separation": "2020-02-01"}
    base.update(kw)
    return base


def test_amendment_needs_notice_evidence_period_and_findings(client):
    labor, worker, public = visitor("labor"), visitor("new_worker"), visitor("new_worker")
    ref = _certified_event(labor, worker, "Amended quarry closure")
    assert "Amend the scope on new evidence" in labor.get(f"/labor/events/{ref}").text
    post(labor, f"/labor/events/{ref}/amendments", {**_scope(), "new_evidence": "short"}, ok=400)
    post(labor, f"/labor/events/{ref}/amendments", {**_scope(occupations=""), "new_evidence": EVIDENCE}, ok=400)
    unchanged = {"occupations": "Quarry workers", "worksites": "Quarry 1", "geography": "County", "causal_mechanism": "Order",
                 "acceptable_proof": "Payroll", "causal_start": "2020-01-01", "causal_end": "", "first_separation": "2020-02-01"}
    post(labor, f"/labor/events/{ref}/amendments", {**unchanged, "new_evidence": EVIDENCE}, ok=400)
    post(labor, f"/labor/events/{ref}/amendments", {**_scope(), "new_evidence": EVIDENCE})
    post(labor, f"/labor/events/{ref}/amendments", {**_scope(), "new_evidence": EVIDENCE}, ok=400)  # one open at a time
    with SessionLocal() as s:
        a = s.scalar(select(EventAmendment).join(Event).where(Event.ref == ref))
        assert a.comment_ends == date.today() + timedelta(days=60)
    page = client.get(f"/exchange/events/{ref}").text
    assert "Scope amendments" in page and "crushing plants" in page
    # the public may respond while the amendment is open, though the original petition period is over
    assert "proposed amending" in public.get(f"/app/events/{ref}/respond").text
    post(public, f"/app/events/{ref}/respond", {"position": "oppose", "body": "Crushing plants were closed for other reasons."})
    with SessionLocal() as s:
        assert s.scalar(select(EventComment.amendment_id).where(EventComment.body.like("Crushing plants%"))) == a.id
    assert "Adopt the amendment" in labor.get(f"/labor/events/{ref}").text
    post(labor, f"/labor/amendments/{a.id}/decide", {"decision": "adopt", "findings": LONG}, ok=400)  # evidence period open
    post(labor, f"/labor/amendments/{a.id}/end-evidence")
    post(labor, f"/labor/amendments/{a.id}/decide", {"decision": "adopt", "findings": "short"}, ok=400)
    post(labor, f"/labor/amendments/{a.id}/decide", {"decision": "adopt", "findings": LONG})
    with SessionLocal() as s:
        e = s.scalar(select(Event).where(Event.ref == ref))
        a = s.get(EventAmendment, a.id)
        assert e.occupations == "Quarry workers and crusher operators" and e.worksites == "Quarry 1; crushing plants"
        assert a.status == "adopted" and a.previous["occupations"] == "Quarry workers"
    assert "was: Quarry workers" in client.get(f"/exchange/events/{ref}").text
    # after the decision, the event is closed to responses again
    post(public, f"/app/events/{ref}/respond", {"position": "evidence", "body": "Late."}, ok=400)


def test_rejected_amendment_leaves_scope_and_only_certified_events_amend(client):
    labor, worker = visitor("labor"), visitor("new_worker")
    ref = _certified_event(labor, worker, "Rejected amendment closure")
    post(labor, f"/labor/events/{ref}/amendments", {**_scope(first_separation="2019-06-01"), "new_evidence": EVIDENCE})
    with SessionLocal() as s:
        aid = s.scalar(select(EventAmendment.id).join(Event).where(Event.ref == ref))
    post(labor, f"/labor/amendments/{aid}/end-evidence")
    post(labor, f"/labor/amendments/{aid}/decide", {"decision": "reject", "findings": LONG})
    with SessionLocal() as s:
        assert s.scalar(select(Event.first_separation).where(Event.ref == ref)) == date(2020, 2, 1)
    from test_act_procedures import _petition

    pending = _petition(worker, "Not yet certified closure")
    post(labor, f"/labor/events/{pending}/amendments", {**_scope(), "new_evidence": EVIDENCE}, ok=400)
    assert worker.post(f"/labor/events/{ref}/amendments", data={**_scope(), "new_evidence": EVIDENCE, "csrf": worker.token},
                       follow_redirects=False).status_code == 403
