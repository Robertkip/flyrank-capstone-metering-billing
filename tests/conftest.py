import os
import sys
import tempfile

_tmp = tempfile.mkdtemp()
os.environ.update({"DATABASE_URL": f"sqlite:///{_tmp}/t.db", "STRIPE_WEBHOOK_SECRET": "whsec_test_only_not_real",
                   "STRIPE_SECRET_KEY": "sk_test_fake_for_tests", "STRIPE_PRICE_PRO": "price_test_pro",
                   "ALERT_LOG": f"{_tmp}/alerts.log"})
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import json  # noqa
import time  # noqa
import uuid  # noqa
import pytest  # noqa
from fastapi.testclient import TestClient  # noqa
from app.main import app  # noqa
from app.db import SessionLocal, Tenant, UsageEvent, WebhookEvent  # noqa
from app.metering import current_period  # noqa
from app.stripe_client import sign  # noqa

SECRET = "whsec_test_only_not_real"


@pytest.fixture(scope="session")
def client():
    return TestClient(app)


@pytest.fixture
def tenant(client):
    r = client.post("/tenants", json={"name": f"Acme {uuid.uuid4().hex[:6]}"}).json()
    return {"id": r["tenant_id"], "headers": {"X-API-Key": r["api_key"]}}


def gen(client, tenant, key, prompt="hello world", tokens=None):
    body = {"prompt": prompt, **({"tokens": tokens} if tokens else {})}
    return client.post("/generate", json=body, headers={**tenant["headers"], "Idempotency-Key": key})


def prefill(tenant_id, api_calls=0, quota_tokens=0):
    """Put a tenant at a known usage level without 1,000 HTTP calls (one aggregate row, current period)."""
    db = SessionLocal()
    db.add(UsageEvent(tenant_id=tenant_id, idempotency_key=f"prefill-{uuid.uuid4().hex}", request_hash="x",
                      period=current_period(), api_calls=api_calls, input_tokens=quota_tokens, quota_tokens=quota_tokens,
                      response={}))
    db.commit()
    db.close()


def post_event(client, event, secret=SECRET, sig=None):
    raw = json.dumps(event).encode()
    return client.post("/webhooks/stripe", content=raw,
                       headers={"Stripe-Signature": sig or sign(raw, secret), "Content-Type": "application/json"})


def wait_processed(event_id, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        db = SessionLocal()
        row = db.query(WebhookEvent).filter_by(stripe_event_id=event_id).one_or_none()
        db.close()
        if row and row.status != "received":
            return row
        time.sleep(0.05)
    raise AssertionError("webhook not processed in time")


def checkout_completed(tenant_id, sub_id, created=None, eid=None):
    return {"id": eid or f"evt_{uuid.uuid4().hex}", "type": "checkout.session.completed", "created": created or int(time.time()),
            "data": {"object": {"id": "cs_test_1", "object": "checkout.session", "mode": "subscription", "payment_status": "paid",
                                "client_reference_id": str(tenant_id), "customer": f"cus_{tenant_id}", "subscription": sub_id,
                                "metadata": {"tenant_id": str(tenant_id)}}}}


def sub_event(kind, tenant_id, sub_id, status, created=None):
    return {"id": f"evt_{uuid.uuid4().hex}", "type": kind, "created": created or int(time.time()),
            "data": {"object": {"id": sub_id, "object": "subscription", "status": status, "customer": f"cus_{tenant_id}",
                                "metadata": {"tenant_id": str(tenant_id)}}}}
