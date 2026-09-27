"""Seed demo tenants (idempotent). Demo API keys are fixed so the README's curl commands work.
They are demo credentials for a local sandbox only - not secrets."""
import hashlib
import uuid
from sqlalchemy import select
from app.db import SessionLocal, Tenant, UsageEvent
from app.metering import current_period
from app.migrations import migrate

DEMO = [
    ("Acme Free", "sk_tenant_demo_acme_free", 0),
    ("Boundary Labs", "sk_tenant_demo_boundary", 998),     # 998/1000 calls used: probe 2 in two requests
]


def main():
    migrate()
    db = SessionLocal()
    for name, key, used in DEMO:
        h = hashlib.sha256(key.encode()).hexdigest()
        t = db.scalar(select(Tenant).where(Tenant.api_key_hash == h))
        if not t:
            t = Tenant(name=name, api_key_hash=h, plan_code="free")
            db.add(t)
            db.commit()
            if used:
                db.add(UsageEvent(tenant_id=t.id, idempotency_key=f"seed-{uuid.uuid4().hex}", request_hash="seed",
                                  period=current_period(), api_calls=used, response={"seed": True}))
                db.commit()
        print(f"tenant #{t.id:<3} {name:15s} plan={t.plan_code:4s}  X-API-Key: {key}")
    db.close()


if __name__ == "__main__":
    main()
