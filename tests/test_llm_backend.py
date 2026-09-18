"""
Tests for the Anthropic backend's mapping layer, using a fake client.

The network call itself cannot be tested without a key, but the part that
actually protects us - turning model output into rules, and refusing to enforce
anything it invents - is pure and is tested here.
"""

from __future__ import annotations

import pytest

from mandate_compiler.backends.anthropic_llm import (
    AnthropicBackend,
    LLMAnalysis,
    LLMClause,
    LLMRule,
)
from mandate_compiler.config import Config
from mandate_compiler.models import Bucket, Operator, Scope

CFG = Config(backend="anthropic", api_key="not-used-by-these-tests")


def to_clauses(analysis: LLMAnalysis, config: Config = CFG):
    return AnthropicBackend()._to_clauses(analysis, config)


def test_valid_rule_is_kept():
    result = to_clauses(
        LLMAnalysis(
            clauses=[
                LLMClause(
                    text="CHF 20 or less",
                    bucket="hard_rule",
                    reason="explicit comparator, value and currency",
                    rule=LLMRule(
                        field="authorization.billing_amount_chf",
                        operator="<=",
                        value=20,
                        currency="CHF",
                        scope="purchase",
                    ),
                )
            ],
            uncertainty_policy="ask",
        )
    )
    rules = [c.rule for c in result.clauses if c.bucket is Bucket.HARD_RULE]
    assert len(rules) == 1
    assert rules[0].field == "authorization.billing_amount_chf"
    assert rules[0].operator is Operator.LTE
    assert result.uncertainty_policy.value == "ask"


def test_invented_field_is_demoted_not_enforced():
    """The guard that matters: a hallucinated field must never become a rule."""
    result = to_clauses(
        LLMAnalysis(
            clauses=[
                LLMClause(
                    text="only from shops I use regularly",
                    bucket="hard_rule",
                    reason="model decided this was comparable",
                    rule=LLMRule(
                        field="authorization.merchant.visit_frequency",
                        operator=">=",
                        value=3,
                        scope="purchase",
                    ),
                )
            ]
        )
    )
    assert not [c for c in result.clauses if c.bucket is Bucket.HARD_RULE]
    assert [c for c in result.clauses if c.bucket is Bucket.GUIDANCE]
    assert [c for c in result.clauses if c.bucket is Bucket.OPEN_QUESTION]
    assert any("demoted" in w for w in result.warnings)


def test_scope_mismatch_is_demoted():
    result = to_clauses(
        LLMAnalysis(
            clauses=[
                LLMClause(
                    text="max 3 a week",
                    bucket="hard_rule",
                    reason="frequency",
                    rule=LLMRule(
                        field="rolling.purchase_count",
                        operator="<=",
                        value=3,
                        scope="purchase",  # wrong: rolling.* is period-scoped
                    ),
                )
            ]
        )
    )
    assert not [c for c in result.clauses if c.bucket is Bucket.HARD_RULE]
    assert any("demoted" in w for w in result.warnings)


def test_money_rule_without_currency_is_demoted():
    result = to_clauses(
        LLMAnalysis(
            clauses=[
                LLMClause(
                    text="under 20",
                    bucket="hard_rule",
                    reason="looks like a cap",
                    rule=LLMRule(
                        field="authorization.billing_amount_chf",
                        operator="<",
                        value=20,
                        currency=None,
                    ),
                )
            ]
        )
    )
    assert not [c for c in result.clauses if c.bucket is Bucket.HARD_RULE]


def test_hard_rule_with_no_rule_object_falls_back_to_guidance():
    result = to_clauses(
        LLMAnalysis(
            clauses=[
                LLMClause(text="something cheap", bucket="hard_rule",
                          reason="mislabelled", rule=None)
            ]
        )
    )
    assert not [c for c in result.clauses if c.bucket is Bucket.HARD_RULE]
    assert [c for c in result.clauses if c.bucket is Bucket.GUIDANCE]


def test_count_value_is_coerced_to_int():
    result = to_clauses(
        LLMAnalysis(
            clauses=[
                LLMClause(
                    text="at most 3 a week",
                    bucket="hard_rule",
                    reason="frequency",
                    rule=LLMRule(
                        field="rolling.purchase_count",
                        operator="<=",
                        value=3.0,
                        scope="period",
                        period_days=7,
                    ),
                )
            ]
        )
    )
    rules = [c.rule for c in result.clauses if c.bucket is Bucket.HARD_RULE]
    assert len(rules) == 1
    assert isinstance(rules[0].value, int)
    assert rules[0].value == 3


def test_open_question_without_text_is_dropped_with_a_warning():
    result = to_clauses(
        LLMAnalysis(
            clauses=[
                LLMClause(text="vague thing", bucket="open_question",
                          reason="unclear", question=None)
            ]
        )
    )
    assert not [c for c in result.clauses if c.bucket is Bucket.OPEN_QUESTION]
    assert any("dropped" in w for w in result.warnings)


def test_demotion_can_be_turned_off_for_strict_debugging():
    strict = Config(backend="anthropic", api_key="x", demote_unknown_fields=False)
    with pytest.raises(RuntimeError):
        to_clauses(
            LLMAnalysis(
                clauses=[
                    LLMClause(
                        text="bad", bucket="hard_rule", reason="r",
                        rule=LLMRule(field="authorization.nope", operator="<=", value=1),
                    )
                ]
            ),
            strict,
        )


def test_non_chf_rule_raises_the_fx_warning():
    result = to_clauses(
        LLMAnalysis(
            clauses=[
                LLMClause(
                    text="up to EUR 45",
                    bucket="hard_rule",
                    reason="cap",
                    rule=LLMRule(
                        field="authorization.billing_amount_chf",
                        operator="<=",
                        value=45,
                        currency="EUR",
                    ),
                )
            ]
        )
    )
    assert any("fx_rates" in w for w in result.warnings)
