"""HTTP layer: auth, validation, status codes. Billing logic lives in metering/pricing/webhooks."""
import hashlib
import logging
import re
import secrets
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from .config import settings
from .db import SessionLocal, Tenant
from .migrations import migrate
from .pricing import (API_CALL_PER_1K_MICROS, CACHED_INPUT_PER_1M_MICROS, INPUT_PER_1M_MICROS, OUTPUT_PER_1M_MICROS,
                      PLANS, TokenUsage)
from .schemas import GenerateIn, TenantIn
from . import metering, stripe_client, webhooks

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
app = FastAPI(title="Usage Metering & Billing Engine", version="1.0.0")
migrate()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def hash_key(k: str) -> str:
    return hashlib.sha256(k.encode()).hexdigest()


def tenant_from_key(x_api_key: str | None = Header(default=None), db=Depends(get_db)) -> Tenant:
    if not x_api_key:
        raise HTTPException(401, "missing X-API-Key header")
    t = db.query(Tenant).filter(Tenant.api_key_hash == hash_key(x_api_key)).one_or_none()
    if not t:
        raise HTTPException(401, "invalid API key")
    return t


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    logging.getLogger("api").exception("unhandled error")
    return JSONResponse(status_code=500, content={"error": "internal error"})


@app.get("/health")
def health():
    return {"ok": True, "stripe_mode": "test" if settings.stripe_secret_key.startswith("sk_test_") else "not configured"}


@app.get("/pricing")
def pricing():
    """The pinned constants (integer micro-dollars)."""
    return {"plans": PLANS, "rates_micros": {"api_call_per_1k": API_CALL_PER_1K_MICROS, "input_per_1m": INPUT_PER_1M_MICROS,
            "cached_input_per_1m": CACHED_INPUT_PER_1M_MICROS, "output_and_reasoning_per_1m": OUTPUT_PER_1M_MICROS},
            "rules": ["cached_input_tokens are a subset of input_tokens and billed at the cached rate",
                      "reasoning_tokens are billed at the output rate",
                      "quota tokens = input_tokens + output_tokens + reasoning_tokens (cached counted once)",
                      "money is integer micro-dollars; displayed rounded half-up to cents"]}


@app.post("/tenants", status_code=201)
def create_tenant(body: TenantIn, db=Depends(get_db)):
    """Sign up a tenant on the Free plan. The API key is shown once and stored only as a hash."""
    key = "sk_tenant_" + secrets.token_urlsafe(24)
    t = Tenant(name=body.name, api_key_hash=hash_key(key), plan_code="free")
    db.add(t)
    db.commit()
    return {"tenant_id": t.id, "name": t.name, "plan": t.plan_code, "api_key": key}


@app.get("/me")
def me(t: Tenant = Depends(tenant_from_key)):
    return {"tenant_id": t.id, "name": t.name, "plan": t.plan_code, "status": t.status}


@app.post("/generate")
def generate(body: GenerateIn, t: Tenant = Depends(tenant_from_key), db=Depends(get_db),
             idempotency_key: str | None = Header(default=None)):
    """Dummy billable endpoint: simulated AI response, metered exactly once per Idempotency-Key."""
    if not idempotency_key or not re.fullmatch(r"[A-Za-z0-9._:-]{8,100}", idempotency_key):
        raise HTTPException(400, "Idempotency-Key header required (8-100 chars: letters, digits, . _ : -)")
    if body.tokens:
        tok = TokenUsage(**body.tokens.model_dump())
    else:   # deterministic simulation from the prompt
        n = max(len(body.prompt) // 4, 1)
        tok = TokenUsage(input_tokens=n, cached_input_tokens=0, output_tokens=4 * n, reasoning_tokens=n)
    try:
        resp, replayed = metering.record(db, t, idempotency_key, body.model_dump(), tok)
    except metering.IdempotencyConflict as e:
        raise HTTPException(422, str(e))
    except metering.QuotaExceeded as e:
        return JSONResponse(status_code=429, headers={"Retry-After": str(e.retry_after)},
                            content={"error": "quota_exceeded", "message": str(e), **e.detail})
    except metering.PaymentRequired as e:
        return JSONResponse(status_code=402, content={"error": "payment_required", "message": str(e), **e.detail})
    return JSONResponse(status_code=200 if replayed else 201, content=resp,
                        headers={"Idempotent-Replayed": "true" if replayed else "false"})


@app.get("/usage")
def usage(period: str | None = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
          t: Tenant = Depends(tenant_from_key), db=Depends(get_db)):
    return metering.rollup(db, t, period or metering.current_period())


# ---------- Stripe (test mode) ----------
@app.post("/billing/checkout", status_code=201)
def checkout(t: Tenant = Depends(tenant_from_key), db=Depends(get_db)):
    if t.plan_code == "pro" and t.status == "active":
        raise HTTPException(409, "tenant is already on an active Pro subscription")
    try:
        if not t.stripe_customer_id:
            t.stripe_customer_id = stripe_client.create_customer(t)
            db.commit()
        s = stripe_client.create_checkout_session(t, t.stripe_customer_id)
    except stripe_client.StripeError as e:
        raise HTTPException(503, f"Stripe unavailable: {e}")
    return {"checkout_session_id": s["id"], "checkout_url": s["url"],
            "note": "Test mode: pay with card 4242 4242 4242 4242, any future expiry, any CVC."}


@app.get("/billing/success", response_class=HTMLResponse)
def success():
    return "<h2>Payment received (test mode).</h2><p>Your plan updates as soon as Stripe's webhook arrives. Check <code>GET /usage</code>.</p>"


@app.get("/billing/cancelled", response_class=HTMLResponse)
def cancelled():
    return "<h2>Checkout cancelled.</h2><p>You are still on your current plan.</p>"


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request, stripe_signature: str | None = Header(default=None), db=Depends(get_db)):
    raw = await request.body()                      # verify against the RAW body, before JSON parsing
    try:
        stripe_client.verify_signature(raw, stripe_signature, settings.stripe_webhook_secret)
    except stripe_client.SignatureError as e:
        return JSONResponse(status_code=400, content={"error": "invalid_signature", "message": str(e)})
    try:
        event = __import__("json").loads(raw)
        _ = event["id"], event["type"], event["data"]["object"]
    except (ValueError, KeyError, TypeError):
        return JSONResponse(status_code=400, content={"error": "invalid_payload"})
    row, is_new = webhooks.store(db, event)
    if not is_new:
        return {"received": True, "duplicate": True, "status": row.status}
    webhooks.enqueue(row.id)                          # applied by the background worker (retries + alert)
    return {"received": True, "duplicate": False}
