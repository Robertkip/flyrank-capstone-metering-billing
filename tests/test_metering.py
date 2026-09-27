"""Probe 1 (exactly-once) and Probe 2 (quota boundary), plus boundary validation."""
from sqlalchemy.exc import IntegrityError
import pytest
from app.db import SessionLocal, UsageEvent
from app.metering import current_period
from conftest import gen, prefill

TOK = {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 200, "reasoning_tokens": 100}


def events(tid):
    db = SessionLocal()
    n = db.query(UsageEvent).filter_by(tenant_id=tid).count()
    db.close()
    return n


def test_same_request_twice_creates_one_event(client, tenant):
    r1 = gen(client, tenant, "req-0001-abcdef", tokens=TOK)
    r2 = gen(client, tenant, "req-0001-abcdef", tokens=TOK)
    assert r1.status_code == 201 and r2.status_code == 200
    assert r1.json() == r2.json()                                   # second response mirrors the first
    assert r2.headers["Idempotent-Replayed"] == "true"
    assert events(tenant["id"]) == 1
    u = client.get("/usage", headers=tenant["headers"]).json()["usage"]
    assert u["api_calls"]["used"] == 1 and u["ai_tokens"]["used"] == 1300


def test_key_reuse_with_different_body_is_rejected(client, tenant):
    assert gen(client, tenant, "req-0002-abcdef", prompt="one").status_code == 201
    assert gen(client, tenant, "req-0002-abcdef", prompt="two").status_code == 422
    assert events(tenant["id"]) == 1


def test_database_forbids_duplicate_keys_even_if_app_logic_is_bypassed(client, tenant):
    gen(client, tenant, "req-0003-abcdef")
    db = SessionLocal()
    db.add(UsageEvent(tenant_id=tenant["id"], idempotency_key="req-0003-abcdef", request_hash="x", period=current_period(), response={}))
    with pytest.raises(IntegrityError):
        db.commit()
    db.close()


def test_idempotency_keys_are_scoped_per_tenant(client, tenant):
    other = client.post("/tenants", json={"name": "Other Co"}).json()
    o = {"id": other["tenant_id"], "headers": {"X-API-Key": other["api_key"]}}
    assert gen(client, tenant, "shared-key-001").status_code == 201
    assert gen(client, o, "shared-key-001").status_code == 201       # no cross-tenant collision or leak
    assert events(o["id"]) == 1


def test_api_call_quota_boundary(client, tenant):
    prefill(tenant["id"], api_calls=998)
    r999 = gen(client, tenant, "boundary-999")
    r1000 = gen(client, tenant, "boundary-1000")                      # lands exactly on the limit -> allowed
    r1001 = gen(client, tenant, "boundary-1001")                      # one over -> 429
    assert r999.status_code == 201 and r999.json()["remaining"]["api_calls"] == 1
    assert r1000.status_code == 201 and r1000.json()["remaining"]["api_calls"] == 0
    assert r1001.status_code == 429 and int(r1001.headers["Retry-After"]) > 0
    b = r1001.json()
    assert b["error"] == "quota_exceeded" and b["exceeded"][0] == {"metric": "api_calls", "used": 1000, "requested": 1, "limit": 1000}
    assert "Upgrade to Pro" in b["message"]
    assert client.get("/usage", headers=tenant["headers"]).json()["usage"]["api_calls"]["used"] == 1000   # rejected call not metered


def test_token_quota_boundary(client, tenant):
    prefill(tenant["id"], quota_tokens=99_000)
    exact = {"input_tokens": 600, "cached_input_tokens": 0, "output_tokens": 300, "reasoning_tokens": 100}   # = 1,000 -> 100,000
    assert gen(client, tenant, "tok-exact-0001", tokens=exact).status_code == 201
    r = gen(client, tenant, "tok-over-00001", tokens={"input_tokens": 1, "output_tokens": 0})
    assert r.status_code == 429 and r.json()["exceeded"][0]["metric"] == "ai_tokens"


def test_rejected_request_can_be_retried_after_upgrade_with_same_key(client, tenant):
    prefill(tenant["id"], api_calls=1000)
    assert gen(client, tenant, "retry-after-up").status_code == 429
    assert events(tenant["id"]) == 1                                  # only the prefill row; rejection stored nothing


def test_validation_is_4xx_never_500(client, tenant):
    assert client.post("/generate", json={"prompt": "x"}, headers={**tenant["headers"], "Idempotency-Key": "k"}).status_code == 400
    assert client.post("/generate", json={"prompt": "x"}, headers=tenant["headers"]).status_code == 400
    bad = {"input_tokens": 5, "cached_input_tokens": 9, "output_tokens": 1}
    assert gen(client, tenant, "bad-tokens-01", tokens=bad).status_code == 422
    assert client.post("/generate", json={"prompt": "x"}, headers={"Idempotency-Key": "abcdefgh1"}).status_code == 401
    assert client.get("/usage?period=2026-13", headers=tenant["headers"]).status_code == 422
    assert client.post("/tenants", json={"name": ""}).status_code == 422
