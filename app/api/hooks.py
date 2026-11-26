"""NetBox's webhook, called by the event rules on fabric changes (docs/06-drift-and-approvals.md).

NetBox signs each call with the webhook's secret: X-Hook-Signature is the hex HMAC-SHA512 of the body. A call only
queues a reconcile of the whole fabric, `netbox_debounce_seconds` from now, which the changes that follow join. The
body isn't otherwise read. The event rules themselves leave out sharpwire's own NetBox user.
"""

import hashlib
import hmac

from fastapi import APIRouter, Header, Request

from app.api._deps import Session, SettingsDep
from app.errors import AppHTTPException
from app.jobs import queue

router = APIRouter(prefix="/hooks", tags=["hooks"])


@router.post("/netbox", status_code=202)
async def netbox_changed(
    request: Request, session: Session, settings: SettingsDep, x_hook_signature: str = Header(default="")
) -> dict[str, bool]:
    body = await request.body()
    expected = hmac.new(settings.netbox_webhook_secret.encode(), body, hashlib.sha512).hexdigest()
    # Bytes, not str: compare_digest refuses non-ASCII str, and a header is the caller's to choose.
    if not hmac.compare_digest(expected.encode(), x_hook_signature.lower().encode("utf-8", "replace")):
        raise AppHTTPException(
            401, "The webhook signature doesn't match. Check its secret in NetBox.", error_code="bad_signature"
        )
    queued = await queue.enqueue(session, "reconcile", delay_seconds=settings.netbox_debounce_seconds)
    await session.commit()
    return {"queued": queued}
