"""Cornerstone Associations (s.4): forming, enrolling, the founding roll, ballots decided by a majority of ALL
founding units, sealed rankings (s.5), opportunity choices, Founding Fund requests (s.8), quarterly reports and
requests to the supervisor (s.6)."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from foundation.auth_core import require_user
from foundation.db import SessionLocal, audit
from foundation.models import User

from ..models import (
    FUND_CATEGORIES, Association, Ballot, Claim, Compact, Consortium, DemoAccount, FundAdvance, Match, Membership, Offer,
    QuarterlyReport, Ranking, Restoration, Round, SupervisorDecision, Vote,
)
from ..program import association_priority, founding_units, is_preview, next_ref, roll_size, visible
from ..rules import (
    ENROLLMENT_DAYS, MAX_FOUNDING_UNITS, MIN_FOUNDING_UNITS, ballot_outcome, days_from, fund_remittance, state_share,
    votes_needed,
)
from .common import clean, go, page, preview_only, to_int

router = APIRouter()
OPPORTUNITIES = {"training": "Training for a new trade", "employment": "Employment in the enterprise",
                 "franchise": "A franchise or unit of my own", "owner_only": "Ownership only (a share of the enterprise)",
                 "undecided": "Not decided yet"}
SUPERVISION_KINDS = ("pricing", "access", "service", "safety", "exclusion")


def _assoc(s, ref: str) -> Association:
    a = s.scalar(visible(select(Association), Association).where(Association.ref == ref))
    if a is None:
        raise HTTPException(404, "No such association")
    return a


def _my_membership(s, a: Association, user: User) -> Membership | None:
    return s.scalar(select(Membership).where(Membership.association_id == a.id, Membership.user_id == user.id))


def _board(s, a: Association, user: User) -> Membership:
    m = _my_membership(s, a, user)
    if m is None or not m.on_board:
        raise HTTPException(403, "Only the association's board may do that.")
    return m


def _compact(s, a: Association) -> Compact | None:
    return s.scalar(select(Compact).where(Compact.association_id == a.id).order_by(Compact.id.desc()))


def tally(s, b: Ballot) -> dict:
    yes = s.scalar(select(func.count()).select_from(Vote).where(Vote.ballot_id == b.id, Vote.choice == "yes")) or 0
    no = s.scalar(select(func.count()).select_from(Vote).where(Vote.ballot_id == b.id, Vote.choice == "no")) or 0
    units = founding_units(s, b.association_id)
    return {"yes": yes, "no": no, "units": units, "needed": votes_needed(units), "outcome": ballot_outcome(yes, no, units)}


def settle_ballot(s, b: Ballot, closing: bool = False) -> str:
    """Apply a ballot's result once it is decided. A ballot still open at its close fails: the Act requires a
    majority of all founding units, so abstention counts against."""
    t = tally(s, b)
    outcome = t["outcome"]
    if outcome == "open" and closing:
        outcome = "failed"
    if outcome == "open" or b.status != "open":
        return b.status if b.status != "open" else "open"
    b.status = outcome
    if b.kind == "rankings":
        rk = s.scalar(select(Ranking).where(Ranking.ballot_id == b.id))
        if rk:
            rk.status = "submitted" if outcome == "passed" else "rejected"
            rk.submitted_on = date.today() if outcome == "passed" else None
    elif b.kind == "consortium":
        c = s.get(Consortium, (b.payload or {}).get("consortium_id"))
        if c is not None and c.status == "pending_votes":
            if outcome == "failed":
                c.status = "rejected"
            elif all(getattr(s.get(Ballot, i), "status", "") == "passed" for i in c.ballot_ids):
                c.status = "approved"
                c.units_snapshot = {str(i): founding_units(s, i) for i in c.association_ids}
    elif b.kind == "compact":
        k = s.get(Compact, (b.payload or {}).get("compact_id")) or s.scalar(select(Compact).where(Compact.ballot_id == b.id))
        c = s.get(Consortium, k.match.consortium_id) if k and k.match.consortium_id else None
        if k and c is not None and k.status == "member_vote":
            # s.4(c): a consortium's compact needs a majority of all founding units in EVERY constituent
            if outcome == "passed" and all(getattr(s.get(Ballot, i), "status", "") == "passed" for i in c.compact_ballot_ids):
                k.status, k.executed_on = "executed", date.today()
                k.operative_on = k.operative_on or date.today()
                k.offer.status, k.match.status = "compacted", "compacted"
                for aid in c.association_ids:
                    s.get(Association, aid).status = "compact"
                    if s.scalar(select(Restoration).where(Restoration.association_id == aid)) is None:
                        s.add(Restoration(association_id=aid, compact_id=k.id, checks={}, status="pending", is_demo=k.is_demo))
            elif outcome == "failed":
                k.status = "drafting"
                for i in c.compact_ballot_ids:
                    other = s.get(Ballot, i)
                    if other is not None and other.status == "open":
                        other.status = "failed"
        elif k:
            if outcome == "passed":
                k.status, k.executed_on = "executed", date.today()
                k.operative_on = k.operative_on or date.today()
                k.association.status = "compact"
                k.offer.status = "compacted"
                k.match.status = "compacted"
                if s.scalar(select(Restoration).where(Restoration.association_id == k.association_id)) is None:
                    s.add(Restoration(association_id=k.association_id, compact_id=k.id, checks={}, status="pending",
                                      is_demo=k.is_demo))
            else:
                k.status = "drafting"
    audit(s, "ballot.settle", None, target=str(b.id), outcome=outcome, **{k: v for k, v in t.items() if k != "outcome"})
    return outcome


# --------------------------------------------------------------------------
# Listing, forming, joining
# --------------------------------------------------------------------------
@router.get("/app/associations", response_class=HTMLResponse)
def associations(request: Request, user: User = Depends(require_user)):
    with SessionLocal() as s:
        rows = s.scalars(visible(select(Association), Association).order_by(Association.id)).all()
        my_claims = s.scalars(visible(select(Claim), Claim).where(Claim.user_id == user.id, Claim.status == "certified")).all()
        in_roll = set(s.scalars(select(Membership.claim_id).where(Membership.claim_id.in_([c.id for c in my_claims]))).all())
        free_claims = [c for c in my_claims if c.id not in in_roll]
        reviewing = s.scalars(visible(select(Claim), Claim).where(Claim.user_id == user.id, Claim.status.in_(UNDER_REVIEW))).all()
        on_roll = set(s.scalars(select(Membership.claim_id).where(Membership.claim_id.in_([c.id for c in reviewing])))
                      .all()) if reviewing else set()
        cohort_claims = [c for c in reviewing if c.id not in on_roll]
        data = [{"a": a, "units": founding_units(s, a.id)} for a in rows]
        return page(request, "assoc/list.html", {"rows": data, "free_claims": free_claims, "cohort_claims": cohort_claims})


@router.post("/app/associations/new")
def association_new(request: Request, name: str = Form(...), principal_state: str = Form(...), purpose: str = Form(""),
                    claim_ref: str = Form(...), user: User = Depends(require_user)):
    with SessionLocal() as s:
        c = s.scalar(visible(select(Claim), Claim).where(Claim.ref == claim_ref, Claim.user_id == user.id,
                                                        Claim.status == "certified"))
        if c is None:
            raise HTTPException(400, "Forming an association requires your own certified claim.")
        if s.scalar(select(Membership).where(Membership.claim_id == c.id)):
            raise HTTPException(400, "That claim is already on a founding roll. A claim may be on one roll at a time.")
        t = date.today()
        a = Association(ref=next_ref(s, Association, "A", 3), name=clean(name, 300), principal_state=clean(principal_state, 80),
                        purpose=clean(purpose), status="chartering", enrollment_opens=t,
                        enrollment_closes=days_from(t, ENROLLMENT_DAYS), created_by=user.id, is_demo=is_preview())
        s.add(a)
        s.flush()
        s.add(Membership(association_id=a.id, claim_id=c.id, user_id=user.id, on_board=True))
        audit(s, "association.form", user.id, target=a.ref)
        s.commit()
        ref = a.ref
    return go(f"/app/associations/{ref}", f"{ref} is filed with Labor for its charter. Enrollment opens for "
                                          f"{ENROLLMENT_DAYS} days once it is chartered.")


UNDER_REVIEW = ("filed", "hearing_requested")


@router.post("/app/associations/{ref}/join")
def association_join(request: Request, ref: str, claim_ref: str = Form(...), user: User = Depends(require_user)):
    """A certified claim joins as a founding unit. A timely applicant whose claim is still under review may elect this
    association as a cohort (s.4(a)); the claim becomes a unit only if certified before the roll closes."""
    with SessionLocal() as s:
        a = _assoc(s, ref)
        if a.status != "enrolling":
            raise HTTPException(400, "This association is not enrolling.")
        c = s.scalar(visible(select(Claim), Claim).where(Claim.ref == claim_ref, Claim.user_id == user.id,
                                                        Claim.status.in_(("certified",) + UNDER_REVIEW)))
        if c is None:
            raise HTTPException(400, "Joining requires your own claim, certified or still under review.")
        if s.scalar(select(Membership).where(Membership.claim_id == c.id)):
            raise HTTPException(400, "That claim is already on a founding roll.")
        if roll_size(s, a.id) >= MAX_FOUNDING_UNITS:
            raise HTTPException(400, f"The founding roll is full at {MAX_FOUNDING_UNITS:,} units.")
        status = "founding" if c.status == "certified" else "cohort"
        s.add(Membership(association_id=a.id, claim_id=c.id, user_id=user.id, status=status))
        audit(s, "association.join", user.id, target=ref, claim=c.ref, status=status)
        s.commit()
    if status == "cohort":
        return go(f"/app/associations/{ref}", "You have elected this association while your claim is under review. You hold a "
                                              "founding unit, with a vote, once Labor certifies the claim; the roll cannot "
                                              "close until it is decided.")
    return go(f"/app/associations/{ref}", "You hold one founding unit. It cannot be sold, pledged or transferred.")


# --------------------------------------------------------------------------
# The association's own page
# --------------------------------------------------------------------------
@router.get("/app/associations/{ref}", response_class=HTMLResponse)
def association_page(request: Request, ref: str, user: User = Depends(require_user)):
    with SessionLocal() as s:
        a = _assoc(s, ref)
        me = _my_membership(s, a, user)
        members = s.scalars(select(Membership).where(Membership.association_id == a.id).order_by(Membership.id)).all()
        ballots = s.scalars(select(Ballot).where(Ballot.association_id == a.id).order_by(Ballot.id.desc())).all()
        ballot_rows = []
        for b in ballots:
            mine = s.scalar(select(Vote).where(Vote.ballot_id == b.id, Vote.membership_id == me.id)) if me else None
            ballot_rows.append({"b": b, "t": tally(s, b), "mine": mine})
        open_rounds = s.scalars(visible(select(Round), Round).where(Round.status == "open").order_by(Round.id)).all()
        round_offers = {r.id: s.scalars(select(Offer).where(Offer.round_id == r.id, Offer.status == "published")
                                        .order_by(Offer.ref)).all() for r in open_rounds}
        rankings = s.scalars(select(Ranking).where(Ranking.association_id == a.id).order_by(Ranking.id.desc())).all()
        offer_refs = {o.id: o for o in s.scalars(visible(select(Offer), Offer)).all()}
        round_names = {r.id: r.name for r in s.scalars(visible(select(Round), Round)).all()}
        matches = s.scalars(select(Match).where(Match.association_id == a.id).order_by(Match.id.desc())).all()
        compact = _compact(s, a)
        reports = s.scalars(select(QuarterlyReport).where(QuarterlyReport.compact_id == compact.id)
                            .order_by(QuarterlyReport.quarter)).all() if compact else []
        sup = s.scalars(select(SupervisorDecision).where(SupervisorDecision.compact_id == compact.id)
                        .order_by(SupervisorDecision.id.desc())).all() if compact else []
        advances = s.scalars(select(FundAdvance).where(FundAdvance.association_id == a.id).order_by(FundAdvance.id.desc())).all()
        restoration = s.scalar(select(Restoration).where(Restoration.association_id == a.id))
        opp_counts = dict(s.execute(select(Membership.opportunity, func.count()).where(Membership.association_id == a.id)
                                    .group_by(Membership.opportunity)).all())
        repaid = sum(r.fund_remittance for r in reports)
        seed_ids = set(s.scalars(select(DemoAccount.user_id).where(DemoAccount.persona == "seed")).all())
        consortia = [c for c in s.scalars(visible(select(Consortium), Consortium).order_by(Consortium.id.desc())).all()
                     if a.id in (c.association_ids or [])]
        assoc_refs = {x.id: x.ref for x in s.scalars(visible(select(Association), Association)).all()}
        cohort = [m for m in members if m.status == "cohort"]
        return page(request, "assoc/detail.html", {
            "consortia": consortia, "assoc_refs": assoc_refs, "cohort": cohort,
            "a": a, "member": me, "members": members, "units": len(members) - len(cohort), "ballots": ballot_rows,
            "open_rounds": open_rounds, "round_offers": round_offers, "rankings": rankings, "offer_refs": offer_refs, "round_names": round_names,
            "matches": matches, "compact": compact, "reports": reports, "sup": sup, "advances": advances,
            "restoration": restoration, "opportunities": OPPORTUNITIES, "opp_counts": opp_counts,
            "fund_categories": FUND_CATEGORIES, "supervision_kinds": SUPERVISION_KINDS, "repaid": repaid,
            "priority": association_priority(s, a.id), "min_units": MIN_FOUNDING_UNITS, "max_units": MAX_FOUNDING_UNITS,
            "today": date.today(), "seed_ids": seed_ids})


@router.post("/app/associations/{ref}/close-roll")
def close_roll(request: Request, ref: str, now: str = Form(""), user: User = Depends(require_user)):
    with SessionLocal() as s:
        a = _assoc(s, ref)
        _board(s, a, user)
        if a.status != "enrolling":
            raise HTTPException(400, "Only an enrolling association can close its roll.")
        if date.today() < a.enrollment_closes:
            preview_only()
            a.enrollment_closes = date.today()
        # s.4(a): a cohort's roll must be resolved before it closes
        cohort = s.scalars(select(Membership).where(Membership.association_id == a.id, Membership.status == "cohort")).all()
        pending = [m.claim.ref for m in cohort if m.claim.status in UNDER_REVIEW]
        if pending:
            raise HTTPException(400, "The roll cannot close while claims on it are still under review: " + ", ".join(pending))
        for m in cohort:
            if m.claim.status == "certified":
                m.status = "founding"
            else:
                audit(s, "association.cohort_removed", user.id, target=ref, claim=m.claim.ref, claim_status=m.claim.status)
                s.delete(m)
        s.flush()
        if founding_units(s, a.id) < MIN_FOUNDING_UNITS:
            raise HTTPException(400, f"A founding roll needs at least {MIN_FOUNDING_UNITS} units.")
        a.status = "roll_closed"
        audit(s, "association.close_roll", user.id, target=ref, units=founding_units(s, a.id))
        s.commit()
    return go(f"/app/associations/{ref}", "The founding roll is closed and published. The association may now rank offers.")


@router.post("/app/associations/{ref}/opportunity")
def choose_opportunity(request: Request, ref: str, opportunity: str = Form(...), user: User = Depends(require_user)):
    if opportunity not in OPPORTUNITIES:
        raise HTTPException(400)
    with SessionLocal() as s:
        a = _assoc(s, ref)
        m = _my_membership(s, a, user)
        if m is None:
            raise HTTPException(403, "Only members choose an opportunity.")
        m.opportunity = opportunity
        audit(s, "membership.opportunity", user.id, target=ref, choice=opportunity)
        s.commit()
    return go(f"/app/associations/{ref}#opportunity", "Your choice is recorded. Your founding unit is the same whatever you choose.")


@router.post("/app/associations/{ref}/rankings")
async def propose_rankings(request: Request, ref: str, user: User = Depends(require_user)):
    form = await request.form()
    round_id = to_int(form.get("round_id"))
    with SessionLocal() as s:
        a = _assoc(s, ref)
        _board(s, a, user)
        if a.status != "roll_closed":
            raise HTTPException(400, "Only an association with a closed founding roll may rank offers.")
        r = s.get(Round, round_id)
        if r is None or r.is_demo != is_preview() or r.status != "open":
            raise HTTPException(400, "Choose an open round.")
        published = {o.ref: o for o in s.scalars(select(Offer).where(Offer.round_id == r.id, Offer.status == "published")).all()}
        chosen: list[Offer] = []
        for i in range(1, 6):
            v = str(form.get(f"rank{i}", "")).strip()
            if v and v in published and published[v] not in chosen:
                chosen.append(published[v])
        if not chosen:
            raise HTTPException(400, "Rank at least one offer.")
        if s.scalar(select(Ranking).where(Ranking.round_id == r.id, Ranking.association_id == a.id,
                                          Ranking.status.in_(["pending_vote", "submitted"]))):
            raise HTTPException(400, "This association already has a ranking for this round.")
        if _consortium_in_round(s, a.id, r.id):
            raise HTTPException(400, "This association is entering this round as part of a consortium.")
        desc = "Ranking, most preferred first: " + "; then ".join(f"{o.ref} {o.trade} ({o.district}, {o.state_name})" for o in chosen)
        b = Ballot(association_id=a.id, kind="rankings", title=f"Approve our sealed ranking for {r.name}",
                   description=desc + ". Passing requires a majority of ALL founding units, not just of those who vote.",
                   payload={"round_id": r.id, "offer_ids": [o.id for o in chosen]}, closes_on=r.rankings_close,
                   created_by=user.id, is_demo=is_preview())
        s.add(b)
        s.flush()
        s.add(Ranking(round_id=r.id, association_id=a.id, offer_ids=[o.id for o in chosen], status="pending_vote",
                      ballot_id=b.id, is_demo=is_preview()))
        audit(s, "ranking.propose", user.id, target=ref, round=r.id)
        s.commit()
    return go(f"/app/associations/{ref}#ballots", "The ranking is sent to the members. It stays sealed from everyone outside "
                                                  "the association until the round closes.")


def _consortium_in_round(s, association_id: int, round_id: int) -> Consortium | None:
    for c in s.scalars(select(Consortium).where(Consortium.round_id == round_id,
                                                Consortium.status.in_(["pending_votes", "approved", "matched"]))).all():
        if association_id in (c.association_ids or []):
            return c
    return None


@router.post("/app/associations/{ref}/consortium")
async def propose_consortium(request: Request, ref: str, user: User = Depends(require_user)):
    """s.5(b): the board proposes entering a round together with other associations. Each constituent's members must
    approve by a majority of all its founding units (s.4(c)); rolls must stay unchanged; combined units must fit every
    ranked offer; each constituent keeps its own governance."""
    form = await request.form()
    with SessionLocal() as s:
        a = _assoc(s, ref)
        _board(s, a, user)
        r = s.get(Round, to_int(form.get("round_id")))
        if r is None or r.is_demo != is_preview() or r.status != "open":
            raise HTTPException(400, "Choose an open round.")
        refs = [x.strip().upper() for x in str(form.get("partners", "")).replace(";", ",").split(",") if x.strip()]
        partners = []
        for pref in refs:
            p = s.scalar(visible(select(Association), Association).where(Association.ref == pref))
            if p is None or p.id == a.id or p in partners:
                raise HTTPException(400, f"{pref} is not another association.")
            partners.append(p)
        if not partners:
            raise HTTPException(400, "Name at least one other association.")
        members = [a] + partners
        for x in members:
            if x.status != "roll_closed":
                raise HTTPException(400, f"{x.ref} does not have a closed founding roll free to be matched.")
            if s.scalar(select(Ranking).where(Ranking.round_id == r.id, Ranking.association_id == x.id,
                                              Ranking.status.in_(["pending_vote", "submitted"]))) or _consortium_in_round(s, x.id, r.id):
                raise HTTPException(400, f"{x.ref} has already entered this round.")
        published = {o.ref: o for o in s.scalars(select(Offer).where(Offer.round_id == r.id, Offer.status == "published")).all()}
        chosen: list[Offer] = []
        for i in range(1, 6):
            v = str(form.get(f"rank{i}", "")).strip()
            if v and v in published and published[v] not in chosen:
                chosen.append(published[v])
        if not chosen:
            raise HTTPException(400, "Rank at least one offer.")
        units = {x.id: founding_units(s, x.id) for x in members}
        total = sum(units.values())
        too_small = [o.ref for o in chosen if total > o.max_founding_units]
        if too_small:
            raise HTTPException(400, f"The consortium's {total:,} founding units exceed what these offers can serve: "
                                     + ", ".join(too_small))
        c = Consortium(ref=next_ref(s, Consortium, "CN", 3), name=clean(str(form.get("name", "")), 300) or
                       " + ".join(x.ref for x in members), round_id=r.id, association_ids=[x.id for x in members],
                       offer_ids=[o.id for o in chosen], units_snapshot={str(k): v for k, v in units.items()},
                       created_by=user.id, is_demo=is_preview())
        s.add(c)
        s.flush()
        desc = (f"Enter {r.name} as consortium {c.ref} with " + ", ".join(x.ref + " " + x.name for x in members if x.id != a.id)
                + ". Joint ranking, most preferred first: " + "; then ".join(f"{o.ref} {o.trade} ({o.district})" for o in chosen)
                + f". Combined founding units: {total:,}. Each association keeps its own governance; any compact needs "
                  "the approval of a majority of all founding units in every association.")
        ids = []
        for x in members:
            b = Ballot(association_id=x.id, kind="consortium", title=f"Approve consortium {c.ref} for {r.name}",
                       description=desc, payload={"consortium_id": c.id}, closes_on=r.rankings_close, created_by=user.id,
                       is_demo=is_preview())
            s.add(b)
            s.flush()
            ids.append(b.id)
        c.ballot_ids = ids
        audit(s, "consortium.propose", user.id, target=c.ref, associations=[x.ref for x in members])
        s.commit()
        cref = c.ref
    return go(f"/app/associations/{ref}#ballots", f"Consortium {cref} is proposed. It enters the round only if the members of "
                                                  "every association approve it, and only if no founding roll changes.")


# --------------------------------------------------------------------------
# Ballots
# --------------------------------------------------------------------------
def _ballot(s, ballot_id: int) -> Ballot:
    b = s.get(Ballot, ballot_id)
    if b is None or b.is_demo != is_preview():
        raise HTTPException(404)
    return b


@router.post("/app/ballots/{ballot_id}/vote")
def vote(request: Request, ballot_id: int, choice: str = Form(...), user: User = Depends(require_user)):
    if choice not in {"yes", "no"}:
        raise HTTPException(400)
    with SessionLocal() as s:
        b = _ballot(s, ballot_id)
        m = _my_membership(s, b.association, user)
        if m is None or m.status != "founding":
            raise HTTPException(403, "Only founding unit holders vote.")
        if b.status != "open":
            raise HTTPException(400, "This ballot is closed.")
        if s.scalar(select(Vote).where(Vote.ballot_id == b.id, Vote.membership_id == m.id)):
            raise HTTPException(400, "You have already voted on this ballot.")
        s.add(Vote(ballot_id=b.id, membership_id=m.id, choice=choice))
        s.flush()
        outcome = settle_ballot(s, b)
        audit(s, "ballot.vote", user.id, target=str(b.id))
        s.commit()
        ref = b.association.ref
    msg = "Your vote is counted." + (f" The ballot has {outcome}." if outcome != "open" else "")
    return go(f"/app/associations/{ref}#ballots", msg)


@router.post("/app/ballots/{ballot_id}/simulate")
def simulate_votes(request: Request, ballot_id: int, choice: str = Form("yes"), user: User = Depends(require_user)):
    """Preview only: the other example members vote, so a visitor can see a ballot decided."""
    preview_only()
    with SessionLocal() as s:
        b = _ballot(s, ballot_id)
        if _my_membership(s, b.association, user) is None and user.role != "admin":
            raise HTTPException(403)
        seed_ids = set(s.scalars(select(DemoAccount.user_id).where(DemoAccount.persona == "seed")).all())
        voted = set(s.scalars(select(Vote.membership_id).where(Vote.ballot_id == b.id)).all())
        for m in s.scalars(select(Membership).where(Membership.association_id == b.association_id)).all():
            if m.user_id in seed_ids and m.id not in voted and b.status == "open" and m.status == "founding":
                s.add(Vote(ballot_id=b.id, membership_id=m.id, choice="yes" if choice == "yes" else "no"))
                s.flush()
                settle_ballot(s, b)
        s.commit()
        ref, status = b.association.ref, b.status
    return go(f"/app/associations/{ref}#ballots", f"The example members have voted. The ballot is {status}.")


@router.post("/app/ballots/{ballot_id}/close")
def close_ballot(request: Request, ballot_id: int, user: User = Depends(require_user)):
    with SessionLocal() as s:
        b = _ballot(s, ballot_id)
        _board(s, b.association, user)
        if b.status != "open":
            raise HTTPException(400, "This ballot is already closed.")
        if date.today() < b.closes_on:
            preview_only()
        outcome = settle_ballot(s, b, closing=True)
        s.commit()
        ref = b.association.ref
    return go(f"/app/associations/{ref}#ballots", f"Voting is closed. The ballot {outcome}.")


# --------------------------------------------------------------------------
# Section 8: Founding Fund requests; section 6: reports and supervision
# --------------------------------------------------------------------------
@router.post("/app/associations/{ref}/fund")
def request_advance(request: Request, ref: str, category: str = Form(...), amount: str = Form(...), provider: str = Form(""),
                    description: str = Form(""), user: User = Depends(require_user)):
    if category not in FUND_CATEGORIES:
        raise HTTPException(400)
    with SessionLocal() as s:
        a = _assoc(s, ref)
        _board(s, a, user)
        amt = to_int(amount)
        if amt <= 0:
            raise HTTPException(400, "Enter the amount requested.")
        s.add(FundAdvance(association_id=a.id, category=category, amount=amt, provider=clean(provider, 200),
                          description=clean(description), requested_by=user.id, is_demo=is_preview()))
        audit(s, "fund.request", user.id, target=ref, category=category, amount=amt)
        s.commit()
    return go(f"/app/associations/{ref}#fund", "The request is sent to Labor. Advances are paid to the provider, never to members.")


@router.post("/app/associations/{ref}/reports")
def quarterly_report(request: Request, ref: str, quarter: str = Form(...), operating_receipts: str = Form(...),
                     shareable_surplus: str = Form("0"), user: User = Depends(require_user)):
    import re

    if not re.fullmatch(r"\d{4}-Q[1-4]", quarter.strip()):
        raise HTTPException(400, "Write the quarter like 2029-Q3.")
    with SessionLocal() as s:
        a = _assoc(s, ref)
        _board(s, a, user)
        k = _compact(s, a)
        if k is None or k.status != "executed":
            raise HTTPException(400, "Reports begin once a compact is executed.")
        if s.scalar(select(QuarterlyReport).where(QuarterlyReport.compact_id == k.id, QuarterlyReport.quarter == quarter.strip())):
            raise HTTPException(400, "That quarter has already been reported.")
        paid = sum(r.fund_remittance for r in s.scalars(select(QuarterlyReport).where(QuarterlyReport.compact_id == k.id)).all())
        receipts, surplus = to_int(operating_receipts), to_int(shareable_surplus)
        rem = fund_remittance(receipts, k.fund_rate_bp, max(0, k.fund_max_obligation - paid))
        share = state_share(surplus, k.surplus_rate_pct)
        s.add(QuarterlyReport(compact_id=k.id, quarter=quarter.strip(), operating_receipts=receipts, shareable_surplus=surplus,
                              fund_remittance=rem, state_share=share, submitted_by=user.id, is_demo=is_preview()))
        audit(s, "compact.report", user.id, target=k.ref, quarter=quarter, remittance=rem, state_share=share)
        s.commit()
    return go(f"/app/associations/{ref}#reports", f"Reported. Fund remittance ${rem:,}; State share ${share:,}.")


@router.post("/app/associations/{ref}/supervision")
def supervision_request(request: Request, ref: str, kind: str = Form(...), body: str = Form(...),
                        user: User = Depends(require_user)):
    if kind not in SUPERVISION_KINDS:
        raise HTTPException(400)
    with SessionLocal() as s:
        a = _assoc(s, ref)
        _board(s, a, user)
        k = _compact(s, a)
        if k is None or k.status != "executed":
            raise HTTPException(400, "Supervision begins once a compact is executed.")
        s.add(SupervisorDecision(compact_id=k.id, kind=kind, request=clean(body), requested_by=user.id, is_demo=is_preview()))
        audit(s, "supervision.request", user.id, target=k.ref, kind=kind)
        s.commit()
    return go(f"/app/associations/{ref}#supervision", "Sent to the independent supervisor. The decision will be public.")
