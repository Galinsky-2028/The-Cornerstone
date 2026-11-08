"""Site administration for the Cornerstone: the single preview/enacted switch, protected by its own password,
plus resetting the preview, statistics and the interest list.

The switch is guarded four ways:
  1. only a site administrator can reach it;
  2. it needs the separate switch password, stored only as a scrypt hash, which only someone who knows the
     current switch password can change;
  3. the administrator must type the exact confirmation phrase for the direction of the change;
  4. attempts are rate-limited, every attempt (including failures) is audited, and every administrator is
     emailed whenever the mode changes or the switch password is set or changed.
"""
from __future__ import annotations

import csv
import io
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select

from foundation.auth_core import require_role
from foundation.config import settings
from foundation.db import SessionLocal, audit
from foundation.mail import send_email
from foundation.models import AuditEvent, User
from foundation.security import RateLimiter, client_ip, hash_password, password_problem, verify_password

from ..models import Association, Claim, Compact, DemoAccount, Event, Interest, Offer, SiteSetting
from ..program import legal_mode, set_legal_mode
from .common import go, page

router = APIRouter(prefix="/site")
admin_only = require_role("admin")
PASSWORD_KEY = "switch_password_hash"
switch_limiter = RateLimiter(max_events=5, window_seconds=3600)
PHRASES = {"enacted": "THE ACT IS LAW", "preview": "RETURN TO PREVIEW"}


def _switch_hash(s) -> str:
    row = s.get(SiteSetting, PASSWORD_KEY)
    return row.value if row else ""


def _notify_admins(subject: str, body: str) -> None:
    with SessionLocal() as s:
        emails = s.scalars(select(User.email).where(User.role == "admin", User.active.is_(True))).all()
    for e in emails:
        try:
            send_email(e, f"[{settings.app_name}] {subject}", body)
        except Exception:  # an email failure must never block or undo the change itself
            pass


def _limit(request: Request, user: User) -> str:
    ip = client_ip(request)
    key = f"{user.id}:{ip}"
    if switch_limiter.blocked(key):
        raise HTTPException(429, "Too many attempts. The switch is locked for an hour.")
    return key


@router.get("", response_class=HTMLResponse)
def site_home(request: Request, user: User = Depends(admin_only)):
    with SessionLocal() as s:
        stats = {}
        for label, model in (("Events", Event), ("Claims", Claim), ("Associations", Association), ("Offers", Offer),
                             ("Compacts", Compact), ("Interest sign-ups", Interest)):
            stats[label] = {"real": s.scalar(select(func.count()).select_from(model).where(model.is_demo.is_(False))) or 0,
                            "demo": s.scalar(select(func.count()).select_from(model).where(model.is_demo.is_(True))) or 0}
        preview_accounts = s.scalar(select(func.count()).select_from(DemoAccount).where(DemoAccount.persona != "seed")) or 0
        interest = s.scalars(select(Interest).where(Interest.is_demo.is_(False)).order_by(Interest.id.desc()).limit(200)).all()
        history = s.scalars(select(AuditEvent).where(AuditEvent.action.like("site.%")).order_by(AuditEvent.id.desc()).limit(30)).all()
        has_password = bool(_switch_hash(s))
    return page(request, "site/index.html", {"mode": legal_mode(), "stats": stats, "preview_accounts": preview_accounts,
                                              "interest": interest, "history": history, "has_password": has_password,
                                              "phrases": PHRASES})


@router.post("/switch-password")
def set_switch_password(request: Request, current: str = Form(""), new: str = Form(...), confirm: str = Form(...),
                        user: User = Depends(admin_only)):
    key = _limit(request, user)
    if new != confirm:
        return go("/site#password", "The two new passwords do not match.")
    problem = password_problem(new)
    if problem or len(new) < 14:
        return go("/site#password", problem or "Use at least 14 characters for the switch password.")
    with SessionLocal() as s:
        stored = _switch_hash(s)
        if stored and not verify_password(current, stored):
            switch_limiter.hit(key)
            audit(s, "site.switch_password_failed", user.id, ip=client_ip(request))
            s.commit()
            return go("/site#password", "The current switch password is wrong.")
        if verify_password(new, user.password_hash):
            return go("/site#password", "Use a different password from your sign-in password.")
        row = s.get(SiteSetting, PASSWORD_KEY)
        if row is None:
            s.add(SiteSetting(key=PASSWORD_KEY, value=hash_password(new)))
        else:
            row.value = hash_password(new)
        action = "site.switch_password_changed" if stored else "site.switch_password_set"
        audit(s, action, user.id, ip=client_ip(request))
        s.commit()
    _notify_admins("The switch password was " + ("changed" if stored else "set"),
                   f"{user.name or user.email} {'changed' if stored else 'set'} the password that protects the "
                   f"preview/enacted switch on {date.today():%d %B %Y}.\n\nIf this was not expected, sign in and change it now.")
    return go("/site#password", "The switch password is " + ("changed." if stored else "set. Keep it somewhere safe; it cannot be recovered."))


@router.post("/mode")
def switch_mode(request: Request, mode: str = Form(...), password: str = Form(...), phrase: str = Form(...),
                user: User = Depends(admin_only)):
    if mode not in PHRASES:
        raise HTTPException(400)
    key = _limit(request, user)
    with SessionLocal() as s:
        stored = _switch_hash(s)
        if not stored:
            return go("/site#switch", "Set a switch password first.")
        if mode == legal_mode():
            return go("/site#switch", f"The site is already in {mode} mode.")
        ok = verify_password(password, stored) and phrase.strip() == PHRASES[mode]
        if not ok:
            switch_limiter.hit(key)
            audit(s, "site.switch_failed", user.id, target=mode, ip=client_ip(request))
            s.commit()
            return go("/site#switch", "The switch password or the confirmation phrase is wrong. Nothing was changed.")
    previous = legal_mode()
    set_legal_mode(mode, user.id)
    switch_limiter.reset(key)
    _notify_admins(f"The site is now in {mode} mode",
                   f"{user.name or user.email} switched the Cornerstone from {previous} to {mode} mode on "
                   f"{date.today():%d %B %Y} from {client_ip(request)}.\n\n"
                   + ("Preview accounts are switched off and every example record is hidden. The register starts empty."
                      if mode == "enacted" else "Preview warnings and example records are shown again.")
                   + "\n\nIf this was not expected, sign in, change the switch password and switch back.")
    return go("/site#switch", f"The site is now in {mode} mode.")


@router.post("/reset-preview")
def reset_preview(request: Request, user: User = Depends(admin_only)):
    from ..seed import reset_demo_world

    if legal_mode() != "preview":
        raise HTTPException(400, "The preview has closed.")
    reset_demo_world(user.id)
    return go("/site", "The preview has been reset to its starting example world. Every preview account was removed.")


def _cell(v: str) -> str:
    """Stop spreadsheet programs treating a submitted value as a formula."""
    v = v or ""
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


@router.get("/interest.csv")
def interest_csv(user: User = Depends(admin_only)):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["date", "kind", "name", "email", "state", "organization", "census_id", "message"])
    with SessionLocal() as s:
        for i in s.scalars(select(Interest).where(Interest.is_demo.is_(False)).order_by(Interest.id)).all():
            w.writerow([_cell(v) for v in (f"{i.created_at:%Y-%m-%d}", i.kind, i.name, i.email, i.state_name,
                                           i.organization, i.census_id, i.message)])
        audit(s, "site.interest_export", user.id)
        s.commit()
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="cornerstone-interest.csv"'})
