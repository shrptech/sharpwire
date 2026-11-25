from fastapi import FastAPI

from app.api import alerts, health, hooks, plans, targets
from app.errors import register_error_handlers


def create_app() -> FastAPI:
    app = FastAPI(title="sharpwire")
    register_error_handlers(app)
    app.include_router(health.router)
    for module in (targets, plans, alerts, hooks):
        app.include_router(module.router)
    return app


app = create_app()
