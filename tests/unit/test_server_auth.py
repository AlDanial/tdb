"""Access control for the JSON-RPC debug server: Host/Origin validation
(DNS-rebinding defense) and a bearer token (other local users)."""

from __future__ import annotations

import os
import stat
from unittest.mock import AsyncMock

import httpx
import pytest

from tdb.server import auth
from tdb.server.app import create_app
from tdb.server.rpc_types import RpcResponse

PORT = 8150
TOKEN = "s3cret-token"


class _Handlers:
    """Just enough of RpcHandlers for create_app."""

    def __init__(self):
        import asyncio

        self.session_lock = asyncio.Lock()
        self.evaluate = AsyncMock(return_value=RpcResponse.ok("42"))
        self.event_handler = None

    def dispatch_table(self):
        return {"evaluate": self.evaluate}


def _client(handlers, token=TOKEN, host=f"127.0.0.1:{PORT}"):
    app = create_app(handlers, token=token, port=PORT)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=f"http://{host}"
    )


BODY = {"action": "evaluate", "params": ["1+1"]}
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.mark.asyncio
async def test_valid_token_and_host_is_served():
    h = _Handlers()
    async with _client(h) as c:
        r = await c.post("/rpc", json=BODY, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["success"] is True
    h.evaluate.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["localhost", "[::1]", "127.0.0.1"])
async def test_loopback_names_accepted(host):
    h = _Handlers()
    async with _client(h, host=f"{host}:{PORT}") as c:
        r = await c.post("/rpc", json=BODY, headers=AUTH)
    assert r.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}],
    ids=["missing", "wrong", "no-scheme"],
)
async def test_bad_token_rejected_before_dispatch(headers):
    h = _Handlers()
    async with _client(h) as c:
        r = await c.post("/rpc", json=BODY, headers=headers)
    assert r.status_code == 401
    h.evaluate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host", ["evil.example:8150", "127.0.0.1:9999", "127.0.0.1.evil.example:8150"]
)
async def test_foreign_host_rejected_even_with_token(host):
    """DNS rebinding: the attacker's page reaches 127.0.0.1 under its own
    hostname, so the Host header is what gives it away."""
    h = _Handlers()
    async with _client(h, host=host) as c:
        r = await c.post("/rpc", json=BODY, headers=AUTH)
    assert r.status_code == 421
    h.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_foreign_origin_rejected():
    h = _Handlers()
    async with _client(h) as c:
        r = await c.post(
            "/rpc", json=BODY, headers={**AUTH, "Origin": "http://evil.example"}
        )
    assert r.status_code == 403
    h.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_loopback_origin_allowed():
    h = _Handlers()
    async with _client(h) as c:
        r = await c.post(
            "/rpc", json=BODY, headers={**AUTH, "Origin": f"http://localhost:{PORT}"}
        )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_events_endpoint_is_guarded_too():
    h = _Handlers()
    async with _client(h) as c:
        r = await c.get("/events")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_insecure_mode_skips_token_but_keeps_host_check():
    h = _Handlers()
    async with _client(h, token=None) as c:
        ok = await c.post("/rpc", json=BODY)
    assert ok.status_code == 200
    async with _client(h, token=None, host="evil.example:8150") as c:
        bad = await c.post("/rpc", json=BODY)
    assert bad.status_code == 421


# --- token resolution + token file ---------------------------------------


def test_resolve_token_insecure_is_none(monkeypatch):
    monkeypatch.setenv("TDB_SERVER_TOKEN", "x")
    assert auth.resolve_token(insecure=True) is None


def test_resolve_token_from_env(monkeypatch):
    monkeypatch.setenv("TDB_SERVER_TOKEN", "from-env")
    assert auth.resolve_token(insecure=False) == "from-env"


def test_resolve_token_random_when_unset(monkeypatch):
    monkeypatch.delenv("TDB_SERVER_TOKEN", raising=False)
    a, b = auth.resolve_token(insecure=False), auth.resolve_token(insecure=False)
    assert a and b and a != b and len(a) >= 32


def test_token_file_is_private_and_removed(tmp_path):
    path = auth.write_token_file("tok", PORT, directory=tmp_path)
    assert path == tmp_path / f"server-{PORT}.token"
    assert path.read_text() == "tok\n"
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    auth.remove_token_file(path)
    assert not path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_token_file_does_not_follow_planted_symlink(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("precious")
    (tmp_path / f"server-{PORT}.token").symlink_to(victim)
    path = auth.write_token_file("tok", PORT, directory=tmp_path)
    assert victim.read_text() == "precious"
    assert path is not None and not path.is_symlink()


def test_token_file_tolerates_read_only_dir(tmp_path):
    missing = tmp_path / "nope" / "deeper"
    (tmp_path / "nope").write_text("a file, not a dir")
    assert auth.write_token_file("tok", PORT, directory=missing) is None


def test_setup_token_from_env_writes_no_file(monkeypatch, tmp_path):
    monkeypatch.setenv("TDB_SERVER_TOKEN", "chosen")
    monkeypatch.setattr(auth, "write_token_file", lambda *a, **k: pytest.fail("wrote"))
    assert auth.setup_token(insecure=False, port=PORT) == ("chosen", None)


def test_setup_token_generated_is_written(monkeypatch, tmp_path):
    monkeypatch.delenv("TDB_SERVER_TOKEN", raising=False)
    monkeypatch.setattr(
        auth, "token_file_path", lambda port, directory=None: tmp_path / "t.token"
    )
    token, path = auth.setup_token(insecure=False, port=PORT)
    assert path == tmp_path / "t.token" and path.read_text() == token + "\n"


def test_setup_token_insecure(monkeypatch):
    assert auth.setup_token(insecure=True, port=PORT) == (None, None)


def test_token_file_path_follows_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("TDB_CONFIG_DIR", str(tmp_path))
    assert auth.token_file_path(5000) == tmp_path / "server-5000.token"
