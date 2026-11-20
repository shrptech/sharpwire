from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class AppHTTPException(HTTPException):
    """An error worth branching on: the response carries `error_code` next to `detail`.

    `detail` is shown to users verbatim, so write it as what to do, in plain short sentences.
    Log the underlying exception instead of interpolating it.
    """

    def __init__(self, status_code: int, detail: str, *, error_code: str) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.error_code = error_code


async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    error_code = exc.error_code if isinstance(exc, AppHTTPException) else None
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "error_code": error_code},
        headers=exc.headers,
    )


def register_error_handlers(app: FastAPI) -> None:
    # Starlette's base class, so routing errors (404, 405) get the same shape as our own.
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
