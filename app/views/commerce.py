"""The Department of Commerce Exchange (s.5): verifying offers, opening rounds with a public random seed,
publishing offers, and running the published matching rule."""
from __future__ import annotations

import re
import secrets
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from foundation.db import SessionLocal, audit
from foundation.models import User

from ..models import Association, Ballot, Match, Offer, Ranking, Round
from ..program import association_priority, is_preview, require_program_role, visible
from ..rules import NEGOTIATION_DAYS, OFFER_PUBLIC_DAYS, Applicant, days_from, run_matching
from .association import settle_ballot
from .common import clean, go, page, parse_date, preview_only, to_int

router = APIRouter(prefix="/commerce")
commerce = require_program_role("commerce")


@router.get("", response_class=HTMLResponse)
def desk(request: Request, user: User = Depends(commerce)):
    with SessionLocal() as s:
        submitted = s.scalars(visible(select(Offer), Offer).where(Offer.status == "submitted").order_by(Offer.id)).all()
        verified = s.scalars(visible(select(Offer), Offer).where(Offer.status == "verified").order_by(Offer.id)).all()
        rounds = s.scalars(visible(select(Round), Round).order_by(Round.id.desc())).all()
        counts = {r.id: {"offers": len([o for o in r.offers if o.status in ("published", "matched", "compacted")]),
                         "rankings": len(s.scalars(select(Ranking.id).where(Ranking.round_id == r.id,
                                                                             Ranking.status == "submitted")).all()),
                         "pending": len(s.scalars(select(Ranking.id).where(Ranking.round_id == r.id,
                                                                            Ranking.status == "pending_vote")).all())}
                  for r in rounds}
        return page(request, "commerce/desk.html", {"submitted": submitted, "verified": verified, "rounds": rounds,
                                                     "counts": counts, "today": date.today(),
                                                     "min_close": days_from(date.today(), OFFER_PUBLIC_DAYS)})


@router.post("/offers/{ref}/verify")
def verify(request: Request, ref: str, decision: str = Form(...), notes: str = Form(""), user: User = Depends(commerce)):
    with SessionLocal() as s:
        o = s.scalar(visible(select(Offer), Offer).where(Offer.ref == ref, Offer.status == "submitted"))
        if o is None:
            raise HTTPException(404)
        if decision == "verified":
            o.status = "verified"
        elif decision == "returned":
            if not notes.strip():
                raise HTTPException(400, "Say what is missing.")
            o.status = "draft"
        else:
            raise HTTPException(400)
        o.verification_notes = clean(notes)
        audit(s, f"offer.{decision}", user.id, target=ref)
        s.commit()
    return go("/commerce", f"{ref} {'is verified complete' if decision == 'verified' else 'is returned to the State'}.")


@router.post("/rounds/new")
def new_round(request: Request, name: str = Form(...), rankings_close: str = Form(...), user: User = Depends(commerce)):
    close = parse_date(rankings_close, "date rankings close")
    if close < days_from(date.today(), OFFER_PUBLIC_DAYS) and not is_preview():
        raise HTTPException(400, f"Offers must be public at least {OFFER_PUBLIC_DAYS} days before rankings close.")
    with SessionLocal() as s:
        r = Round(name=clean(name, 200), rankings_close=close, random_seed=secrets.token_hex(16), created_by=user.id,
                  is_demo=is_preview())
        s.add(r)
        audit(s, "round.open", user.id, target=r.name, seed=r.random_seed)
        s.commit()
    return go("/commerce#rounds", "Round opened. Its random seed is fixed and published now, before any ranking is seen.")


@router.post("/offers/{ref}/publish")
def publish(request: Request, ref: str, round_id: str = Form(...), user: User = Depends(commerce)):
    with SessionLocal() as s:
        o = s.scalar(visible(select(Offer), Offer).where(Offer.ref == ref, Offer.status == "verified"))
        r = s.get(Round, to_int(round_id))
        if o is None or r is None or r.is_demo != is_preview() or r.status != "open":
            raise HTTPException(400, "Choose a verified offer and an open round.")
        if (r.rankings_close - date.today()).days < OFFER_PUBLIC_DAYS and not is_preview():
            raise HTTPException(400, f"Too late for this round: offers must be public {OFFER_PUBLIC_DAYS} days before rankings close.")
        o.status, o.round_id, o.published_on = "published", r.id, date.today()
        audit(s, "offer.publish", user.id, target=ref, round=r.id)
        s.commit()
    return go("/commerce", f"{ref} is published on the Exchange. It cannot change now without withdrawal and republication.")


def _readable(line: str, assoc_refs: dict[int, str], offer_refs: dict[int, str]) -> str:
    line = re.sub(r"Association (\d+)", lambda m: assoc_refs.get(int(m.group(1)), m.group(0)), line)
    line = re.sub(r"offer (\d+)", lambda m: "offer " + offer_refs.get(int(m.group(1)), m.group(1)), line)
    return re.sub(r"(?<=[;,] )(\d+)(?= moves)", lambda m: assoc_refs.get(int(m.group(1)), m.group(1)), line)


@router.post("/rounds/{round_id}/run")
def run_round(request: Request, round_id: int, user: User = Depends(commerce)):
    with SessionLocal() as s:
        r = s.get(Round, round_id)
        if r is None or r.is_demo != is_preview() or r.status != "open":
            raise HTTPException(400, "Choose an open round.")
        if date.today() < r.rankings_close:
            preview_only()
            r.rankings_close = date.today()
        # Ballots still open when rankings close are decided now; unapproved rankings are not entered.
        for rk in s.scalars(select(Ranking).where(Ranking.round_id == r.id, Ranking.status == "pending_vote")).all():
            b = s.get(Ballot, rk.ballot_id) if rk.ballot_id else None
            if b is not None and b.status == "open":
                settle_ballot(s, b, closing=True)
            elif b is None:
                rk.status = "rejected"
        s.flush()
        offers = s.scalars(select(Offer).where(Offer.round_id == r.id, Offer.status == "published")).all()
        applicants, assoc_refs = [], {}
        for rk in s.scalars(select(Ranking).where(Ranking.round_id == r.id, Ranking.status == "submitted")).all():
            a = rk.association
            p = association_priority(s, a.id)
            if a.status != "roll_closed" or p is None:
                continue
            applicants.append(Applicant(association_id=a.id, priority_date=p, ranking=list(rk.offer_ids)))
            assoc_refs[a.id] = a.ref
        held, log = run_matching(applicants, [o.id for o in offers], r.random_seed)
        offer_refs = {o.id: o.ref for o in offers}
        pri = {a.association_id: a.priority_date for a in applicants}
        for offer_id, assoc_id in held.items():
            s.add(Match(round_id=r.id, association_id=assoc_id, offer_id=offer_id, priority_date=pri[assoc_id],
                        negotiation_deadline=days_from(date.today(), NEGOTIATION_DAYS), is_demo=r.is_demo))
            s.get(Offer, offer_id).status = "matched"
            s.get(Association, assoc_id).status = "matched"
        for o in offers:
            if o.id not in held:
                o.status = "verified"  # unmatched offers return to the Exchange for the next round
        r.status, r.matched_on = "matched", date.today()
        r.match_log = [_readable(x, assoc_refs, offer_refs) for x in log] or ["No rankings were entered."]
        audit(s, "round.run", user.id, target=str(r.id), matches=len(held), applicants=len(applicants))
        s.commit()
    return go(f"/exchange/rounds/{round_id}", f"Matching is complete: {len(held)} match(es). Every ranking, priority date, "
                                              "draw number and step is now public.")
