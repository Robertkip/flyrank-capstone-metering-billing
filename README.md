# Usage Metering & Billing Engine

FlyRank Internship · Backend Track · Capstone (`flyrank-capstone-metering-billing`)

This service records every billable action **exactly once**, even when clients retry. It enforces monthly plan quotas with honest **429 / 402** answers, prices AI tokens correctly (cached input is cheaper, reasoning bills as output), and syncs plans from **Stripe test-mode** subscriptions through verified, deduplicated webhooks.

**Stack:** Python 3.12 · FastAPI · SQLAlchemy · PostgreSQL (Docker) · Stripe test mode (REST + Stripe CLI) · pytest (23 tests).

## Plans

| Plan | API calls / month | AI tokens / month | Price |
|---|---|---|---|
| Free | 1,000 | 100,000 | $0 |
| **Pro** | **50,000** | **5,000,000** | **$29 / month** |

## Pricing constants

These are pinned in `app/pricing.py`. All money is stored as **integer micro-dollars**; there are no floats.

| Item | Rate |
|---|---|
| API calls | $0.40 per 1,000 |
| Fresh (uncached) input tokens | $0.30 per 1M |
| Cached input tokens | $0.075 per 1M (a *subset* of input tokens) |
| Output tokens **and reasoning tokens** | $2.50 per 1M |

**Worked example:** 1M input tokens (400k of them cached) + 200k output + 100k reasoning comes to 600k × $0.30 + 400k × $0.075 + 300k × $2.50 per 1M = **$0.96**. Simply adding the categories would give $0.83, which is wrong.

## Rules

| Situation | Response |
|---|---|
| Same `Idempotency-Key` + same body | **200**, the original response replayed (`Idempotent-Replayed: true`); no new event |
| Same key + different body | **422** |
| `used + requested <= limit` | Allowed; the call that lands exactly on the limit succeeds |
| Over a quota | **429 Too Many Requests** + `Retry-After` (seconds to the next UTC month) + the numbers + an upgrade hint on Free |
| Pro subscription `past_due` / `unpaid` | **402 Payment Required** until Stripe reports it `active` again |
| Forged, stale or missing webhook signature | **400**, nothing stored |
| Replayed webhook event id | 200 `duplicate: true`; processed once |

## Architecture

```
 Client ──POST /generate (X-API-Key, Idempotency-Key)──► HTTP layer (validation, auth)            app/main.py
                                                           │
                                                           ▼
                                     MeterService.record(tenant, key, body, tokens)               app/metering.py
                                       ├─ key seen?  same body → replay stored response (200)
                                       │             different body → 422
                                       ├─ lock tenant row → quota check: used + requested <= limit
                                       │       ├─ over quota → 429 + Retry-After + explanation
                                       │       └─ payment failed → 402
                                       └─ INSERT usage_event  (UNIQUE tenant_id+key → duplicate impossible)
 GET /usage ◄── rollup(usage_events for month) → {used, limit, remaining, cost}                  app/pricing.py

 POST /billing/checkout ──► Stripe Checkout (test mode, card 4242…) ──► subscription created
 Stripe ──signed webhook──► POST /webhooks/stripe                                                 app/webhooks.py
                              ├─ verify HMAC on raw body (+5 min tolerance)  ── forged → 400
                              ├─ INSERT webhook_events (UNIQUE event id)      ── replay → no-op
                              └─ background worker (3 retries, alert on failure)
                                    → update tenant plan/status + subscription (ignores out-of-order)
```

The data model and the idempotency strategy are described in `docs/DESIGN.md`.

## Run

```bash
cp .env.example .env                                  # Stripe TEST keys (see below); works without them except /billing/checkout
docker compose up --build -d                          # run   → http://localhost:8000  (docs at /docs)
docker compose exec api python -m scripts.seed        # seed  → demo tenants with fixed demo API keys
docker compose exec api python -m pytest -q           # tests → 23 passed
```

Without Docker: `pip install -r requirements.txt && python -m scripts.seed && uvicorn app.main:app`. It uses SQLite by default.

## Demo

The seeded demo keys are sandbox-only and are not secrets:

| Tenant | API key |
|---|---|
| Acme Free | `sk_tenant_demo_acme_free` |
| Boundary Labs (998/1,000 calls used) | `sk_tenant_demo_boundary` |

```bash
A='X-API-Key: sk_tenant_demo_acme_free'
# 1. Exactly once: run twice → 201 then 200 with the identical body
curl -i -X POST localhost:8000/generate -H "$A" -H 'Idempotency-Key: order-0001' -H 'Content-Type: application/json' \
  -d '{"prompt":"hi","tokens":{"input_tokens":1000,"cached_input_tokens":400,"output_tokens":200,"reasoning_tokens":100}}'
# 2. Boundary: 201 (999), 201 (1000), 429 (1001)
for k in a b c; do curl -s -o /dev/null -w "%{http_code}\n" -X POST localhost:8000/generate \
  -H 'X-API-Key: sk_tenant_demo_boundary' -H "Idempotency-Key: edge-$k-0001" -H 'Content-Type: application/json' -d '{"prompt":"hi"}'; done
# 5. Usage + cost
curl localhost:8000/usage -H "$A"
```

## Stripe test-mode walkthrough (probe 3 and 4)

This is free and needs no card. **Test mode only:** live keys are refused at startup.

1. In the Stripe dashboard (Test mode), go to **Products**, create a **Pro** product with a **$29 monthly recurring** price, and copy the `price_…` into `STRIPE_PRICE_PRO`. Copy the test secret key `sk_test_…` into `STRIPE_SECRET_KEY`.
2. Install the Stripe CLI, then run `stripe login` and `stripe listen --forward-to localhost:8000/webhooks/stripe`. Copy the printed `whsec_…` into `STRIPE_WEBHOOK_SECRET`, then run `docker compose up -d` again.
3. Run `curl -X POST localhost:8000/billing/checkout -H "$A"`. Open the `checkout_url` and pay with **4242 4242 4242 4242**, any future date and any CVC.
4. The CLI shows `checkout.session.completed` → 200. `GET /usage` now shows `plan: pro` with the 50,000 / 5,000,000 limits.
5. Replay: `stripe events resend <evt_id>` returns `duplicate: true`. Forged: `python -m scripts.send_test_webhook 1 --forged` returns 400.
6. Cancel the subscription in the dashboard. `customer.subscription.deleted` sets the tenant back to Free.

**No Stripe account yet?** `python -m scripts.send_test_webhook <tenant_id>` sends a correctly signed `checkout.session.completed` using your `STRIPE_WEBHOOK_SECRET`.

## Shared requirements

| Requirement | Where |
|---|---|
| Layers | HTTP `main.py` → logic `metering.py`, `pricing.py`, `webhooks.py` → data `db.py` |
| Validation | Pydantic + header checks; 400/401/409/422, never 500 |
| Background job | Webhook worker with 3 retries and a failure alert in `data/alerts.log` |
| Persistence | Migration `001_initial_schema`; unique constraints and indexes; tenant isolation |
| Idempotency | Usage events and webhook events |
| Secrets | Env only; API keys stored as SHA-256 hashes; `.env` git-ignored; live Stripe keys refused |
| AI cost | Not applicable: AI calls are simulated. Token *cost* is the product itself, computed per event and per month |

## Limitations

- Background work runs in an in-process thread pool. A crash between storing an event and processing it leaves the row in `received`. A reconciliation job (stretch goal) would re-drive those rows and compare against Stripe.
- Quota locking uses `SELECT … FOR UPDATE` on Postgres. SQLite serializes writes instead; that's fine for the demo but not for production concurrency.
- No invoices, proration or overage billing (stretch goals). Periods are calendar months in UTC, not the Stripe billing anchor.
- The token counts on `/generate` are simulated, as the brief allows.
