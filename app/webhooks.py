"""Webhook inbox: verify -> store once (dedupe by event id) -> background worker applies it with retries.
Payment truth lives at Stripe; the tenant's plan only changes through verified events."""
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .config import settings
from .db import SessionLocal, Subscription, Tenant, WebhookEvent

log = logging.getLogger("webhooks")
worker = ThreadPoolExecutor(max_workers=1)     # one worker = events applied in arrival order
HANDLED = {"checkout.session.completed", "customer.subscription.updated", "customer.subscription.deleted"}
MAX_ATTEMPTS = 3


def alert(msg: str):
    """Failure alert: ERROR log + data/alerts.log (swap for email/Slack in production)."""
    log.error("ALERT %s", msg)
    os.makedirs(os.path.dirname(settings.alert_log) or ".", exist_ok=True)
    with open(settings.alert_log, "a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")


def store(db, event: dict) -> tuple[WebhookEvent | None, bool]:
    """Returns (row, is_new). A replayed event id is a no-op."""
    row = WebhookEvent(stripe_event_id=event["id"], type=event["type"], created=int(event.get("created", 0)), payload=event)
    db.add(row)
    try:
        db.commit()
        return row, True
    except IntegrityError:
        db.rollback()
        return db.scalar(select(WebhookEvent).where(WebhookEvent.stripe_event_id == event["id"])), False


def _tenant_for(db, obj: dict) -> Tenant | None:
    tid = (obj.get("metadata") or {}).get("tenant_id") or obj.get("client_reference_id")
    if tid and str(tid).isdigit():
        t = db.get(Tenant, int(tid))
        if t:
            return t
    if obj.get("customer"):
        return db.scalar(select(Tenant).where(Tenant.stripe_customer_id == obj["customer"]))
    return None


def _upsert_sub(db, tenant, sub_id, status, created):
    sub = db.scalar(select(Subscription).where(Subscription.stripe_subscription_id == sub_id))
    if sub and created < sub.last_event_created:
        return None                                   # older than what we already applied: ignore (out of order)
    if not sub:
        sub = Subscription(tenant_id=tenant.id, stripe_subscription_id=sub_id, plan_code="pro", status=status)
        db.add(sub)
    sub.status, sub.last_event_created = status, created
    return sub


def apply(db, ev: WebhookEvent) -> str:
    obj = ev.payload["data"]["object"]
    if ev.type not in HANDLED:
        return "ignored"
    tenant = _tenant_for(db, obj)
    if tenant is None:   # e.g. a generic `stripe trigger` event not created by our checkout: nothing to update
        log.warning("webhook %s: no matching tenant, ignored", ev.stripe_event_id)
        return "ignored"
    if ev.type == "checkout.session.completed":
        if obj.get("mode") != "subscription" or obj.get("payment_status") not in ("paid", "no_payment_required"):
            return "ignored"
        tenant.stripe_customer_id = tenant.stripe_customer_id or obj.get("customer")
        if obj.get("subscription") and _upsert_sub(db, tenant, obj["subscription"], "active", ev.created) is not None:
            tenant.plan_code, tenant.status = "pro", "active"
    elif ev.type == "customer.subscription.updated":
        status = obj["status"]
        if _upsert_sub(db, tenant, obj["id"], status, ev.created) is None:
            return "ignored"
        if status in ("active", "trialing"):
            tenant.plan_code, tenant.status = "pro", "active"
        elif status in ("past_due", "unpaid", "incomplete"):
            tenant.plan_code, tenant.status = "pro", "past_due"     # -> 402 until paid
        else:                                                       # canceled, incomplete_expired, paused
            tenant.plan_code, tenant.status = "free", "active"
    elif ev.type == "customer.subscription.deleted":
        if _upsert_sub(db, tenant, obj["id"], "canceled", ev.created) is not None:
            tenant.plan_code, tenant.status = "free", "active"
    return "processed"


def process(event_row_id: int):
    """Background job with retries + failure alert."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        db = SessionLocal()
        ev = db.get(WebhookEvent, event_row_id)
        try:
            ev.attempts = attempt
            ev.status = apply(db, ev)
            ev.error = None
            db.commit()
            log.info("webhook %s %s -> %s", ev.stripe_event_id, ev.type, ev.status)
            return
        except Exception as e:
            db.rollback()
            ev = db.get(WebhookEvent, event_row_id)
            ev.attempts, ev.error = attempt, str(e)[:500]
            if attempt == MAX_ATTEMPTS:
                ev.status = "failed"
                db.commit()
                alert(f"webhook {ev.stripe_event_id} ({ev.type}) failed after {attempt} attempts: {e}")
                return
            db.commit()
            time.sleep(0.2 * 2 ** attempt)
        finally:
            db.close()


def enqueue(event_row_id: int):
    return worker.submit(process, event_row_id)
