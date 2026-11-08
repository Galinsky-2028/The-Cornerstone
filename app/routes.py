"""All of the Cornerstone's pages and API routes, collected into one router for Foundation."""
from fastapi import APIRouter

from foundation.web import templates

from . import seed  # noqa: F401  (registers the nightly preview rebuild)
from .program import is_preview, legal_mode
from .views import api, association, commerce, exchange, labor, preview, public, site, state, supervisor, worker

templates.env.globals.update(is_preview=is_preview, legal_mode=legal_mode)

router = APIRouter()
for module in (api, public, exchange, preview, worker, association, labor, state, commerce, supervisor, site):
    router.include_router(module.router)
