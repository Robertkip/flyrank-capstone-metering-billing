"""Minimal Stripe test-mode client (httpx) + webhook signature verification (Stripe's v1 HMAC-SHA256 scheme)."""
import hashlib
import hmac
import time
import httpx
from .config import settings


class StripeError(Exception):
    pass


class SignatureError(Exception):
    pass


def verify_signature(payload: bytes, header: str | None, secret: str, tolerance: int | None = None, now: int | None = None) -> None:
    """Stripe-Signature: t=<unix>,v1=<hex hmac of '<t>.<raw body>'>. Raises SignatureError if forged or stale."""
    if not secret:
        raise SignatureError("webhook secret not configured")
    if not header:
        raise SignatureError("missing Stripe-Signature header")
    parts = [p.split("=", 1) for p in header.split(",") if "=" in p]
    ts = next((v for k, v in parts if k == "t"), None)
    sigs = [v for k, v in parts if k == "v1"]
    if not ts or not ts.isdigit() or not sigs:
        raise SignatureError("malformed Stripe-Signature header")
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in sigs):
        raise SignatureError("signature mismatch")
    tol = settings.webhook_tolerance_s if tolerance is None else tolerance
    if abs((now or int(time.time())) - int(ts)) > tol:
        raise SignatureError("timestamp outside tolerance (possible replay)")


def sign(payload: bytes, secret: str, ts: int | None = None) -> str:
    """Build a valid Stripe-Signature header - used by tests and scripts/send_test_webhook.py."""
    t = ts or int(time.time())
    return f"t={t},v1=" + hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()


def _post(path: str, data: dict) -> dict:
    if not settings.stripe_secret_key.startswith("sk_test_"):
        raise StripeError("STRIPE_SECRET_KEY must be a test-mode key (sk_test_...)")
    r = httpx.post(f"{settings.stripe_api_base}/v1/{path}", data=data, auth=(settings.stripe_secret_key, ""), timeout=20)
    if r.status_code >= 300:
        raise StripeError(f"Stripe {r.status_code}: {r.json().get('error', {}).get('message', r.text[:200])}")
    return r.json()


def create_customer(tenant) -> str:
    return _post("customers", {"name": tenant.name, "metadata[tenant_id]": str(tenant.id)})["id"]


def create_checkout_session(tenant, customer_id: str) -> dict:
    if not settings.stripe_price_pro:
        raise StripeError("STRIPE_PRICE_PRO is not set (create a recurring test price in the Stripe dashboard)")
    s = _post("checkout/sessions", {
        "mode": "subscription", "customer": customer_id, "client_reference_id": str(tenant.id),
        "line_items[0][price]": settings.stripe_price_pro, "line_items[0][quantity]": "1",
        "metadata[tenant_id]": str(tenant.id), "subscription_data[metadata][tenant_id]": str(tenant.id),
        "success_url": f"{settings.public_base_url}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{settings.public_base_url}/billing/cancelled"})
    return {"id": s["id"], "url": s["url"]}
