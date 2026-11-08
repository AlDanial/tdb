"""Access control for the JSON-RPC debug server.

The server can evaluate expressions in the debuggee and read local
files, so anything that can reach it can run code as the user. It only
listens on 127.0.0.1, which leaves two ways in:

- Another local user on the same machine. Defense: a random bearer
  token, written to a file only the user can read. Every request to
  /rpc and /events must carry ``Authorization: Bearer <token>``.
- A web page using DNS rebinding: its own hostname re-pointed at
  127.0.0.1, so the browser treats the requests as same-origin.
  Defense: the Host header must name a loopback address and our port,
  and an Origin header, when there is one, must too.

``--insecure-no-auth`` drops the token (for trusted single-user
machines and old scripts); the Host/Origin check always stays on.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

TOKEN_ENV = "TDB_SERVER_TOKEN"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def resolve_token(*, insecure: bool) -> str | None:
    """The token clients must present, or None when auth is disabled.
    $TDB_SERVER_TOKEN lets a script choose it in advance."""
    if insecure:
        return None
    return os.environ.get(TOKEN_ENV) or secrets.token_urlsafe(32)


def setup_token(*, insecure: bool, port: int) -> tuple[str | None, Path | None]:
    """(token, token file) for a server on `port`. A token the caller
    chose via $TDB_SERVER_TOKEN is already known to them: no file."""
    token = resolve_token(insecure=insecure)
    if token is None or os.environ.get(TOKEN_ENV) == token:
        return token, None
    return token, write_token_file(token, port)


def token_file_path(port: int, directory: Path | None = None) -> Path:
    if directory is None:
        from tdb.persist import CONFIG_DIR

        directory = CONFIG_DIR
    return Path(directory) / f"server-{port}.token"


def write_token_file(
    token: str, port: int, directory: Path | None = None
) -> Path | None:
    """Write `token` to a file only this user can read. None when the
    directory can't be written (read-only config dir): the caller then
    shows the token itself."""
    path = token_file_path(port, directory)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Replace whatever is there (a stale file, or a planted symlink)
        # with a file we create exclusively, mode 0600 from the start.
        path.unlink(missing_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(token + "\n")
    except OSError as e:
        log.warning("Could not write server token file %s: %s", path, e)
        return None
    return path


def describe_token(token: str | None, token_file: Path | None) -> str:
    """One line for the user: where the token is, or that there is none."""
    if token is None:
        return (
            "WARNING: --insecure-no-auth: any local user can drive this "
            "debugger (no token required)"
        )
    if os.environ.get(TOKEN_ENV) == token:
        return f"Clients must send 'Authorization: Bearer ${TOKEN_ENV}'"
    if token_file is not None:
        return f"Clients must send 'Authorization: Bearer <token>'; token in {token_file}"
    return f"Clients must send 'Authorization: Bearer {token}'"


def remove_token_file(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _split_host(value: str) -> tuple[str, int | None] | None:
    """'host[:port]' (IPv6 bracketed) -> (host, port); None if malformed."""
    try:
        parts = urlsplit("//" + value)
        return (parts.hostname or "", parts.port)
    except ValueError:
        return None


def _is_local(host_port: str, port: int) -> bool:
    split = _split_host(host_port)
    if split is None:
        return False
    host, got_port = split
    return host in LOOPBACK_HOSTS and got_port in (None, port)


def _origin_is_local(origin: str, port: int) -> bool:
    parts = urlsplit(origin)
    return parts.scheme in ("http", "https") and _is_local(parts.netloc, port)


class AccessGuard:
    """ASGI middleware enforcing the Host/Origin check and the token on
    every HTTP request, before any route (or the request body) runs."""

    def __init__(self, app, *, token: str | None, port: int) -> None:
        self.app = app
        self.token = token
        self.port = port

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope["headers"]
        }
        status = self._check(headers)
        if status is None:
            await self.app(scope, receive, send)
            return
        code, message = status
        body = json.dumps({"success": False, "value": message}).encode()
        extra = [(b"www-authenticate", b"Bearer")] if code == 401 else []
        await send(
            {
                "type": "http.response.start",
                "status": code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    *extra,
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def _check(self, headers: dict[str, str]) -> tuple[int, str] | None:
        if not _is_local(headers.get("host", ""), self.port):
            return 421, "Host header must be a loopback address and this port"
        origin = headers.get("origin")
        if origin is not None and not _origin_is_local(origin, self.port):
            return 403, "cross-origin requests are not allowed"
        if self.token is None:
            return None
        scheme, _, presented = headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            presented.strip().encode(), self.token.encode()
        ):
            return 401, (
                "missing or wrong token: send 'Authorization: Bearer <token>' "
                "(the token is in the file tdb printed at startup, or "
                "$TDB_SERVER_TOKEN)"
            )
        return None
