"""Workers: the dashboard, event petitions (s.2), comments, and individual claims (s.3)."""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select

from foundation import blobstore
from foundation.auth_core import require_user
from foundation.config import settings
from foundation.db import SessionLocal, audit
from foundation.models import User

from ..models import (
    Association, Ballot, Claim, ClaimDocument, DemoAccount, Event, EventComment, FundAdvance, Membership, Offer, Restoration,
    SupervisorDecision, Vote,
)
from ..program import ensure_worker_role, is_preview, membership_of, next_ref, roles_of, visible
from ..rules import (
    CLAIM_DECISION_DAYS, COMMENT_PERIOD_DAYS, EVENT_DECISION_DAYS, HEARING_REQUEST_DAYS, assess_claim, days_from,
)
from .common import clean, go, page, parse_date, to_int
from .preview import TOURS

router = APIRouter()
DOC_KINDS = {"payroll": "Payroll or pay stubs", "tax": "Tax return or 1099", "union": "Union or dispatch record",
             "license": "License or permit", "contract": "Contract or invoices", "archival": "Archival record", "other": "Other"}
WORK_TYPES = {"employee": "Employee", "independent": "Independent worker", "owner_operator": "Working owner-operator",
              "passive_owner": "Owner who did not work in the business"}
SEPARATIONS = {"total": "Lost the covered work entirely", "substantial": "Lost a substantial part of it",
               "forced_retirement": "Forced into retirement", "business_cancellation": "My business was cancelled or closed",
               "compliance_cost": "Only had compliance costs or lost a prospect"}


def _assess(c: Claim, e: Event):
    return assess_claim(work_type=c.work_type, baseline_total_earned=c.baseline_total_earned,
                        baseline_covered_earned=c.baseline_covered_earned, baseline_covered_hours=c.baseline_covered_hours,
                        separation_type=c.separation_type, separation_date=c.separation_date,
                        after_covered_hours=c.after_covered_hours, causal_start=e.causal_start, causal_end=e.causal_end,
                        is_supplier=c.is_supplier, supplier_share_pct=c.supplier_share_pct,
                        filed_on=c.filed_on, certified_on=e.decided_on)


def can_see_claim(user: User, c: Claim) -> bool:
    return c.user_id == user.id or user.role == "admin" or "labor" in roles_of(user.id)


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
@router.get("/app", response_class=HTMLResponse)
def dashboard(request: Request, user: User = Depends(require_user)):
    ensure_worker_role(user)
    roles = roles_of(user.id)
    with SessionLocal() as s:
        demo = s.get(DemoAccount, user.id)
        claims = s.scalars(visible(select(Claim), Claim).where(Claim.user_id == user.id).order_by(Claim.id.desc())).all()
        m = membership_of(s, user.id)
        assoc = m.association if m else None
        open_ballots = []
        if m:
            voted = set(s.scalars(select(Vote.ballot_id).where(Vote.membership_id == m.id)).all())
            open_ballots = [b for b in s.scalars(select(Ballot).where(Ballot.association_id == m.association_id,
                                                                       Ballot.status == "open")).all() if b.id not in voted]
        counts = {}
        if "labor" in roles or user.role == "admin":
            counts["labor"] = (len(s.scalars(visible(select(Event.id), Event).where(Event.status.in_(["comment_period", "under_review"]))).all())
                               + len(s.scalars(visible(select(Claim.id), Claim).where(Claim.status.in_(["filed", "hearing_requested"]))).all())
                               + len(s.scalars(visible(select(Association.id), Association).where(Association.status == "chartering")).all())
                               + len(s.scalars(visible(select(FundAdvance.id), FundAdvance).where(FundAdvance.status.in_(["requested", "approved"]))).all()))
        if "commerce" in roles or user.role == "admin":
            counts["commerce"] = len(s.scalars(visible(select(Offer.id), Offer).where(Offer.status == "submitted")).all())
        if "state" in roles or user.role == "admin":
            st = roles.get("state").state_name if roles.get("state") else ""
            q = visible(select(Offer.id), Offer).where(Offer.status.in_(["draft", "objection_period"]))
            counts["state"] = len(s.scalars(q.where(Offer.state_name == st) if st else q).all())
        if "supervisor" in roles or user.role == "admin":
            counts["supervisor"] = len(s.scalars(visible(select(SupervisorDecision.id), SupervisorDecision)
                                                 .where(SupervisorDecision.decision == "pending")).all())
        certified_events = s.scalars(visible(select(Event), Event).where(Event.status == "certified").order_by(Event.ref)).all()
        return page(request, "worker/dashboard.html", {
            "roles": roles, "claims": claims, "membership": m, "assoc": assoc, "open_ballots": open_ballots,
            "counts": counts, "persona": demo.persona if demo else "", "tour": TOURS.get(demo.persona if demo else "", []),
            "certified_events": certified_events})


# --------------------------------------------------------------------------
# Section 2: event petitions and comments
# --------------------------------------------------------------------------
@router.get("/app/petitions/new", response_class=HTMLResponse)
def petition_form(request: Request, census_id: str = "", user: User = Depends(require_user)):
    from .public import census_event

    return page(request, "worker/petition.html", {"census": census_event(census_id) if census_id else None,
                                                   "census_id": census_id})


@router.post("/app/petitions/new")
def petition_submit(request: Request, title: str = Form(...), sovereign: str = Form(...), action: str = Form(...),
                    targeted_work: str = Form(...), affected_group: str = Form(...), intent_evidence: str = Form(...),
                    attributed_losses: str = Form(...), petitioner_type: str = Form("workers"),
                    petitioner_name: str = Form(""), census_id: str = Form(""), user: User = Depends(require_user)):
    if petitioner_type not in {"workers", "union", "state_agency", "tribe", "authorized_representative"}:
        raise HTTPException(400, "Choose who is petitioning.")
    t = date.today()
    with SessionLocal() as s:
        e = Event(ref=next_ref(s, Event, "E", 4), title=clean(title, 300), census_id=clean(census_id, 20),
                  sovereign=clean(sovereign, 200), action=clean(action), targeted_work=clean(targeted_work),
                  affected_group=clean(affected_group), intent_evidence=clean(intent_evidence),
                  attributed_losses=clean(attributed_losses), petitioner_type=petitioner_type, petitioner_id=user.id,
                  petitioner_name=clean(petitioner_name, 200) or user.name or "A worker", status="comment_period",
                  filed_on=t, comment_ends=days_from(t, COMMENT_PERIOD_DAYS), decision_due=days_from(t, EVENT_DECISION_DAYS),
                  is_demo=is_preview())
        s.add(e)
        audit(s, "event.petition", user.id, target=e.ref)
        s.commit()
        ref = e.ref
    return go(f"/exchange/events/{ref}", f"Petition {ref} is filed and public. The evidence period runs {COMMENT_PERIOD_DAYS} days; "
                                         f"Labor must decide within {EVENT_DECISION_DAYS} days.")


@router.get("/app/events/{ref}/respond", response_class=HTMLResponse)
def comment_form(request: Request, ref: str, user: User = Depends(require_user)):
    with SessionLocal() as s:
        e = s.scalar(visible(select(Event), Event).where(Event.ref == ref))
        if e is None:
            raise HTTPException(404)
        return page(request, "worker/comment.html", {"e": e, "amendment": _open_amendment(s, e)})


def _open_amendment(s, e: Event):
    """s.2(e): while a proposed scope amendment is open, anyone may submit evidence or responses on it."""
    from ..models import EventAmendment

    return s.scalar(select(EventAmendment).where(EventAmendment.event_id == e.id, EventAmendment.status == "comment_period",
                                                 EventAmendment.comment_ends >= date.today()))


@router.post("/app/events/{ref}/respond")
def comment_submit(request: Request, ref: str, position: str = Form("evidence"), body: str = Form(...),
                   organization: str = Form(""), user: User = Depends(require_user)):
    if position not in {"evidence", "support", "oppose"}:
        raise HTTPException(400)
    with SessionLocal() as s:
        e = s.scalar(visible(select(Event), Event).where(Event.ref == ref))
        if e is None:
            raise HTTPException(404)
        amendment = _open_amendment(s, e)
        if e.status != "comment_period" and amendment is None:
            raise HTTPException(400, "The evidence period for this petition has closed.")
        s.add(EventComment(event_id=e.id, author_name=user.name or "A participant", organization=clean(organization, 200),
                           position=position, body=clean(body), user_id=user.id, is_demo=is_preview(),
                           amendment_id=amendment.id if amendment is not None and e.status != "comment_period" else None))
        audit(s, "event.comment", user.id, target=ref)
        s.commit()
    return go(f"/exchange/events/{ref}", "Your submission is on the public record.")


# --------------------------------------------------------------------------
# Section 3: individual claims
# --------------------------------------------------------------------------
@router.get("/app/claims/new", response_class=HTMLResponse)
def claim_form(request: Request, event: str = "", user: User = Depends(require_user)):
    with SessionLocal() as s:
        events = s.scalars(visible(select(Event), Event).where(Event.status == "certified").order_by(Event.ref)).all()
        chosen = next((e for e in events if e.ref == event), None)
        return page(request, "worker/claim_new.html", {"events": events, "chosen": chosen, "work_types": WORK_TYPES,
                                                        "separations": SEPARATIONS, "worker": user})


@router.post("/app/claims/new")
def claim_submit(request: Request, event_ref: str = Form(...), worker_name: str = Form(...), work_type: str = Form(...),
                 occupation: str = Form(...), employer: str = Form(""), worksite: str = Form(""),
                 baseline_start: str = Form(...), baseline_end: str = Form(...), baseline_total_earned: str = Form("0"),
                 baseline_covered_earned: str = Form("0"), baseline_covered_hours: str = Form("0"),
                 separation_type: str = Form(...), separation_date: str = Form(...), after_covered_hours: str = Form("0"),
                 is_supplier: str = Form(""), supplier_share_pct: str = Form("0"), prior_benefits: str = Form(""),
                 deceased: str = Form(""), successor_name: str = Form(""), successor_relationship: str = Form(""),
                 user: User = Depends(require_user)):
    if work_type not in WORK_TYPES or separation_type not in SEPARATIONS:
        raise HTTPException(400, "Choose how you worked and how you lost the work.")
    with SessionLocal() as s:
        e = s.scalar(visible(select(Event), Event).where(Event.ref == event_ref, Event.status == "certified"))
        if e is None:
            raise HTTPException(400, "Choose a certified event.")
        if deceased and not successor_name.strip():
            raise HTTPException(400, "Name the successor filing for a worker who has died.")
        bs, be = parse_date(baseline_start, "start of the representative year"), parse_date(baseline_end, "end of the representative year")
        if be <= bs:
            raise HTTPException(400, "The representative period must end after it starts.")
        c = Claim(ref=next_ref(s, Claim, "CL", 5), event_id=e.id, user_id=user.id, worker_name=clean(worker_name, 200),
                  work_type=work_type, occupation=clean(occupation, 200), employer=clean(employer, 200),
                  worksite=clean(worksite, 300), baseline_start=bs, baseline_end=be,
                  baseline_total_earned=to_int(baseline_total_earned), baseline_covered_earned=to_int(baseline_covered_earned),
                  baseline_covered_hours=to_int(baseline_covered_hours), separation_type=separation_type,
                  separation_date=parse_date(separation_date, "separation date"), after_covered_hours=to_int(after_covered_hours),
                  is_supplier=bool(is_supplier), supplier_share_pct=max(0, min(100, to_int(supplier_share_pct))),
                  prior_benefits=clean(prior_benefits), deceased=bool(deceased), successor_name=clean(successor_name, 200),
                  successor_relationship=clean(successor_relationship, 100), filed_on=date.today(), is_demo=is_preview())
        if c.baseline_covered_earned > c.baseline_total_earned:
            raise HTTPException(400, "Covered earnings cannot exceed total earned income.")
        if deceased and s.scalar(select(Claim.id).where(Claim.worker_name == c.worker_name, Claim.event_id == e.id,
                                                        Claim.deceased.is_(True), Claim.is_demo.is_(c.is_demo))):
            raise HTTPException(400, "A successor has already filed for this worker. The Act allows one successor.")
        s.add(c)
        audit(s, "claim.file", user.id, target=c.ref, event=e.ref)
        s.commit()
        ref = c.ref
    return go(f"/app/claims/{ref}", f"Claim {ref} is filed. Labor must decide it within {CLAIM_DECISION_DAYS} days. "
                                    "Add your evidence below.")


@router.get("/app/claims/{ref}", response_class=HTMLResponse)
def claim_page(request: Request, ref: str, user: User = Depends(require_user)):
    with SessionLocal() as s:
        c = s.scalar(visible(select(Claim), Claim).where(Claim.ref == ref))
        if c is None or not can_see_claim(user, c):
            raise HTTPException(404)
        e = c.event
        result = _assess(c, e)
        hearing_open = (c.status == "denied" and c.decided_on is not None
                        and date.today() <= days_from(c.decided_on, HEARING_REQUEST_DAYS))
        m = s.scalar(select(Membership).where(Membership.claim_id == c.id))
        return page(request, "worker/claim.html", {
            "c": c, "e": e, "result": result, "docs": list(c.documents), "doc_kinds": DOC_KINDS, "work_types": WORK_TYPES,
            "separations": SEPARATIONS, "hearing_open": hearing_open, "membership": m, "mine": c.user_id == user.id,
            "decision_due": days_from(c.filed_on, CLAIM_DECISION_DAYS),
            "hearing_deadline": days_from(c.decided_on, HEARING_REQUEST_DAYS) if c.decided_on else None})


@router.post("/app/claims/{ref}/documents")
async def claim_upload(request: Request, ref: str, kind: str = Form("other"), file: UploadFile = File(...),
                       user: User = Depends(require_user)):
    if kind not in DOC_KINDS:
        raise HTTPException(400)
    data = await file.read(settings.max_upload_mb * 1024 * 1024 + 1)
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"Files may be up to {settings.max_upload_mb} MB.")
    if not data:
        raise HTTPException(400, "Choose a file to upload.")
    with SessionLocal() as s:
        c = s.scalar(visible(select(Claim), Claim).where(Claim.ref == ref))
        if c is None or c.user_id != user.id:
            raise HTTPException(404)
        name = (file.filename or "evidence").replace("/", "_")[-200:]
        key = blobstore.put(data, f"claims/{c.ref}/{len(c.documents) + 1}-{name}", file.content_type, owner_id=user.id)
        s.add(ClaimDocument(claim_id=c.id, kind=kind, filename=name, blob_key=key, size=len(data)))
        audit(s, "claim.document", user.id, target=c.ref, kind=kind)
        s.commit()
    return go(f"/app/claims/{ref}#evidence", f"{name} is attached to your claim.")


@router.get("/app/claims/{ref}/documents/{doc_id}")
def claim_document(ref: str, doc_id: int, user: User = Depends(require_user)):
    with SessionLocal() as s:
        c = s.scalar(visible(select(Claim), Claim).where(Claim.ref == ref))
        d = s.get(ClaimDocument, doc_id)
        if c is None or d is None or d.claim_id != c.id or not can_see_claim(user, c):
            raise HTTPException(404)
        got = blobstore.get(d.blob_key)
        if got is None:
            raise HTTPException(404, "The file is no longer stored.")
        data, ctype = got
        return Response(data, media_type="application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="{d.filename}"', "X-Content-Type-Options": "nosniff"})


@router.post("/app/claims/{ref}/hearing")
def claim_hearing(request: Request, ref: str, reasons: str = Form(...), user: User = Depends(require_user)):
    with SessionLocal() as s:
        c = s.scalar(visible(select(Claim), Claim).where(Claim.ref == ref))
        if c is None or c.user_id != user.id:
            raise HTTPException(404)
        if c.status != "denied" or c.decided_on is None or date.today() > days_from(c.decided_on, HEARING_REQUEST_DAYS):
            raise HTTPException(400, f"A hearing may be requested within {HEARING_REQUEST_DAYS} days of a denial.")
        c.status, c.hearing_request = "hearing_requested", clean(reasons)
        audit(s, "claim.hearing_request", user.id, target=ref)
        s.commit()
    return go(f"/app/claims/{ref}", "Your hearing request is filed. An independent hearing officer will decide it on the record.")
