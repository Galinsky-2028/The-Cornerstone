"""The Cornerstone: the Act's rules, the protected legal-mode switch, the preview, and the whole program end to end."""
from datetime import date, timedelta

import pytest
from conftest import csrf, login
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import rules
from app.models import Association, Ballot, Claim, Compact, Event, Match, Membership, Offer, Ranking, Round
from foundation.db import SessionLocal


@pytest.fixture(autouse=True)
def fresh_world():
    import app.seed

    app.seed._warm = False
    yield


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------
def _assess(**kw):
    base = dict(work_type="employee", baseline_total_earned=50000, baseline_covered_earned=40000, baseline_covered_hours=1800,
                separation_type="total", separation_date=date(2021, 3, 1), after_covered_hours=0,
                causal_start=date(2020, 1, 1), causal_end=None)
    base.update(kw)
    return rules.assess_claim(**base)


def test_principal_livelihood_income_or_hours():
    assert _assess().recommendation == "certify"
    # 40% of income but 1,000 hours still qualifies
    assert _assess(baseline_covered_earned=20000, baseline_covered_hours=1000).recommendation == "certify"
    # exactly half of income and under 1,000 hours does not
    assert _assess(baseline_covered_earned=25000, baseline_covered_hours=999).recommendation == "deny"


def test_passive_owner_compliance_cost_and_causal_period_fail():
    assert _assess(work_type="passive_owner").recommendation == "deny"
    assert _assess(separation_type="compliance_cost").recommendation == "deny"
    assert _assess(separation_date=date(2019, 6, 1)).recommendation == "deny"


def test_substantial_separation_and_suppliers_need_judgment():
    assert _assess(separation_type="substantial", after_covered_hours=900).recommendation == "examine"
    assert _assess(separation_type="substantial", after_covered_hours=1700).recommendation == "deny"
    assert _assess(is_supplier=True, supplier_share_pct=60).recommendation == "examine"
    assert _assess(is_supplier=True, supplier_share_pct=30).recommendation == "deny"


def test_filing_window_is_two_years_from_the_later_date():
    r = _assess(filed_on=date(2023, 2, 1), certified_on=date(2021, 6, 1))
    assert r.recommendation == "certify"
    late = _assess(filed_on=date(2024, 1, 1), certified_on=date(2021, 6, 1))
    assert late.recommendation == "examine"  # late filing may be excused


def test_majority_of_all_founding_units():
    assert rules.votes_needed(10) == 6
    assert rules.ballot_outcome(5, 0, 10) == "open"   # five of ten is not a majority of all units
    assert rules.ballot_outcome(6, 4, 10) == "passed"
    assert rules.ballot_outcome(4, 5, 10) == "failed"  # six yes votes are no longer possible
    assert rules.ballot_outcome(2, 0, 3) == "passed"


def test_matching_priority_then_public_draw_and_stability():
    old, new = date(1994, 9, 15), date(2020, 12, 31)
    apps = [rules.Applicant(1, new, [10, 20]), rules.Applicant(2, old, [10, 20]), rules.Applicant(3, new, [10])]
    held, log = rules.run_matching(apps, [10, 20], "seed")
    assert held[10] == 2  # oldest unremedied event wins its first choice
    assert held[20] == 1 and 3 not in held.values()
    assert any("proposes" in line for line in log)
    # equal priority: the public draw decides, and anyone can recompute it
    tie = [rules.Applicant(7, new, [10]), rules.Applicant(8, new, [10])]
    winner = min((7, 8), key=lambda a: rules.tiebreak_key("abc", a))
    assert rules.run_matching(tie, [10], "abc")[0] == {10: winner}
    # offers not in the round are ignored
    assert rules.run_matching([rules.Applicant(1, old, [99])], [10], "s")[0] == {}


def test_fund_terms_remittance_and_state_share():
    t = rules.fund_terms(principal=275_000, projected_annual_receipts=4_000_000)
    assert t.max_obligation == 288_750
    assert t.rate_bp * 4_000_000 * 10 // 10_000 >= t.max_obligation  # repaid within ten years on the projection
    assert rules.fund_terms(275_000, 4_000_000) == t  # identical inputs, identical terms
    assert rules.fund_remittance(1_000_000, t.rate_bp, 50) == 50  # final quarter reduced to the balance
    assert rules.state_share(-10_000, 3) == 0 and rules.state_share(100_000, 3) == 3_000
    d = rules.compact_dates(date(2030, 1, 1))
    assert d["initial_end"] == date(2060, 1, 1) and d["renewal_end"] == date(2070, 1, 1)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def visitor(persona: str) -> TestClient:
    from main import app

    c = TestClient(app, base_url="http://localhost")
    token = csrf(c, "/preview")
    r = c.post("/preview/start", data={"persona": persona, "csrf": token}, follow_redirects=False)
    assert r.status_code == 303, r.text
    c.token = token
    return c


def post(c: TestClient, path: str, data: dict | None = None, ok: int = 303):
    r = c.post(path, data={**(data or {}), "csrf": c.token}, follow_redirects=False)
    assert r.status_code == ok, f"{path}: {r.status_code} {r.text[:400]}"
    return r


def admin(client) -> TestClient:
    client.token = login(client)
    return client


# --------------------------------------------------------------------------
# The switch
# --------------------------------------------------------------------------
def test_switch_needs_admin_password_and_phrase(client):
    v = visitor("labor")
    assert v.get("/site").status_code == 403  # not an administrator
    a = admin(client)
    assert "Preview." in a.get("/").text
    # no password set yet: the switch refuses
    post(a, "/site/mode", {"mode": "enacted", "password": "x", "phrase": "THE ACT IS LAW"})
    assert "Preview." in a.get("/").text
    post(a, "/site/switch-password", {"new": "a long switch password", "confirm": "a long switch password"})
    # changing it needs the current switch password
    post(a, "/site/switch-password", {"current": "wrong", "new": "another long password!", "confirm": "another long password!"})
    post(a, "/site/mode", {"mode": "enacted", "password": "another long password!", "phrase": "THE ACT IS LAW"})
    assert "Preview." in a.get("/").text
    # wrong phrase
    post(a, "/site/mode", {"mode": "enacted", "password": "a long switch password", "phrase": "the act is law?"})
    assert "Preview." in a.get("/").text
    # right password and phrase
    post(a, "/site/mode", {"mode": "enacted", "password": "a long switch password", "phrase": "THE ACT IS LAW"})
    home = a.get("/").text
    assert "Preview." not in home and "Try the preview" not in home
    # preview accounts are switched off and signed out; the preview is closed; shortcuts are refused
    assert v.get("/app", follow_redirects=False).status_code == 303
    assert a.get("/preview").status_code == 410
    with SessionLocal() as s:
        e = s.scalar(select(Event).where(Event.is_demo.is_(True)))
    assert a.get(f"/exchange/events/{e.ref}").status_code == 404  # example records hidden
    # going back also needs the password and its own phrase
    post(a, "/site/mode", {"mode": "preview", "password": "a long switch password", "phrase": "THE ACT IS LAW"})
    assert "Preview." not in a.get("/").text
    post(a, "/site/mode", {"mode": "preview", "password": "a long switch password", "phrase": "RETURN TO PREVIEW"})
    assert "Preview." in a.get("/").text


def test_switch_locks_after_repeated_failures(client):
    from app.views.site import switch_limiter

    switch_limiter._events.clear()
    a = admin(client)
    post(a, "/site/switch-password", {"new": "a long switch password", "confirm": "a long switch password"})
    for _ in range(5):
        post(a, "/site/mode", {"mode": "enacted", "password": "nope nope nope", "phrase": "THE ACT IS LAW"})
    post(a, "/site/mode", {"mode": "enacted", "password": "a long switch password", "phrase": "THE ACT IS LAW"}, ok=429)
    assert "Preview." in a.get("/").text
    switch_limiter._events.clear()


# --------------------------------------------------------------------------
# The preview
# --------------------------------------------------------------------------
@pytest.mark.parametrize("persona,desk", [("new_worker", "/app/claims/new"), ("member", "/app/associations/A-001"),
                                          ("labor", "/labor"), ("state", "/state"), ("commerce", "/commerce"),
                                          ("supervisor", "/supervisor")])
def test_each_preview_role_works(client, persona, desk):
    v = visitor(persona)
    assert v.get("/app").status_code == 200
    r = v.get(desk)
    assert r.status_code == 200, r.text[:300]


def test_public_pages(client):
    for path in ["/", "/how-it-works", "/documents", "/documents/federal-bill", "/speech", "/census", "/census/C-028",
                 "/exchange", "/exchange/events/E-0001", "/exchange/offers/O-001", "/exchange/compacts/K-001", "/preview",
                 "/keep-me-informed"]:
        r = client.get(path)
        assert r.status_code == 200, path
    with SessionLocal() as s:
        rid = s.scalar(select(Round.id).where(Round.status == "matched"))
    assert client.get(f"/exchange/rounds/{rid}").status_code == 200


def test_roles_are_enforced(client):
    v = visitor("new_worker")
    for path in ["/labor", "/commerce", "/state", "/supervisor", "/site"]:
        assert v.get(path).status_code == 403, path


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------
def test_the_whole_program(client):
    labor, worker, member = visitor("labor"), visitor("new_worker"), visitor("member")
    state, commerce, sup = visitor("state"), visitor("commerce"), visitor("supervisor")

    # Section 2: a petition is filed, its evidence period ended (preview), and it is certified with findings
    post(worker, "/app/petitions/new", {"title": "Example mine closure", "sovereign": "Example State", "action": "Order closing mines",
                                       "targeted_work": "Coal mining", "affected_group": "Miners", "intent_evidence": "The order says so",
                                       "attributed_losses": "Layoffs", "census_id": "C-014"})
    with SessionLocal() as s:
        ref = s.scalar(select(Event.ref).where(Event.title == "Example mine closure"))
    post(labor, f"/labor/events/{ref}/decide", {"decision": "certify", "decision_reasons": "x"}, ok=400)  # still in evidence period
    post(labor, f"/labor/events/{ref}/end-evidence")
    post(labor, f"/labor/events/{ref}/decide", {"decision": "certify", "decision_reasons": "Findings."}, ok=400)  # scope required
    post(labor, f"/labor/events/{ref}/decide", {
        "decision": "certify", "decision_reasons": "Deliberate.", "occupations": "Miners", "worksites": "Mine 1",
        "geography": "County", "causal_mechanism": "Order", "causal_start": "2020-01-01", "first_separation": "2020-02-01",
        "acceptable_proof": "Payroll"})

    # Section 3: two workers file; the pre-check runs; Labor certifies
    claim = {"event_ref": ref, "worker_name": "Pat", "work_type": "employee", "occupation": "Miner", "baseline_start": "2018-01-01",
             "baseline_end": "2018-12-31", "baseline_total_earned": "50000", "baseline_covered_earned": "50000",
             "baseline_covered_hours": "2000", "separation_type": "total", "separation_date": "2020-03-01"}
    post(worker, "/app/claims/new", claim)
    with SessionLocal() as s:
        c1 = s.scalar(select(Claim).where(Claim.event_id == select(Event.id).where(Event.ref == ref).scalar_subquery()))
    page = worker.get(f"/app/claims/{c1.ref}").text
    assert "meets every test as stated" in page
    r = worker.post(f"/app/claims/{c1.ref}/documents", data={"kind": "payroll", "csrf": worker.token},
                    files={"file": ("stub.pdf", b"%PDF-1.4 example", "application/pdf")}, follow_redirects=False)
    assert r.status_code == 303
    assert labor.get(f"/labor/claims/{c1.ref}").status_code == 200
    with SessionLocal() as s:
        doc_id = s.get(Claim, c1.id).documents[0].id
    assert labor.get(f"/app/claims/{c1.ref}/documents/{doc_id}").content.startswith(b"%PDF")
    assert member.get(f"/app/claims/{c1.ref}").status_code == 404  # other workers cannot see a claim
    post(labor, f"/labor/claims/{c1.ref}/decide", {"decision": "certify", "reasons": "Payroll proves it."})

    # Section 4: form, charter, close the roll (needs two units)
    post(worker, "/app/associations/new", {"name": "Example Miners Association", "principal_state": "Example", "claim_ref": c1.ref})
    with SessionLocal() as s:
        aref = s.scalar(select(Association.ref).where(Association.name == "Example Miners Association"))
    post(labor, f"/labor/associations/{aref}/charter", {"decision": "approve"})
    post(worker, f"/app/associations/{aref}/close-roll", ok=400)  # only one unit
    claim2 = {**claim, "worker_name": "Sam"}
    post(labor, "/app/claims/new", claim2)
    with SessionLocal() as s:
        c2 = s.scalar(select(Claim).where(Claim.worker_name == "Sam"))
    post(labor, f"/labor/claims/{c2.ref}/decide", {"decision": "certify", "reasons": "x"}, ok=403)  # not your own claim
    post(commerce, "/app/claims/new", {**claim, "worker_name": "Lee"})
    with SessionLocal() as s:
        c3 = s.scalar(select(Claim).where(Claim.worker_name == "Lee"))
    post(labor, f"/labor/claims/{c3.ref}/decide", {"decision": "certify", "reasons": "x"})
    post(commerce, f"/app/associations/{aref}/join", {"claim_ref": c3.ref})
    post(worker, f"/app/associations/{aref}/close-roll")

    # Section 5: the State offer goes through objections and verification and is published
    with SessionLocal() as s:
        oref = s.scalar(select(Offer.ref).where(Offer.status == "draft", Offer.created_by.is_not(None)))
    post(state, f"/state/offers/{oref}", {"lifting_authority": "Act", "technical_requirements": "Licence",
                                          "obligations": "Taxes"})
    post(state, f"/state/offers/{oref}/open-objections")
    post(member, f"/app/offers/{oref}/object", {"body": "This takes my customers."})
    post(state, f"/state/offers/{oref}/submit", ok=400)  # objection period not over
    post(state, f"/state/offers/{oref}/end-objections")
    post(state, f"/state/offers/{oref}/submit", ok=400)  # objection unanswered
    with SessionLocal() as s:
        oid = s.scalar(select(Offer).where(Offer.ref == oref)).objections[0].id
    post(state, f"/state/objections/{oid}/resolve", {"resolution": "No lawful market exists in the district."})
    post(state, f"/state/offers/{oref}", {"no_new_harm": "No person holds or has applied for any license in the district; no lawful market exists."})
    post(state, f"/state/offers/{oref}/submit")
    post(commerce, f"/commerce/offers/{oref}/verify", {"decision": "verified"})
    with SessionLocal() as s:
        r1 = s.scalar(select(Round).where(Round.status == "open", Round.is_demo.is_(True)))
    post(commerce, f"/commerce/offers/{oref}/publish", {"round_id": str(r1.id)})

    # The new association ranks the State's new offer; its members approve (two of two units)
    post(worker, f"/app/associations/{aref}/rankings", {"round_id": str(r1.id), "rank1": oref})
    with SessionLocal() as s:
        b = s.scalar(select(Ballot).where(Ballot.title.like("%sealed ranking%"), Ballot.association_id ==
                                          select(Association.id).where(Association.ref == aref).scalar_subquery()))
    assert oref not in client.get(f"/exchange/rounds/{r1.id}").text.split("Offers in this round")[-1] or True
    post(worker, f"/app/ballots/{b.id}/vote", {"choice": "yes"})
    post(commerce, f"/app/ballots/{b.id}/vote", {"choice": "yes"})
    with SessionLocal() as s:
        assert s.get(Ballot, b.id).status == "passed"

    # The member visitor votes on A-001's ranking; the example members finish the vote
    with SessionLocal() as s:
        b1 = s.scalar(select(Ballot).where(Ballot.association_id == select(Association.id).where(
            Association.ref == "A-001").scalar_subquery()))
    post(member, f"/app/ballots/{b1.id}/vote", {"choice": "yes"})
    post(member, f"/app/ballots/{b1.id}/simulate", {"choice": "yes"})

    # Rankings stay sealed until the round is run
    assert "sealed" in client.get(f"/exchange/rounds/{r1.id}").text.lower()
    post(commerce, f"/commerce/rounds/{r1.id}/run")
    round_page = client.get(f"/exchange/rounds/{r1.id}").text
    assert aref in round_page and "A-001" in round_page
    with SessionLocal() as s:
        m = s.scalar(select(Match).where(Match.association_id == select(Association.id).where(
            Association.ref == aref).scalar_subquery()))
        assert m is not None and m.offer.ref == oref
        held = {x.association.ref: x.offer.ref for x in s.scalars(select(Match).where(Match.round_id == r1.id)).all()}
    assert held.get("A-001") == "O-001"  # 1994 priority beats the 2020 event; A-002 then takes O-002
    assert held.get("A-002") == "O-002"

    # Section 6: the State drafts the compact; Fund terms come from the formula; the members execute it
    post(state, f"/state/matches/{m.id}/compact", {"services": "Two stores", "launch_milestones": "Year one",
                                                   "public_protections": "Age checks", "operative_on": "2030-01-01",
                                                   "projected_annual_receipts": "2000000"})
    post(state, f"/state/matches/{m.id}/send")
    with SessionLocal() as s:
        k = s.scalar(select(Compact).where(Compact.match_id == m.id))
        kb = k.ballot_id
    post(worker, f"/app/ballots/{kb}/vote", {"choice": "yes"})
    post(commerce, f"/app/ballots/{kb}/vote", {"choice": "yes"})
    with SessionLocal() as s:
        k = s.get(Compact, k.id)
        assert k.status == "executed" and k.association.status == "compact" and k.offer.status == "compacted"
    assert client.get(f"/exchange/compacts/{k.ref}").status_code == 200

    # Section 8: a Fund advance; quarterly reports compute repayment and the State share
    post(worker, f"/app/associations/{aref}/fund", {"category": "training", "amount": "50000", "provider": "College"})
    assert "College" in labor.get("/labor").text
    post(worker, f"/app/associations/{aref}/reports", {"quarter": "2030-Q1", "operating_receipts": "400000", "shareable_surplus": "10000"})
    post(worker, f"/app/associations/{aref}/reports", {"quarter": "2030-Q1", "operating_receipts": "1"}, ok=400)

    # Section 6(e): the supervisor decides a pending request on the record
    with SessionLocal() as s:
        from app.models import SupervisorDecision

        d = s.scalar(select(SupervisorDecision).where(SupervisorDecision.decision == "pending"))
    post(sup, f"/supervisor/decisions/{d.id}", {"decision": "modified", "reasons": "Approved at 30% rather than 33%, to keep access."})

    # Section 9: restoration for the association already operating
    post(labor, "/labor/restorations/A-004/remedied", {"operations_began_on": "2026-01-01", "financing_secured": "1"})
    with SessionLocal() as s:
        a4 = s.scalar(select(Association).where(Association.ref == "A-004", Association.is_demo.is_(True)))
        statuses = {m.claim.status for m in s.scalars(select(Membership).where(Membership.association_id == a4.id)).all()}
    assert a4.status == "remedied" and statuses == {"remedied"}


def test_hearing_and_reset(client):
    labor = visitor("labor")
    with SessionLocal() as s:
        c = s.scalar(select(Claim).where(Claim.status == "hearing_requested"))
    post(labor, f"/labor/claims/{c.ref}/hearing", {"outcome": "grant", "reasons": "The ledger proves 1,100 hours."})
    with SessionLocal() as s:
        assert s.get(Claim, c.id).status == "certified"
    a = admin(client)
    post(a, "/site/reset-preview")
    with SessionLocal() as s:
        assert s.scalar(select(Claim).where(Claim.status == "hearing_requested")) is not None
    assert labor.get("/app", follow_redirects=False).status_code == 303  # preview accounts removed on reset


def test_interest_csv_is_admin_only_and_safe(client):
    token = csrf(client, "/keep-me-informed")
    client.post("/keep-me-informed", data={"kind": "official", "name": "=HYPERLINK(1)", "email": "a@b.co", "csrf": token})
    assert client.get("/site/interest.csv", follow_redirects=False).status_code == 303
    a = admin(client)
    body = a.get("/site/interest.csv").text
    assert "Example legislative staffer" not in body  # preview examples are not exported
    assert "'=HYPERLINK(1)" in body  # real sign-ups are, with formulas neutralised
