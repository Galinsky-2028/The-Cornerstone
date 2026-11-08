"""The independent State supervisor (s.6(e)): approve, modify or reject material pricing, access, service, safety
and exclusion decisions on a reasoned public record."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from foundation.db import SessionLocal, audit
from foundation.models import User

from ..models import Compact, Offer, SupervisorDecision
from ..program import is_preview, require_program_role, state_of, visible
from .common import clean, go, page

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
        return page(request, "supervisor/desk.html", {"rows": rows, "compacts": compacts,
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
        d.decision, d.reasons, d.decided_by, d.decided_on = decision, clean(reasons), user.id, date.today()
        audit(s, f"supervision.{decision}", user.id, target=d.compact.ref, kind=d.kind)
        s.commit()
    return go("/supervisor", f"Decision {decision} and published on the compact's public record.")
