"""Send a correctly SIGNED checkout.session.completed to the local API without the Stripe CLI.
Usage: python -m scripts.send_test_webhook <tenant_id> [--forged] [--event-id evt_x]
Signs with STRIPE_WEBHOOK_SECRET from .env, exactly like Stripe does."""
import json
import os
import sys
import time
import uuid
import httpx
from dotenv import load_dotenv
from app.stripe_client import sign

load_dotenv()


def main():
    tid = sys.argv[1]
    eid = sys.argv[sys.argv.index("--event-id") + 1] if "--event-id" in sys.argv else f"evt_local_{uuid.uuid4().hex[:12]}"
    event = {"id": eid, "type": "checkout.session.completed", "created": int(time.time()),
             "data": {"object": {"id": f"cs_test_{uuid.uuid4().hex[:8]}", "object": "checkout.session", "mode": "subscription",
                                 "payment_status": "paid", "client_reference_id": tid, "customer": f"cus_local_{tid}",
                                 "subscription": f"sub_local_{tid}", "metadata": {"tenant_id": tid}}}}
    raw = json.dumps(event).encode()
    secret = "whsec_forged_by_attacker" if "--forged" in sys.argv else os.environ["STRIPE_WEBHOOK_SECRET"]
    r = httpx.post(os.getenv("PUBLIC_BASE_URL", "http://localhost:8000") + "/webhooks/stripe", content=raw,
                   headers={"Stripe-Signature": sign(raw, secret), "Content-Type": "application/json"})
    print(r.status_code, r.text, f"(event {eid})")


if __name__ == "__main__":
    main()
