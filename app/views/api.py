"""A read-only JSON API over the public Exchange, for researchers, journalists and other services.
Every value here is already public on the Exchange pages; private worker details are never included."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select

from foundation.auth_core import require_api_key
from foundation.db import SessionLocal
from foundation.models import ApiKey

from ..models import Compact, Event, Offer, Round
from ..program import founding_units, legal_mode, visible

router = APIRouter(prefix="/api/v1")


@router.get("/ping")
def ping(key: ApiKey = Depends(require_api_key())):
    return {"ok": True, "key": key.name}


@router.get("/exchange")
def exchange(key: ApiKey = Depends(require_api_key("exchange:read"))):
    with SessionLocal() as s:
        events = s.scalars(visible(select(Event), Event).order_by(Event.id)).all()
        offers = s.scalars(visible(select(Offer), Offer).where(Offer.status != "draft").order_by(Offer.id)).all()
        rounds = s.scalars(visible(select(Round), Round).order_by(Round.id)).all()
        compacts = s.scalars(visible(select(Compact), Compact).where(Compact.status == "executed")).all()
        return {
            "mode": legal_mode(),
            "events": [{"ref": e.ref, "title": e.title, "status": e.status, "sovereign": e.sovereign,
                        "census_id": e.census_id, "first_separation": e.first_separation and e.first_separation.isoformat()}
                       for e in events],
            "offers": [{"ref": o.ref, "state": o.state_name, "district": o.district, "trade": o.trade, "status": o.status}
                       for o in offers],
            "rounds": [{"id": r.id, "name": r.name, "status": r.status, "rankings_close": r.rankings_close.isoformat(),
                        "random_seed": r.random_seed, "log": r.match_log if r.status == "matched" else None}
                       for r in rounds],
            "compacts": [{"ref": k.ref, "association": k.association.name, "founding_units": founding_units(s, k.association_id),
                          "offer": k.offer.ref, "fund_rate_bp": k.fund_rate_bp, "state_share_pct": k.surplus_rate_pct}
                         for k in compacts],
        }
