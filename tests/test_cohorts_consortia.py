"""Section 4(a) eligibility cohorts and section 5(b) consortia."""
import pytest
from sqlalchemy import select

from app.models import Association, Ballot, Claim, Compact, Consortium, Match, Membership, Offer, Restoration, Round
from app.program import founding_units
from foundation.db import SessionLocal
from test_act_procedures import _own_draft, _petition, _through_objections, _to_verified
from test_cornerstone import post, visitor


@pytest.fixture(autouse=True)
def fresh_world():
    import app.seed
    from app.views.preview import preview_limiter

    app.seed._warm = False
    preview_limiter._events.clear()  # these tests start many preview accounts
    yield


def _certified_event(labor, worker, title) -> str:
    ref = _petition(worker, title)
    post(labor, f"/labor/events/{ref}/end-evidence")
    post(labor, f"/labor/events/{ref}/decide", {
        "decision": "certify", "decision_reasons": "Deliberate.", "occupations": "Quarry workers", "worksites": "Quarry 1",
        "geography": "County", "causal_mechanism": "Order", "causal_start": "2020-01-01", "first_separation": "2020-02-01",
        "acceptable_proof": "Payroll"})
    return ref


def _claim(c, event_ref, name) -> str:
    post(c, "/app/claims/new", {"event_ref": event_ref, "worker_name": name, "work_type": "employee", "occupation": "Quarrier",
                                "baseline_start": "2018-01-01", "baseline_end": "2018-12-31", "baseline_total_earned": "50000",
                                "baseline_covered_earned": "50000", "baseline_covered_hours": "2000",
                                "separation_type": "total", "separation_date": "2020-03-01"})
    with SessionLocal() as s:
        return s.scalar(select(Claim.ref).where(Claim.worker_name == name))


def _decide(labor, cref, decision="certify"):
    post(labor, f"/labor/claims/{cref}/decide", {"decision": decision, "reasons": "On the record."})


def _new_association(labor, founder, founder_claim, name) -> str:
    post(founder, "/app/associations/new", {"name": name, "principal_state": "Example", "claim_ref": founder_claim})
    with SessionLocal() as s:
        aref = s.scalar(select(Association.ref).where(Association.name == name))
    post(labor, f"/labor/associations/{aref}/charter", {"decision": "approve"})
    return aref


def _units(aref):
    with SessionLocal() as s:
        return founding_units(s, s.scalar(select(Association.id).where(Association.ref == aref)))


# --------------------------------------------------------------------------
# Section 4(a): cohorts
# --------------------------------------------------------------------------
def test_cohort_applicants_hold_no_unit_until_certified_and_block_closing(client):
    labor, w1, w2, w3 = visitor("labor"), visitor("new_worker"), visitor("new_worker"), visitor("new_worker")
    e = _certified_event(labor, w1, "Cohort quarry closure")
    c1 = _claim(w1, e, "Founder One")
    _decide(labor, c1)
    aref = _new_association(labor, w1, c1, "Cohort Quarry Association")
    c2, c3 = _claim(w2, e, "Pending Two"), _claim(w3, e, "Pending Three")
    assert "Elect as a cohort" in w2.get("/app/associations").text
    post(w2, f"/app/associations/{aref}/join", {"claim_ref": c2})
    post(w3, f"/app/associations/{aref}/join", {"claim_ref": c3})
    assert _units(aref) == 1  # cohort applicants hold no unit
    assert "cohort applicants" in w1.get(f"/app/associations/{aref}").text
    r = post(w1, f"/app/associations/{aref}/close-roll", ok=400)
    assert c2 in r.text and c3 in r.text  # unresolved claims block closing
    _decide(labor, c2)  # certified: becomes a founding unit at once
    assert _units(aref) == 2
    _decide(labor, c3, "deny")
    post(w1, f"/app/associations/{aref}/close-roll")
    with SessionLocal() as s:
        a = s.scalar(select(Association).where(Association.ref == aref))
        assert a.status == "roll_closed"
        claims = {m.claim.ref: m.status for m in s.scalars(select(Membership).where(Membership.association_id == a.id)).all()}
    assert claims == {c1: "founding", c2: "founding"}  # the denied applicant left the roll


def test_cohort_member_cannot_vote(client):
    labor, w1, w2 = visitor("labor"), visitor("new_worker"), visitor("new_worker")
    e = _certified_event(labor, w1, "Vote quarry closure")
    c1 = _claim(w1, e, "Voter One")
    _decide(labor, c1)
    aref = _new_association(labor, w1, c1, "Vote Quarry Association")
    c2 = _claim(w2, e, "Pending Voter")
    post(w2, f"/app/associations/{aref}/join", {"claim_ref": c2})
    with SessionLocal() as s:
        a = s.scalar(select(Association).where(Association.ref == aref))
        b = Ballot(association_id=a.id, kind="other", title="Board election", closes_on=a.enrollment_closes, is_demo=True)
        s.add(b)
        s.commit()
        bid = b.id
    post(w2, f"/app/ballots/{bid}/vote", {"choice": "yes"}, ok=403)


# --------------------------------------------------------------------------
# Section 5(b): consortia
# --------------------------------------------------------------------------
def _two_unit_association(labor, e, tag):
    f, j = visitor("new_worker"), visitor("new_worker")
    cf, cj = _claim(f, e, f"{tag} founder"), _claim(j, e, f"{tag} joiner")
    _decide(labor, cf)
    _decide(labor, cj)
    aref = _new_association(labor, f, cf, f"{tag} Association")
    post(j, f"/app/associations/{aref}/join", {"claim_ref": cj})
    post(f, f"/app/associations/{aref}/close-roll")
    return aref, f, j


def _published_offer(rid) -> str:
    state, commerce = visitor("state"), visitor("commerce")
    oref = _own_draft(state)
    _through_objections(state, oref, {"max_founding_units": "4"})
    _to_verified(state, commerce, oref)
    post(commerce, f"/commerce/offers/{oref}/publish", {"round_id": str(rid)})
    return oref


def _setup_consortium():
    labor, starter = visitor("labor"), visitor("new_worker")
    e = _certified_event(labor, starter, "Consortium quarry closure")
    x, xf, xj = _two_unit_association(labor, e, "Upper")
    y, yf, yj = _two_unit_association(labor, e, "Lower")
    with SessionLocal() as s:
        rid = s.scalar(select(Round.id).where(Round.status == "open", Round.is_demo.is_(True)))
    oref = _published_offer(rid)
    return labor, rid, oref, (x, xf, xj), (y, yf, yj)


def test_consortium_needs_every_association_and_fitting_offer(client):
    labor, rid, oref, (x, xf, xj), (y, yf, yj) = _setup_consortium()
    with SessionLocal() as s:
        s.scalar(select(Offer).where(Offer.ref == oref)).max_founding_units = 3
        s.commit()
    r = post(xf, f"/app/associations/{x}/consortium", {"round_id": str(rid), "partners": y, "rank1": oref}, ok=400)
    assert "exceed" in r.text
    with SessionLocal() as s:
        s.scalar(select(Offer).where(Offer.ref == oref)).max_founding_units = 4
        s.commit()
    post(xj, f"/app/associations/{x}/consortium", {"round_id": str(rid), "partners": y, "rank1": oref}, ok=403)  # not board
    post(xf, f"/app/associations/{x}/consortium", {"round_id": str(rid), "partners": "A-003", "rank1": oref}, ok=400)  # enrolling
    post(xf, f"/app/associations/{x}/consortium", {"round_id": str(rid), "partners": y, "rank1": oref, "name": "Quarry Valley"})
    with SessionLocal() as s:
        c = s.scalar(select(Consortium).where(Consortium.name == "Quarry Valley"))
        bx, by = c.ballot_ids
    # neither may also rank alone, nor join a second consortium in the round
    post(yf, f"/app/associations/{y}/rankings", {"round_id": str(rid), "rank1": oref}, ok=400)
    for v in (xf, xj):
        post(v, f"/app/ballots/{bx}/vote", {"choice": "yes"})
    with SessionLocal() as s:
        assert s.get(Consortium, c.id).status == "pending_votes"  # the other association has not approved
    post(yf, f"/app/ballots/{by}/vote", {"choice": "no"})  # with two units, one no means a majority is impossible
    with SessionLocal() as s:
        assert s.get(Consortium, c.id).status == "rejected"


def test_consortium_matches_and_its_compact_needs_every_association(client):
    labor, rid, oref, (x, xf, xj), (y, yf, yj) = _setup_consortium()
    post(xf, f"/app/associations/{x}/consortium", {"round_id": str(rid), "partners": y, "rank1": oref})
    with SessionLocal() as s:
        c = s.scalar(select(Consortium).where(Consortium.round_id == rid))
        bx, by = c.ballot_ids
    for v, b in ((xf, bx), (xj, bx), (yf, by), (yj, by)):
        post(v, f"/app/ballots/{b}/vote", {"choice": "yes"})
    with SessionLocal() as s:
        assert s.get(Consortium, c.id).status == "approved"
    commerce, state = visitor("commerce"), visitor("state")
    post(commerce, f"/commerce/rounds/{rid}/run")
    with SessionLocal() as s:
        m = s.scalar(select(Match).where(Match.consortium_id == c.id))
        assert m is not None and m.offer.ref == oref
        assert {a.status for a in s.scalars(select(Association).where(Association.ref.in_([x, y]))).all()} == {"matched"}
        cref = s.get(Consortium, c.id).ref
    page = client.get(f"/exchange/rounds/{rid}").text
    assert cref in page and f"{x} + {y}" in page
    # the compact: one ballot in each association; executed only when both pass
    post(state, f"/state/matches/{m.id}/compact", {"services": "One quarry", "launch_milestones": "Year one",
                                                   "public_protections": "Safety plan", "operative_on": "2030-01-01",
                                                   "projected_annual_receipts": "1000000"})
    post(state, f"/state/matches/{m.id}/send")
    with SessionLocal() as s:
        kx, ky = s.get(Consortium, c.id).compact_ballot_ids
    post(xf, f"/app/ballots/{kx}/vote", {"choice": "yes"})
    post(xj, f"/app/ballots/{kx}/vote", {"choice": "yes"})
    with SessionLocal() as s:
        assert s.scalar(select(Compact.status).where(Compact.match_id == m.id)) == "member_vote"
    post(yf, f"/app/ballots/{ky}/vote", {"choice": "yes"})
    post(yj, f"/app/ballots/{ky}/vote", {"choice": "yes"})
    with SessionLocal() as s:
        k = s.scalar(select(Compact).where(Compact.match_id == m.id))
        assert k.status == "executed"
        ids = s.scalars(select(Association.id).where(Association.ref.in_([x, y]))).all()
        assert {s.get(Association, i).status for i in ids} == {"compact"}
        assert len(s.scalars(select(Restoration).where(Restoration.association_id.in_(ids))).all()) == 2


def test_consortium_with_changed_roll_is_not_entered(client):
    labor, rid, oref, (x, xf, xj), (y, yf, yj) = _setup_consortium()
    post(xf, f"/app/associations/{x}/consortium", {"round_id": str(rid), "partners": y, "rank1": oref})
    with SessionLocal() as s:
        c = s.scalar(select(Consortium).where(Consortium.round_id == rid))
        bx, by = c.ballot_ids
    for v, b in ((xf, bx), (xj, bx), (yf, by), (yj, by)):
        post(v, f"/app/ballots/{b}/vote", {"choice": "yes"})
    with SessionLocal() as s:  # a roll changes after approval
        c = s.get(Consortium, c.id)
        c.units_snapshot = {**c.units_snapshot, list(c.units_snapshot)[0]: 3}
        s.commit()
    post(visitor("commerce"), f"/commerce/rounds/{rid}/run")
    with SessionLocal() as s:
        assert s.get(Consortium, c.id).status == "excluded"
        assert s.scalar(select(Match.id).where(Match.consortium_id == c.id)) is None
        log = s.get(Round, rid).match_log
    assert any("founding roll changed" in line for line in log)
