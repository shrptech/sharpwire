import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class AppHTTPException(HTTPException):
    """An error worth branching on: the response carries `error_code` next to `detail`.

    `detail` is shown to users verbatim, so write it as what to do, in plain short sentences.
    Log the underlying exception instead of interpolating it.
    """

    def __init__(
        self, status_code: int, detail: str, *, error_code: str, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.error_code = error_code


async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    error_code = exc.error_code if isinstance(exc, AppHTTPException) else None
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "error_code": error_code},
        headers=exc.headers,
    )


async def _validation_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
    return JSONResponse(
        status_code=422, content={"detail": f"The request isn't valid: {problems}.", "error_code": "invalid_request"}
    )


async def _unexpected_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Something went wrong. Try again, or check the logs.", "error_code": "internal"},
    )


def register_error_handlers(app: FastAPI) -> None:
    # Starlette's base class, so routing errors (404, 405) get the same shape as our own.
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(Exception, _unexpected_handler)
