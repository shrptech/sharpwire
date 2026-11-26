"""Reading a RouterOS device's live state over REST (RouterOS 7.25, HTTPS on the mgmt VLAN).

It reads only the menus a role renders into, and of each row only the managed fields (`.proplist`), plus `.id`,
`.nextid` and `dynamic`, which the diff needs for order and to skip dynamic rows. It never writes.

The device's certificate is checked against the CA the bootstrap issued it from, hostname included: devices are
reached by their mgmt address, which the certificate names.

Behaviour this relies on, checked on a CHR running 7.25.1 (tests/fixtures/routeros/):
- `.proplist` limits a GET to the listed fields; unknown names are ignored, and `.id` always comes back;
- errors come back as `{"detail": ..., "error": <status>, "message": ...}`, and a menu the device doesn't have as
  400 "no such command prefix" (a CHR has no `/interface/bridge/mlag`).
"""

import logging
import ssl
from collections.abc import Iterable
from ipaddress import IPv4Address, IPv6Address
from typing import Any, Self

import httpx

from app.drivers.routeros.diff import LiveState
from app.drivers.routeros.fields import FIELDS

logger = logging.getLogger(__name__)

# The RouterOS release line sharpwire is tested against (docs/04-drivers.md). Anything else is refused.
SUPPORTED_VERSION = "7.25"
TIMEOUT_SECONDS = 30
_ALWAYS = (".id", ".nextid", "dynamic")


class RouterOSError(Exception):
    """A failure reading the device. `detail` is safe to show: it never holds credentials."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class UnsupportedVersion(RouterOSError):
    pass


def base_url(address: IPv4Address | IPv6Address, port: int = 443) -> str:
    host = f"[{address}]" if isinstance(address, IPv6Address) else str(address)
    return f"https://{host}:{port}/rest"


def tls_context(ca_file: str) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=ca_file)


def version_supported(version: str) -> bool:
    """`7.25.1` or `7.25.1 (stable)` is on the 7.25 line; `7.250` and `7.2` aren't."""
    number = version.split()[0] if version else ""
    return number == SUPPORTED_VERSION or number.startswith(f"{SUPPORTED_VERSION}.")


def _is_row(value: Any) -> bool:
    return isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())


class RouterOSClient:
    """Read-only. `verify` must be an SSL context trusting the bootstrap CA: there's no way to turn checks off. Proxy
    environment variables are ignored, so mgmt traffic always goes straight to the device.
    """

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        verify: ssl.SSLContext,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not isinstance(verify, ssl.SSLContext):
            raise TypeError("verify must be an ssl.SSLContext (see tls_context)")
        self.url = url
        self._http = httpx.AsyncClient(
            base_url=url,
            auth=(username, password),
            verify=verify,
            timeout=TIMEOUT_SECONDS,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        try:
            response = await self._http.get(f"/{path}", params=params)
        except httpx.HTTPError as exc:
            logger.warning("RouterOS GET %s/%s failed", self.url, path, exc_info=True)
            raise RouterOSError(f"{self.url} didn't answer ({type(exc).__name__}). Check it's reachable.") from exc
        if response.status_code == 401:
            raise RouterOSError(f"{self.url} refused sharpwire's login. Check the service account.")
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code >= 400:
            detail = body.get("detail") if isinstance(body, dict) else None
            raise RouterOSError(f"{self.url} failed to read /{path}: {detail or f'HTTP {response.status_code}'}")
        if response.status_code != 200 or body is None:
            raise RouterOSError(f"{self.url} answered /{path} with something other than RouterOS JSON.")
        return body

    async def version(self) -> str:
        resource = await self._get("system/resource")
        if not isinstance(resource, dict) or not isinstance(resource.get("version"), str):
            raise RouterOSError(f"{self.url} didn't say which RouterOS it runs.")
        return str(resource["version"])

    async def read(self, menus: Iterable[str]) -> LiveState:
        """Every row of each menu, with the managed fields only. Refuses a RouterOS outside the supported line."""
        version = await self.version()
        if not version_supported(version):
            raise UnsupportedVersion(
                f"{self.url} runs RouterOS {version}; sharpwire supports {SUPPORTED_VERSION}.x only. "
                "Upgrade or downgrade it first."
            )
        live: dict[str, Any] = {}
        for path in menus:
            proplist = ",".join((*_ALWAYS, *FIELDS[path]))
            rows = await self._get(path, {".proplist": proplist})
            # A settings menu is one object; every other menu a list of rows. All values are strings.
            if not (_is_row(rows) or (isinstance(rows, list) and all(_is_row(r) for r in rows))):
                raise RouterOSError(f"{self.url} returned /{path} in a shape RouterOS doesn't use.")
            live[path] = rows
        return live
