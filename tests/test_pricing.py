"""Probe 5: pinned pricing rules produce exact expected totals (integer micro-dollars)."""
import pytest
from app.pricing import TokenUsage, api_call_cost, monthly_cost, to_usd, token_cost


def test_worked_example_exact_totals():
    # 1,000,000 input of which 400,000 cached; 200,000 output; 100,000 reasoning
    u = TokenUsage(input_tokens=1_000_000, cached_input_tokens=400_000, output_tokens=200_000, reasoning_tokens=100_000)
    c = token_cost(u)
    assert c["fresh_input_micros"] == 180_000      # 600k fresh  x $0.30/1M  = $0.18
    assert c["cached_input_micros"] == 30_000      # 400k cached x $0.075/1M = $0.03
    assert c["output_micros"] == 750_000           # (200k + 100k reasoning) x $2.50/1M = $0.75
    assert c["total_micros"] == 960_000            # $0.96
    assert u.quota_tokens == 1_300_000             # cached counted once, not twice


def test_naive_addition_would_be_wrong():
    u = TokenUsage(input_tokens=1_000_000, cached_input_tokens=400_000, output_tokens=200_000, reasoning_tokens=100_000)
    naive = (1_000_000 * 300_000 + 400_000 * 75_000 + 200_000 * 2_500_000) // 1_000_000   # adds cached on top, drops reasoning
    assert naive == 830_000 and token_cost(u)["total_micros"] != naive


def test_reasoning_bills_as_output():
    a = token_cost(TokenUsage(output_tokens=1_000_000))["total_micros"]
    b = token_cost(TokenUsage(output_tokens=0, reasoning_tokens=1_000_000))["total_micros"]
    assert a == b == 2_500_000


def test_cached_is_cheaper_than_fresh():
    fresh = token_cost(TokenUsage(input_tokens=1_000_000))["total_micros"]
    cached = token_cost(TokenUsage(input_tokens=1_000_000, cached_input_tokens=1_000_000))["total_micros"]
    assert (fresh, cached) == (300_000, 75_000)


def test_monthly_rollup_pro():
    c = monthly_cost("pro", 12_345, TokenUsage(input_tokens=2_000_000, cached_input_tokens=500_000, output_tokens=300_000, reasoning_tokens=50_000))
    # base 29_000_000 + calls 12,345 x $0.40/1k = 4_938_000 + tokens (450_000 + 37_500 + 875_000 = 1_362_500)
    assert c["total_micros"] == 29_000_000 + 4_938_000 + 1_362_500 == 35_300_500
    assert c["total_usd"] == "35.30"


def test_rounding_is_integer_half_up():
    assert api_call_cost(1) == 400 and to_usd(5_000) == "0.01" and to_usd(4_999) == "0.00"


def test_invalid_token_counts_rejected():
    with pytest.raises(ValueError):
        TokenUsage(input_tokens=10, cached_input_tokens=11)
    with pytest.raises(ValueError):
        TokenUsage(output_tokens=-1)
