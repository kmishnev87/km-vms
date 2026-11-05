"""Small isolated requests and synthetic identities, never a live login flood."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from app.core import request_client, request_limits
from app.core.login_throttle import LoginThrottle, LoginThrottled
from app.core.request_limits import RequestBodyLimitMiddleware


def _request(source="198.51.100.10", **headers):
    return Request({"type": "http", "path": "/auth/login", "method": "POST",
                    "client": (source, 1234),
                    "headers": [(name.lower().replace("_", "-").encode(), value.encode())
                                for name, value in headers.items()]})


def test_proxy_identity_requires_exact_trusted_peer_and_overwritten_real_ip(monkeypatch):
    monkeypatch.setattr(request_client, "_trusted_proxy_ips", lambda _peer=None: {"172.25.0.4"})
    assert request_client.request_client_ip(_request(x_real_ip="203.0.113.1", x_forwarded_for="203.0.113.2")) == "198.51.100.10"
    assert request_client.request_client_ip(_request("172.25.0.4", x_real_ip="203.0.113.1", x_forwarded_for="forged, 203.0.113.2")) == "203.0.113.1"
    assert request_client.request_client_ip(_request("172.25.0.4", x_real_ip="203.0.113.1, 203.0.113.2")) == "172.25.0.4"
    assert request_client.request_client_ip(_request("::ffff:198.51.100.10")) == "198.51.100.10"


def test_proxy_dns_failure_and_replacement_are_refreshed_on_demand(monkeypatch):
    clock = [0.0]
    answers = [OSError(), [(None, None, None, None, ("172.25.0.4", 80))],
               [(None, None, None, None, ("172.25.0.5", 80))]]
    calls = []

    def resolve(*args, **kwargs):
        calls.append(args)
        result = answers.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(request_client, "_proxy_refresh_at", 0)
    monkeypatch.setattr(request_client, "_proxy_ips", frozenset())
    monkeypatch.setattr(request_client.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(request_client.socket, "getaddrinfo", resolve)
    assert not request_client._trusted_proxy_ips()
    clock[0] = 1
    assert not request_client._trusted_proxy_ips()
    assert len(calls) == 1
    clock[0] = 2
    assert request_client._trusted_proxy_ips() == {"172.25.0.4"}
    clock[0] = 33
    assert request_client._trusted_proxy_ips() == {"172.25.0.5"}


def _bad(limiter, source):
    attempt = limiter.acquire(source)
    limiter.finish(attempt)


def test_proxy_replacement_before_ttl_and_dns_failure_never_share_login_budgets(monkeypatch):
    clock = [0.0]
    answers = ["172.25.0.4", "172.25.0.5", OSError(), "172.25.0.6"]

    def resolve(*_args, **_kwargs):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return [(None, None, None, None, (answer, 80))]

    monkeypatch.setattr(request_client.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(request_client.socket, "getaddrinfo", resolve)
    for key, value in {"_proxy_ips": frozenset(), "_proxy_refresh_at": 0,
                       "_proxy_lookup_ok": False, "_negative_peer": None, "_negative_until": 0}.items():
        monkeypatch.setattr(request_client, key, value)
    assert request_client.request_client_ip(_request("172.25.0.4", x_real_ip="203.0.113.1"), strict=True) == "203.0.113.1"
    clock[0] = 1
    limiter = LoginThrottle(clock=lambda: clock[0])
    source_a = request_client.request_client_ip(_request("172.25.0.5", x_real_ip="203.0.113.1"), strict=True)
    assert source_a == "203.0.113.1"  # Immediate refresh, not a shared proxy bucket.
    for _ in range(5):
        _bad(limiter, source_a)
    source_b = request_client.request_client_ip(_request("172.25.0.5", x_real_ip="203.0.113.2"), strict=True)
    valid = limiter.acquire(source_b)
    valid.failed = False
    limiter.finish(valid)
    clock[0] = 40
    assert request_client.request_client_ip(_request("172.25.0.5", x_real_ip="203.0.113.2"), strict=True) == source_b
    clock[0] = 41
    with pytest.raises(request_client.ClientIdentityUnavailable):
        request_client.request_client_ip(_request("172.25.0.6", x_real_ip="203.0.113.2"), strict=True)
    assert request_client.request_client_ip(_request("198.51.100.30"), strict=True) == "198.51.100.30"
    assert "172.25.0.6" not in limiter._sources
    clock[0] = 42
    assert request_client.request_client_ip(_request("172.25.0.6", x_real_ip="203.0.113.2"), strict=True) == source_b
    assert not answers


def test_unavailable_proxy_identity_is_retryable_before_login_budget_or_database(monkeypatch):
    from app.routers import auth
    from app.schemas.auth import LoginRequest

    def unresolved(*_args, **_kwargs):
        raise request_client.ClientIdentityUnavailable()

    monkeypatch.setattr(auth, "request_client_ip", unresolved)
    monkeypatch.setattr(auth.login_throttle, "acquire", lambda *_: pytest.fail("unresolved proxy changed login budget"))
    with pytest.raises(auth.HTTPException) as result:
        auth.login(LoginRequest(username="owner", password="synthetic-password"), _request(), object())
    assert result.value.status_code == 503 and result.value.headers["Retry-After"] == "2"


def test_five_failures_throttle_only_the_source_and_denial_does_not_extend_expiry():
    clock = [0.0]
    limiter = LoginThrottle(clock=lambda: clock[0])
    for _ in range(5):
        _bad(limiter, "source-a")
    with pytest.raises(LoginThrottled) as denied:
        limiter.acquire("source-a")
    assert denied.value.retry_after == 60
    other = limiter.acquire("source-b")
    other.failed = False
    limiter.finish(other)
    clock[0] = 10
    with pytest.raises(LoginThrottled) as denied:
        limiter.acquire("source-a")
    assert denied.value.retry_after == 50
    clock[0] = 60
    recovered = limiter.acquire("source-a")
    recovered.failed = False
    limiter.finish(recovered)
    assert not limiter._sources


def test_success_does_not_launder_failures_and_state_is_bounded():
    clock = [0.0]
    limiter = LoginThrottle(max_sources=2, clock=lambda: clock[0])
    for _ in range(4):
        _bad(limiter, "a")
    valid = limiter.acquire("a")
    valid.failed = False
    limiter.finish(valid)
    _bad(limiter, "a")
    with pytest.raises(LoginThrottled):
        limiter.acquire("a")
    _bad(limiter, "b")
    with pytest.raises(LoginThrottled):
        limiter.acquire("c")
    assert len(limiter._sources) == 2
    clock[0] = 61
    _bad(limiter, "c")
    assert len(limiter._sources) == 1


def test_concurrent_hash_attempts_reserve_capacity_atomically():
    limiter = LoginThrottle()
    start = threading.Barrier(10)
    reserved = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    accepted = []

    def worker():
        start.wait(timeout=5)
        try:
            attempt = limiter.acquire("same-source")
        except LoginThrottled:
            return False
        with lock:
            accepted.append(attempt)
            if len(accepted) == 5:
                reserved.set()
        assert release.wait(timeout=5)
        limiter.finish(attempt)
        return True

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(worker) for _ in range(10)]
        assert reserved.wait(timeout=5)
        release.set()
        results = [future.result(timeout=5) for future in futures]
    assert sum(results) == 5
    assert len(limiter._sources["same-source"].failures) == 5


@pytest.mark.parametrize("known,active", [(False, True), (True, False), (True, True)])
def test_login_route_counts_generic_failures_and_allows_same_owner_from_other_source(monkeypatch, known, active):
    from app.routers import auth
    from app.schemas.auth import LoginRequest

    now = [0.0]
    limiter = LoginThrottle(clock=lambda: now[0])
    monkeypatch.setattr(auth, "login_throttle", limiter)
    monkeypatch.setattr(request_client, "_trusted_proxy_ips", lambda _peer=None: set())
    user = SimpleNamespace(id=1, username="owner", password_hash="synthetic-hash", is_active=active)
    query = SimpleNamespace(filter=lambda *_: query, first=lambda: user if known else None)
    db = SimpleNamespace(query=lambda *_: query, add=lambda *_: None, commit=lambda: None)
    monkeypatch.setattr(auth, "create_event", lambda **_: None)
    monkeypatch.setattr(auth, "verify_password", lambda password, _: password == "right-password")
    monkeypatch.setattr(auth, "create_access_token", lambda *_, **__: "synthetic-test-token")
    bad = LoginRequest(username="owner", password="wrong-password")
    for _ in range(5):
        with pytest.raises(auth.HTTPException) as response:
            auth.login(bad, _request(), db)
        assert response.value.status_code == 401
    with pytest.raises(auth.HTTPException) as response:
        auth.login(bad, _request(x_forwarded_for="forged-address"), db)
    assert response.value.status_code == 429
    assert response.value.headers["Retry-After"] == "60"
    # Same account is never globally disabled by source-a's failures.
    user.is_active = True
    query.first = lambda: user
    good = LoginRequest(username="owner", password="right-password")
    assert auth.login(good, _request("198.51.100.11"), db).access_token == "synthetic-test-token"
    now[0] = 60
    assert auth.login(good, _request(), db).access_token == "synthetic-test-token"


def _asgi(messages, *, headers=(), path="/ordinary", max_bytes=8, application=None):
    sent, reads, called = [], [], []
    incoming = iter(messages)

    async def receive():
        reads.append(True)
        return next(incoming)

    async def send(message):
        sent.append(message)

    async def app(scope, receive, send):
        called.append(True)
        message = await receive()
        await Response(message["body"])(scope, receive, send)

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
             "method": "POST", "path": path, "headers": list(headers)}
    asyncio.run(RequestBodyLimitMiddleware(application or app, max_bytes=max_bytes, login_bytes=4)(scope, receive, send))
    return sent, len(reads), called


def test_declared_oversize_is_rejected_without_reading_or_running_app():
    sent, reads, called = _asgi([], headers=[(b"content-length", b"9")])
    assert sent[0]["status"] == 413 and reads == 0 and not called


@pytest.mark.parametrize("headers", [[], [(b"content-length", b"1")], [(b"content-type", b"text/plain")], [(b"content-type", b"application/vendor+json")]])
def test_streaming_actual_limit_cannot_be_bypassed_by_headers(headers):
    messages = [{"type": "http.request", "body": b"1234", "more_body": True},
                {"type": "http.request", "body": b"56789", "more_body": True}]
    sent, reads, called = _asgi(messages, headers=headers)
    assert sent[0]["status"] == 413 and reads == 2 and not called


@pytest.mark.parametrize("length", [b"-1", b"nonsense", b"9" * 21])
def test_invalid_lengths_do_not_read_the_body(length):
    sent, reads, called = _asgi([], headers=[(b"content-length", length)])
    assert sent[0]["status"] == 400 and not reads and not called


def test_exact_limit_passes_login_has_its_own_smaller_bound_and_disconnect_is_preserved():
    message = {"type": "http.request", "body": b"12345678", "more_body": False}
    sent, reads, called = _asgi([message])
    assert sent[0]["status"] == 200 and sent[-1]["body"] == b"12345678" and called
    sent, _, called = _asgi([message], path="/auth/login/")
    assert sent[0]["status"] == 413 and not called
    sent, reads, called = _asgi([{"type": "http.disconnect"}])
    assert not sent and reads == 1 and not called


def test_body_limit_never_limits_response_stream_and_replay_does_not_spin():
    async def app(scope, receive, send):
        assert (await receive())["body"] == b"ok"
        assert (await receive())["type"] == "http.disconnect"

        async def content():
            yield b"x" * 100
            yield b"y" * 100

        await StreamingResponse(content(), status_code=206, headers={"Content-Range": "bytes 0-199/200"})(scope, receive, send)

    sent, reads, _ = _asgi([{"type": "http.request", "body": b"ok"}, {"type": "http.disconnect"}], application=app)
    assert sent[0]["status"] == 206 and reads == 2
    assert sum(len(message.get("body", b"")) for message in sent) == 200


def test_short_body_timeout_returns_408(monkeypatch):
    monkeypatch.setattr(request_limits, "REQUEST_BODY_TIMEOUT_SECONDS", 0.001)
    sent = []

    async def receive():
        await asyncio.sleep(60)

    async def send(message):
        sent.append(message)

    async def app(*_):
        raise AssertionError("timed-out input reached parser")

    asyncio.run(RequestBodyLimitMiddleware(app)({"type": "http", "path": "/ordinary", "headers": []}, receive, send))
    assert sent[0]["status"] == 408


def test_413_remains_readable_through_allowed_cors():
    app = FastAPI()
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=8)
    app.add_middleware(CORSMiddleware, allow_origins=["https://trusted.example"])
    with TestClient(app) as client:
        response = client.post("/ordinary", content=b"too-large", headers={"Origin": "https://trusted.example"})
    assert response.status_code == 413
    assert response.headers["access-control-allow-origin"] == "https://trusted.example"


def test_shipped_ingress_has_consistent_caps_and_explicit_raw_peer_contract():
    root = Path(__file__).resolve().parents[3]
    config = (root / "deploy/nginx/default.conf").read_text()
    dockerfile = (root / "apps/api/Dockerfile").read_text()
    assert "client_max_body_size 1m;" in config
    assert "client_max_body_size 16k;" in config
    assert "client_max_body_size 2g;" not in config
    assert '"--no-proxy-headers"' in dockerfile
