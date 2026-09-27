"""Probe 3 (Free -> Pro via webhook), Probe 4 (forged -> 400, replay -> once), 402 on payment failure."""
import json
import time
from app import stripe_client
from app.db import SessionLocal, Tenant, WebhookEvent
from conftest import SECRET, checkout_completed, gen, post_event, prefill, sub_event, wait_processed


def plan_of(tid):
    db = SessionLocal()
    t = db.get(Tenant, tid)
    db.close()
    return t.plan_code, t.status


def test_checkout_session_is_created_in_test_mode(client, tenant, monkeypatch):
    calls = []
    def fake_post(path, data):
        calls.append((path, data))
        return {"id": "cus_test_123"} if path == "customers" else {"id": "cs_test_abc", "url": "https://checkout.stripe.com/c/pay/cs_test_abc"}
    monkeypatch.setattr(stripe_client, "_post", fake_post)
    r = client.post("/billing/checkout", headers=tenant["headers"])
    assert r.status_code == 201 and r.json()["checkout_url"].startswith("https://checkout.stripe.com/")
    session = dict(calls[1][1])
    assert session["mode"] == "subscription" and session["client_reference_id"] == str(tenant["id"])


def test_webhook_flips_free_to_pro_and_usage_shows_new_limits(client, tenant):
    assert client.get("/usage", headers=tenant["headers"]).json()["usage"]["api_calls"]["limit"] == 1000
    ev = checkout_completed(tenant["id"], f"sub_{tenant['id']}")
    assert post_event(client, ev).status_code == 200
    assert wait_processed(ev["id"]).status == "processed"
    assert plan_of(tenant["id"]) == ("pro", "active")
    u = client.get("/usage", headers=tenant["headers"]).json()
    assert u["tenant"]["plan"] == "pro" and u["usage"]["api_calls"]["limit"] == 50_000 and u["usage"]["ai_tokens"]["limit"] == 5_000_000


def test_forged_webhook_is_rejected_and_changes_nothing(client, tenant):
    ev = checkout_completed(tenant["id"], "sub_forged")
    assert post_event(client, ev, secret="whsec_attacker").status_code == 400
    assert post_event(client, ev, sig="t=123,v1=deadbeef").status_code == 400
    raw = json.dumps(ev).encode()
    assert client.post("/webhooks/stripe", content=raw).status_code == 400          # no signature header
    stale = stripe_client.sign(raw, SECRET, ts=int(time.time()) - 3600)
    assert post_event(client, ev, sig=stale).status_code == 400                      # replayed old signature
    assert plan_of(tenant["id"]) == ("free", "active")
    db = SessionLocal()
    assert db.query(WebhookEvent).filter_by(stripe_event_id=ev["id"]).count() == 0
    db.close()


def test_replayed_event_is_processed_once(client, tenant):
    ev = checkout_completed(tenant["id"], f"sub_r{tenant['id']}")
    first = post_event(client, ev).json()
    wait_processed(ev["id"])
    second = post_event(client, ev).json()
    assert first["duplicate"] is False and second["duplicate"] is True
    db = SessionLocal()
    row = db.query(WebhookEvent).filter_by(stripe_event_id=ev["id"]).one()
    assert row.attempts == 1 and row.status == "processed"
    db.close()


def test_payment_failure_returns_402_then_recovers(client, tenant):
    sub = f"sub_p{tenant['id']}"
    ev = checkout_completed(tenant["id"], sub, created=int(time.time()) - 10)
    post_event(client, ev); wait_processed(ev["id"])
    past_due = sub_event("customer.subscription.updated", tenant["id"], sub, "past_due")
    post_event(client, past_due); wait_processed(past_due["id"])
    r = gen(client, tenant, "after-pastdue-1")
    assert r.status_code == 402 and r.json()["reason"] == "subscription_payment_failed"
    paid = sub_event("customer.subscription.updated", tenant["id"], sub, "active", created=int(time.time()) + 5)
    post_event(client, paid); wait_processed(paid["id"])
    assert gen(client, tenant, "after-paid-0001").status_code == 201


def test_subscription_deleted_downgrades_to_free_and_old_events_are_ignored(client, tenant):
    sub = f"sub_d{tenant['id']}"
    now = int(time.time())
    for ev in (checkout_completed(tenant["id"], sub, created=now - 20),
               sub_event("customer.subscription.deleted", tenant["id"], sub, "canceled", created=now)):
        post_event(client, ev); wait_processed(ev["id"])
    assert plan_of(tenant["id"]) == ("free", "active")
    late = sub_event("customer.subscription.updated", tenant["id"], sub, "active", created=now - 5)   # arrives late, older
    post_event(client, late)
    assert wait_processed(late["id"]).status == "ignored" and plan_of(tenant["id"]) == ("free", "active")


def test_pro_tenant_gets_higher_quota(client, tenant):
    ev = checkout_completed(tenant["id"], f"sub_q{tenant['id']}")
    post_event(client, ev); wait_processed(ev["id"])
    prefill(tenant["id"], api_calls=1000)
    assert gen(client, tenant, "pro-over-free-1").status_code == 201


def test_live_keys_are_refused():
    import importlib, os, pytest
    from app import config
    os.environ["STRIPE_SECRET_KEY"] = "sk_live_xxx"
    try:
        with pytest.raises(RuntimeError):
            importlib.reload(config)
    finally:
        os.environ["STRIPE_SECRET_KEY"] = "sk_test_fake_for_tests"
        importlib.reload(config)
