"""MeterService: idempotent usage recording with quota enforcement. Pure logic over the data layer."""
import hashlib
import json
from datetime import datetime, timezone
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from .db import Plan, Tenant, UsageEvent
from .pricing import TokenUsage, api_call_cost, token_cost, monthly_cost


class QuotaExceeded(Exception):
    """-> 429 Too Many Requests"""
    def __init__(self, message, detail, retry_after):
        super().__init__(message)
        self.detail, self.retry_after = detail, retry_after


class PaymentRequired(Exception):
    """-> 402 Payment Required"""
    def __init__(self, message, detail):
        super().__init__(message)
        self.detail = detail


class IdempotencyConflict(Exception):
    """-> 422: same key reused with a different request body"""


def current_period(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m")


def seconds_to_next_period(now: datetime | None = None) -> int:
    n = now or datetime.now(timezone.utc)
    nxt = datetime(n.year + (n.month == 12), 1 if n.month == 12 else n.month + 1, 1, tzinfo=timezone.utc)
    return int((nxt - n).total_seconds())


def request_fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def usage_totals(db, tenant_id: int, period: str) -> dict:
    row = db.execute(select(
        func.coalesce(func.sum(UsageEvent.api_calls), 0), func.coalesce(func.sum(UsageEvent.input_tokens), 0),
        func.coalesce(func.sum(UsageEvent.cached_input_tokens), 0), func.coalesce(func.sum(UsageEvent.output_tokens), 0),
        func.coalesce(func.sum(UsageEvent.reasoning_tokens), 0), func.coalesce(func.sum(UsageEvent.quota_tokens), 0),
        func.count(UsageEvent.id),
    ).where(UsageEvent.tenant_id == tenant_id, UsageEvent.period == period)).one()
    return {"api_calls": int(row[0]), "tokens": TokenUsage(int(row[1]), int(row[2]), int(row[3]), int(row[4])),
            "quota_tokens": int(row[5]), "events": int(row[6])}


def check_quota(db, tenant: Tenant, plan: Plan, period: str, calls: int, tokens: int):
    """Rule (documented in README): a request is allowed if used + requested <= limit.
    So the call that lands EXACTLY on the limit succeeds; the next one is rejected."""
    if tenant.status != "active":
        raise PaymentRequired(
            "Payment required: your Pro subscription payment failed. Update your payment method to continue.",
            {"reason": "subscription_payment_failed", "status": tenant.status, "action": "POST /billing/checkout"})
    t = usage_totals(db, tenant.id, period)
    over = []
    if t["api_calls"] + calls > plan.api_calls_limit:
        over.append({"metric": "api_calls", "used": t["api_calls"], "requested": calls, "limit": plan.api_calls_limit})
    if t["quota_tokens"] + tokens > plan.ai_tokens_limit:
        over.append({"metric": "ai_tokens", "used": t["quota_tokens"], "requested": tokens, "limit": plan.ai_tokens_limit})
    if over:
        o = over[0]
        hint = " Upgrade to Pro for higher limits (POST /billing/checkout)." if plan.code == "free" else ""
        raise QuotaExceeded(
            f"Monthly {o['metric'].replace('_', ' ')} quota exceeded for plan '{plan.name}': used {o['used']:,} + "
            f"requested {o['requested']:,} > limit {o['limit']:,}. Quota resets at the start of next month (UTC).{hint}",
            {"reason": "quota_exceeded", "plan": plan.code, "period": period, "exceeded": over},
            seconds_to_next_period())


def record(db, tenant: Tenant, idempotency_key: str, payload: dict, tokens: TokenUsage) -> tuple[dict, bool]:
    """Record one billable action exactly once. Returns (response, replayed)."""
    fp = request_fingerprint(payload)
    existing = db.scalar(select(UsageEvent).where(UsageEvent.tenant_id == tenant.id,
                                                  UsageEvent.idempotency_key == idempotency_key))
    if existing:
        if existing.request_hash != fp:
            raise IdempotencyConflict("Idempotency-Key was already used with a different request body")
        return existing.response, True

    # Lock the tenant row so concurrent requests for the same tenant are checked one at a time (Postgres).
    tenant = db.scalar(select(Tenant).where(Tenant.id == tenant.id).with_for_update())
    plan = db.get(Plan, tenant.plan_code)
    period = current_period()
    check_quota(db, tenant, plan, period, 1, tokens.quota_tokens)

    cost = api_call_cost(1) + token_cost(tokens)["total_micros"]
    ev = UsageEvent(tenant_id=tenant.id, idempotency_key=idempotency_key, request_hash=fp, period=period, api_calls=1,
                    input_tokens=tokens.input_tokens, cached_input_tokens=tokens.cached_input_tokens,
                    output_tokens=tokens.output_tokens, reasoning_tokens=tokens.reasoning_tokens,
                    quota_tokens=tokens.quota_tokens, cost_micros=cost, response={})
    db.add(ev)
    try:
        db.flush()
    except IntegrityError:
        # A concurrent retry with the same key won the race: return its stored result, record nothing.
        db.rollback()
        winner = db.scalar(select(UsageEvent).where(UsageEvent.tenant_id == tenant.id,
                                                    UsageEvent.idempotency_key == idempotency_key))
        return winner.response, True
    after = usage_totals(db, tenant.id, period)
    ev.response = {
        "usage_event_id": ev.id, "idempotency_key": idempotency_key, "period": period,
        "output": f"[simulated AI response to: {payload.get('prompt', '')[:60]}]",
        "metered": {"api_calls": 1, "input_tokens": tokens.input_tokens, "cached_input_tokens": tokens.cached_input_tokens,
                    "output_tokens": tokens.output_tokens, "reasoning_tokens": tokens.reasoning_tokens,
                    "quota_tokens": tokens.quota_tokens, "event_cost_micros": cost},
        "remaining": {"api_calls": plan.api_calls_limit - after["api_calls"],
                      "ai_tokens": plan.ai_tokens_limit - after["quota_tokens"]},
    }
    db.commit()
    return ev.response, False


def rollup(db, tenant: Tenant, period: str) -> dict:
    plan = db.get(Plan, tenant.plan_code)
    t = usage_totals(db, tenant.id, period)
    tok: TokenUsage = t["tokens"]
    return {
        "tenant": {"id": tenant.id, "name": tenant.name, "plan": plan.code, "status": tenant.status},
        "period": period,
        "usage": {
            "api_calls": {"used": t["api_calls"], "limit": plan.api_calls_limit, "remaining": max(plan.api_calls_limit - t["api_calls"], 0)},
            "ai_tokens": {"used": t["quota_tokens"], "limit": plan.ai_tokens_limit, "remaining": max(plan.ai_tokens_limit - t["quota_tokens"], 0),
                          "breakdown": {"input_tokens": tok.input_tokens, "of_which_cached": tok.cached_input_tokens,
                                        "fresh_input": tok.fresh_input, "output_tokens": tok.output_tokens,
                                        "reasoning_tokens": tok.reasoning_tokens, "billable_output": tok.billable_output}},
            "events": t["events"],
        },
        "cost": monthly_cost(plan.code, t["api_calls"], tok),
    }
