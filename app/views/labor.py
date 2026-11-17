"""The Department of Labor desk: event decisions (s.2), claim decisions and hearings (s.3), charters (s.4),
Founding Fund advances (s.8) and restoration findings (s.9)."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from foundation.db import SessionLocal, audit
from foundation.models import User

from ..models import (
    FUND_CATEGORIES, RESTORATION_CHECKS, SUBPOENA_STATUSES, Association, Claim, Compact, Event, FundAdvance, Membership,
    SCOPE_FIELDS, Ballot, ClaimRescission, EventAmendment, Restoration, Subpoena,
)
from ..program import founding_units, is_preview, require_not_recused, require_program_role, visible
from ..rules import (
    COMMENT_PERIOD_DAYS, ENROLLMENT_DAYS, MAX_EXTENSION_DAYS, NOT_REOPEN_GROUNDS, REOPEN_GROUNDS, days_from, extension_problem,
    preopening_outcome, reopen_problem,
)
from .common import clean, go, page, parse_date, preview_only, to_int
from .worker import DOC_KINDS, SEPARATIONS, WORK_TYPES, _assess

router = APIRouter(prefix="/labor")
labor = require_program_role("labor")


def _event(s, ref: str) -> Event:
    e = s.scalar(visible(select(Event), Event).where(Event.ref == ref))
    if e is None:
        raise HTTPException(404)
    if e.status == "comment_period" and date.today() > e.comment_ends:
        e.status = "under_review"
    return e


@router.get("", response_class=HTMLResponse)
def desk(request: Request, user: User = Depends(labor)):
    with SessionLocal() as s:
        events = s.scalars(visible(select(Event), Event).where(Event.status.in_(["comment_period", "under_review"]))
                           .order_by(Event.decision_due)).all()
        claims = s.scalars(visible(select(Claim), Claim).where(Claim.status == "filed").order_by(Claim.filed_on)).all()
        hearings = s.scalars(visible(select(Claim), Claim).where(Claim.status == "hearing_requested")).all()
        chartering = s.scalars(visible(select(Association), Association).where(Association.status == "chartering")).all()
        advances = s.scalars(visible(select(FundAdvance), FundAdvance).where(FundAdvance.status.in_(["requested", "approved"]))
                             .order_by(FundAdvance.id)).all()
        restorations = s.scalars(visible(select(Restoration), Restoration).order_by(Restoration.id)).all()
        large_bodies = s.scalars(visible(select(Association), Association).where(
            Association.large_body_approved_on.is_(None),
            Association.id.in_(select(Ballot.association_id).where(Ballot.kind == "large_body", Ballot.status == "passed")))).all()
        return page(request, "labor/desk.html", {"large_bodies": large_bodies,
            "events": events, "claims": claims, "hearings": hearings, "chartering": chartering,
            "charter_units": {a.id: founding_units(s, a.id) for a in chartering}, "advances": advances,
            "restorations": restorations, "fund_categories": FUND_CATEGORIES, "today": date.today()})


# --------------------------------------------------------------------------
# Section 2(c): event decisions
# --------------------------------------------------------------------------
@router.get("/events/{ref}", response_class=HTMLResponse)
def event_review(request: Request, ref: str, user: User = Depends(labor)):
    with SessionLocal() as s:
        e = _event(s, ref)
        s.commit()
        subpoenas = s.scalars(select(Subpoena).where(Subpoena.event_id == e.id).order_by(Subpoena.id)).all()
        amendments = s.scalars(select(EventAmendment).where(EventAmendment.event_id == e.id).order_by(EventAmendment.id)).all()
        return page(request, "labor/event.html", {"amendments": amendments,"e": e, "comments": list(e.comments), "today": date.today(),
                                                   "subpoenas": subpoenas, "max_extension": MAX_EXTENSION_DAYS,
                                                   "subpoena_statuses": SUBPOENA_STATUSES})


@router.post("/events/{ref}/extend")
def event_extend(request: Request, ref: str, days: str = Form(...), reasons: str = Form(...), user: User = Depends(labor)):
    """s.2(c): one extension of not more than 90 days requires public reasons."""
    n = to_int(days)
    with SessionLocal() as s:
        e = _event(s, ref)
        if e.status not in ("comment_period", "under_review"):
            raise HTTPException(400, "Only an undecided petition can be extended.")
        problem = extension_problem(e.extension_days, n, reasons)
        if problem:
            raise HTTPException(400, problem)
        e.extension_days, e.extension_reasons = n, clean(reasons)
        e.decision_due = days_from(e.decision_due, n)
        audit(s, "event.extend", user.id, target=ref, days=n)
        s.commit()
    return go(f"/labor/events/{ref}", f"The decision is now due {e.decision_due:%d %B %Y}. The reasons are published.")


@router.post("/events/{ref}/subpoenas")
def subpoena_issue(request: Request, ref: str, recipient: str = Form(...), records: str = Form(...),
                   relevance: str = Form(...), return_by: str = Form(...), user: User = Depends(labor)):
    """s.2(c): Labor may require relevant records by subpoena while it investigates a petition."""
    due = parse_date(return_by, "return date")
    with SessionLocal() as s:
        e = _event(s, ref)
        if e.status not in ("comment_period", "under_review"):
            raise HTTPException(400, "Subpoenas issue only while a petition is being investigated.")
        if not recipient.strip() or not records.strip() or len(relevance.strip()) < 20:
            raise HTTPException(400, "Name the recipient and the records, and state why they are relevant to a material dispute.")
        if due <= date.today():
            raise HTTPException(400, "Give the recipient a return date in the future.")
        s.add(Subpoena(event_id=e.id, recipient=clean(recipient, 300), records=clean(records), relevance=clean(relevance),
                       return_by=due, issued_by=user.id, is_demo=e.is_demo))
        audit(s, "subpoena.issue", user.id, target=ref, recipient=clean(recipient, 300))
        s.commit()
    return go(f"/labor/events/{ref}#subpoenas", "Subpoena issued. It is enforceable in United States district court, "
                                               "subject to privilege and applicable privacy law.")


@router.post("/subpoenas/{sid}")
def subpoena_update(request: Request, sid: int, status: str = Form(...), response: str = Form(...), user: User = Depends(labor)):
    if status not in SUBPOENA_STATUSES or status == "issued" or not response.strip():
        raise HTTPException(400, "Choose what happened and record it.")
    with SessionLocal() as s:
        sp = s.get(Subpoena, sid)
        if sp is None or sp.is_demo != is_preview():
            raise HTTPException(404)
        if sp.status not in ("issued", "contested"):
            raise HTTPException(400, "This subpoena is closed.")
        ref = s.get(Event, sp.event_id).ref
        sp.status, sp.response = status, clean(response)
        audit(s, f"subpoena.{status}", user.id, target=ref)
        s.commit()
    note = {"complied": "Recorded: the records were produced. They stay private.",
            "contested": "Recorded: the recipient contests the subpoena, claiming privilege or privacy.",
            "withdrawn": "The subpoena is withdrawn.",
            "referred": "Referred for enforcement in United States district court."}[status]
    return go(f"/labor/events/{ref}#subpoenas", note)


@router.post("/events/{ref}/end-evidence")
def end_evidence(request: Request, ref: str, user: User = Depends(labor)):
    preview_only()
    with SessionLocal() as s:
        e = _event(s, ref)
        if e.status != "comment_period":
            raise HTTPException(400, "The evidence period has already closed.")
        e.comment_ends, e.status = date.today(), "under_review"
        audit(s, "event.end_evidence_preview", user.id, target=ref)
        s.commit()
    return go(f"/labor/events/{ref}", "Preview: the 60-day evidence period has been ended early. You may now decide the petition.")


@router.post("/events/{ref}/decide")
def event_decide(request: Request, ref: str, decision: str = Form(...), decision_reasons: str = Form(...),
                 occupations: str = Form(""), worksites: str = Form(""), geography: str = Form(""),
                 contrary_evidence: str = Form(""), causal_mechanism: str = Form(""), causal_start: str = Form(""),
                 causal_end: str = Form(""), first_separation: str = Form(""), acceptable_proof: str = Form(""),
                 user: User = Depends(labor)):
    if decision not in {"certify", "deny"} or not decision_reasons.strip():
        raise HTTPException(400, "Choose a decision and give your reasons.")
    with SessionLocal() as s:
        e = _event(s, ref)
        if e.status != "under_review":
            raise HTTPException(400, "A petition is decided after its evidence period closes.")
        if decision == "certify":
            needed = {"occupations": occupations, "worksites": worksites, "geography": geography,
                      "causal mechanism": causal_mechanism, "acceptable proof": acceptable_proof}
            missing = [k for k, v in needed.items() if not v.strip()]
            if missing or not causal_start or not first_separation:
                raise HTTPException(400, "A certification must state the covered occupations, worksites, geography, causal "
                                         "mechanism and period, first separation, and acceptable proof. Missing: "
                                         + ", ".join(missing + ([] if causal_start else ["causal start"])
                                                     + ([] if first_separation else ["first separation"])))
            e.occupations, e.worksites, e.geography = clean(occupations), clean(worksites), clean(geography)
            e.contrary_evidence, e.causal_mechanism, e.acceptable_proof = clean(contrary_evidence), clean(causal_mechanism), clean(acceptable_proof)
            e.causal_start = parse_date(causal_start, "start of the causal period")
            e.causal_end = parse_date(causal_end, "end of the causal period") if causal_end else None
            e.first_separation = parse_date(first_separation, "first separation")
        e.status = "certified" if decision == "certify" else "denied"
        e.decision_reasons, e.decided_by, e.decided_on = clean(decision_reasons), user.id, date.today()
        audit(s, f"event.{e.status}", user.id, target=ref)
        s.commit()
    return go(f"/exchange/events/{ref}", f"{ref} is {'certified' if decision == 'certify' else 'denied'}, with the reasons published.")


# --------------------------------------------------------------------------
# Section 3(d): claims and hearings
# --------------------------------------------------------------------------
def _claim(s, ref: str) -> Claim:
    c = s.scalar(visible(select(Claim), Claim).where(Claim.ref == ref))
    if c is None:
        raise HTTPException(404)
    return c


def _promote_cohort(s, c: Claim) -> None:
    """s.4(a): a cohort applicant certified while the roll is open becomes a founding unit."""
    m = s.scalar(select(Membership).where(Membership.claim_id == c.id, Membership.status == "cohort"))
    if m is not None and c.status == "certified":
        m.status = "founding"


@router.get("/claims/{ref}", response_class=HTMLResponse)
def claim_review(request: Request, ref: str, user: User = Depends(labor)):
    with SessionLocal() as s:
        c = _claim(s, ref)
        rescissions = s.scalars(select(ClaimRescission).where(ClaimRescission.claim_id == c.id).order_by(ClaimRescission.id)).all()
        return page(request, "labor/claim.html", {"rescissions": rescissions, "c": c, "e": c.event, "result": _assess(c, c.event), "docs": list(c.documents),
                                                   "doc_kinds": DOC_KINDS, "work_types": WORK_TYPES, "separations": SEPARATIONS})


@router.post("/claims/{ref}/decide")
def claim_decide(request: Request, ref: str, decision: str = Form(...), reasons: str = Form(...), user: User = Depends(labor)):
    if decision not in {"certify", "deny"} or not reasons.strip():
        raise HTTPException(400, "Choose a decision and give your reasons.")
    with SessionLocal() as s:
        c = _claim(s, ref)
        if c.status != "filed":
            raise HTTPException(400, "This claim has already been decided.")
        if c.user_id == user.id:
            raise HTTPException(403, "An examiner may not decide their own claim.")
        c.status = "certified" if decision == "certify" else "denied"
        c.decision_reasons, c.decided_by, c.decided_on = clean(reasons), user.id, date.today()
        _promote_cohort(s, c)
        audit(s, f"claim.{c.status}", user.id, target=ref)
        s.commit()
    return go("/labor#claims", f"{ref} is {c.status}. The worker has been sent the written reasons.")


@router.post("/claims/{ref}/hearing")
def hearing_decide(request: Request, ref: str, outcome: str = Form(...), reasons: str = Form(...), user: User = Depends(labor)):
    if outcome not in {"grant", "uphold"} or not reasons.strip():
        raise HTTPException(400, "Choose an outcome and give your reasons.")
    with SessionLocal() as s:
        c = _claim(s, ref)
        if c.status != "hearing_requested":
            raise HTTPException(400, "No hearing is pending on this claim.")
        if c.decided_by == user.id:
            raise HTTPException(403, "The hearing must be decided by someone other than the original examiner.")
        c.status = "certified" if outcome == "grant" else "denied"
        c.hearing_outcome = clean(reasons)
        _promote_cohort(s, c)
        c.decided_on = date.today() if outcome == "grant" else c.decided_on
        audit(s, f"claim.hearing_{outcome}", user.id, target=ref)
        s.commit()
    return go("/labor#hearings", f"Hearing decided: {ref} is {c.status}.")


# --------------------------------------------------------------------------
# Section 4: charters
# --------------------------------------------------------------------------
@router.post("/associations/{ref}/charter")
def charter(request: Request, ref: str, decision: str = Form("approve"), reasons: str = Form(""), user: User = Depends(labor)):
    with SessionLocal() as s:
        a = s.scalar(visible(select(Association), Association).where(Association.ref == ref))
        if a is None or a.status != "chartering":
            raise HTTPException(404)
        require_not_recused(s, user, association_id=a.id)
        if decision == "approve":
            t = date.today()
            a.status, a.chartered_on, a.enrollment_opens, a.enrollment_closes = "enrolling", t, t, days_from(t, ENROLLMENT_DAYS)
            msg = f"{ref} is chartered. Enrollment is open for {ENROLLMENT_DAYS} days."
        else:
            a.status = "charter_refused"
            a.purpose = (a.purpose + "\n\nCharter refused: " + clean(reasons)).strip()
            msg = f"{ref}'s charter is refused, with reasons."
        audit(s, f"association.charter_{decision}", user.id, target=ref)
        s.commit()
    return go("/labor#associations", msg)


# --------------------------------------------------------------------------
# Section 8: Founding Fund
# --------------------------------------------------------------------------
@router.post("/fund/{advance_id}")
def fund_action(request: Request, advance_id: int, action: str = Form(...), reasons: str = Form(""), user: User = Depends(labor)):
    with SessionLocal() as s:
        f = s.get(FundAdvance, advance_id)
        if f is None or f.is_demo != is_preview():
            raise HTTPException(404)
        allowed = {"requested": {"approve", "reject"}, "approved": {"disburse"}}
        if action not in allowed.get(f.status, set()):
            raise HTTPException(400, "That step is not available for this advance.")
        require_not_recused(s, user, association_id=f.association_id)
        r = s.scalar(select(Restoration).where(Restoration.association_id == f.association_id))
        if action == "approve" and r is not None and r.no_automatic_advance and len(reasons.strip()) < 20:
            raise HTTPException(400, "Section 9(a): after a second preopening failure or a reopening there is no automatic "
                                     "right to another advance. Approving one needs written reasons.")
        f.status = {"approve": "approved", "reject": "rejected", "disburse": "disbursed"}[action]
        f.reasons, f.decided_by = clean(reasons) or f.reasons, user.id
        audit(s, f"fund.{f.status}", user.id, target=f.association.ref, amount=f.amount, category=f.category)
        s.commit()
    return go("/labor#fund", f"Advance {f.status}." + (" Paid directly to the provider." if action == "disburse" else ""))


# --------------------------------------------------------------------------
# Section 9: restoration
# --------------------------------------------------------------------------
def _restoration(s, ref: str) -> tuple[Association, Restoration | None, Compact | None]:
    a = s.scalar(visible(select(Association), Association).where(Association.ref == ref))
    if a is None:
        raise HTTPException(404)
    r = s.scalar(select(Restoration).where(Restoration.association_id == a.id))
    k = s.get(Compact, r.compact_id) if r is not None else None
    if k is None or k.status != "executed":
        k = s.scalar(select(Compact).where(Compact.association_id == a.id, Compact.status == "executed"))
    return a, r, k


@router.get("/restorations/{ref}", response_class=HTMLResponse)
def restoration_page(request: Request, ref: str, user: User = Depends(labor)):
    with SessionLocal() as s:
        a, r, k = _restoration(s, ref)
        claims = [m.claim for m in s.scalars(select(Membership).where(Membership.association_id == a.id)).all()]
        return page(request, "labor/restoration.html", {"a": a, "r": r, "k": k, "checks": RESTORATION_CHECKS,
                                                         "claims": claims, "reopen_grounds": REOPEN_GROUNDS,
                                                         "not_reopen_grounds": NOT_REOPEN_GROUNDS, "today": date.today()})


@router.post("/restorations/{ref}/provisional")
async def restoration_provisional(request: Request, ref: str, user: User = Depends(labor)):
    form = await request.form()
    with SessionLocal() as s:
        a, r, k = _restoration(s, ref)
        if k is None:
            raise HTTPException(400, "Restoration requires an executed compact.")
        require_not_recused(s, user, association_id=a.id)
        checks = {key: bool(form.get(key)) for key in RESTORATION_CHECKS}
        if r is None:
            r = Restoration(association_id=a.id, compact_id=k.id, is_demo=a.is_demo)
            s.add(r)
        r.checks, r.findings = checks, clean(str(form.get("findings", "")))
        if not all(checks.values()):
            r.status = "pending"
            msg = "Recorded. Restoration cannot be found until every condition is met."
        else:
            if not form.get("launch_deadline") and r.launch_deadline is None:
                raise HTTPException(400, "Record the published launch deadline by which capital must be secured (section 9(a)).")
            if form.get("launch_deadline"):
                r.launch_deadline = parse_date(str(form.get("launch_deadline")), "launch deadline")
            r.status, r.decided_by, r.decided_on = "provisional", user.id, date.today()
            a.status = "restoration_provisional"
            msg = "Provisional restoration found. Claims are marked remedied when financing is secured and operations begin."
        audit(s, "restoration.review", user.id, target=ref, status=r.status)
        s.commit()
    return go(f"/labor/restorations/{ref}", msg)


@router.post("/restorations/{ref}/remedied")
def restoration_remedied(request: Request, ref: str, operations_began_on: str = Form(...), financing_secured: str = Form(""),
                         user: User = Depends(labor)):
    with SessionLocal() as s:
        a, r, k = _restoration(s, ref)
        if r is None or r.status != "provisional":
            raise HTTPException(400, "A provisional restoration finding comes first.")
        require_not_recused(s, user, association_id=a.id)
        if not financing_secured:
            raise HTTPException(400, "Confirm that financing has been secured.")
        r.financing_secured, r.operations_began_on = True, parse_date(operations_began_on, "date operations began")
        r.status, r.decided_by, r.decided_on = "remedied", user.id, date.today()
        a.status = "remedied"
        n = 0
        for m in s.scalars(select(Membership).where(Membership.association_id == a.id)).all():
            if m.claim.status in ("certified", "reopened"):
                m.claim.status = "remedied"
                n += 1
        audit(s, "restoration.remedied", user.id, target=ref, claims=n)
        s.commit()
    return go(f"/labor/restorations/{ref}", f"Restoration confirmed. {n} claims are marked remedied; their events lose "
                                            "priority only as to these workers.")


@router.post("/restorations/{ref}/reopen")
def restoration_reopen(request: Request, ref: str, reasons: str = Form(...), ground: str = Form(""),
                       user: User = Depends(labor)):
    """s.9(a): governmental withdrawal, judicial invalidation or other governmental defeat of the compact right
    reopens the affected claims on findings; ordinary commercial failure, cost or demand changes, and lawful
    enforcement for the Association's material violation do not. No duplicate advance follows automatically."""
    problem = reopen_problem(ground)
    if problem:
        raise HTTPException(400, problem)
    if len(reasons.strip()) < 20:
        raise HTTPException(400, "State the findings on the final action or judgment.")
    with SessionLocal() as s:
        a, r, k = _restoration(s, ref)
        if r is None or r.status not in ("provisional", "remedied"):
            raise HTTPException(400)
        require_not_recused(s, user, association_id=a.id)
        r.status, r.reopen_ground, r.no_automatic_advance = "reopened", ground, True
        r.findings = (r.findings + f"\n\nReopened {date.today():%d %B %Y} ({REOPEN_GROUNDS[ground]}): " + clean(reasons)).strip()
        a.status = "compact" if k else "roll_closed"
        for m in s.scalars(select(Membership).where(Membership.association_id == a.id)).all():
            if m.claim.status == "remedied":
                m.claim.status = "reopened"
        audit(s, "restoration.reopen", user.id, target=ref, ground=ground)
        s.commit()
    return go(f"/labor/restorations/{ref}", "Restoration reopened. The members' claims regain their standing; no further "
                                            "Fund advance follows automatically.")


@router.post("/restorations/{ref}/preopening-failure")
def preopening_failure(request: Request, ref: str, choice: str = Form(""), reasons: str = Form(...),
                       revised_plan: str = Form(""), new_launch_deadline: str = Form(""), user: User = Depends(labor)):
    """s.9(a): capital was not secured before the published launch deadline."""
    if len(reasons.strip()) < 20:
        raise HTTPException(400, "State the findings.")
    with SessionLocal() as s:
        a, r, k = _restoration(s, ref)
        if r is None or r.status != "provisional" or k is None:
            raise HTTPException(400, "A preopening failure is recorded on a provisional restoration.")
        require_not_recused(s, user, association_id=a.id)
        if r.launch_deadline and date.today() <= r.launch_deadline:
            preview_only()
        try:
            outcome = preopening_outcome(r.preopening_failures, choice)
        except ValueError as e:
            raise HTTPException(400, str(e))
        r.preopening_failures += 1
        note = f"\n\nPreopening failure {r.preopening_failures} ({date.today():%d %B %Y}): " + clean(reasons)
        if outcome == "revise":
            if len(revised_plan.strip()) < 20 or not new_launch_deadline:
                raise HTTPException(400, "Give the revised plan within the offer and its new launch deadline.")
            new = parse_date(new_launch_deadline, "new launch deadline")
            if new <= date.today():
                raise HTTPException(400, "The new launch deadline must be in the future.")
            r.revised_plan, r.launch_deadline = clean(revised_plan), new
            msg = "First preopening failure recorded. The association continues under its revised plan within the offer."
        else:
            # return to the Exchange without losing claims or original priority (priority comes from the events)
            k.status = "returned"
            k.offer.status, k.offer.round_id = "verified", None
            for aid in _compact_associations(s, k):
                s.get(Association, aid).status = "roll_closed"
            status = "returned" if outcome == "return" else "failed_preopening"
            for other in s.scalars(select(Restoration).where(Restoration.compact_id == k.id)).all():
                other.status = status  # every constituent of a consortium compact
                other.preopening_failures = r.preopening_failures
                other.no_automatic_advance = other.no_automatic_advance or outcome == "second_failure"
            if outcome == "second_failure":
                msg = ("Second preopening failure recorded. The members' claims stay open, but there is no automatic right "
                       "to another Fund advance.")
            else:
                msg = "The association returns to the Exchange with its claims and original priority."
        r.findings = (r.findings + note).strip()
        audit(s, "restoration.preopening_failure", user.id, target=ref, outcome=outcome, count=r.preopening_failures)
        s.commit()
    return go(f"/labor/restorations/{ref}", msg)


def _compact_associations(s, k: Compact) -> list[int]:
    from ..models import Consortium

    if k.match and k.match.consortium_id:
        return list(s.get(Consortium, k.match.consortium_id).association_ids)
    return [k.association_id]


# --------------------------------------------------------------------------
# Section 4(f): rescission of fraudulently obtained eligibility
# --------------------------------------------------------------------------
@router.post("/claims/{ref}/rescission")
def rescission_notice(request: Request, ref: str, notice: str = Form(...), user: User = Depends(labor)):
    if len(notice.strip()) < 40:
        raise HTTPException(400, "The notice must state the alleged fraud and the evidence relied on.")
    with SessionLocal() as s:
        c = _claim(s, ref)
        if c.status not in ("certified", "remedied", "reopened"):
            raise HTTPException(400, "Only a certified claim can be the subject of rescission.")
        if s.scalar(select(ClaimRescission).where(ClaimRescission.claim_id == c.id, ClaimRescission.outcome == "pending")):
            raise HTTPException(400, "A rescission proceeding is already open.")
        s.add(ClaimRescission(claim_id=c.id, notice=clean(notice), noticed_by=user.id, is_demo=c.is_demo))
        audit(s, "claim.rescission_notice", user.id, target=ref)
        s.commit()
    return go(f"/labor/claims/{ref}#rescission", "Notice issued. The worker is entitled to a hearing before any finding.")


def _rescission(s, rid: int) -> ClaimRescission:
    r = s.get(ClaimRescission, rid)
    if r is None or r.is_demo != is_preview() or r.outcome != "pending":
        raise HTTPException(404)
    return r


@router.post("/rescissions/{rid}/hearing")
def rescission_hearing(request: Request, rid: int, record: str = Form(...), user: User = Depends(labor)):
    if len(record.strip()) < 20:
        raise HTTPException(400, "Record what was heard.")
    with SessionLocal() as s:
        r = _rescission(s, rid)
        r.hearing_record = clean(record)
        ref = s.get(Claim, r.claim_id).ref
        audit(s, "claim.rescission_hearing", user.id, target=ref)
        s.commit()
    return go(f"/labor/claims/{ref}#rescission", "Hearing recorded.")


@router.post("/rescissions/{rid}/decide")
def rescission_decide(request: Request, rid: int, outcome: str = Form(...), findings: str = Form(...), user: User = Depends(labor)):
    if outcome not in ("rescinded", "dismissed"):
        raise HTTPException(400)
    with SessionLocal() as s:
        r = _rescission(s, rid)
        c = s.get(Claim, r.claim_id)
        if len(r.hearing_record.strip()) < 20:
            raise HTTPException(400, "Eligibility may be rescinded only after a hearing.")
        if len(findings.strip()) < 40:
            raise HTTPException(400, "Written findings are required.")
        if c.user_id == user.id:
            raise HTTPException(403, "An examiner may not decide their own claim.")
        r.outcome, r.findings, r.decided_by, r.decided_on = outcome, clean(findings), user.id, date.today()
        if outcome == "rescinded":
            c.status = "rescinded"
            m = s.scalar(select(Membership).where(Membership.claim_id == c.id))
            if m is not None:
                s.delete(m)  # no charter amendment or expulsion confiscates a unit; only this finding removes it
        audit(s, f"claim.rescission_{outcome}", user.id, target=c.ref)
        s.commit()
        ref = c.ref
    return go(f"/labor/claims/{ref}#rescission", "Eligibility rescinded on written findings." if outcome == "rescinded"
              else "Proceeding dismissed. The claim stands.")


# --------------------------------------------------------------------------
# Section 4(a): a body larger than 2,500 units
# --------------------------------------------------------------------------
@router.post("/associations/{ref}/large-body")
def large_body(request: Request, ref: str, findings: str = Form(...), user: User = Depends(labor)):
    with SessionLocal() as s:
        a = s.scalar(visible(select(Association), Association).where(Association.ref == ref))
        if a is None:
            raise HTTPException(404)
        require_not_recused(s, user, association_id=a.id)
        passed = s.scalar(select(Ballot.id).where(Ballot.association_id == a.id, Ballot.kind == "large_body", Ballot.status == "passed"))
        if passed is None:
            raise HTTPException(400, "A larger body first needs approval by a majority of all founders.")
        if len(findings.strip()) < 40:
            raise HTTPException(400, "Write findings that accessible meetings, voting, records and member deliberation remain practical.")
        a.large_body_approved_on, a.large_body_findings = date.today(), clean(findings)
        audit(s, "association.large_body", user.id, target=ref)
        s.commit()
    return go(f"/app/associations/{ref}", "Approved: the founding roll may exceed 2,500 units.")


# --------------------------------------------------------------------------
# Section 2(e): amending a certification's scope on new evidence
# --------------------------------------------------------------------------
DATE_SCOPE = ("causal_start", "causal_end", "first_separation")


def _scope_of(e: Event) -> dict:
    return {f: (getattr(e, f).isoformat() if getattr(e, f) else "") if f in DATE_SCOPE else (getattr(e, f) or "")
            for f in SCOPE_FIELDS}


@router.post("/events/{ref}/amendments")
async def amendment_propose(request: Request, ref: str, user: User = Depends(labor)):
    """Publish a proposed amendment. Notice, the evidence period and findings follow the certification procedure."""
    form = await request.form()
    with SessionLocal() as s:
        e = _event(s, ref)
        if e.status != "certified":
            raise HTTPException(400, "Only a certified event's scope can be amended.")
        if s.scalar(select(EventAmendment.id).where(EventAmendment.event_id == e.id, EventAmendment.status == "comment_period")):
            raise HTTPException(400, "An amendment of this event is already open for evidence.")
        evidence = clean(str(form.get("new_evidence", "")))
        if len(evidence) < 40:
            raise HTTPException(400, "Describe the new evidence that supports the amendment.")
        current = _scope_of(e)
        proposed = {}
        for f in SCOPE_FIELDS:
            raw = str(form.get(f, current[f])).strip()
            if f in DATE_SCOPE:
                raw = parse_date(raw, f.replace("_", " ")).isoformat() if raw else ""
            proposed[f] = clean(raw)
        for f in ("occupations", "worksites", "geography", "causal_mechanism", "acceptable_proof", "causal_start", "first_separation"):
            if not proposed[f]:
                raise HTTPException(400, "An amended certification must still state the " + f.replace("_", " ") + ".")
        if proposed == current:
            raise HTTPException(400, "The proposal does not change the scope.")
        s.add(EventAmendment(event_id=e.id, new_evidence=evidence, proposed=proposed, proposed_by=user.id, is_demo=e.is_demo,
                             comment_ends=days_from(date.today(), COMMENT_PERIOD_DAYS)))
        audit(s, "event.amendment_propose", user.id, target=ref)
        s.commit()
    return go(f"/labor/events/{ref}#amendments", f"The proposed amendment is public. Anyone may submit evidence or responses for "
                                                 f"{COMMENT_PERIOD_DAYS} days; affected parties should be notified directly.")


def _amendment(s, aid: int) -> EventAmendment:
    a = s.get(EventAmendment, aid)
    if a is None or a.is_demo != is_preview() or a.status != "comment_period":
        raise HTTPException(404)
    return a


@router.post("/amendments/{aid}/end-evidence")
def amendment_end_evidence(request: Request, aid: int, user: User = Depends(labor)):
    preview_only()
    with SessionLocal() as s:
        a = _amendment(s, aid)
        a.comment_ends = date.today()
        ref = s.get(Event, a.event_id).ref
        audit(s, "event.amendment_end_evidence_preview", user.id, target=ref)
        s.commit()
    return go(f"/labor/events/{ref}#amendments", "Preview: the evidence period for the amendment has been ended early.")


@router.post("/amendments/{aid}/decide")
def amendment_decide(request: Request, aid: int, decision: str = Form(...), findings: str = Form(...), user: User = Depends(labor)):
    if decision not in ("adopt", "reject"):
        raise HTTPException(400)
    with SessionLocal() as s:
        a = _amendment(s, aid)
        e = s.get(Event, a.event_id)
        if date.today() < a.comment_ends:
            raise HTTPException(400, f"The evidence period runs until {a.comment_ends:%d %B %Y}.")
        if len(findings.strip()) < 40:
            raise HTTPException(400, "A reasoned decision with written findings is required.")
        if decision == "adopt":
            a.previous = _scope_of(e)
            for f, v in a.proposed.items():
                setattr(e, f, (date.fromisoformat(v) if v else None) if f in DATE_SCOPE else v)
        a.status, a.findings, a.decided_by, a.decided_on = ("adopted" if decision == "adopt" else "rejected"), clean(findings), user.id, date.today()
        audit(s, f"event.amendment_{a.status}", user.id, target=e.ref)
        s.commit()
        ref = e.ref
    return go(f"/exchange/events/{ref}", "Amendment adopted. The certification's scope is updated and the change is on the register."
              if decision == "adopt" else "Amendment rejected with written findings; the scope is unchanged.")
