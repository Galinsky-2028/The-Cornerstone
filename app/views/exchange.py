"""The public Exchange (section 5(a)): certified events and petitions, closed founding rolls without private
identifiers, offers, matching rules and results, compacts, supervision decisions and audit summaries."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from foundation.db import SessionLocal
from .common import page

from ..models import (
    Association, Claim, Compact, Event, Match, Membership, Offer, QuarterlyReport, Ranking, Round, SupervisorDecision,
)
from ..program import association_priority, founding_units, is_preview, visible
from ..rules import tiebreak_key

router = APIRouter()


@router.get("/exchange", response_class=HTMLResponse)
def exchange(request: Request):
    from ..seed import warm

    warm()
    with SessionLocal() as s:
        events = s.scalars(visible(select(Event), Event).order_by(Event.id.desc())).all()
        claims_by_event = dict(s.execute(visible(select(Claim.event_id, func.count()), Claim)
                                         .where(Claim.status.in_(["certified", "remedied"])).group_by(Claim.event_id)).all())
        assocs = s.scalars(visible(select(Association), Association).where(Association.status != "chartering")
                           .order_by(Association.id)).all()
        assoc_rows = [{"a": a, "units": founding_units(s, a.id), "priority": association_priority(s, a.id),
                       "events": sorted({m.claim.event.ref for m in a.members})} for a in assocs]
        offers = s.scalars(visible(select(Offer), Offer).where(Offer.status.in_(
            ["objection_period", "published", "matched", "compacted"])).order_by(Offer.id)).all()
        rounds = s.scalars(visible(select(Round), Round).order_by(Round.id.desc())).all()
        compacts = s.scalars(visible(select(Compact), Compact).where(Compact.status == "executed")).all()
        decisions = s.scalars(visible(select(SupervisorDecision), SupervisorDecision)
                              .where(SupervisorDecision.decision != "pending").order_by(SupervisorDecision.id.desc()).limit(20)).all()
        reports = s.scalars(visible(select(QuarterlyReport), QuarterlyReport).order_by(QuarterlyReport.id.desc()).limit(20)).all()
        compact_refs = {c.id: c.ref for c in s.scalars(select(Compact)).all()}
        return page(request, "exchange/index.html", {
            "events": events, "claims_by_event": claims_by_event, "assocs": assoc_rows, "offers": offers,
            "rounds": rounds, "compacts": compacts, "decisions": decisions, "reports": reports, "compact_refs": compact_refs})


@router.get("/exchange/events/{ref}", response_class=HTMLResponse)
def event_public(request: Request, ref: str):
    with SessionLocal() as s:
        e = s.scalar(visible(select(Event), Event).where(Event.ref == ref))
        if e is None:
            raise HTTPException(404, "No such event in the register")
        comments = list(e.comments)
        certified = s.scalar(select(func.count()).select_from(Claim).where(
            Claim.event_id == e.id, Claim.status.in_(["certified", "remedied"]))) or 0
        return page(request, "exchange/event.html", {"e": e, "comments": comments, "certified": certified})


@router.get("/exchange/offers/{ref}", response_class=HTMLResponse)
def offer_public(request: Request, ref: str):
    with SessionLocal() as s:
        o = s.scalar(visible(select(Offer), Offer).where(Offer.ref == ref, Offer.status != "draft"))
        if o is None:
            raise HTTPException(404, "No such published offer")
        return page(request, "exchange/offer.html", {"o": o, "objections": list(o.objections)})


@router.get("/exchange/rounds/{round_id}", response_class=HTMLResponse)
def round_public(request: Request, round_id: int):
    """After the deadline, everything needed to reproduce the match is published: every ranking, every priority
    date, the random seed and the step-by-step log."""
    with SessionLocal() as s:
        r = s.get(Round, round_id)
        if r is None or r.is_demo != is_preview():
            raise HTTPException(404)
        offers = s.scalars(select(Offer).where(Offer.round_id == r.id)).all()
        sealed = r.status != "matched"
        rankings = []
        if not sealed:
            for rk in s.scalars(select(Ranking).where(Ranking.round_id == r.id, Ranking.status == "submitted")).all():
                rankings.append({"a": rk.association, "offers": [s.get(Offer, o).ref for o in rk.offer_ids if s.get(Offer, o)],
                                 "priority": association_priority(s, rk.association_id),
                                 "draw": tiebreak_key(r.random_seed, rk.association_id)[:12]})
        submitted = s.scalar(select(func.count()).select_from(Ranking).where(
            Ranking.round_id == r.id, Ranking.status == "submitted")) or 0
        matches = s.scalars(select(Match).where(Match.round_id == r.id)).all()
        return page(request, "exchange/round.html", {"r": r, "offers": offers, "sealed": sealed, "rankings": rankings,
                                                       "submitted": submitted, "matches": matches})


@router.get("/exchange/compacts/{ref}", response_class=HTMLResponse)
def compact_public(request: Request, ref: str):
    from ..rules import compact_dates

    with SessionLocal() as s:
        c = s.scalar(visible(select(Compact), Compact).where(Compact.ref == ref, Compact.status == "executed"))
        if c is None:
            raise HTTPException(404)
        decisions = s.scalars(select(SupervisorDecision).where(SupervisorDecision.compact_id == c.id)
                              .order_by(SupervisorDecision.id.desc())).all()
        reports = s.scalars(select(QuarterlyReport).where(QuarterlyReport.compact_id == c.id)
                            .order_by(QuarterlyReport.quarter)).all()
        repaid = sum(r.fund_remittance for r in reports)
        dates = compact_dates(c.operative_on) if c.operative_on else None
        return page(request, "exchange/compact.html", {"c": c, "decisions": decisions, "reports": reports,
                                                         "repaid": repaid, "dates": dates,
                                                         "units": founding_units(s, c.association_id)})
