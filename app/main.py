from fastapi import FastAPI

from app.api import health
from app.errors import register_error_handlers


def create_app() -> FastAPI:
    app = FastAPI(title="sharpwire")
    register_error_handlers(app)
    app.include_router(health.router)
    return app


app = create_app()
