# BUILDLOG: AI usage log

| Date | Where AI helped | What was wrong / what I changed |
|---|---|---|
| 2026-09-27 | Claude drafted the skeleton: models, MeterService, pricing module, webhook inbox, tests, docs. | Reviewed each module before committing. |
| 2026-09-27 | Token pricing. | Made explicit that `cached_input_tokens` is a **subset** of `input_tokens` (that's how providers such as Gemini report it). A first idea of simply summing all four categories would double-count cached tokens and forget that reasoning bills as output. I added `test_naive_addition_would_be_wrong` to guard against it. |
| 2026-09-27 | Webhook handling. | A generic `stripe trigger checkout.session.completed` has no tenant metadata. The first version raised an error, which caused 3 retries and a false alert. It now logs and marks the event `ignored`, because retries are for transient failures, not unknown tenants. |
| 2026-09-27 | 402 vs 429. | Decided and documented: 429 = quota exceeded (includes an upgrade hint on Free); 402 = payment failed on Pro. |
| _add yours_ | _e.g. first real Stripe CLI run: what surprised you_ | |

Lines I can explain: `metering.record` (why the stored response is returned on replay; why IntegrityError is caught), `pricing._per_million` (integer rounding), `stripe_client.verify_signature` (why the raw body, why `compare_digest`, why a timestamp tolerance), `webhooks._upsert_sub` (out-of-order events).
