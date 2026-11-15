"""The preview's example world, and the preview accounts visitors use to try each role.

Everything here is created with is_demo=True and is labelled as an example. The example events are based
on real Displacement Event Census entries so that the preview is realistic, but no event has been certified
under the Act: each example certification says so, and its dates are illustrative.

The world is rebuilt every night and whenever an administrator presses "Reset the preview".
"""
from __future__ import annotations

import secrets
from datetime import date, timedelta

from sqlalchemy import delete, select

from foundation.auth_core import create_user, start_session
from foundation.db import SessionLocal, audit
from foundation import blobstore
from foundation.models import User, WebSession

from .models import (
    AuditReport, Association, Ballot, Claim, ClaimDocument, Compact, ConflictDisclosure, Consortium, DemoAccount, Event, EventComment, FederalAuthorization, FundAdvance,
    Interest, Match, Membership,
    Offer, OfferObjection, QuarterlyReport, Ranking, Restoration, RoleGrant, Round, Subpoena, SupervisorDecision, Vote,
)
from .rules import fund_remittance, fund_terms, state_share

EXAMPLE_NOTE = ("Example certification made for the preview. No event has been certified under the Cornerstone "
                "Livelihood Restoration Act, which is not yet law; the dates in this example are illustrative.")
STATE_A = "Columbia (example state)"
STATE_B = "Lincoln (example state)"

PERSONAS = {
    "new_worker": {"title": "A displaced worker, starting out",
                   "summary": "File a claim against a certified event, have it decided, then join or form a Cornerstone Association.",
                   "roles": ["worker"]},
    "member": {"title": "A member of a Cornerstone Association",
               "summary": "You hold a certified claim and a founding unit. Vote on your association's sealed ranking of state offers, choose your opportunity, and follow the match.",
               "roles": ["worker"]},
    "labor": {"title": "A Department of Labor examiner",
              "summary": "Decide an event petition after the evidence period, decide individual claims and a hearing, charter an association, approve Founding Fund advances, and confirm a restoration.",
              "roles": ["worker", "labor"]},
    "state": {"title": f"A State official in {STATE_A}",
              "summary": "Draft an offer under the State enabling law, prove it takes nothing from anyone, answer objections, submit it to the Exchange, and draft a compact after a match.",
              "roles": ["worker", "state"], "state": STATE_A},
    "commerce": {"title": "A Department of Commerce Exchange administrator",
                 "summary": "Verify a State offer is complete, open a matching round, and run the published matching rule once rankings close.",
                 "roles": ["worker", "commerce"]},
    "supervisor": {"title": f"The independent State supervisor in {STATE_B}",
                   "summary": "Approve, modify or reject an enterprise's material pricing, access, service, safety and exclusion decisions on a public record.",
                   "roles": ["worker", "supervisor"], "state": STATE_B},
}

DEMO_TABLES = [Vote, Ballot, QuarterlyReport, SupervisorDecision, Restoration, FundAdvance, Compact, Match, Ranking,
               AuditReport, ConflictDisclosure, FederalAuthorization, OfferObjection, Offer, Consortium, Round, Membership, Association, Claim, EventComment, Subpoena,
               Event, Interest]


def _user(s, name: str, email: str) -> User:
    u = create_user(s, email, secrets.token_urlsafe(24), name=name)
    s.add(DemoAccount(user_id=u.id, persona="seed"))
    s.add(RoleGrant(user_id=u.id, role="worker"))
    return u


def clear_demo(s) -> list[str]:
    """Delete every preview record and preview account. Returns the stored-file keys to remove afterwards."""
    keys: list[str] = []
    ballots = [b.id for b in s.scalars(select(Ballot).where(Ballot.is_demo.is_(True))).all()]
    if ballots:
        s.execute(delete(Vote).where(Vote.ballot_id.in_(ballots)))
    claim_ids = [c.id for c in s.scalars(select(Claim).where(Claim.is_demo.is_(True))).all()]
    if claim_ids:
        for d in s.scalars(select(ClaimDocument).where(ClaimDocument.claim_id.in_(claim_ids))).all():
            keys.append(d.blob_key)
        s.execute(delete(ClaimDocument).where(ClaimDocument.claim_id.in_(claim_ids)))
    assoc_ids = [a.id for a in s.scalars(select(Association).where(Association.is_demo.is_(True))).all()]
    if assoc_ids:
        s.execute(delete(Membership).where(Membership.association_id.in_(assoc_ids)))
    for model in DEMO_TABLES:
        if model in (Vote, Membership):
            continue
        if model is Offer:
            s.execute(Offer.__table__.update().where(Offer.is_demo.is_(True)).values(round_id=None))
        s.execute(delete(model).where(model.is_demo.is_(True)))
    demo_ids = [d.user_id for d in s.scalars(select(DemoAccount)).all()]
    if demo_ids:
        s.execute(delete(WebSession).where(WebSession.user_id.in_(demo_ids)))
        s.execute(delete(RoleGrant).where(RoleGrant.user_id.in_(demo_ids)))
        s.execute(delete(DemoAccount))
        s.execute(delete(User).where(User.id.in_(demo_ids)))
    s.flush()
    return keys


def _claim(s, ref, event, user, occupation, employer, worksite, sep, status="certified", **kw) -> Claim:
    c = Claim(ref=ref, event_id=event.id, user_id=user.id, worker_name=user.name, work_type=kw.get("work_type", "employee"),
              occupation=occupation, employer=employer, worksite=worksite,
              baseline_start=sep - timedelta(days=395), baseline_end=sep - timedelta(days=30),
              baseline_total_earned=kw.get("total", 52000), baseline_covered_earned=kw.get("covered", 52000),
              baseline_covered_hours=kw.get("hours", 2080), separation_type=kw.get("sep_type", "total"),
              separation_date=sep, status=status, filed_on=kw.get("filed_on", date.today() - timedelta(days=200)),
              decided_on=None if status == "filed" else date.today() - timedelta(days=150),
              decision_reasons="" if status == "filed" else kw.get("reasons", "Example decision for the preview: "
                                "payroll and union records show covered work as principal livelihood and a separation within the causal period."),
              prior_benefits=kw.get("benefits", ""), is_demo=True)
    s.add(c)
    s.flush()
    return c


def seed_demo(s) -> None:
    """Build the example world. Assumes clear_demo has run."""
    t = date.today()

    # ---- Section 2: events -----------------------------------------------------------------
    timber = Event(ref="E-0001", title="Northwest Forest Plan timber contraction", census_id="C-028",
                   sovereign="United States (Forest Service and Bureau of Land Management)",
                   action="1994 Northwest Forest Plan record of decision reducing timber offered from federal forests to protect old-forest habitat.",
                   targeted_work="Logging, hauling and sawmill work dependent on federal timber sales",
                   affected_group="Loggers, log-truck owner-operators and mill workers in western Oregon",
                   intent_evidence="The plan's record states the reduction of timber supply as a chosen means of protecting habitat.",
                   attributed_losses="Mill closures and logging-crew layoffs following the reduced federal timber offering.",
                   petitioner_type="union", petitioner_name="Example woodworkers' local", status="certified",
                   filed_on=t - timedelta(days=420), comment_ends=t - timedelta(days=360), decision_due=t - timedelta(days=240),
                   occupations="Fallers, buckers, choker setters, log-truck drivers, sawyers, mill laborers",
                   worksites="Sawmills and logging operations supplied by federal timber sales in the covered counties",
                   geography="Western Oregon (example scope)", contrary_evidence="Harvest decline predated the plan in some areas; mills with other supply sources are excluded.",
                   causal_mechanism="Reduced federal timber offering removed the supply the covered mills and crews depended on.",
                   causal_start=date(1994, 7, 1), causal_end=date(2000, 12, 31), first_separation=date(1994, 9, 15),
                   acceptable_proof="Payroll, union dispatch, tax records or owner-operator contracts for covered work in the representative year.",
                   decision_reasons=EXAMPLE_NOTE, decided_on=t - timedelta(days=250), is_demo=True)
    nets = Event(ref="E-0002", title="Florida gill-net ban", census_id="C-004", sovereign="State of Florida",
                 action="Constitutional amendment prohibiting entangling nets and large nets in Florida waters.",
                 targeted_work="Inshore commercial net fishing", affected_group="Licensed net fishers and crew",
                 intent_evidence="The amendment's text prohibits the gear by name; ending its use was the express objective.",
                 attributed_losses="Loss of net-fishing livelihoods on the effective date.",
                 petitioner_type="workers", petitioner_name="Two example net fishers", status="certified",
                 filed_on=t - timedelta(days=500), comment_ends=t - timedelta(days=440), decision_due=t - timedelta(days=320),
                 occupations="Net fishing vessel owner-operators and crew", worksites="Licensed inshore net-fishing vessels",
                 geography="Florida inshore waters (example scope)", contrary_evidence="Gear substitution and earlier stock decline; fishers who switched gear without material loss are excluded.",
                 causal_mechanism="The gear prohibition ended the covered method of fishing.",
                 causal_start=date(1995, 7, 1), causal_end=date(1997, 6, 30), first_separation=date(1995, 7, 1),
                 acceptable_proof="Saltwater product licenses, trip tickets and landings records for the representative year.",
                 decision_reasons=EXAMPLE_NOTE, decided_on=t - timedelta(days=330), is_demo=True)
    coal = Event(ref="E-0003", title="New York coal phaseout", census_id="C-014", sovereign="State of New York",
                 action="CO2 performance standards requiring existing coal units to repower or cease burning coal.",
                 targeted_work="Coal-fired generation work at covered units", affected_group="Plant operators, maintenance and fuel-handling workers",
                 intent_evidence="The rulemaking record states that ending coal combustion at existing units was the purpose of the standard.",
                 attributed_losses="Unit retirements and layoffs at covered stations.",
                 petitioner_type="state_agency", petitioner_name="Example State workforce agency", status="certified",
                 filed_on=t - timedelta(days=300), comment_ends=t - timedelta(days=240), decision_due=t - timedelta(days=120),
                 occupations="Control-room operators, mechanics, electricians, coal handlers", worksites="Covered coal-fired units",
                 geography="New York (example scope)", contrary_evidence="Plant economics and gas prices; units that closed for reasons predating the rule are excluded.",
                 causal_mechanism="The standard made continued coal operation unlawful at the covered units.",
                 causal_start=date(2020, 1, 1), causal_end=None, first_separation=date(2020, 12, 31),
                 acceptable_proof="Employer payroll and separation notices for covered units.",
                 decision_reasons=EXAMPLE_NOTE, decided_on=t - timedelta(days=130), is_demo=True)
    centralia = Event(ref="E-0004", title="Centralia coal-unit retirement", census_id="C-015", sovereign="State of Washington",
                      action="State law and order requiring coal burning at the Centralia plant to end.",
                      targeted_work="Coal-specific operations at the Centralia generating station",
                      affected_group="Coal-handling and coal-unit operating staff",
                      intent_evidence="The statute names the plant and sets dates by which coal burning must end.",
                      attributed_losses="Positions eliminated at the coal unit as it converted.",
                      petitioner_type="workers", petitioner_name="Two example plant workers", status="comment_period",
                      filed_on=t - timedelta(days=58), comment_ends=t + timedelta(days=2), decision_due=t + timedelta(days=122),
                      is_demo=True)
    s.add_all([timber, nets, coal, centralia])
    s.flush()
    s.add_all([
        EventComment(event_id=centralia.id, author_name="Example plant employee", position="evidence",
                     body="My position in coal handling was eliminated when the unit stopped burning coal. Payroll records attached to my future claim.", is_demo=True),
        EventComment(event_id=centralia.id, author_name="Example utility", organization="Plant owner (example)", position="oppose",
                     body="Part of the station continues to operate on natural gas; the scope should be limited to coal-specific positions.", is_demo=True),
    ])

    # ---- Section 3: people and claims --------------------------------------------------------
    people = {}
    names = ["Ray Hollister", "Dana Kemp", "Luis Ortega", "Ann Whitcomb", "Bill Tanner", "Marie Dupre", "Cal Jensen",
             "Joy Abernathy", "Sam Petrakis", "Lena Ruiz", "Tom Greer", "Ida Vance", "Hal Brody", "Rosa Kim",
             "Earl Fisk", "Nina Holt", "Gus Moreno", "Pat Lyle", "Vic Strand", "Opal Reed", "Wes Carlin", "June Ashby"]
    for i, n in enumerate(names, 1):
        people[i] = _user(s, n + " (example)", f"seed{i}@preview.invalid")
    s.flush()
    cl = []
    timber_jobs = [("Faller", "Example Logging Co.", "Douglas County, OR"), ("Log-truck owner-operator", "Self", "Lane County, OR"),
                   ("Sawyer", "Example Mill", "Coos County, OR"), ("Mill laborer", "Example Mill", "Coos County, OR"),
                   ("Choker setter", "Example Logging Co.", "Douglas County, OR"), ("Millwright", "Example Mill", "Linn County, OR")]
    for i, (occ, emp, site) in enumerate(timber_jobs, 1):
        cl.append(_claim(s, f"CL-{i:05d}", timber, people[i], occ, emp, site, date(1995, 3, 1) + timedelta(days=40 * i),
                         work_type="owner_operator" if "owner" in occ else "employee"))
    for i in range(7, 12):
        cl.append(_claim(s, f"CL-{i:05d}", nets, people[i], "Net fisher" if i % 2 else "Deckhand", "Self" if i % 2 else "Example vessel",
                         "Gulf coast of Florida", date(1995, 7, 1), work_type="owner_operator" if i % 2 else "employee"))
    for i in range(12, 16):
        cl.append(_claim(s, f"CL-{i:05d}", coal, people[i], ["Control-room operator", "Mechanic", "Electrician", "Coal handler"][i - 12],
                         "Example generating station", "Western New York", date(2021, 2, 1) + timedelta(days=30 * (i - 12))))
    # Four more timber workers form the association that already holds a compact
    for i in range(16, 20):
        cl.append(_claim(s, f"CL-{i:05d}", timber, people[i], ["Head rig sawyer", "Planer operator", "Forklift operator", "Saw filer"][i - 16],
                         "Example Willamette mill", "Lane County, OR", date(1996, 5, 1) + timedelta(days=60 * (i - 16)), status="remedied"))
    # Waiting for the examiner: two filed claims and one hearing request
    pending1 = _claim(s, "CL-00020", coal, people[20], "Instrument technician", "Example generating station", "Western New York",
                      date(2021, 9, 15), status="filed", filed_on=t - timedelta(days=20), benefits="Received severance and 26 weeks of unemployment insurance.")
    pending2 = _claim(s, "CL-00021", coal, people[21], "Barge unloader (contractor)", "Example fuel contractor", "Western New York",
                      date(2021, 6, 1), status="filed", filed_on=t - timedelta(days=12), total=48000, covered=21000, hours=820)
    pending2.is_supplier, pending2.supplier_share_pct = True, 40
    hearing = _claim(s, "CL-00022", nets, people[22], "Net-mender", "Self", "Gulf coast of Florida", date(1996, 2, 1), status="hearing_requested",
                     work_type="independent", total=30000, covered=12000, hours=700,
                     reasons="Denied: covered work provided 40% of earned income and 700 hours, below the principal-livelihood test.")
    hearing.hearing_request = "My net-mending income was under-reported because much of it was paid in kind; I have the boatyard's ledger."
    s.flush()

    # ---- Section 4: associations -------------------------------------------------------------
    a1 = Association(ref="A-001", name="Oregon Timber Families Cornerstone Association", principal_state="Oregon",
                     purpose="Restore the livelihoods of loggers and mill workers displaced by the Northwest Forest Plan.",
                     status="roll_closed", enrollment_opens=t - timedelta(days=220), enrollment_closes=t - timedelta(days=40),
                     chartered_on=t - timedelta(days=220), is_demo=True)
    a2 = Association(ref="A-002", name="Gulf Coast Net Fishers Cornerstone Association", principal_state="Florida",
                     purpose="Restore the livelihoods of net fishers displaced by the Florida gill-net ban.",
                     status="roll_closed", enrollment_opens=t - timedelta(days=250), enrollment_closes=t - timedelta(days=70),
                     chartered_on=t - timedelta(days=250), is_demo=True)
    a3 = Association(ref="A-003", name="Finger Lakes Power Workers Cornerstone Association", principal_state="New York",
                     purpose="Restore the livelihoods of coal-unit workers displaced by the New York coal phaseout.",
                     status="enrolling", enrollment_opens=t - timedelta(days=60), enrollment_closes=t + timedelta(days=120),
                     chartered_on=t - timedelta(days=60), is_demo=True)
    a4 = Association(ref="A-004", name="Willamette Mill Workers Cornerstone Association", principal_state="Oregon",
                     purpose="Mill workers displaced by the Northwest Forest Plan, now operating a compact.",
                     status="restoration_provisional", enrollment_opens=t - timedelta(days=900), enrollment_closes=t - timedelta(days=720),
                     chartered_on=t - timedelta(days=900), is_demo=True)
    s.add_all([a1, a2, a3, a4])
    s.flush()
    for c in cl[0:6]:
        s.add(Membership(association_id=a1.id, claim_id=c.id, user_id=c.user_id, on_board=c.ref in ("CL-00001", "CL-00002"),
                         opportunity="employment"))
    for c in cl[6:11]:
        s.add(Membership(association_id=a2.id, claim_id=c.id, user_id=c.user_id, on_board=c.ref == "CL-00007", opportunity="franchise"))
    for c in cl[11:15]:
        s.add(Membership(association_id=a3.id, claim_id=c.id, user_id=c.user_id, on_board=c.ref == "CL-00012"))
    for c in cl[15:19]:
        s.add(Membership(association_id=a4.id, claim_id=c.id, user_id=c.user_id, on_board=c.ref == "CL-00016", opportunity="employment"))
    s.flush()

    # ---- Sections 5 and 6: offers and rounds -------------------------------------------------
    common = dict(enabling_law="Cornerstone Enabling Act of the example state (modelled on the Model State Cornerstone Enabling Act)",
                  obligations="State licensing, ordinary taxes, consumer protection, responsible-operation and labor law apply in full.",
                  negotiable_terms="Launch schedule within 24 months; number of locations between the published minimum and maximum; training partner from the approved list.",
                  label="cornerstone", is_demo=True)
    r0 = Round(name="Round 0 (example, completed)", rankings_close=t - timedelta(days=760), random_seed=secrets.token_hex(16),
               status="matched", matched_on=t - timedelta(days=759),
               match_log=["1. Association 4 proposes to offer O-004; the offer holds it."], is_demo=True)
    r1 = Round(name="Round 1 (example)", rankings_close=t + timedelta(days=30), random_seed=secrets.token_hex(16), is_demo=True)
    s.add_all([r0, r1])
    s.flush()
    o1 = Offer(ref="O-001", state_name=STATE_A, district="North District", trade="Retail sports wagering",
               prohibition="Sports wagering is prohibited to every person in the North District under the State gaming code.",
               lifting_authority="The State Cornerstone Enabling Act authorizes the Gaming Commission to lift the prohibition in this district for the compact holder only.",
               technical_requirements="Licensed premises, certified wagering systems, responsible-gaming program, background checks for key staff.",
               supervisor="Columbia Office of Cornerstone Supervision (independent of the association and of market participants)",
               surplus_rate_pct=3, no_new_harm="No person holds or has applied for a wagering license in the district; lawful wagering is not available there; no cross-border supplier is displaced. No objections were filed.",
               status="published", published_on=t - timedelta(days=60), round_id=r1.id, **common)
    o2 = Offer(ref="O-002", state_name=STATE_A, district="River District (a dry county)", trade="Craft distilling and tasting-room licenses",
               prohibition="The county's local-option law prohibits the manufacture and sale of distilled spirits by every person.",
               lifting_authority="County voters approved participation; the Enabling Act lets the Alcohol Commission issue licenses to the compact holder only.",
               technical_requirements="Federal distilled spirits permit, State manufacturer license, fire-code approval.",
               supervisor="Columbia Office of Cornerstone Supervision", surplus_rate_pct=3,
               no_new_harm="No distillery, bar or package store lawfully operates in the county; no license applications are pending.",
               status="published", published_on=t - timedelta(days=60), round_id=r1.id, **common)
    o3 = Offer(ref="O-003", state_name=STATE_B, district="Hill District", trade="Destination casino gaming",
               prohibition="Casino gaming is prohibited throughout the Hill District under the State constitution as amended.",
               lifting_authority="Voters approved a constitutional amendment authorizing one Cornerstone casino compact in the district.",
               technical_requirements="Gaming Control Board licensing, surveillance and cage standards, problem-gambling funding.",
               supervisor="Lincoln Cornerstone Supervisor", surplus_rate_pct=4,
               no_new_harm="No casino operates or is licensed in the district; tribal gaming compacts are outside the district and unaffected.",
               status="published", published_on=t - timedelta(days=60), round_id=r1.id, **common)
    o4 = Offer(ref="O-004", state_name=STATE_B, district="Valley District (a formerly dry county)", trade="Package liquor retail",
               prohibition="Package liquor sales were prohibited to every person in the county under local option.",
               lifting_authority="County referendum and the State Enabling Act.", technical_requirements="State retail license per store; age-verification systems.",
               supervisor="Lincoln Cornerstone Supervisor", surplus_rate_pct=4,
               no_new_harm="No package store lawfully operated in the county before the compact.",
               status="compacted", published_on=t - timedelta(days=860), round_id=r0.id, **common)
    o5_draft = Offer(ref="O-005", state_name=STATE_A, district="East District", trade="Riverboat gaming",
                     prohibition="Riverboat gaming is prohibited on all waters of the East District.", status="draft",
                     surplus_rate_pct=3, supervisor="Columbia Office of Cornerstone Supervision", **common)
    o6 = Offer(ref="O-006", state_name=STATE_B, district="Lake District", trade="Pari-mutuel wagering on horse racing",
               prohibition="Pari-mutuel wagering is prohibited in the Lake District.", lifting_authority="State Enabling Act and Racing Commission rule.",
               technical_requirements="Totalizator certification, track or simulcast facility licensing.", supervisor="Lincoln Cornerstone Supervisor",
               surplus_rate_pct=4, no_new_harm="No wagering facility operates or is licensed in the district. One objection was filed and answered.",
               status="submitted", objection_ends=t - timedelta(days=5), **common)
    s.add_all([o1, o2, o3, o4, o5_draft, o6])
    s.flush()
    s.add(OfferObjection(offer_id=o6.id, author_name="Example neighboring track", body="Our simulcast customers in the district would be taken.",
                         resolution="Simulcast wagering is not lawful in the district today, so no existing customer relationship exists there; the objector's facility is outside the district and unaffected.",
                         is_demo=True))

    # Rankings for Round 1: A-002 submitted; A-001 awaiting its members' vote
    s.add(Ranking(round_id=r1.id, association_id=a2.id, offer_ids=[o2.id, o1.id], status="submitted", submitted_on=t - timedelta(days=3), is_demo=True))
    b1 = Ballot(association_id=a1.id, kind="rankings", title="Approve our sealed ranking for Round 1",
                description="Ranking, most preferred first: O-001 Retail sports wagering (North District), then O-003 Destination casino gaming (Hill District). "
                            "Passing requires a majority of ALL founding units, not just of those who vote.",
                payload={"round_id": r1.id, "offer_ids": [o1.id, o3.id]}, closes_on=t + timedelta(days=25), is_demo=True)
    s.add(b1)
    s.flush()
    s.add(Ranking(round_id=r1.id, association_id=a1.id, offer_ids=[o1.id, o3.id], status="pending_vote", ballot_id=b1.id, is_demo=True))
    a1_members = s.scalars(select(Membership).where(Membership.association_id == a1.id).order_by(Membership.id)).all()
    for m in a1_members[:3]:
        s.add(Vote(ballot_id=b1.id, membership_id=m.id, choice="yes"))

    # ---- Round 0 result, compact, fund, reports, supervision, restoration (A-004) ------------
    m0 = Match(round_id=r0.id, association_id=a4.id, offer_id=o4.id, priority_date=date(1994, 9, 15), status="compacted",
               negotiation_deadline=t - timedelta(days=580), is_demo=True)
    s.add(m0)
    s.flush()
    advances = [("feasibility", 85000, "Example Feasibility Partners"), ("legal_accounting", 40000, "Example Law & Accounting LLP"),
                ("licensing", 30000, "State licensing fees"), ("training", 120000, "Example Community College retail-management program")]
    for cat, amt, prov in advances:
        s.add(FundAdvance(association_id=a4.id, category=cat, amount=amt, provider=prov, status="disbursed",
                          requested_on=t - timedelta(days=700), is_demo=True))
    terms = fund_terms(principal=sum(a for _, a, _ in advances), projected_annual_receipts=4_000_000)
    k1 = Compact(ref="K-001", match_id=m0.id, offer_id=o4.id, association_id=a4.id, status="executed",
                 services="Up to six package liquor stores in the Valley District under the association's brand.",
                 launch_milestones="First store open within 12 months; all six within 30 months.",
                 public_protections="Age verification at every sale; hours limited by county ordinance; prices subject to supervisor review.",
                 selected_terms="Six locations; training partner: Example Community College.", operative_on=t - timedelta(days=500),
                 surplus_rate_pct=o4.surplus_rate_pct, fund_rate_bp=terms.rate_bp, fund_return_bp=terms.return_bp,
                 fund_max_obligation=terms.max_obligation, projected_annual_receipts=4_000_000, executed_on=t - timedelta(days=560), is_demo=True)
    s.add(k1)
    s.flush()
    remaining = terms.max_obligation
    for q, receipts, surplus in [("Q1", 610000, 0), ("Q2", 880000, 42000), ("Q3", 1040000, 96000), ("Q4", 1150000, 131000)]:
        rem = fund_remittance(receipts, terms.rate_bp, remaining)
        remaining -= rem
        s.add(QuarterlyReport(compact_id=k1.id, quarter=f"{(t - timedelta(days=400)).year}-{q}", operating_receipts=receipts,
                              shareable_surplus=surplus, fund_remittance=rem, state_share=state_share(surplus, k1.surplus_rate_pct), is_demo=True))
    s.add(SupervisorDecision(compact_id=k1.id, kind="access", request="Open the third store in Millbrook rather than Elk Crossing.",
                             decision="approved", reasons="Both towns are inside the district; Millbrook has no comparable retail access.",
                             created_on=t - timedelta(days=200), decided_on=t - timedelta(days=190), is_demo=True))
    s.add(SupervisorDecision(compact_id=k1.id, kind="pricing", request="Raise the standard markup on spirits from 28% to 33% to fund a fourth store.",
                             created_on=t - timedelta(days=4), is_demo=True))
    s.add(Restoration(association_id=a4.id, compact_id=k1.id, checks={k: True for k in
                      ("compact", "authorizations", "feasibility", "training", "capital")}, status="provisional",
                      findings="Example provisional restoration finding: all five conditions are met. The claims will be marked remedied when financing is secured and operations begin.",
                      decided_on=t - timedelta(days=480), is_demo=True))
    s.add(FundAdvance(association_id=a1.id, category="feasibility", amount=60000, provider="Example Feasibility Partners",
                      description="Independent feasibility review of the Round 1 offers the members ranked.", is_demo=True))

    s.add_all([Interest(kind="worker", name="Example mill worker", email="example1@preview.invalid", state_name="Oregon", census_id="C-028", is_demo=True),
               Interest(kind="official", name="Example legislative staffer", email="example2@preview.invalid", state_name="Ohio",
                        organization="Office of an example state senator", is_demo=True)])
    s.flush()


def reset_demo_world(actor_id: int | None = None) -> None:
    with SessionLocal() as s:
        keys = clear_demo(s)
        seed_demo(s)
        audit(s, "preview.reset", actor_id)
        s.commit()
    for k in keys:
        blobstore.remove(k)


def ensure_demo_world() -> None:
    from .program import is_preview

    if not is_preview():
        return
    with SessionLocal() as s:
        if s.scalar(select(Event.id).where(Event.is_demo.is_(True)).limit(1)) is not None:
            return
    reset_demo_world()


# --------------------------------------------------------------------------
# Preview accounts
# --------------------------------------------------------------------------
def create_persona(persona: str) -> tuple[User, str]:
    """Create a fresh preview account for one visitor, with what that role needs to try every step.
    Returns (user, session token)."""
    from .program import next_ref

    spec = PERSONAS[persona]
    ensure_demo_world()
    with SessionLocal() as s:
        tag = secrets.token_hex(3)
        u = create_user(s, f"preview-{persona}-{tag}@preview.invalid", secrets.token_urlsafe(24),
                        name=f"Preview visitor ({spec['title'].lower()})")
        s.add(DemoAccount(user_id=u.id, persona=persona))
        for role in spec["roles"]:
            s.add(RoleGrant(user_id=u.id, role=role, state_name=spec.get("state", "")))
        s.flush()
        t = date.today()
        if persona == "member":
            timber = s.scalar(select(Event).where(Event.ref == "E-0001", Event.is_demo.is_(True)))
            c = _claim(s, next_ref(s, Claim, "CL", 5), timber, u, "Choker setter", "Example Logging Co.", "Douglas County, OR",
                       date(1995, 10, 2))
            c.worker_name = "You (preview visitor)"
            a1 = s.scalar(select(Association).where(Association.ref == "A-001", Association.is_demo.is_(True)))
            s.add(Membership(association_id=a1.id, claim_id=c.id, user_id=u.id, joined_on=t - timedelta(days=100)))
        if persona == "state":
            # Each State official gets a draft offer of their own, so visitors do not overwrite each other.
            template = s.scalar(select(Offer).where(Offer.ref == "O-005", Offer.is_demo.is_(True)))
            if template is not None:
                s.add(Offer(ref=next_ref(s, Offer, "O", 3), state_name=template.state_name, district=f"East District ({tag})",
                            trade=template.trade, prohibition=template.prohibition, supervisor=template.supervisor,
                            surplus_rate_pct=template.surplus_rate_pct, enabling_law=template.enabling_law,
                            obligations=template.obligations, negotiable_terms=template.negotiable_terms,
                            status="draft", created_by=u.id, is_demo=True))
        audit(s, "preview.start", u.id, target=persona)
        s.commit()
        user = s.get(User, u.id)
    return user, start_session(user, "preview")


# --------------------------------------------------------------------------
# Daily rebuild
# --------------------------------------------------------------------------
# Render's free plan stops the process after 15 minutes without visitors, and Foundation's repeating tasks first
# run one interval after the process starts, so an hourly task alone almost never runs on a sleeping service.
# The rebuild is therefore also checked on page views: the first visit of each UTC day rebuilds the example world,
# whether or not the hourly task got there first. Nothing here is reachable by a request parameter; it only ever
# rebuilds preview records, and only while the site is in preview mode.
import threading  # noqa: E402
import time  # noqa: E402

from foundation.jobs import every  # noqa: E402

RESET_KEY = "preview_reset_on"
CHECK_SECONDS = 60
_lock = threading.Lock()
_last_check = 0.0


def _today() -> str:
    from .program import today

    return today().isoformat()


def preview_is_stale() -> bool:
    from .models import SiteSetting
    from .program import is_preview

    if not is_preview():
        return False
    with SessionLocal() as s:
        row = s.get(SiteSetting, RESET_KEY)
        return (row.value if row else "") != _today()


def rebuild_if_stale(log=None) -> bool:
    """Rebuild the preview once per UTC day. Returns True when this call rebuilt it. Concurrent callers in this
    process do not wait: one rebuilds, the others carry on with the current world."""
    from .models import SiteSetting

    if not preview_is_stale():
        ensure_demo_world()
        return False
    if not _lock.acquire(blocking=False):
        return False
    try:
        if not preview_is_stale():  # another process may have just done it
            return False
        reset_demo_world()
        with SessionLocal() as s:
            row = s.get(SiteSetting, RESET_KEY)
            if row is None:
                s.add(SiteSetting(key=RESET_KEY, value=_today()))
            else:
                row.value = _today()
            s.commit()
        if log:
            log("preview world rebuilt")
        return True
    finally:
        _lock.release()


@every(hours=1)
def nightly_preview_reset(log) -> None:
    """Build the preview on first start, then rebuild it once a day so it always starts from the same story."""
    rebuild_if_stale(log)


_warm = False


def warm() -> None:
    """Called by the public pages. Checks at most once a minute per process whether today's rebuild is due."""
    global _warm, _last_check
    now = time.monotonic()
    if _warm and now - _last_check < CHECK_SECONDS:
        return
    _warm, _last_check = True, now
    try:
        rebuild_if_stale()
    except Exception:  # never let the preview break a public page
        _warm = False
