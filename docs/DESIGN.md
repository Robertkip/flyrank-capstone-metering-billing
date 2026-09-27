# Design doc: Usage Metering & Billing Engine

**Problem.** A SaaS with AI features must charge each tenant exactly for what they used, even when clients retry requests, networks fail, and Stripe re-sends webhooks. It must also stop usage at plan limits with honest, machine-readable answers.

## Data model
| Table | Key columns | Integrity |
|---|---|---|
| `plans` | code, api_calls_limit, ai_tokens_limit, base_fee_micros | rows mirror `app/pricing.py` |
| `tenants` | api_key_hash, plan_code, status (`active`/`past_due`), stripe_customer_id | unique key hash and customer id |
| `subscriptions` | stripe_subscription_id, status, last_event_created | unique subscription id |
| `usage_events` | tenant_id, idempotency_key, request_hash, period, api_calls, input/cached/output/reasoning tokens, quota_tokens, cost_micros, response | **UNIQUE(tenant_id, idempotency_key)**, index (tenant_id, period) |
| `webhook_events` | stripe_event_id, type, created, payload, status, attempts | **UNIQUE(stripe_event_id)** |

## Plans (monthly)
| Plan | API calls | AI tokens | Base fee |
|---|---|---|---|
| Free | 1,000 | 100,000 | $0 |
| Pro | 50,000 | 5,000,000 | $29 |

## Metering contract
`POST /generate` requires the headers `X-API-Key` and `Idempotency-Key` (8–100 chars). It returns:
- **201** on the first request;
- **200** with `Idempotent-Replayed: true` for the same key and same body (the stored response is returned verbatim);
- **422** for the same key with a different body.

The request fingerprint is the SHA-256 of the canonical JSON body.

## Idempotency strategy (three layers)
1. **App:** look up the key before doing anything.
2. **Database:** the unique constraint.
3. **Race:** a concurrent duplicate hits the constraint, rolls back and replays the winner.

Rejected (429/402) requests are **not** stored, so a client can retry the same key after upgrading.

## Quota rule
Allowed if `used + requested <= limit`, checked before recording, with the tenant row locked (`SELECT … FOR UPDATE` on Postgres).
- **429** means a quota is exceeded. It carries `Retry-After` (seconds to the next UTC month) and the numbers.
- **402** means payment is required: the Pro subscription is `past_due`/`unpaid`.

Quota tokens = input + output + reasoning. Cached tokens are already inside input, so they count once.

## Money
Integer micro-dollars throughout. Each month is priced once from the aggregated counts: rounding half-up per line, then displayed in cents.

## Stripe
The Checkout Session uses `mode=subscription` with `client_reference_id=tenant`.

The webhook handler:
1. verifies the HMAC over the **raw** body (plus a 5-minute tolerance), otherwise 400;
2. inserts into the `webhook_events` inbox, where a duplicate id is a no-op;
3. has a background worker apply the event with 3 retries and a failure alert.

Older events than the last one applied to a subscription are ignored.

## Layers
`main.py` (HTTP) → `metering.py`, `pricing.py`, `webhooks.py`, `stripe_client.py` (logic) → `db.py` (data).

## Non-goal
No invoices, proration or overage billing. Stripe is the payment source of truth.
