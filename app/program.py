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
        .where(Membership.association_id == association_id, Claim.status != "remedied",
               Event.first_separation.is_not(None))
    ).scalars().all()
    return min(rows) if rows else None


def founding_units(s, association_id: int) -> int:
    return s.scalar(select(func.count()).select_from(Membership).where(Membership.association_id == association_id)) or 0


def membership_of(s, user_id: int) -> Membership | None:
    return s.scalar(select(Membership).join(Association).where(
        Membership.user_id == user_id, Association.is_demo.is_(is_preview())))


def today() -> date:
    return utcnow().date()
