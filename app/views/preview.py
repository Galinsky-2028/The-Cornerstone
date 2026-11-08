"""The public preview: anyone can pick a role and walk through the program with a fresh example account."""
from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from foundation.security import RateLimiter, client_ip

from ..program import is_preview
from ..seed import PERSONAS, create_persona
from .common import go, page, set_session_cookie

router = APIRouter()
preview_limiter = RateLimiter(max_events=30, window_seconds=3600)

# A guided path for each role; shown on the dashboard of a preview account.
TOURS = {
    "new_worker": [
        ("Read the certified example event your work was affected by.", "/exchange/events/E-0003"),
        ("File a claim. The form checks your facts against the Act's tests as you file.", "/app/claims/new?event=E-0003"),
        ("Upload a payroll record or tax form as evidence.", None),
        ("Sign in as the Labor examiner (open the preview in a private window) to decide it, or wait for an example decision.", "/preview"),
        ("Join the enrolling association for your event, or form a new one.", "/app/associations"),
        ("Petition for a new event yourself from any Census entry.", "/census"),
    ],
    "member": [
        ("Open your association. You hold one founding unit, the same as every member.", "/app/associations/A-001"),
        ("Vote on the board's sealed ranking of State offers. It passes only with a majority of ALL founding units.", "/app/associations/A-001#ballots"),
        ("Choose your opportunity: training, employment, a franchise or owner-only.", "/app/associations/A-001#opportunity"),
        ("Compare the offers your association ranked.", "/exchange#offers"),
        ("When rankings close, the Exchange runs the published matching rule. The Commerce role can close the round now.", "/exchange#rounds"),
        ("See an association that already operates a compact, with its Fund repayments and supervisor decisions.", "/exchange/compacts/K-001"),
    ],
    "labor": [
        ("Close the evidence period on the Centralia petition, then certify or deny it with findings.", "/labor/events/E-0004"),
        ("Decide two individual claims. The Act's tests are checked for you; you decide what needs judgment.", "/labor#claims"),
        ("Hear a denied worker's appeal.", "/labor#hearings"),
        ("Approve and disburse a Founding Fund advance for an independent feasibility review.", "/labor#fund"),
        ("Confirm restoration for the Willamette mill workers once operations begin.", "/labor/restorations/A-004"),
    ],
    "state": [
        ("Complete your draft offer: the exact prohibition, the authority to lift it, and the bounded terms.", "/state"),
        ("Publish it for 60 days of objections and prove it takes no existing job, license or market.", "/state"),
        ("Answer an objection on the record, then submit the offer to Commerce.", "/state"),
        ("Draft and execute a compact with an association after a match.", "/state#matches"),
    ],
    "commerce": [
        ("Verify that a State offer is complete and publish it to the Exchange.", "/commerce"),
        ("Close Round 1 and run the matching rule. The step-by-step log is published so anyone can reproduce it.", "/commerce#rounds"),
        ("Open a new round with a fresh public random seed.", "/commerce#rounds"),
    ],
    "supervisor": [
        ("Decide a pending pricing request from the Willamette enterprise on a reasoned public record.", "/supervisor"),
        ("See the public record of earlier decisions.", "/exchange/compacts/K-001"),
    ],
}


@router.get("/preview", response_class=HTMLResponse)
def preview_home(request: Request):
    from ..seed import warm

    warm()
    if not is_preview():
        return page(request, "preview/closed.html", status=410)
    return page(request, "preview/index.html", {"personas": PERSONAS, "tours": TOURS})


@router.post("/preview/start")
def preview_start(request: Request, persona: str = Form(...)):
    if not is_preview():
        raise HTTPException(410, "The preview has closed: the Act is in force.")
    if persona not in PERSONAS:
        raise HTTPException(400, "Choose one of the roles.")
    ip = client_ip(request)
    if preview_limiter.blocked(ip):
        raise HTTPException(429, "Too many preview accounts from this address. Try again in an hour.")
    preview_limiter.hit(ip)
    _user, token = create_persona(persona)
    resp = go("/app", f"You are signed in as {PERSONAS[persona]['title'].lower()}. Nothing you do here is legally effective.")
    return set_session_cookie(resp, token)
