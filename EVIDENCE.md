# EVIDENCE

One proof per requirement in Section 6. The transcripts come from a real local run (`uvicorn` + SQLite, seeded with `scripts.seed`) against the demo tenants.

> 🔁 **Stripe end-to-end must be re-captured by you.** Here, webhook events were signed locally with the same HMAC scheme Stripe uses (`scripts/send_test_webhook.py`). Before submitting, run the real test-mode Checkout with the Stripe CLI (see README, *Stripe test-mode walkthrough*) and paste the `stripe listen` output and the resulting `GET /usage` below the 🔁 markers.

Full test suite, `python -m pytest -v` (23 tests):
```
tests/test_metering.py::test_same_request_twice_creates_one_event PASSED
tests/test_metering.py::test_key_reuse_with_different_body_is_rejected PASSED
tests/test_metering.py::test_database_forbids_duplicate_keys_even_if_app_logic_is_bypassed PASSED
tests/test_metering.py::test_idempotency_keys_are_scoped_per_tenant PASSED
tests/test_metering.py::test_api_call_quota_boundary PASSED
tests/test_metering.py::test_token_quota_boundary PASSED
tests/test_metering.py::test_rejected_request_can_be_retried_after_upgrade_with_same_key PASSED
tests/test_metering.py::test_validation_is_4xx_never_500 PASSED
tests/test_pricing.py::test_worked_example_exact_totals PASSED
tests/test_pricing.py::test_naive_addition_would_be_wrong PASSED
tests/test_pricing.py::test_reasoning_bills_as_output PASSED
tests/test_pricing.py::test_cached_is_cheaper_than_fresh PASSED
tests/test_pricing.py::test_monthly_rollup_pro PASSED
tests/test_pricing.py::test_rounding_is_integer_half_up PASSED
tests/test_pricing.py::test_invalid_token_counts_rejected PASSED
tests/test_stripe.py::test_checkout_session_is_created_in_test_mode PASSED
tests/test_stripe.py::test_webhook_flips_free_to_pro_and_usage_shows_new_limits PASSED
tests/test_stripe.py::test_forged_webhook_is_rejected_and_changes_nothing PASSED
tests/test_stripe.py::test_replayed_event_is_processed_once PASSED
tests/test_stripe.py::test_payment_failure_returns_402_then_recovers PASSED
tests/test_stripe.py::test_subscription_deleted_downgrades_to_free_and_old_events_are_ignored PASSED
tests/test_stripe.py::test_pro_tenant_gets_higher_quota PASSED
tests/test_stripe.py::test_live_keys_are_refused PASSED
============================== 23 passed in 0.53s ==============================
```

## Metering

**☑ A billable action creates exactly one usage event, even under retries.** The same request is sent twice with `Idempotency-Key: order-7f3a-0001`:
```
HTTP/1.1 201 Created
idempotent-replayed: false
{"usage_event_id":2,"idempotency_key":"order-7f3a-0001","period":"2026-09","output":"[simulated AI response to: Summarise this invoice]","metered":{"api_calls":1,"input_tokens":1000,"cached_input_tokens":400,"output_tokens":200,"reasoning_tokens":100,"quota_tokens":1300,"event_cost_micros":1360},"remaining":{"api_calls":999,"ai_tokens":98700}}
HTTP/1.1 200 OK
idempotent-replayed: true
{"usage_event_id":2,"idempotency_key":"order-7f3a-0001","period":"2026-09","output":"[simulated AI response to: Summarise this invoice]","metered":{"api_calls":1,"input_tokens":1000,"cached_input_tokens":400,"output_tokens":200,"reasoning_tokens":100,"quota_tokens":1300,"event_cost_micros":1360},"remaining":{"api_calls":999,"ai_tokens":98700}}
usage_events for key order-7f3a-0001: 1
```
The second response is byte-for-byte the first (`Idempotent-Replayed: true`, status 200 instead of 201).

**☑ Proof double-counting cannot happen.** Three independent layers:
1. **App:** the key is looked up first, and the stored response is replayed.
2. **Database:** `UNIQUE(tenant_id, idempotency_key)`. `test_database_forbids_duplicate_keys_even_if_app_logic_is_bypassed` PASSED.
3. **Race:** if two identical requests race, the loser hits the unique constraint, rolls back and returns the winner's response (`app/metering.py::record`).

Reusing a key with a *different* body is refused:
```
422   (422: Idempotency-Key was already used with a different request body)
```

## Quotas

**☑ Usage is checked against the plan; over-limit requests are rejected.** The rule is `used + requested <= limit` is allowed. Tenant *Boundary Labs* (Free, 998/1,000 used) sends three calls:
```
HTTP/1.1 201 Created
{"usage_event_id":3,"idempotency_key":"probe2-b-0999","period":"2026-09","output":"[simulated AI response to: hi]","metered":{"api_calls":1,"input_tokens":1,"cached_input_tokens":0,"output_tokens":4,"reasoning_tokens":1,"quota_tokens":6,"event_cost_micros":413},"remaining":{"api_calls":1,"ai_tokens":99994}}
HTTP/1.1 201 Created
{"usage_event_id":4,"idempotency_key":"probe2-b-1000","period":"2026-09","output":"[simulated AI response to: hi]","metered":{"api_calls":1,"input_tokens":1,"cached_input_tokens":0,"output_tokens":4,"reasoning_tokens":1,"quota_tokens":6,"event_cost_micros":413},"remaining":{"api_calls":0,"ai_tokens":99988}}
HTTP/1.1 429 Too Many Requests
retry-after: 315592
{"error":"quota_exceeded","message":"Monthly api calls quota exceeded for plan 'Free': used 1,000 + requested 1 > limit 1,000. Quota resets at the start of next month (UTC). Upgrade to Pro for higher limits (POST /billing/checkout).","reason":"quota_exceeded","plan":"free","period":"2026-09","exceeded":[{"metric":"api_calls","used":1000,"requested":1,"limit":1000}]}
```
Call 999 → 201 (1 left). Call 1,000 lands **exactly on** the limit → 201 (0 left). Call 1,001 → **429**. The rejected call is not metered.

**☑ Correct status codes plus an explanation.**
- **429:** see above. It includes `Retry-After` (seconds until the UTC month resets) and the exact numbers.
- **402:** returned while a Pro subscription is `past_due`. `test_payment_failure_returns_402_then_recovers` PASSED: 402 with `"reason": "subscription_payment_failed"`, then 201 again after the `active` event.

Bad input is always a clean 4xx:
```
no key 401
no idem key 400
cached>input 422
```

## Cost calculation

**☑ Monthly usage rolls up into a cost per tenant.** **☑ AI token pricing handles cached and reasoning tokens.** **☑ Pricing constants are pinned.** The constants live in `app/pricing.py` (integer µ$):
- fresh input: $0.30 / 1M;
- cached input: $0.075 / 1M;
- output **and reasoning**: $2.50 / 1M;
- API calls: $0.40 / 1k;
- Pro base fee: $29.

Hand calculation for the event above:

| Line | Tokens | Rate (µ$ / 1M) | µ$ |
|---|---|---|---|
| Fresh input = 1,000 − 400 cached | 600 | 300,000 | 180 |
| Cached input | 400 | 75,000 | 30 |
| Output 200 + reasoning 100 | 300 | 2,500,000 | 750 |
| API call | 1 | $0.40/1k → 400 µ$ | 400 |
| Pro base fee | | | 29,000,000 |
| **Total** | | | **29,001,360 µ$ = $29.00** |

`GET /usage` returns exactly these numbers:
```json
{
 "usage": {
  "api_calls": {
   "used": 1,
   "limit": 50000,
   "remaining": 49999
  },
  "ai_tokens": {
   "used": 1300,
   "limit": 5000000,
   "remaining": 4998700,
   "breakdown": {
    "input_tokens": 1000,
    "of_which_cached": 400,
    "fresh_input": 600,
    "output_tokens": 200,
    "reasoning_tokens": 100,
    "billable_output": 300
   }
  },
  "events": 1
 },
 "cost": {
  "base_fee_micros": 29000000,
  "api_calls_micros": 400,
  "tokens": {
   "fresh_input_micros": 180,
   "cached_input_micros": 30,
   "output_micros": 750,
   "total_micros": 960
  },
  "total_micros": 29001360,
  "total_usd": "29.00",
  "currency": "usd"
 }
}
```

Larger worked example (`tests/test_pricing.py::test_worked_example_exact_totals`): 1M input with 400k cached, 200k output and 100k reasoning comes to 180,000 + 30,000 + 750,000 = **960,000 µ$ ($0.96)**. Naively adding the categories gives 830,000 µ$, which is wrong: it counts cached tokens twice and drops reasoning. `test_naive_addition_would_be_wrong` PASSED.

## Stripe integration

**☑ Subscription checkout works end to end in test mode.**
Locally signed `checkout.session.completed` for tenant 1:
```
before: plan free api_calls limit 1000 ai_tokens limit 100000
webhook: 200 {"received":true,"duplicate":false} (event evt_demo_checkout_1)
after:  plan pro api_calls limit 50000 ai_tokens limit 5000000
```
The Checkout Session is built with `mode=subscription` and `client_reference_id=<tenant>` (`test_checkout_session_is_created_in_test_mode` PASSED).
🔁 *Paste here: the `POST /billing/checkout` response, the `stripe listen` lines, and `GET /usage` after paying with card 4242 4242 4242 4242.*

**☑ Webhooks verify signatures, ignore duplicates, and update the plan.**
Forged signature → 400, nothing changes:
```
400 {"error":"invalid_signature","message":"signature mismatch"} (event evt_local_92b83679f4d5)
{"tenant_id":2,"name":"Boundary Labs","plan":"free","status":"active"}
```
Replaying the real event → processed once:
```
200 {"received":true,"duplicate":true,"status":"processed"} (event evt_demo_checkout_1)
webhook_events rows for evt_demo_checkout_1: (1, 1, 'processed')
```
Also covered by tests: missing, malformed and stale (>5 min) signatures → 400; `customer.subscription.deleted` → Free; a late, older `subscription.updated` is ignored (out-of-order protection).

## Data model, tests & documentation

**☑ Tenants, plans, subscriptions and usage events; tenant isolation.** Tables are in `app/db.py`, created by migration `001_initial_schema`. Every query is scoped by the tenant resolved from the hashed API key, and idempotency keys are per tenant (`test_idempotency_keys_are_scoped_per_tenant` PASSED).

**☑ README + architecture diagram + setup; required files present.** `README.md`, `capstone.yaml`, `EVIDENCE.md`, `BUILDLOG.md`, `.env.example`, `docs/DESIGN.md`, `LICENSE`.
