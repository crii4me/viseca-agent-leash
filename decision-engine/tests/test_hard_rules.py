"""
Tests for decision_engine.hard_rules.evaluate_hard_rules().

Every fixture rule comes from actually calling mandate_compiler.compile_mandate()
on the same instructions mandate-compiler/tests/fixtures/instructions.json uses
(see conftest.py) -- not hand-typed dicts -- so these tests exercise the real
contract shape, not an assumed one. A couple of tests use a hand-built HardRule
dict where the compiler's deterministic backend doesn't produce that field
(merchant.category), since FIELDS still declares it evaluable.
"""

from __future__ import annotations

from decision_engine import CurrencyNotConvertible, Ledger, evaluate_hard_rules
from mandate_compiler.models import HardRule


# ---------------------------------------------------------------------------
# The Viseca worked example: CHF 20 or less, purchase-scoped.
# ---------------------------------------------------------------------------

class TestWorkedExample:
    def test_under_the_cap_satisfies(self, event_factory, worked_example_rules):
        event = event_factory(hard_rules=worked_example_rules, billing_amount_chf=15.0)
        result = evaluate_hard_rules(event)
        assert result.compliant is True
        assert result.violations() == []

    def test_over_the_cap_violates(self, event_factory, worked_example_rules):
        event = event_factory(hard_rules=worked_example_rules, billing_amount_chf=25.0)
        result = evaluate_hard_rules(event)
        assert result.compliant is False
        assert result.violations()[0].rule["field"] == "authorization.billing_amount_chf"

    def test_exactly_at_the_cap_satisfies_lte(self, event_factory, worked_example_rules):
        """The compiled rule is <=, not < -- CHF 20.0 exactly must pass."""
        event = event_factory(hard_rules=worked_example_rules, billing_amount_chf=20.0)
        result = evaluate_hard_rules(event)
        assert result.compliant is True


# ---------------------------------------------------------------------------
# Non-CHF currency: must convert via fx_rates, never assume CHF, never
# silently skip on a mismatch.
# ---------------------------------------------------------------------------

class TestCurrencyConversion:
    def test_eur_rule_is_converted_before_comparing(self, event_factory, eur_cap_rules, eur_fx_rates):
        # EUR 45 cap * 0.93 = CHF 41.85. A CHF 40 purchase must pass.
        event = event_factory(hard_rules=eur_cap_rules, billing_amount_chf=40.0)
        result = evaluate_hard_rules(event, fx_rates=eur_fx_rates)
        assert result.compliant is True

    def test_eur_rule_still_blocks_over_the_converted_cap(self, event_factory, eur_cap_rules, eur_fx_rates):
        # CHF 45 is over the converted CHF 41.85 cap, even though 45 < the
        # raw, unconverted EUR number -- this is exactly the bug silently
        # comparing CHF against an EUR-labeled threshold would produce.
        event = event_factory(hard_rules=eur_cap_rules, billing_amount_chf=45.0)
        result = evaluate_hard_rules(event, fx_rates=eur_fx_rates)
        assert result.compliant is False

    def test_missing_fx_rate_fails_closed_not_skipped(self, event_factory, eur_cap_rules):
        """No fx_rates supplied at all -- must be unresolvable (non-compliant),
        never silently treated as satisfied."""
        event = event_factory(hard_rules=eur_cap_rules, billing_amount_chf=1.0)
        result = evaluate_hard_rules(event)  # fx_rates omitted entirely
        assert result.compliant is False
        assert result.rule_results[0].status == "unresolvable"
        assert "fx_rate" in result.rule_results[0].detail.lower() or "convert" in result.rule_results[0].detail.lower()

    def test_money_field_with_no_currency_on_the_rule_fails_closed(self, event_factory):
        """A money field requires a currency by contract (mandate_compiler.models
        enforces this at compile time); if one somehow arrives without it
        anyway, evaluation must not assume CHF."""
        rule = {"field": "authorization.billing_amount_chf", "operator": "<=", "value": 100, "scope": "purchase"}
        event = event_factory(hard_rules=[rule], billing_amount_chf=10.0)
        result = evaluate_hard_rules(event)
        assert result.compliant is False
        assert result.rule_results[0].status == "unresolvable"


# ---------------------------------------------------------------------------
# Two rules of different scope from one sentence: rolling purchase-count
# (period) + per-purchase amount (purchase). Operators differ: <= vs <.
# ---------------------------------------------------------------------------

class TestWeeklyLunchCap:
    def test_requires_a_ledger_for_the_period_scope_rule(self, event_factory, weekly_lunch_rules):
        event = event_factory(hard_rules=weekly_lunch_rules, billing_amount_chf=20.0)
        import pytest

        with pytest.raises(ValueError, match="ledger"):
            evaluate_hard_rules(event, ledger=None)

    def test_strict_less_than_is_not_less_than_or_equal(self, event_factory, weekly_lunch_rules, ledger):
        """The compiled rule for the amount is '<', not '<=' -- CHF 25.0
        exactly must VIOLATE ('under CHF 25' means strictly under)."""
        event = event_factory(hard_rules=weekly_lunch_rules, billing_amount_chf=25.0, card_id="CA_LUNCH")
        result = evaluate_hard_rules(event, ledger=ledger)
        assert result.compliant is False
        amount_rule = next(r for r in result.rule_results if r.rule["field"] == "authorization.billing_amount_chf")
        assert amount_rule.status == "violated"

    def test_third_lunch_this_week_still_satisfies_the_count_cap(self, event_factory, weekly_lunch_rules, ledger):
        """<= 3 per rolling 7 days: with 2 prior approved lunches this week,
        a 3rd (count becomes 3, still <= 3) must satisfy the count rule."""
        card_id = "CA_LUNCH2"
        for i in range(2):
            prior_event = event_factory(
                hard_rules=weekly_lunch_rules,
                billing_amount_chf=10.0,
                card_id=card_id,
                timestamp=f"2026-09-1{5 + i}T12:00:00Z",
                authorization_id=f"AU_PRIOR_{i}",
            )
            ledger.record_approved(prior_event)

        event = event_factory(hard_rules=weekly_lunch_rules, billing_amount_chf=10.0, card_id=card_id, timestamp="2026-09-19T12:00:00Z")
        result = evaluate_hard_rules(event, ledger=ledger)
        count_rule = next(r for r in result.rule_results if r.rule["field"] == "rolling.purchase_count")
        assert count_rule.ok is True
        assert count_rule.actual_value == 3  # 2 prior + this one being evaluated

    def test_fourth_lunch_this_week_violates_the_count_cap(self, event_factory, weekly_lunch_rules, ledger):
        card_id = "CA_LUNCH3"
        for i in range(3):
            prior_event = event_factory(
                hard_rules=weekly_lunch_rules,
                billing_amount_chf=10.0,
                card_id=card_id,
                timestamp=f"2026-09-1{5 + i}T12:00:00Z",
                authorization_id=f"AU_PRIOR_{i}",
            )
            ledger.record_approved(prior_event)

        event = event_factory(hard_rules=weekly_lunch_rules, billing_amount_chf=10.0, card_id=card_id, timestamp="2026-09-19T12:00:00Z")
        result = evaluate_hard_rules(event, ledger=ledger)
        assert result.compliant is False
        count_rule = next(r for r in result.rule_results if r.rule["field"] == "rolling.purchase_count")
        assert count_rule.status == "violated"

    def test_window_excludes_rows_older_than_period_days(self, event_factory, weekly_lunch_rules, ledger):
        """A lunch 10 days ago (outside the 7-day window) must not count."""
        card_id = "CA_LUNCH4"
        old_event = event_factory(
            hard_rules=weekly_lunch_rules, billing_amount_chf=10.0, card_id=card_id,
            timestamp="2026-09-09T12:00:00Z", authorization_id="AU_OLD",
        )
        ledger.record_approved(old_event)

        event = event_factory(hard_rules=weekly_lunch_rules, billing_amount_chf=10.0, card_id=card_id, timestamp="2026-09-19T12:00:00Z")
        result = evaluate_hard_rules(event, ledger=ledger)
        count_rule = next(r for r in result.rule_results if r.rule["field"] == "rolling.purchase_count")
        assert count_rule.actual_value == 1  # 0 prior in-window + this one being evaluated


# ---------------------------------------------------------------------------
# Fail-closed behavior: unknown fields, scope/field mismatches.
# ---------------------------------------------------------------------------

class TestFailClosedBehavior:
    def test_field_outside_the_compiler_vocabulary_is_unresolvable(self, event_factory):
        rule = {"field": "authorization.nonexistent_field", "operator": "==", "value": 1}
        event = event_factory(hard_rules=[rule])
        result = evaluate_hard_rules(event)
        assert result.compliant is False
        assert result.rule_results[0].status == "unresolvable"

    def test_scope_field_mismatch_is_unresolvable_not_silently_reinterpreted(self, event_factory):
        """A rolling.* field claiming scope=purchase (or vice versa) is
        incoherent per the compiler's own model_validator -- this can only
        arrive here if something bypassed compile_mandate. Must fail closed,
        not silently 'fix' the scope."""
        rule = {"field": "rolling.purchase_count", "operator": "<=", "value": 3, "scope": "purchase"}
        event = event_factory(hard_rules=[rule])
        result = evaluate_hard_rules(event)
        assert result.compliant is False
        assert result.rule_results[0].status == "unresolvable"
        assert "scope" in result.rule_results[0].detail.lower()

    def test_provisional_field_is_flagged_in_the_detail(self, event_factory):
        """authorization.merchant.category is a real, evaluable field per
        FIELDS -- but it's PROVISIONAL (unconfirmed against the live event
        schema), and every result involving it must say so."""
        rule = HardRule(
            field="authorization.merchant.category", operator="not_in", value=["gambling", "adult"]
        ).model_dump(mode="json", exclude_none=True)
        event = event_factory(hard_rules=[rule], merchant_category="groceries")
        result = evaluate_hard_rules(event)
        assert result.compliant is True  # "groceries" not_in ["gambling", "adult"] -> satisfied
        assert "PROVISIONAL" in result.rule_results[0].detail

    def test_provisional_merchant_category_blocks_a_denylisted_category(self, event_factory):
        rule = HardRule(
            field="authorization.merchant.category", operator="not_in", value=["gambling", "adult"]
        ).model_dump(mode="json", exclude_none=True)
        event = event_factory(hard_rules=[rule], merchant_category="gambling")
        result = evaluate_hard_rules(event)
        assert result.compliant is False


# ---------------------------------------------------------------------------
# The idempotency contract the ledger provides (retried delivery of the
# same authorization_id must not be decided or counted twice).
# ---------------------------------------------------------------------------

class TestLedgerIdempotency:
    def test_record_approved_then_seen_again_is_flagged(self, ledger, event_factory, worked_example_rules):
        event = event_factory(hard_rules=worked_example_rules, authorization_id="AU_DUP", billing_amount_chf=5.0)
        assert ledger.already_handled("AU_DUP") is False
        ledger.record_seen("AU_DUP")
        ledger.record_approved(event)
        assert ledger.already_handled("AU_DUP") is True
