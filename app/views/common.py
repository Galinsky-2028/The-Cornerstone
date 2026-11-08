"""Helpers shared by the signed-in pages: one-time notices, preview-only shortcuts, dates and money."""
from __future__ import annotations

from datetime import date

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer

from foundation.auth_core import SESSION_COOKIE
from foundation.config import settings
from foundation.web import render, templates

from ..program import is_preview

NOTE_COOKIE = "cs_note"


def _signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt="cs-note")


def go(url: str, note: str = "") -> RedirectResponse:
    """Redirect after a form, carrying a one-time confirmation for the next page."""
    resp = RedirectResponse(url, status_code=303)
    if note:
        resp.set_cookie(NOTE_COOKIE, _signer().dumps(note[:500]), max_age=60, httponly=True, samesite="lax",
                        secure=settings.https)
    return resp


def page(request: Request, name: str, ctx: dict | None = None, status: int = 200):
    ctx = dict(ctx or {})
    note = ""
    raw = request.cookies.get(NOTE_COOKIE)
    if raw:
        try:
            note = _signer().loads(raw, max_age=60)
        except BadSignature:
            note = ""
    ctx.setdefault("note", note)
    resp = render(request, name, ctx, status)
    if raw:
        resp.delete_cookie(NOTE_COOKIE)
    return resp


def set_session_cookie(resp, token: str):
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", secure=settings.https,
                    max_age=60 * 60 * 24 * settings.session_days)
    return resp


def preview_only() -> None:
    """Shortcuts that skip a statutory waiting period exist only in the preview."""
    if not is_preview():
        raise HTTPException(403, "Waiting periods cannot be shortened: the Act is in force.")


def parse_date(value: str, field: str = "date") -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(400, f"Enter the {field} as a date.")


def to_int(value: str | int | None, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        return int(str(value).replace(",", "").replace("$", "").strip())
    except ValueError:
        raise HTTPException(400, "Enter whole numbers without letters.")


def clean(text: str, limit: int = 10_000) -> str:
    return (text or "").strip()[:limit]


def money(n) -> str:
    try:
        return f"${int(n):,}"
    except (TypeError, ValueError):
        return "$0"


def pct_bp(bp) -> str:
    return f"{(bp or 0) / 100:.2f}%"


templates.env.filters["money"] = money
templates.env.filters["bp"] = pct_bp
templates.env.filters["label"] = lambda s: (s or "").replace("_", " ")
