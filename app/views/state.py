"""State officials (s.5 and the Model State Enabling Act): offers, the no-new-harm record and objections,
submission to the Exchange, and compact negotiation with the matched Association (s.6)."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from foundation.auth_core import require_user
from foundation.db import SessionLocal, audit
from foundation.models import User

from ..models import Ballot, Compact, FundAdvance, Match, Offer, OfferObjection
from ..program import is_preview, next_ref, require_program_role, state_of, visible
from ..rules import (
    DRAFT_COMPACT_PUBLIC_DAYS, NEGOTIATION_DAYS, OBJECTION_DAYS, bundle_problems, days_from, fund_terms, tiebreak_key,
)
from .common import clean, go, page, parse_date, preview_only, to_int

router = APIRouter()
official = require_program_role("state")
OFFER_FIELDS = ("district", "trade", "prohibition", "lifting_authority", "enabling_law", "technical_requirements",
                "obligations", "supervisor", "negotiable_terms", "no_new_harm", "federal_authorization",
                "bundle_districts", "bundle_finding", "bundle_finder")
REQUIRED_TO_PUBLISH = ("district", "trade", "prohibition", "lifting_authority", "enabling_law", "technical_requirements",
                       "obligations", "supervisor", "negotiable_terms")


def _scope(user: User) -> str:
    """The State an official acts for. Administrators without a State grant see every State."""
    return state_of(user, "state")


def _offer(s, ref: str, user: User) -> Offer:
    o = s.scalar(visible(select(Offer), Offer).where(Offer.ref == ref))
    st = _scope(user)
    if o is None or (st and o.state_name != st):
        raise HTTPException(404, "No such offer for your State")
    return o


@router.get("/state", response_class=HTMLResponse)
def desk(request: Request, user: User = Depends(official)):
    st = _scope(user)
    with SessionLocal() as s:
        q = visible(select(Offer), Offer).order_by(Offer.id.desc())
        offers = s.scalars(q.where(Offer.state_name == st) if st else q).all()
        ids = [o.id for o in offers]
        matches = s.scalars(select(Match).where(Match.offer_id.in_(ids)).order_by(Match.id.desc())).all() if ids else []
        compacts = {k.match_id: k for k in s.scalars(select(Compact).where(Compact.offer_id.in_(ids))).all()} if ids else {}
        return page(request, "state/desk.html", {"offers": offers, "matches": matches, "compacts": compacts, "state": st,
                                                  "today": date.today()})


@router.get("/state/offers/new", response_class=HTMLResponse)
def offer_new_form(request: Request, user: User = Depends(official)):
    return page(request, "state/offer_edit.html", {"o": None, "state": _scope(user), "editable": True})


def _apply(o: Offer, form) -> None:
    for f in OFFER_FIELDS:
        if f in form:
            setattr(o, f, clean(str(form.get(f, "")), 300 if f in ("district", "trade", "enabling_law", "supervisor",
                                                                    "bundle_finder") else 10_000))
    if "offer_form" in form:  # a checkbox sends nothing when cleared
        o.federal_prohibition = bool(form.get("federal_prohibition"))
    if "surplus_rate_pct" in form:
        o.surplus_rate_pct = max(0, min(20, to_int(form.get("surplus_rate_pct"), 3)))
    if "max_founding_units" in form:
        o.max_founding_units = max(2, min(2500, to_int(form.get("max_founding_units"), 2500)))
    if form.get("label") in ("cornerstone", "garden", "both"):
        o.label = form.get("label")


@router.post("/state/offers/new")
async def offer_create(request: Request, user: User = Depends(official)):
    form = await request.form()
    st = _scope(user) or clean(str(form.get("state_name", "")), 80)
    if not st:
        raise HTTPException(400, "Name the State making the offer.")
    with SessionLocal() as s:
        o = Offer(ref=next_ref(s, Offer, "O", 3), state_name=st, district="", trade="", created_by=user.id, is_demo=is_preview())
        _apply(o, form)
        if not o.district or not o.trade:
            raise HTTPException(400, "Name the district and the trade.")
        s.add(o)
        audit(s, "offer.create", user.id, target=o.ref)
        s.commit()
        ref = o.ref
    return go(f"/state/offers/{ref}", f"Draft {ref} saved. Nothing is public until you open it for objections.")


@router.get("/state/offers/{ref}", response_class=HTMLResponse)
def offer_page(request: Request, ref: str, user: User = Depends(official)):
    with SessionLocal() as s:
        o = _offer(s, ref, user)
        missing = [f.replace("_", " ") for f in REQUIRED_TO_PUBLISH if not getattr(o, f)]
        missing += bundle_problems(o.district, o.bundle_districts, o.bundle_finding, o.bundle_finder, o.state_name)
        return page(request, "state/offer_edit.html", {"o": o, "state": o.state_name, "objections": list(o.objections),
                                                        "missing": missing, "today": date.today(),
                                                        "editable": o.status == "draft"})


@router.post("/state/offers/{ref}")
async def offer_save(request: Request, ref: str, user: User = Depends(official)):
    form = await request.form()
    with SessionLocal() as s:
        o = _offer(s, ref, user)
        if o.status not in ("draft", "objection_period"):
            raise HTTPException(400, "A submitted offer is fixed. A material change requires withdrawal and republication.")
        if o.status == "objection_period":
            # Only the no-new-harm record may be completed while objections are open.
            o.no_new_harm = clean(str(form.get("no_new_harm", o.no_new_harm)))
        else:
            _apply(o, form)
        audit(s, "offer.edit", user.id, target=ref)
        s.commit()
    return go(f"/state/offers/{ref}", "Saved.")


@router.post("/state/offers/{ref}/open-objections")
def open_objections(request: Request, ref: str, user: User = Depends(official)):
    with SessionLocal() as s:
        o = _offer(s, ref, user)
        if o.status != "draft":
            raise HTTPException(400)
        missing = [f.replace("_", " ") for f in REQUIRED_TO_PUBLISH if not getattr(o, f)]
        if missing:
            raise HTTPException(400, "Complete the public record first: " + ", ".join(missing))
        problems = bundle_problems(o.district, o.bundle_districts, o.bundle_finding, o.bundle_finder, o.state_name)
        if problems:
            raise HTTPException(400, " ".join(problems))
        o.status, o.objection_ends = "objection_period", days_from(date.today(), OBJECTION_DAYS)
        audit(s, "offer.open_objections", user.id, target=ref)
        s.commit()
    return go(f"/state/offers/{ref}", f"{ref} is public. Anyone may object for {OBJECTION_DAYS} days; affected right holders "
                                      "should be notified directly.")


@router.post("/state/offers/{ref}/end-objections")
def end_objections(request: Request, ref: str, user: User = Depends(official)):
    preview_only()
    with SessionLocal() as s:
        o = _offer(s, ref, user)
        if o.status != "objection_period":
            raise HTTPException(400)
        o.objection_ends = date.today()
        audit(s, "offer.end_objections_preview", user.id, target=ref)
        s.commit()
    return go(f"/state/offers/{ref}", "Preview: the objection period has been ended early.")


@router.post("/state/objections/{oid}/resolve")
def resolve_objection(request: Request, oid: int, resolution: str = Form(...), user: User = Depends(official)):
    with SessionLocal() as s:
        ob = s.get(OfferObjection, oid)
        if ob is None:
            raise HTTPException(404)
        o = _offer(s, s.get(Offer, ob.offer_id).ref, user)
        if not resolution.strip():
            raise HTTPException(400, "State the finding on the record.")
        ob.resolution = clean(resolution)
        audit(s, "offer.objection_resolve", user.id, target=o.ref)
        s.commit()
        ref = o.ref
    return go(f"/state/offers/{ref}#objections", "The finding is on the record.")


@router.post("/state/offers/{ref}/submit")
def submit_offer(request: Request, ref: str, user: User = Depends(official)):
    with SessionLocal() as s:
        o = _offer(s, ref, user)
        if o.status != "objection_period":
            raise HTTPException(400, "Open the offer for objections first.")
        if date.today() < o.objection_ends:
            raise HTTPException(400, f"The objection period runs until {o.objection_ends:%d %B %Y}.")
        if any(not ob.resolution for ob in o.objections):
            raise HTTPException(400, "Answer every objection on the record first.")
        if len(o.no_new_harm.strip()) < 40:
            raise HTTPException(400, "Write the no-new-harm finding: that the offer takes or impairs no existing lawful job, "
                                     "license, contract, customer relationship, application, queue position or market.")
        o.status = "submitted"
        audit(s, "offer.submit", user.id, target=ref)
        s.commit()
    return go(f"/state/offers/{ref}", "Submitted to the Department of Commerce for verification.")


@router.post("/state/offers/{ref}/withdraw")
def withdraw_offer(request: Request, ref: str, user: User = Depends(official)):
    with SessionLocal() as s:
        o = _offer(s, ref, user)
        if o.status in ("matched", "compacted", "withdrawn"):
            raise HTTPException(400, "This offer can no longer be withdrawn.")
        o.status, o.round_id = "withdrawn", None
        audit(s, "offer.withdraw", user.id, target=ref)
        s.commit()
    return go("/state", f"{ref} is withdrawn. Any rankings of it are void.")


# --------------------------------------------------------------------------
# Public objections (anyone signed in)
# --------------------------------------------------------------------------
@router.get("/app/offers/{ref}/object", response_class=HTMLResponse)
def objection_form(request: Request, ref: str, user: User = Depends(require_user)):
    with SessionLocal() as s:
        o = s.scalar(visible(select(Offer), Offer).where(Offer.ref == ref, Offer.status == "objection_period"))
        if o is None:
            raise HTTPException(404, "This offer is not open for objections.")
        return page(request, "state/object.html", {"o": o})


@router.post("/app/offers/{ref}/object")
def objection_submit(request: Request, ref: str, body: str = Form(...), author_name: str = Form(""),
                     user: User = Depends(require_user)):
    with SessionLocal() as s:
        o = s.scalar(visible(select(Offer), Offer).where(Offer.ref == ref, Offer.status == "objection_period"))
        if o is None or date.today() > o.objection_ends:
            raise HTTPException(400, "This offer is not open for objections.")
        s.add(OfferObjection(offer_id=o.id, author_name=clean(author_name, 200) or user.name or "A participant",
                             body=clean(body), is_demo=is_preview()))
        audit(s, "offer.object", user.id, target=ref)
        s.commit()
    return go(f"/exchange/offers/{ref}", "Your objection is on the public record. The State must answer it before submitting.")


# --------------------------------------------------------------------------
# Section 6: compacts with the matched Association
# --------------------------------------------------------------------------
def _match(s, match_id: int, user: User) -> Match:
    m = s.get(Match, match_id)
    if m is None or m.is_demo != is_preview():
        raise HTTPException(404)
    _offer(s, m.offer.ref, user)
    return m


@router.get("/state/matches/{match_id}", response_class=HTMLResponse)
def match_page(request: Request, match_id: int, user: User = Depends(official)):
    with SessionLocal() as s:
        m = _match(s, match_id, user)
        k = s.scalar(select(Compact).where(Compact.match_id == m.id))
        disbursed = sum(f.amount for f in s.scalars(select(FundAdvance).where(
            FundAdvance.association_id == m.association_id, FundAdvance.status == "disbursed")).all())
        return page(request, "state/match.html", {"m": m, "k": k, "disbursed": disbursed, "today": date.today(),
                                                   "public_days": DRAFT_COMPACT_PUBLIC_DAYS})


@router.post("/state/matches/{match_id}/compact")
def save_compact(request: Request, match_id: int, services: str = Form(...), launch_milestones: str = Form(...),
                 public_protections: str = Form(...), selected_terms: str = Form(""), operative_on: str = Form(...),
                 projected_annual_receipts: str = Form(...), user: User = Depends(official)):
    with SessionLocal() as s:
        m = _match(s, match_id, user)
        if m.status != "negotiating":
            raise HTTPException(400, "This match is no longer in negotiation.")
        if date.today() > m.negotiation_deadline:
            raise HTTPException(400, "The negotiation period has ended; the offer returns to the Exchange.")
        k = s.scalar(select(Compact).where(Compact.match_id == m.id))
        if k is None:
            k = Compact(ref=next_ref(s, Compact, "K", 3), match_id=m.id, offer_id=m.offer_id, association_id=m.association_id,
                        surplus_rate_pct=m.offer.surplus_rate_pct, status="drafting", is_demo=is_preview())
            s.add(k)
        if k.status not in ("drafting",):
            raise HTTPException(400, "The draft is with the members.")
        principal = sum(f.amount for f in s.scalars(select(FundAdvance).where(
            FundAdvance.association_id == m.association_id, FundAdvance.status == "disbursed")).all())
        k.services, k.launch_milestones = clean(services), clean(launch_milestones)
        k.public_protections, k.selected_terms = clean(public_protections), clean(selected_terms)
        k.operative_on = parse_date(operative_on, "operative date")
        k.projected_annual_receipts = to_int(projected_annual_receipts)
        terms = fund_terms(principal, k.projected_annual_receipts)
        k.fund_rate_bp, k.fund_return_bp, k.fund_max_obligation = terms.rate_bp, terms.return_bp, terms.max_obligation
        audit(s, "compact.draft", user.id, target=k.ref)
        s.commit()
    return go(f"/state/matches/{match_id}", "Draft saved. The Fund percentage is fixed by the published formula from the "
                                            "disbursed principal and the feasibility projection.")


@router.post("/state/matches/{match_id}/send")
def send_to_members(request: Request, match_id: int, user: User = Depends(official)):
    with SessionLocal() as s:
        m = _match(s, match_id, user)
        k = s.scalar(select(Compact).where(Compact.match_id == m.id))
        if k is None or k.status != "drafting" or not k.services:
            raise HTTPException(400, "Draft the compact first.")
        b = Ballot(association_id=m.association_id, kind="compact", title=f"Approve compact {k.ref} with {m.offer.state_name}",
                   description=(f"{m.offer.trade} in {m.offer.district}. Services: {k.services} Milestones: {k.launch_milestones} "
                                f"Fund: {k.fund_rate_bp / 100:.2f}% of quarterly receipts until ${k.fund_max_obligation:,} is repaid. "
                                f"State share: {k.surplus_rate_pct}% of positive surplus. The draft is public for "
                                f"{DRAFT_COMPACT_PUBLIC_DAYS} days before execution."),
                   payload={"compact_id": k.id}, closes_on=days_from(date.today(), DRAFT_COMPACT_PUBLIC_DAYS),
                   created_by=user.id, is_demo=is_preview())
        b.payload = {"compact_id": k.id}
        s.add(b)
        s.flush()
        k.status, k.ballot_id = "member_vote", b.id
        if m.consortium_id:
            # s.4(c): every constituent's founding units decide separately; each keeps its own governance
            from ..models import Consortium

            c = s.get(Consortium, m.consortium_id)
            ids = [b.id]
            for aid in c.association_ids[1:]:
                other = Ballot(association_id=aid, kind="compact", title=b.title, description=b.description,
                               payload={"compact_id": k.id}, closes_on=b.closes_on, created_by=user.id, is_demo=is_preview())
                s.add(other)
                s.flush()
                ids.append(other.id)
            c.compact_ballot_ids = ids
        audit(s, "compact.send", user.id, target=k.ref)
        s.commit()
    return go(f"/state/matches/{match_id}", "Sent to the members' vote and published for 30 days. It is executed when a "
                                            "majority of all founding units approve.")


@router.post("/state/matches/{match_id}/fail")
def negotiation_failed(request: Request, match_id: int, reasons: str = Form(...), user: User = Depends(official)):
    """s.5(c): a failed negotiation returns the offer to the next willing Association under the original rankings
    and priorities, and the unsuccessful Association proceeds to its next choice. Successor matches go only to
    still-unremedied Associations without another active match."""
    with SessionLocal() as s:
        m = _match(s, match_id, user)
        if m.status != "negotiating":
            raise HTTPException(400)
        if date.today() <= m.negotiation_deadline and not is_preview():
            raise HTTPException(400, f"Negotiation continues until {m.negotiation_deadline:%d %B %Y}.")
        m.status = "failed"
        for a in _constituents(s, m):
            a.status = "roll_closed"
        if m.consortium_id:
            from ..models import Consortium

            s.get(Consortium, m.consortium_id).status = "failed"
        audit(s, "match.failed", user.id, target=m.offer.ref, reasons=clean(reasons, 2000))
        s.flush()
        offer_to, assoc_to = successor_matches(s, m)
        if offer_to is None:
            m.offer.status, m.offer.round_id = "verified", None
        s.commit()
    parts = ["Negotiation ended."]
    parts.append(f"The offer passes to {offer_to.ref}, the next willing association under the round's rankings and priorities."
                 if offer_to else "No other association that ranked the offer is free, so it returns to the Exchange.")
    parts.append(f"The association is matched with its next choice, {assoc_to.ref}." if assoc_to
                 else "The association has no remaining available choice in that round and may rank again.")
    return go("/state", " ".join(parts))


def _constituents(s, m: Match) -> list:
    from ..models import Association, Consortium

    if m.consortium_id:
        return [s.get(Association, i) for i in s.get(Consortium, m.consortium_id).association_ids]
    return [m.association]


def successor_matches(s, failed: Match):
    """Create the successor matches after `failed`. Returns (entrant the offer went to, offer the unsuccessful
    entrant went to); either may be None. Entrants are associations and consortia from the round's rankings."""
    from ..models import Association, Consortium, Round
    from ..program import round_applicants

    r = s.get(Round, failed.round_id)
    failed_pairs = {(x.consortium_id and -x.consortium_id or x.association_id, x.offer_id)
                    for x in s.scalars(select(Match).where(Match.round_id == r.id, Match.status == "failed")).all()}
    entrants = [e for e in round_applicants(s, r.id) if not e["problem"]]

    def free(e) -> bool:
        for aid in e["association_ids"]:
            a = s.get(Association, aid)
            active = s.scalar(select(Match.id).where(Match.association_id == aid, Match.status.in_(["negotiating", "compacted"])))
            if active is not None or a.status != "roll_closed":
                return False
        return True

    def new_match(e, o: Offer) -> None:
        s.add(Match(round_id=r.id, association_id=e["association_ids"][0], offer_id=o.id,
                    priority_date=e["priority"] or failed.priority_date,
                    negotiation_deadline=days_from(date.today(), NEGOTIATION_DAYS), successor_of=failed.id,
                    consortium_id=e["consortium"].id if e["consortium"] else None, is_demo=failed.is_demo))
        for aid in e["association_ids"]:
            s.get(Association, aid).status = "matched"
        if e["consortium"]:
            e["consortium"].status = "matched"
        o.status = "matched"
        s.flush()

    # 1. the offer goes to the next willing entrant that ranked it, by priority and then the public draw
    candidates = [e for e in entrants if failed.offer_id in e["ranking"] and (e["key"], failed.offer_id) not in failed_pairs
                  and e["priority"] is not None and free(e)]
    offer_to = None
    if candidates:
        offer_to = min(candidates, key=lambda e: (e["priority"], tiebreak_key(r.random_seed, e["key"])))
        new_match(offer_to, failed.offer)
    # 2. the unsuccessful entrant proceeds to its next choice that is still available
    assoc_to = None
    key = -failed.consortium_id if failed.consortium_id else failed.association_id
    me = next((e for e in entrants if e["key"] == key), None)
    if me and free(me):
        ids = me["ranking"]
        after = ids[ids.index(failed.offer_id) + 1:] if failed.offer_id in ids else []
        for oid in after:
            o = s.get(Offer, oid)
            taken = s.scalar(select(Match.id).where(Match.offer_id == oid, Match.status.in_(["negotiating", "compacted"])))
            if o is not None and o.status == "verified" and o.round_id == r.id and taken is None \
                    and (key, oid) not in failed_pairs:
                new_match(me, o)
                assoc_to = o
                break
    return (s.get(Association, offer_to["association_ids"][0]) if offer_to else None), assoc_to
