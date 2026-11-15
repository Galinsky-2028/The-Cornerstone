"""Section 9(b): decisionmakers disclose conflicts and recuse. Any official may file a disclosure for an
Association or an offer; it is published at once and bars its author from deciding that matter."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from foundation.auth_core import require_user
from foundation.db import SessionLocal, audit
from foundation.models import User

from ..models import PROGRAM_ROLES, Association, ConflictDisclosure, Offer
from ..program import is_preview, roles_of, visible
from .common import clean, go, page

router = APIRouter()
OFFICIAL_ROLES = ("labor", "commerce", "state", "supervisor")


def _official_roles(user: User) -> list[str]:
    held = [r for r in OFFICIAL_ROLES if r in roles_of(user.id)]
    return held or (list(OFFICIAL_ROLES) if user.role == "admin" else [])


@router.get("/app/conflicts", response_class=HTMLResponse)
def conflicts_form(request: Request, user: User = Depends(require_user)):
    roles = _official_roles(user)
    if not roles:
        raise HTTPException(403, "Conflict disclosures are filed by officials.")
    with SessionLocal() as s:
        mine = s.scalars(visible(select(ConflictDisclosure), ConflictDisclosure)
                         .where(ConflictDisclosure.user_id == user.id).order_by(ConflictDisclosure.id.desc())).all()
        refs = _refs(s, mine)
        return page(request, "conflicts.html", {"mine": mine, "refs": refs, "roles": {r: PROGRAM_ROLES[r] for r in roles}})


def _refs(s, rows) -> dict:
    out = {}
    for d in rows:
        if d.association_id:
            a = s.get(Association, d.association_id)
            out[d.id] = f"{a.ref} {a.name}" if a else ""
        elif d.offer_id:
            o = s.get(Offer, d.offer_id)
            out[d.id] = f"{o.ref} {o.trade}" if o else ""
    return out


@router.post("/app/conflicts")
def conflicts_file(request: Request, matter: str = Form(...), nature: str = Form(...), role: str = Form(...),
                   user: User = Depends(require_user)):
    roles = _official_roles(user)
    if role not in roles:
        raise HTTPException(403, "Conflict disclosures are filed by officials.")
    if len(nature.strip()) < 20:
        raise HTTPException(400, "Describe the conflict.")
    ref = matter.strip().upper()
    with SessionLocal() as s:
        a = s.scalar(visible(select(Association), Association).where(Association.ref == ref))
        o = None if a else s.scalar(visible(select(Offer), Offer).where(Offer.ref == ref))
        if a is None and o is None:
            raise HTTPException(400, "Name an association (A-…) or an offer (O-…).")
        s.add(ConflictDisclosure(user_id=user.id, official_name=user.name or user.email, role=role,
                                 association_id=a.id if a else None, offer_id=o.id if o else None,
                                 nature=clean(nature, 4000), is_demo=is_preview()))
        audit(s, "conflict.disclose", user.id, target=ref)
        s.commit()
    return go("/app/conflicts", f"Disclosed and published. You are recused from deciding any matter of {ref}.")
