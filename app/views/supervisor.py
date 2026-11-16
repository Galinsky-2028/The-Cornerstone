"""The independent State supervisor (s.6(e)): approve, modify or reject material pricing, access, service, safety
and exclusion decisions on a reasoned public record."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from foundation.db import SessionLocal, audit
from foundation.models import User

from ..models import REVOCATION_GROUNDS, Compact, CompactProceeding, Offer, QuarterlyReport, ShareAppeal, SupervisorDecision
from ..program import is_preview, require_not_recused, require_program_role, state_of, visible
from ..rules import EMERGENCY_HEARING_DAYS, days_from, revocation_problem, state_share
from .common import clean, go, page, parse_date, preview_only

router = APIRouter(prefix="/supervisor")
supervisor = require_program_role("supervisor")


def _compact_ids(s, user: User) -> list[int]:
    st = state_of(user, "supervisor")
    q = visible(select(Compact.id), Compact).join(Offer, Offer.id == Compact.offer_id)
    return s.scalars(q.where(Offer.state_name == st) if st else q).all()


@router.get("", response_class=HTMLResponse)
def desk(request: Request, user: User = Depends(supervisor)):
    with SessionLocal() as s:
        ids = _compact_ids(s, user)
        rows = s.scalars(select(SupervisorDecision).where(SupervisorDecision.compact_id.in_(ids))
                         .order_by(SupervisorDecision.decision != "pending", SupervisorDecision.id.desc())).all() if ids else []
        compacts = s.scalars(select(Compact).where(Compact.id.in_(ids))).all() if ids else []
        proceedings = s.scalars(select(CompactProceeding).where(CompactProceeding.compact_id.in_(ids))
                                .order_by(CompactProceeding.outcome != "pending", CompactProceeding.id.desc())).all() if ids else []
        appeals = s.scalars(select(ShareAppeal).join(QuarterlyReport, QuarterlyReport.id == ShareAppeal.report_id)
                            .where(QuarterlyReport.compact_id.in_(ids)).order_by(ShareAppeal.status != "pending", ShareAppeal.id.desc())).all() if ids else []
        reports = {a.report_id: s.get(QuarterlyReport, a.report_id) for a in appeals}
        return page(request, "supervisor/desk.html", {"rows": rows, "compacts": compacts, "proceedings": proceedings,
                                                       "appeals": appeals, "reports": reports, "grounds": REVOCATION_GROUNDS,
                                                       "compact_refs": {k.id: k.ref for k in compacts}, "user_id": user.id,
                                                       "state": state_of(user, "supervisor")})


@router.post("/decisions/{did}")
def decide(request: Request, did: int, decision: str = Form(...), reasons: str = Form(...), user: User = Depends(supervisor)):
    if decision not in {"approved", "modified", "rejected"} or len(reasons.strip()) < 20:
        raise HTTPException(400, "Choose approve, modify or reject, and give reasons the public can follow.")
    with SessionLocal() as s:
        d = s.get(SupervisorDecision, did)
        if d is None or d.is_demo != is_preview() or d.compact_id not in _compact_ids(s, user):
            raise HTTPException(404)
        if d.decision != "pending":
            raise HTTPException(400, "Already decided.")
        require_not_recused(s, user, association_id=d.compact.association_id, offer_id=d.compact.offer_id)
        d.decision, d.reasons, d.decided_by, d.decided_on = decision, clean(reasons), user.id, date.today()
        audit(s, f"supervision.{decision}", user.id, target=d.compact.ref, kind=d.kind)
        s.commit()
    return go("/supervisor", f"Decision {decision} and published on the compact's public record.")


# --------------------------------------------------------------------------
# Section 6(e): enforcement proceedings
# --------------------------------------------------------------------------
@router.post("/compacts/{ref}/proceedings")
def open_proceeding(request: Request, ref: str, kind: str = Form(...), ground: str = Form(...), notice: str = Form(...),
                    user: User = Depends(supervisor)):
    if kind not in ("revocation", "emergency") or ground not in REVOCATION_GROUNDS:
        raise HTTPException(400, "Choose revocation or emergency suspension and one of the grounds the Act allows.")
    if len(notice.strip()) < 40:
        raise HTTPException(400, "The notice must state the alleged violation and the facts relied on" +
                            (" and the imminent serious harm." if kind == "emergency" else "."))
    with SessionLocal() as s:
        k = s.scalar(visible(select(Compact), Compact).where(Compact.ref == ref))
        if k is None or k.id not in _compact_ids(s, user):
            raise HTTPException(404)
        if k.status not in ("executed", "suspended"):
            raise HTTPException(400, "Only an executed compact can be the subject of a proceeding.")
        if s.scalar(select(CompactProceeding.id).where(CompactProceeding.compact_id == k.id, CompactProceeding.outcome == "pending")):
            raise HTTPException(400, "A proceeding on this compact is already open.")
        require_not_recused(s, user, association_id=k.association_id, offer_id=k.offer_id)
        p = CompactProceeding(compact_id=k.id, kind=kind, ground=ground, notice=clean(notice), opened_by=user.id, is_demo=k.is_demo)
        if kind == "emergency":
            k.status = "suspended"
            p.hearing_due = days_from(date.today(), EMERGENCY_HEARING_DAYS)
        s.add(p)
        audit(s, f"compact.proceeding_{kind}", user.id, target=ref, ground=ground)
        s.commit()
    return go("/supervisor#proceedings", "Emergency suspension in effect; a hearing is due within "
              f"{EMERGENCY_HEARING_DAYS} days." if kind == "emergency" else "Notice issued. A hearing comes before any finding.")


def _proceeding(s, pid: int, user: User) -> CompactProceeding:
    p = s.get(CompactProceeding, pid)
    if p is None or p.is_demo != is_preview() or p.compact_id not in _compact_ids(s, user) or p.outcome != "pending":
        raise HTTPException(404)
    return p


@router.post("/proceedings/{pid}/hearing")
def proceeding_hearing(request: Request, pid: int, record: str = Form(...), cure_deadline: str = Form(""),
                       user: User = Depends(supervisor)):
    if len(record.strip()) < 20:
        raise HTTPException(400, "Record what was heard.")
    with SessionLocal() as s:
        p = _proceeding(s, pid, user)
        p.hearing_record = clean(record)
        if cure_deadline:
            p.cure_deadline = parse_date(cure_deadline, "cure deadline")
        audit(s, "compact.proceeding_hearing", user.id, target=p.compact.ref)
        s.commit()
    return go("/supervisor#proceedings", "Hearing recorded." + (" The association may cure by the deadline." if cure_deadline else ""))


@router.post("/proceedings/{pid}/decide")
def proceeding_decide(request: Request, pid: int, outcome: str = Form(...), findings: str = Form(...),
                      user: User = Depends(supervisor)):
    if outcome not in ("revoked", "cured", "dismissed", "lifted"):
        raise HTTPException(400)
    with SessionLocal() as s:
        p = _proceeding(s, pid, user)
        k = p.compact
        require_not_recused(s, user, association_id=k.association_id, offer_id=k.offer_id)
        if outcome == "lifted" and p.kind != "emergency":
            raise HTTPException(400, "Only an emergency suspension is lifted.")
        if outcome == "revoked":
            cure = p.cure_deadline
            if cure is not None and date.today() <= cure and is_preview():
                cure = None  # preview: the cure period may be skipped
            problem = revocation_problem(ground=p.ground, hearing_record=p.hearing_record, cure_deadline=cure, today=date.today(),
                                         opened_by=p.opened_by, decider=user.id, findings=findings)
            if problem:
                raise HTTPException(400, problem)
            k.status = "revoked"
            msg = ("Compact revoked on written findings. Exclusivity ends; because this is enforcement for the association's "
                   "material violation, the members' claims are not reopened (section 9(a)).")
        else:
            if len(findings.strip()) < 20:
                raise HTTPException(400, "Give reasons for the record.")
            if p.opened_by == user.id and outcome != "lifted":
                raise HTTPException(403, "An independent adjudicator, not the official who brought the proceeding, decides it.")
            if k.status == "suspended":
                k.status = "executed"
            msg = {"cured": "The violation is cured; the compact continues.", "dismissed": "Proceeding dismissed.",
                   "lifted": "Emergency suspension lifted; the compact continues."}[outcome]
        p.outcome, p.findings, p.decided_by, p.decided_on = outcome, clean(findings), user.id, date.today()
        audit(s, f"compact.proceeding_{outcome}", user.id, target=k.ref)
        s.commit()
    return go("/supervisor#proceedings", msg)


# --------------------------------------------------------------------------
# Section 6(d): calculation appeals
# --------------------------------------------------------------------------
@router.post("/appeals/{aid}")
def appeal_decide(request: Request, aid: int, decision: str = Form(...), reasons: str = Form(...), user: User = Depends(supervisor)):
    if decision not in ("corrected", "denied") or len(reasons.strip()) < 20:
        raise HTTPException(400, "Correct the inputs or deny the appeal, with reasons.")
    with SessionLocal() as s:
        a = s.get(ShareAppeal, aid)
        r = s.get(QuarterlyReport, a.report_id) if a else None
        if a is None or a.is_demo != is_preview() or a.status != "pending" or r.compact_id not in _compact_ids(s, user):
            raise HTTPException(404)
        k = s.get(Compact, r.compact_id)
        if decision == "corrected":
            r.shareable_surplus = a.claimed_surplus
            r.state_share = state_share(a.claimed_surplus, k.surplus_rate_pct)  # the rate is never altered
        a.status, a.decision_reasons, a.decided_by = decision, clean(reasons), user.id
        audit(s, f"compact.share_appeal_{decision}", user.id, target=k.ref, quarter=r.quarter)
        s.commit()
    return go("/supervisor#appeals", "Inputs corrected; the State share is recomputed at the fixed rate." if decision == "corrected"
              else "Appeal denied with reasons.")
