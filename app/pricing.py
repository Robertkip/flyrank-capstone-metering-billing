"""Pinned pricing constants + the money math. All money is INTEGER micro-dollars (1 USD = 1_000_000 µ$).
No floats anywhere in billing. Rates are per 1,000,000 tokens / per 1,000 API calls, also in µ$."""
from dataclasses import dataclass

MICROS_PER_USD = 1_000_000

# ---- Plans (monthly) ----
PLANS = {
    "free": {"name": "Free", "api_calls": 1_000, "ai_tokens": 100_000, "base_fee_micros": 0},
    "pro": {"name": "Pro", "api_calls": 50_000, "ai_tokens": 5_000_000, "base_fee_micros": 29 * MICROS_PER_USD},
}

# ---- Usage rates (pinned; modelled on Gemini Flash-class list prices) ----
API_CALL_PER_1K_MICROS = 400_000            # $0.40 per 1,000 API calls
INPUT_PER_1M_MICROS = 300_000               # $0.30 per 1M fresh (uncached) input tokens
CACHED_INPUT_PER_1M_MICROS = 75_000         # $0.075 per 1M cached input tokens (25% of input)
OUTPUT_PER_1M_MICROS = 2_500_000            # $2.50 per 1M output tokens - reasoning tokens bill at this rate


@dataclass(frozen=True)
class TokenUsage:
    """As reported by the model provider.
    input_tokens:        ALL prompt tokens, INCLUDING the cached ones (like Gemini's promptTokenCount)
    cached_input_tokens: the subset of input_tokens served from cache (must be <= input_tokens)
    output_tokens:       visible response tokens
    reasoning_tokens:    hidden "thinking" tokens - billed as output
    """
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    def __post_init__(self):
        for name in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached_input_tokens is a subset of input_tokens and cannot exceed it")

    # Why categories can't simply be added: cached tokens are ALREADY inside input_tokens.
    @property
    def fresh_input(self) -> int:
        return self.input_tokens - self.cached_input_tokens

    @property
    def billable_output(self) -> int:
        return self.output_tokens + self.reasoning_tokens

    @property
    def quota_tokens(self) -> int:
        """Tokens counted against the plan quota: every token processed once (cached counted once, not twice)."""
        return self.input_tokens + self.output_tokens + self.reasoning_tokens


def _per_million(tokens: int, rate_micros: int) -> int:
    """tokens * rate / 1M, rounded half-up to the nearest micro-dollar - integer math only."""
    return (tokens * rate_micros + 500_000) // 1_000_000


def token_cost(u: TokenUsage) -> dict:
    fresh = _per_million(u.fresh_input, INPUT_PER_1M_MICROS)
    cached = _per_million(u.cached_input_tokens, CACHED_INPUT_PER_1M_MICROS)
    output = _per_million(u.billable_output, OUTPUT_PER_1M_MICROS)
    return {"fresh_input_micros": fresh, "cached_input_micros": cached, "output_micros": output,
            "total_micros": fresh + cached + output}


def api_call_cost(calls: int) -> int:
    return (calls * API_CALL_PER_1K_MICROS + 500) // 1000


def to_usd(micros: int) -> str:
    """Exact decimal string, rounded half-up to cents for display: 12_345_678 -> '12.35'."""
    cents = (micros + 5_000) // 10_000
    return f"{cents // 100}.{cents % 100:02d}"


def monthly_cost(plan_code: str, api_calls: int, tokens: TokenUsage) -> dict:
    """Price the whole month from the aggregated counts (rounding happens once, not per event)."""
    base = PLANS[plan_code]["base_fee_micros"]
    calls = api_call_cost(api_calls)
    tok = token_cost(tokens)
    total = base + calls + tok["total_micros"]
    return {"base_fee_micros": base, "api_calls_micros": calls, "tokens": tok, "total_micros": total,
            "total_usd": to_usd(total), "currency": "usd"}
