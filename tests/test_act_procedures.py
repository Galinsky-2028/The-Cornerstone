"""Sections 2(c), 5(c) and 7(a): extensions, subpoenas, bundles of districts, successor matches after a failed
negotiation, and federal authorization."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app import rules
from app.models import Association, Event, FederalAuthorization, Match, Offer, Ranking, Round, Subpoena
from foundation.db import SessionLocal
from test_cornerstone import post, visitor


@pytest.fixture(autouse=True)
def fresh_world():
    import app.seed
    from app.views.preview import preview_limiter

    app.seed._warm = False
    preview_limiter._events.clear()  # these tests start many preview accounts
    yield


TODAY = date.today()
LONG = "Reasons long enough to be public reasons on the record."


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------
def test_one_extension_of_at_most_ninety_days():
    assert rules.extension_problem(0, 90, LONG) is None
    assert "only one" in rules.extension_problem(30, 10, LONG)
    assert rules.extension_problem(0, 91, LONG) and rules.extension_problem(0, 0, LONG)
    assert "reasons" in rules.extension_problem(0, 30, "short")


def test_bundle_needs_independent_finding_and_each_district_qualifying():
    assert rules.bundle_problems("North", "", "", "", "Columbia") == []  # a single district needs nothing more
    ok = rules.bundle_problems("North", "South: trade closed by statute, no applicants", LONG, "Feasibility Board", "Columbia")
    assert ok == []
    assert rules.bundle_problems("North", "South:", LONG, "Feasibility Board", "Columbia")  # no qualification shown
    assert rules.bundle_problems("North", "South: closed", "too short", "Feasibility Board", "Columbia")
    assert rules.bundle_problems("North", "South: closed", LONG, "Columbia", "Columbia")  # the State is not independent
    assert rules.bundle_problems("North", "north: closed", LONG, "Board", "Columbia")  # the same district twice
    assert rules.districts_of("North", "South: x\nEast: y") == {"north", "south", "east"}


def test_federal_authorization_must_be_complete_issued_and_in_effect():
    class A:
        act_citation = "Pub. L. 999-1, s. 3"
        displaced_prohibitions = "18 U.S.C. 1084 as to the district"
        regulator = "Department of Justice"
        activity = "Retail wagering"
        persons = "The compact holder"
        territory = "North District"
        safeguards = "Age checks"
        starts_on, ends_on = TODAY - timedelta(days=1), TODAY + timedelta(days=365)
        status = "issued"
        findings = LONG

    assert rules.federal_problems(A, TODAY) == []
    assert rules.federal_problems(None, TODAY)
    assert rules.federal_problems(A, TODAY + timedelta(days=400))  # expired
    A.status = "pending"
    assert any("regulator has not issued" in p for p in rules.federal_problems(A, TODAY))
    A.status, A.regulator = "issued", ""
    assert any("administering regulator" in p for p in rules.federal_problems(A, TODAY))


# --------------------------------------------------------------------------
# Section 2(c)
# --------------------------------------------------------------------------
def _petition(worker, title="Example quarry closure") -> str:
    post(worker, "/app/petitions/new", {"title": title, "sovereign": "Example State", "action": "Order closing quarries",
                                       "targeted_work": "Quarrying", "affected_group": "Quarry workers",
                                       "intent_evidence": "The order says so", "attributed_losses": "Layoffs"})
    with SessionLocal() as s:
        return s.scalar(select(Event.ref).where(Event.title == title))


def test_event_extension_once_with_public_reasons(client):
    labor, worker = visitor("labor"), visitor("new_worker")
    ref = _petition(worker)
    with SessionLocal() as s:
        due = s.scalar(select(Event.decision_due).where(Event.ref == ref))
    post(labor, f"/labor/events/{ref}/extend", {"days": "91", "reasons": LONG}, ok=400)
    post(labor, f"/labor/events/{ref}/extend", {"days": "60", "reasons": "x"}, ok=400)
    post(labor, f"/labor/events/{ref}/extend", {"days": "60", "reasons": LONG})
    post(labor, f"/labor/events/{ref}/extend", {"days": "10", "reasons": LONG}, ok=400)  # only once
    with SessionLocal() as s:
        e = s.scalar(select(Event).where(Event.ref == ref))
        assert e.decision_due == due + timedelta(days=60) and e.extension_days == 60
    assert LONG in client.get(f"/exchange/events/{ref}").text
    assert worker.post(f"/labor/events/{ref}/extend", data={"days": "5", "reasons": LONG, "csrf": worker.token},
                       follow_redirects=False).status_code == 403


def test_subpoenas_are_registered_publicly_and_tracked(client):
    labor, worker = visitor("labor"), visitor("new_worker")
    ref = _petition(worker)
    future = (TODAY + timedelta(days=30)).isoformat()
    post(labor, f"/labor/events/{ref}/subpoenas", {"recipient": "Example Quarry Co.", "records": "Payroll 2019-2021",
                                                   "relevance": "short", "return_by": future}, ok=400)
    post(labor, f"/labor/events/{ref}/subpoenas", {"recipient": "Example Quarry Co.", "records": "Payroll 2019-2021",
                                                   "relevance": "The employer disputes the separation dates.",
                                                   "return_by": TODAY.isoformat()}, ok=400)  # return date must be ahead
    post(labor, f"/labor/events/{ref}/subpoenas", {"recipient": "Example Quarry Co.", "records": "Payroll 2019-2021",
                                                   "relevance": "The employer disputes the separation dates.", "return_by": future})
    page = client.get(f"/exchange/events/{ref}").text
    assert "Example Quarry Co." in page and "Payroll 2019-2021" in page
    with SessionLocal() as s:
        sid = s.scalar(select(Subpoena.id).where(Subpoena.recipient == "Example Quarry Co."))
    assert "Example Quarry Co." in labor.get(f"/labor/events/{ref}").text
    post(labor, f"/labor/subpoenas/{sid}", {"status": "contested", "response": "Privilege claimed over legal advice."})
    post(labor, f"/labor/subpoenas/{sid}", {"status": "referred", "response": "Referred to the district court."})
    post(labor, f"/labor/subpoenas/{sid}", {"status": "complied", "response": "Late."}, ok=400)  # closed
    with SessionLocal() as s:
        assert s.get(Subpoena, sid).status == "referred"
    # no subpoena once the petition is decided
    post(labor, f"/labor/events/{ref}/end-evidence")
    post(labor, f"/labor/events/{ref}/decide", {"decision": "deny", "decision_reasons": "Market change."})
    post(labor, f"/labor/events/{ref}/subpoenas", {"recipient": "X", "records": "Y",
                                                   "relevance": "The employer disputes the separation dates.",
                                                   "return_by": future}, ok=400)


# --------------------------------------------------------------------------
# Sections 5(c) and 7(a): offers
# --------------------------------------------------------------------------
def _own_draft(state) -> str:
    with SessionLocal() as s:
        return s.scalar(select(Offer.ref).where(Offer.status == "draft", Offer.created_by.is_not(None)))


def _through_objections(state, oref, extra=None):
    post(state, f"/state/offers/{oref}", {"offer_form": "1", "lifting_authority": "Act", "technical_requirements": "Licence",
                                          "obligations": "Taxes", **(extra or {})})


def _to_verified(state, commerce, oref):
    post(state, f"/state/offers/{oref}/open-objections")
    post(state, f"/state/offers/{oref}/end-objections")
    post(state, f"/state/offers/{oref}", {"no_new_harm": "No person holds or has applied for any license in the district; no lawful market exists."})
    post(state, f"/state/offers/{oref}/submit")
    post(commerce, f"/commerce/offers/{oref}/verify", {"decision": "verified"})


def _open_round():
    with SessionLocal() as s:
        return s.scalar(select(Round.id).where(Round.status == "open", Round.is_demo.is_(True)))


def test_bundle_of_districts_needs_independent_finding(client):
    state, commerce = visitor("state"), visitor("commerce")
    oref = _own_draft(state)
    _through_objections(state, oref, {"bundle_districts": "South District: trade closed by statute; no applicants"})
    assert "independent feasibility finding" in post(state, f"/state/offers/{oref}/open-objections", ok=400).text
    _through_objections(state, oref, {"bundle_districts": "South District: trade closed by statute; no applicants",
                                      "bundle_finding": "Neither district alone has the population to employ the full membership.",
                                      "bundle_finder": "Independent Feasibility Board"})
    _to_verified(state, commerce, oref)
    page = client.get(f"/exchange/offers/{oref}").text
    assert "South District" in page and "Independent Feasibility Board" in page
    post(commerce, f"/commerce/offers/{oref}/publish", {"round_id": str(_open_round())})


def test_overlapping_exclusive_rights_in_one_round_are_refused(client):
    state, commerce = visitor("state"), visitor("commerce")
    oref = _own_draft(state)
    with SessionLocal() as s:
        o1 = s.scalar(select(Offer).where(Offer.ref == "O-001", Offer.is_demo.is_(True)))
        trade, district = o1.trade, o1.district
    _through_objections(state, oref, {"trade": trade, "district": "Harbor District",
                                      "bundle_districts": f"{district}: closed by statute",
                                      "bundle_finding": "Neither district alone has the population to employ the full membership.",
                                      "bundle_finder": "Independent Feasibility Board"})
    _to_verified(state, commerce, oref)
    r = post(commerce, f"/commerce/offers/{oref}/publish", {"round_id": str(_open_round())}, ok=400)
    assert "overlapping district" in r.text


def test_federally_prohibited_activity_needs_issued_authorization(client):
    state, commerce = visitor("state"), visitor("commerce")
    oref = _own_draft(state)
    _through_objections(state, oref, {"federal_prohibition": "1"})
    _to_verified(state, commerce, oref)
    rid = str(_open_round())
    r = post(commerce, f"/commerce/offers/{oref}/publish", {"round_id": rid}, ok=400)  # nothing recorded
    assert "Section 7(a)" in r.text
    auth = {"act_citation": "Example Cornerstone Authorization Act, s. 2", "displaced_prohibitions": "18 U.S.C. 1955 in the district",
            "regulator": "Department of Justice", "activity": "Riverboat gaming", "persons": "The compact holder only",
            "territory": "East District", "safeguards": "Licensing, age checks, anti-money-laundering controls",
            "starts_on": (TODAY - timedelta(days=1)).isoformat(), "ends_on": (TODAY + timedelta(days=3650)).isoformat(),
            "findings": "", "status": "pending"}
    post(commerce, f"/commerce/offers/{oref}/federal", auth)
    assert "Not yet rankable" in commerce.get("/commerce").text
    assert state.get(f"/state/offers/{oref}").status_code == 200
    post(commerce, f"/commerce/offers/{oref}/publish", {"round_id": rid}, ok=400)  # not yet issued
    post(commerce, f"/commerce/offers/{oref}/federal", {**auth, "status": "issued"}, ok=400)  # findings required
    post(commerce, f"/commerce/offers/{oref}/federal", {**auth, "status": "issued",
                                                         "findings": "The statutory criteria are met; safeguards are adequate."})
    post(commerce, f"/commerce/offers/{oref}/publish", {"round_id": rid})
    page = client.get(f"/exchange/offers/{oref}").text
    assert "Example Cornerstone Authorization Act" in page and "Department of Justice" in page
    # an authorization that lapses before matching takes the offer out of the round
    with SessionLocal() as s:
        a = s.scalar(select(FederalAuthorization).join(Offer).where(Offer.ref == oref))
        a.ends_on = TODAY - timedelta(days=1)
        s.commit()
    post(commerce, f"/commerce/rounds/{rid}/run")
    with SessionLocal() as s:
        assert s.scalar(select(Offer.status).where(Offer.ref == oref)) == "verified"
        assert s.scalar(select(Match.id).join(Offer).where(Offer.ref == oref)) is None


# --------------------------------------------------------------------------
# Section 5(c): negotiation extension and failure
# --------------------------------------------------------------------------
def _matched_round(conditions: str = ""):
    """A matched round of two State A offers: A-001 ranked X then Y and holds X; A-002 ranked only X."""
    with SessionLocal() as s:
        tmpl = s.scalar(select(Offer).where(Offer.ref == "O-001", Offer.is_demo.is_(True)))
        a1 = s.scalar(select(Association).where(Association.ref == "A-001", Association.is_demo.is_(True)))
        a2 = s.scalar(select(Association).where(Association.ref == "A-002", Association.is_demo.is_(True)))
        r = Round(name="Test round", rankings_close=TODAY, random_seed="seed", status="matched", matched_on=TODAY,
                  extension_conditions=conditions, is_demo=True)
        s.add(r)
        s.flush()
        x = Offer(ref="O-900", state_name=tmpl.state_name, district="X District", trade="X trade", status="matched",
                  round_id=r.id, is_demo=True)
        y = Offer(ref="O-901", state_name=tmpl.state_name, district="Y District", trade="Y trade", status="verified",
                  round_id=r.id, is_demo=True)
        s.add_all([x, y])
        s.flush()
        s.add_all([Ranking(round_id=r.id, association_id=a1.id, offer_ids=[x.id, y.id], status="submitted", is_demo=True),
                   Ranking(round_id=r.id, association_id=a2.id, offer_ids=[x.id], status="submitted", is_demo=True)])
        m = Match(round_id=r.id, association_id=a1.id, offer_id=x.id, priority_date=date(1994, 9, 15),
                  negotiation_deadline=TODAY + timedelta(days=180), is_demo=True)
        s.add(m)
        a1.status = "matched"
        s.commit()
        return m.id, a1.id, a2.id, x.id, y.id


def test_negotiation_extension_only_on_published_conditions(client):
    commerce = visitor("commerce")
    mid, *_ = _matched_round()
    post(commerce, f"/commerce/matches/{mid}/extend", {"days": "30", "reasons": LONG}, ok=400)  # no conditions published
    with SessionLocal() as s:
        s.get(Round, s.get(Match, mid).round_id).extension_conditions = "Financing term sheet and site control"
        before = s.get(Match, mid).negotiation_deadline
        s.commit()
    post(commerce, f"/commerce/matches/{mid}/extend", {"days": "120", "reasons": LONG}, ok=400)
    post(commerce, f"/commerce/matches/{mid}/extend", {"days": "90", "reasons": LONG})
    assert "extended 90 days" in commerce.get("/commerce").text
    post(commerce, f"/commerce/matches/{mid}/extend", {"days": "10", "reasons": LONG}, ok=400)
    with SessionLocal() as s:
        assert s.get(Match, mid).negotiation_deadline == before + timedelta(days=90)


def test_failed_negotiation_passes_offer_to_next_willing_association(client):
    state = visitor("state")
    mid, a1, a2, x, y = _matched_round()
    assert "next willing association" in state.get(f"/state/matches/{mid}").text
    post(state, f"/state/matches/{mid}/fail", {"reasons": "No agreement on the launch schedule."})
    with SessionLocal() as s:
        assert s.get(Match, mid).status == "failed"
        to_x = s.scalar(select(Match).where(Match.offer_id == x, Match.status == "negotiating"))
        to_y = s.scalar(select(Match).where(Match.offer_id == y, Match.status == "negotiating"))
        assert to_x.association_id == a2 and to_x.successor_of == mid  # the next willing association takes X
        assert to_y.association_id == a1  # the unsuccessful association proceeds to its next choice
        assert s.get(Association, a1).status == "matched" and s.get(Association, a2).status == "matched"
        assert s.get(Offer, y).status == "matched"


def test_failed_negotiation_with_nobody_else_returns_offer(client):
    state = visitor("state")
    mid, a1, a2, x, y = _matched_round()
    with SessionLocal() as s:  # A-002 is busy with another active match, and Y is gone
        s.get(Association, a2).status = "matched"
        s.get(Offer, y).status = "withdrawn"
        s.commit()
    post(state, f"/state/matches/{mid}/fail", {"reasons": "No agreement."})
    with SessionLocal() as s:
        assert s.get(Offer, x).status == "verified" and s.get(Offer, x).round_id is None
        assert s.get(Association, a1).status == "roll_closed"
        assert s.scalar(select(Match.id).where(Match.successor_of == mid)) is None
