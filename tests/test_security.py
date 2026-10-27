from fastapi import FastAPI
from fastapi.testclient import TestClient

from mlh import security
from mlh.sqlsafe import RegisterRequest

app = FastAPI()
security.install(app, 8765)


@app.post("/api/echo")
async def echo(body: RegisterRequest) -> dict:
    return {"ok": True}


@app.get("/api/boom")
async def boom() -> dict:
    raise RuntimeError("Syntax error near password='leaked-secret'")


client = TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)
GOOD = {"Origin": "http://127.0.0.1:8765", "X-MLH-Request": "1"}
BODY = {"name": "n", "host": "127.0.0.1", "port": 9000, "tls": False, "username": "u", "password": "s3cret", "nodes": [{"host": "127.0.0.1", "port": 9000}]}


def test_host_allowlist_blocks_rebinding():
    assert client.get("/api/boom", headers={"Host": "attacker.example:8765"}).status_code == 421


def test_post_requires_origin_header_and_json():
    assert client.post("/api/echo", json=BODY, headers={"X-MLH-Request": "1"}).status_code == 403
    assert client.post("/api/echo", json=BODY, headers={"Origin": "http://evil.example", "X-MLH-Request": "1"}).status_code == 403
    assert client.post("/api/echo", json=BODY, headers={"Origin": "http://127.0.0.1:8765"}).status_code == 403
    form = client.post("/api/echo", data={"a": "b"}, headers=GOOD)
    assert form.status_code == 415
    assert client.post("/api/echo", json=BODY, headers=GOOD).status_code == 200


def test_validation_error_does_not_echo_password():
    bad = {**BODY, "password": "s3cret\x01", "port": 0}
    res = client.post("/api/echo", json=bad, headers=GOOD)
    assert res.status_code == 422
    assert "s3cret" not in res.text


def test_unhandled_exception_text_is_not_returned(caplog):
    res = client.get("/api/boom")
    assert res.status_code == 500 and "leaked-secret" not in res.text
    assert "leaked-secret" not in caplog.text


def test_no_cors_and_security_headers():
    res = client.get("/api/boom", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in res.headers
    assert "default-src 'self'" in res.headers["content-security-policy"]
    assert res.headers["cache-control"] == "no-store"
