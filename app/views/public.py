"""Public pages: home, how it works, the Act and its documents, the census, the speech, keep me informed."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from foundation.db import SessionLocal, audit
from foundation.security import email_limiter, client_ip
from .common import page

from ..models import Interest

router = APIRouter()
CONTENT = Path(__file__).resolve().parent.parent / "content"
DOCS = Path(__file__).resolve().parent.parent / "static" / "documents"


@lru_cache
def documents() -> list[dict]:
    return json.loads((CONTENT / "documents.json").read_text())


@lru_cache
def census() -> dict:
    return json.loads((CONTENT / "census.json").read_text())


def census_event(cid: str) -> dict | None:
    return next((e for e in census()["events"] if e["id"] == cid), None)


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    from ..seed import warm

    warm()
    return page(request, "public/home.html", {"census_count": len(census()["events"])})


@router.get("/how-it-works", response_class=HTMLResponse)
def how_it_works(request: Request):
    return page(request, "public/how_it_works.html")


@router.get("/documents", response_class=HTMLResponse)
def documents_index(request: Request):
    return page(request, "public/documents.html", {"docs": documents()})


@router.get("/documents/{slug}", response_class=HTMLResponse)
def document_page(request: Request, slug: str):
    doc = next((d for d in documents() if d["slug"] == slug and d["readable"]), None)
    if doc is None:
        raise HTTPException(404, "No such document")
    return page(request, "public/document.html", {"doc": doc, "text": (CONTENT / f"{slug}.md").read_text()})


@router.get("/documents/{slug}/download")
def document_download(slug: str):
    doc = next((d for d in documents() if d["slug"] == slug), None)
    if doc is None:
        raise HTTPException(404)
    return FileResponse(DOCS / doc["file"], filename=doc["file"])


@router.get("/act")
def act():
    """A short, stable address for the Act itself."""
    return RedirectResponse("/documents/federal-bill", status_code=308)


@router.get("/speech", response_class=HTMLResponse)
def speech(request: Request):
    return page(request, "public/speech.html", {"text": (CONTENT / "speech.md").read_text()})


@router.get("/census", response_class=HTMLResponse)
def census_index(request: Request, q: str = "", sovereign: str = ""):
    events = census()["events"]
    sovereigns = sorted({e["sovereign"] for e in events if e["sovereign"]})
    shown = events
    if q:
        ql = q.lower()
        shown = [e for e in shown if ql in " ".join(str(v) for v in e.values()).lower()]
    if sovereign:
        shown = [e for e in shown if e["sovereign"] == sovereign]
    return page(request, "public/census.html", {"events": shown, "total": len(events), "q": q,
                                                  "sovereign": sovereign, "sovereigns": sovereigns,
                                                  "intro": census()["introduction"]})


@router.get("/census/{cid}", response_class=HTMLResponse)
def census_detail(request: Request, cid: str):
    e = census_event(cid)
    if e is None:
        raise HTTPException(404, "No census entry with that number")
    return page(request, "public/census_event.html", {"e": e})


@router.get("/keep-me-informed", response_class=HTMLResponse)
def interest_form(request: Request, census_id: str = ""):
    return page(request, "public/interest.html", {"census_id": census_id, "done": False, "error": ""})


@router.post("/keep-me-informed", response_class=HTMLResponse)
def interest_submit(request: Request, kind: str = Form(...), name: str = Form(...), email: str = Form(...),
                    state_name: str = Form(""), organization: str = Form(""), census_id: str = Form(""),
                    message: str = Form("")):
    if kind not in {"worker", "supporter", "official", "press"} or "@" not in email:
        return page(request, "public/interest.html", {"census_id": census_id, "done": False,
                      "error": "Choose who you are and enter a valid email address."}, 400)
    ip = client_ip(request)
    if email_limiter.blocked(ip):
        raise HTTPException(429, "Too many submissions from this address. Try again later.")
    email_limiter.hit(ip)
    with SessionLocal() as s:
        s.add(Interest(kind=kind, name=name.strip()[:200], email=email.strip().lower()[:320], state_name=state_name[:80],
                       organization=organization[:200], census_id=census_id[:20], message=message[:4000]))  # real sign-ups, even during the preview
        audit(s, "interest.submit", None, target=kind)
        s.commit()
    return page(request, "public/interest.html", {"census_id": "", "done": True, "error": ""})
