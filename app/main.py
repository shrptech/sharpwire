from fastapi import FastAPI

from app.api import alerts, health, hooks, plans, targets
from app.errors import register_error_handlers


def create_app() -> FastAPI:
    # No /docs or /openapi.json: the API is behind a token, and its schema shouldn't be served without one.
    app = FastAPI(title="sharpwire", docs_url=None, redoc_url=None, openapi_url=None)
    register_error_handlers(app)
    app.include_router(health.router)
    for module in (targets, plans, alerts, hooks):
        app.include_router(module.router)
    return app


app = create_app()
