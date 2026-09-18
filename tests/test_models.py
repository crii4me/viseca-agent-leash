"""The invariants that stop an uncheckable rule reaching the decision engine."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mandate_compiler.backends import safe_rule
from mandate_compiler.models import HardRule, Operator, Scope
from mandate_compiler.schema import mandate_json_schema, validation_errors


def test_unknown_field_is_rejected():
    with pytest.raises(ValidationError):
        HardRule(field="authorization.vibes", operator=Operator.LTE, value=5, currency="CHF")


def test_purchase_field_cannot_carry_period_scope():
    with pytest.raises(ValidationError):
        HardRule(
            field="authorization.billing_amount_chf",
            operator=Operator.LTE,
            value=20,
            currency="CHF",
            scope=Scope.PERIOD,
            period_days=7,
        )


def test_rolling_field_cannot_carry_purchase_scope():
    with pytest.raises(ValidationError):
        HardRule(
            field="rolling.purchase_count",
            operator=Operator.LTE,
            value=3,
            scope=Scope.PURCHASE,
        )


def test_period_scope_requires_period_days():
    with pytest.raises(ValidationError):
        HardRule(field="rolling.purchase_count", operator=Operator.LTE, value=3,
                 scope=Scope.PERIOD)


def test_purchase_scope_forbids_period_days():
    with pytest.raises(ValidationError):
        HardRule(field="authorization.billing_amount_chf", operator=Operator.LTE,
                 value=20, currency="CHF", period_days=7)


def test_money_field_requires_a_currency():
    with pytest.raises(ValidationError):
        HardRule(field="authorization.billing_amount_chf", operator=Operator.LTE, value=20)


def test_count_field_rejects_a_currency():
    with pytest.raises(ValidationError):
        HardRule(field="rolling.purchase_count", operator=Operator.LTE, value=3,
                 currency="CHF", scope=Scope.PERIOD, period_days=7)


def test_count_field_rejects_a_fractional_value():
    with pytest.raises(ValidationError):
        HardRule(field="rolling.purchase_count", operator=Operator.LTE, value=2.5,
                 scope=Scope.PERIOD, period_days=7)


def test_currency_must_be_iso_4217():
    with pytest.raises(ValidationError):
        HardRule(field="authorization.billing_amount_chf", operator=Operator.LTE,
                 value=20, currency="Swiss Francs")


def test_currency_is_normalised_to_upper_case():
    r = HardRule(field="authorization.billing_amount_chf", operator=Operator.LTE,
                 value=20, currency="chf")
    assert r.currency == "CHF"


def test_negative_period_days_rejected():
    with pytest.raises(ValidationError):
        HardRule(field="rolling.purchase_count", operator=Operator.LTE, value=3,
                 scope=Scope.PERIOD, period_days=-7)


def test_in_operator_requires_a_list():
    with pytest.raises(ValidationError):
        HardRule(field="authorization.merchant.country", operator=Operator.IN, value="CH")

    ok = HardRule(field="authorization.merchant.country", operator=Operator.IN,
                  value=["CH", "DE"])
    assert ok.value == ["CH", "DE"]


def test_safe_rule_never_raises():
    rule, err = safe_rule(field="authorization.nonsense", operator="<=", value=1)
    assert rule is None
    assert err and "unknown field" in err

    rule, err = safe_rule(field="authorization.billing_amount_chf", operator="<=",
                          value=20, currency="CHF")
    assert err is None
    assert rule is not None


def test_describe_is_readable():
    r = HardRule(field="rolling.billing_amount_chf", operator=Operator.LTE, value=200,
                 currency="CHF", scope=Scope.PERIOD, period_days=30)
    assert r.describe() == "rolling.billing_amount_chf <= 200 CHF over 30d"


# --------------------------------------------------------------------------
# Schema gate
# --------------------------------------------------------------------------

def test_schema_is_well_formed():
    schema = mandate_json_schema()
    assert schema["title"] == "VisecaMandate"
    assert set(schema["properties"]) == {
        "hard_rules", "uncertainty_policy", "guidance", "open_questions"
    }


def test_schema_rejects_a_bad_uncertainty_policy():
    bad = {"hard_rules": [], "uncertainty_policy": "maybe", "guidance": [],
           "open_questions": []}
    assert validation_errors(bad)


def test_schema_rejects_extra_top_level_fields():
    bad = {"hard_rules": [], "uncertainty_policy": "ask", "guidance": [],
           "open_questions": [], "secret_backdoor": True}
    assert validation_errors(bad)


def test_schema_accepts_a_minimal_valid_mandate():
    good = {"hard_rules": [], "uncertainty_policy": "ask", "guidance": [],
            "open_questions": []}
    assert validation_errors(good) == []
