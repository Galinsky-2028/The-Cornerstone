"""Shared helpers: the preview/enacted switch, program roles, reference numbers and visibility."""
from __future__ import annotations

from datetime import date

from fastapi import Depends, HTTPException, Request
from sqlalchemy import func, select

from foundation.auth_core import require_user
from foundation.config import env
from foundation.db import SessionLocal, audit, utcnow
from foundation.models import User

from .models import (
    Association, Claim, DemoAccount, Event, Membership, PROGRAM_ROLES, RoleGrant, SiteSetting,
)

# --------------------------------------------------------------------------
# The single switch: preview (the Act is not law) or enacted (the Act is law)
# --------------------------------------------------------------------------
MODE_KEY = "legal_mode"


def legal_mode() -> str:
    with SessionLocal() as s:
        row = s.get(SiteSetting, MODE_KEY)
    if row and row.value in {"preview", "enacted"}:
        return row.value
    return "enacted" if env("APP_LEGAL_MODE", "preview") == "enacted" else "preview"


def is_preview() -> bool:
    return legal_mode() == "preview"


def set_legal_mode(mode: str, actor_id: int) -> None:
    """Switch the whole site. Entering enacted mode switches off every preview account; preview records stay in
    the database for the audit trail but are excluded from every page and list."""
    if mode not in {"preview", "enacted"}:
        raise ValueError(mode)
    with SessionLocal() as s:
        row = s.get(SiteSetting, MODE_KEY)
        if row is None:
            s.add(SiteSetting(key=MODE_KEY, value=mode))
        else:
            row.value = mode
        deactivated = 0
        if mode == "enacted":
            for d in s.scalars(select(DemoAccount)).all():
                u = s.get(User, d.user_id)
                if u and u.active:
                    u.active = False
                    deactivated += 1
        else:
            for d in s.scalars(select(DemoAccount)).all():
                u = s.get(User, d.user_id)
                if u:
                    u.active = True
        audit(s, "site.legal_mode", actor_id, target=mode, demo_accounts_switched_off=deactivated)
        s.commit()
    if mode == "enacted":
        from foundation.auth_core import end_all_sessions

        with SessionLocal() as s:
            ids = [d.user_id for d in s.scalars(select(DemoAccount)).all()]
        for uid in ids:
            end_all_sessions(uid)


def visible(stmt, model):
    """Restrict a query to the records that belong to the current mode."""
    return stmt.where(model.is_demo.is_(is_preview()))


def new_is_demo() -> bool:
    return is_preview()


# --------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------
def roles_of(user_id: int) -> dict[str, RoleGrant]:
    with SessionLocal() as s:
        return {g.role: g for g in s.scalars(select(RoleGrant).where(RoleGrant.user_id == user_id)).all()}


def ensure_worker_role(user: User) -> None:
    with SessionLocal() as s:
        if s.scalar(select(RoleGrant).where(RoleGrant.user_id == user.id, RoleGrant.role == "worker")) is None:
            s.add(RoleGrant(user_id=user.id, role="worker"))
            s.commit()


def require_program_role(role: str):
    """A program role (labor, commerce, state, supervisor). Site administrators may act in any role."""

    def dep(request: Request, user: User = Depends(require_user)) -> User:
        if user.role == "admin" or role in roles_of(user.id) or (role == "worker"):
            request.state.program_roles = roles_of(user.id)
            return user
        raise HTTPException(403, f"This page is for the {PROGRAM_ROLES[role]} role.")

    return dep


def state_of(user: User, role: str) -> str:
    g = roles_of(user.id).get(role)
    return g.state_name if g else ""


def is_demo_user(user_id: int) -> bool:
    with SessionLocal() as s:
        return s.get(DemoAccount, user_id) is not None


# --------------------------------------------------------------------------
# Reference numbers: E-0001, CL-00001, A-001, O-001, K-001
# --------------------------------------------------------------------------
def next_ref(s, model, prefix: str, width: int) -> str:
    n = (s.scalar(select(func.count()).select_from(model)) or 0) + 1
    while True:
        ref = f"{prefix}-{n:0{width}d}"
        if s.scalar(select(model.id).where(model.ref == ref)) is None:
            return ref
        n += 1


# --------------------------------------------------------------------------
# Section 5(b) priority date for an Association
# --------------------------------------------------------------------------
def association_priority(s, association_id: int) -> date | None:
    """The first proven attributable separation of the oldest still-unremedied certified event represented in
    the closed founding roll."""
    rows = s.execute(
        select(Event.first_separation)
        .join(Claim, Claim.event_id == Event.id)
        .join(Membership, Membership.claim_id == Claim.id)
        .where(Membership.association_id == association_id, Membership.status == "founding",
               Claim.status != "remedied", Event.first_separation.is_not(None))
    ).scalars().all()
    return min(rows) if rows else None


def founding_units(s, association_id: int) -> int:
    """Units held by certified claims. Cohort applicants still under review hold none until certified."""
    return s.scalar(select(func.count()).select_from(Membership).where(
        Membership.association_id == association_id, Membership.status == "founding")) or 0


def roll_size(s, association_id: int) -> int:
    """Everyone on the roll, cohort applicants included (for the 2,500-unit ceiling)."""
    return s.scalar(select(func.count()).select_from(Membership).where(Membership.association_id == association_id)) or 0


def round_applicants(s, round_id: int) -> list[dict]:
    """Everyone entered in a round: associations with an approved sealed ranking, and approved consortia.

    Each entry: key (the association id, or minus the consortium id), ref, association_ids, priority, ranking,
    consortium (or None), and `problem` when it may not be entered (then it is left out of matching)."""
    from .models import Consortium, Ranking

    out = []
    in_consortium: set[int] = set()
    for c in s.scalars(select(Consortium).where(Consortium.round_id == round_id,
                                                Consortium.status.in_(["approved", "matched", "failed"]))).all():
        in_consortium.update(c.association_ids)
        assocs = [s.get(Association, i) for i in c.association_ids]
        pri = [p for p in (association_priority(s, a.id) for a in assocs if a) if p is not None]
        problem = ""
        if any(a is None or founding_units(s, a.id) != int((c.units_snapshot or {}).get(str(a.id), -1)) for a in assocs):
            problem = "a founding roll changed after the consortium was approved"
        elif c.status == "approved" and any(a.status != "roll_closed" for a in assocs):
            problem = "a constituent is not free to be matched"
        elif not pri:
            problem = "no unremedied certified event"
        out.append({"key": -c.id, "ref": c.ref, "association_ids": list(c.association_ids), "priority": min(pri) if pri else None,
                    "ranking": list(c.offer_ids), "consortium": c, "problem": problem})
    for rk in s.scalars(select(Ranking).where(Ranking.round_id == round_id, Ranking.status == "submitted")).all():
        a = rk.association
        p = association_priority(s, a.id)
        problem = ""
        if a.id in in_consortium:
            problem = "the association entered this round as part of a consortium"
        elif p is None:
            problem = "no unremedied certified event"
        out.append({"key": a.id, "ref": a.ref, "association_ids": [a.id], "priority": p, "ranking": list(rk.offer_ids),
                    "consortium": None, "problem": problem, "status_ok": a.status == "roll_closed"})
    return out


def membership_of(s, user_id: int) -> Membership | None:
    return s.scalar(select(Membership).join(Association).where(
        Membership.user_id == user_id, Association.is_demo.is_(is_preview())))


def today() -> date:
    return utcnow().date()


# --------------------------------------------------------------------------
# Section 9(b): disclosed conflicts bar the official from deciding
# --------------------------------------------------------------------------
def require_not_recused(s, user: User, association_id: int | None = None, offer_id: int | None = None) -> None:
    from fastapi import HTTPException

    from .models import ConflictDisclosure

    conds = []
    if association_id is not None:
        conds.append(ConflictDisclosure.association_id == association_id)
    if offer_id is not None:
        conds.append(ConflictDisclosure.offer_id == offer_id)
    if not conds:
        return
    from sqlalchemy import or_

    hit = s.scalar(select(ConflictDisclosure.id).where(ConflictDisclosure.user_id == user.id, or_(*conds),
                                                       ConflictDisclosure.is_demo.is_(is_preview())).limit(1))
    if hit is not None:
        raise HTTPException(403, "You disclosed a conflict in this matter and are recused from deciding it (section 9(b)).")
