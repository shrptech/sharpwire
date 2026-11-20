from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Report that the controller process is up. It doesn't check the database or any target."""
    return {"status": "ok"}
