"""End-to-end tests for compile_mandate, driven by the fixture set.

These run entirely offline against the deterministic backend, so they pass on a
machine with no API key - which is every machine until event day.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mandate_compiler import Config, Mandate, compile_mandate
from mandate_compiler.backends.deterministic import DeterministicBackend
from mandate_compiler.models import Operator, Scope, UncertaintyPolicy
from mandate_compiler.schema import validation_errors

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "instructions.json").read_text(encoding="utf-8")
)

OFFLINE = Config(backend="deterministic")

WORKED_EXAMPLE = (
    "Buy one ordinary grocery item for CHF 20 or less from a shop I use regularly. "
    "Ask me when uncertain."
)


def compile_offline(instruction: str, **kw) -> Mandate:
    cfg = kw.pop("config", OFFLINE)
    return compile_mandate(instruction, config=cfg, backend=DeterministicBackend(), **kw)


# --------------------------------------------------------------------------
# The worked example from Viseca's docs - the acceptance test for Function 1.
# --------------------------------------------------------------------------

def test_worked_example_produces_the_documented_split():
    m = compile_offline(WORKED_EXAMPLE)

    # "CHF 20 or less" -> exactly one hard rule, on the confirmed field.
    assert len(m.hard_rules) == 1
    rule = m.hard_rules[0]
    assert rule.field == "authorization.billing_amount_chf"
    assert rule.operator is Operator.LTE
    assert rule.value == 20
    assert rule.currency == "CHF"
    assert rule.scope is Scope.PURCHASE
    assert rule.period_days is None

    # "ask me when uncertain" -> the policy, not a rule.
    assert m.uncertainty_policy is UncertaintyPolicy.ASK

    # "ordinary grocery item" and "a shop I use regularly" -> guidance.
    joined = " ".join(m.guidance).casefold()
    assert "ordinary grocery item" in joined
    assert "shop i use regularly" in joined

    # "regularly" is undefined -> asked about, not guessed at.
    assert m.open_questions
    assert any("regularly" in q.casefold() for q in m.open_questions)


def test_worked_example_does_not_invent_a_regularity_threshold():
    """The failure we care most about: turning 'regularly' into a fake number."""
    m = compile_offline(WORKED_EXAMPLE)
    for rule in m.hard_rules:
        assert "rolling" not in rule.field, "invented a history-based rule from 'regularly'"
    assert len(m.hard_rules) == 1


# --------------------------------------------------------------------------
# Fixture-driven expectations
# --------------------------------------------------------------------------

@pytest.mark.parametrize("fx", FIXTURES, ids=[f["id"] for f in FIXTURES])
def test_fixture_expectations(fx):
    m = compile_offline(fx["instruction"])
    expect = fx["expect"]

    assert m.uncertainty_policy.value == expect["uncertainty_policy"], (
        f"{fx['id']}: wrong uncertainty_policy"
    )

    got_fields = sorted(r.field for r in m.hard_rules)
    assert got_fields == sorted(expect["hard_rule_fields"]), (
        f"{fx['id']}: hard rule fields differ"
    )

    if "currencies" in expect:
        got = sorted({r.currency for r in m.hard_rules if r.currency})
        assert got == sorted(expect["currencies"])

    if "min_guidance" in expect:
        assert len(m.guidance) >= expect["min_guidance"]

    assert len(m.open_questions) >= expect["min_open_questions"], (
        f"{fx['id']}: expected at least {expect['min_open_questions']} open questions, "
        f"got {m.open_questions}"
    )


@pytest.mark.parametrize("fx", FIXTURES, ids=[f["id"] for f in FIXTURES])
def test_every_fixture_validates_against_the_schema(fx):
    m = compile_offline(fx["instruction"])
    assert validation_errors(m.to_viseca_dict()) == []


@pytest.mark.parametrize("fx", FIXTURES, ids=[f["id"] for f in FIXTURES])
def test_canonical_output_has_only_the_four_contract_fields(fx):
    m = compile_offline(fx["instruction"])
    assert set(m.to_viseca_dict()) == {
        "hard_rules",
        "uncertainty_policy",
        "guidance",
        "open_questions",
    }


@pytest.mark.parametrize("fx", FIXTURES, ids=[f["id"] for f in FIXTURES])
def test_output_is_json_serialisable(fx):
    m = compile_offline(fx["instruction"])
    json.dumps(m.to_viseca_dict())  # must not raise


# --------------------------------------------------------------------------
# Never-guess properties
# --------------------------------------------------------------------------

def test_amount_without_a_currency_is_asked_about_not_assumed_chf():
    m = compile_offline("Keep each purchase to 30 or less.")
    assert m.hard_rules == [], "invented a currency for a bare number"
    assert any("currency" in q.casefold() for q in m.open_questions)


def test_unevaluable_constraint_never_becomes_a_hard_rule():
    m = compile_offline("Only buy from shops with at least 4 stars.")
    assert m.hard_rules == []


def test_fully_vague_instruction_yields_no_rules_but_does_yield_questions():
    m = compile_offline("Buy whatever seems reasonable.")
    assert m.hard_rules == []
    assert len(m.open_questions) >= 1


def test_unstated_uncertainty_policy_defaults_to_ask_and_says_so():
    m = compile_offline("Spend up to CHF 10 on coffee.")
    assert m.uncertainty_policy is UncertaintyPolicy.ASK
    assert any("isn't sure" in q or "not sure" in q.casefold() for q in m.open_questions)
    assert m.meta and any("defaulted" in w for w in m.meta.warnings)


# --------------------------------------------------------------------------
# Operator precision - an off-by-one here spends money that wasn't authorised
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,operator,value",
    [
        ("Spend CHF 20 or less.", Operator.LTE, 20),
        ("Spend no more than CHF 20.", Operator.LTE, 20),
        ("Spend at most CHF 20.", Operator.LTE, 20),
        ("Spend up to CHF 20.", Operator.LTE, 20),
        ("Spend under CHF 20.", Operator.LT, 20),
        ("Spend below CHF 20.", Operator.LT, 20),
        ("Spend less than CHF 20.", Operator.LT, 20),
        ("Don't spend more than CHF 20.", Operator.LTE, 20),
        ("Never go over CHF 20.", Operator.LTE, 20),
    ],
)
def test_comparator_maps_to_the_right_operator(text, operator, value):
    m = compile_offline(text)
    assert len(m.hard_rules) == 1, f"{text!r} -> {m.hard_rules}"
    assert m.hard_rules[0].operator is operator
    assert m.hard_rules[0].value == value


@pytest.mark.parametrize(
    "text,currency",
    [
        ("Spend up to CHF 20.", "CHF"),
        ("Spend up to EUR 20.", "EUR"),
        ("Spend up to 20 euros.", "EUR"),
        ("Spend up to 20 francs.", "CHF"),
        ("Spend up to USD 20.", "USD"),
        ("Spend up to 20 dollars.", "USD"),
    ],
)
def test_currency_is_read_not_assumed(text, currency):
    m = compile_offline(text)
    assert len(m.hard_rules) == 1
    assert m.hard_rules[0].currency == currency


def test_non_chf_rule_warns_that_function_two_must_convert():
    m = compile_offline("Spend up to EUR 45 on supplies.")
    assert m.hard_rules[0].currency == "EUR"
    assert m.meta and any("fx_rates" in w for w in m.meta.warnings)


def test_decimal_amounts_survive():
    m = compile_offline("Spend up to CHF 19.50 per item.")
    assert m.hard_rules[0].value == pytest.approx(19.50)


# --------------------------------------------------------------------------
# Period handling
# --------------------------------------------------------------------------

def test_frequency_constraint_becomes_a_period_scoped_count_rule():
    m = compile_offline("Buy coffee no more than 3 times a week.")
    rules = [r for r in m.hard_rules if r.field == "rolling.purchase_count"]
    assert len(rules) == 1
    assert rules[0].scope is Scope.PERIOD
    assert rules[0].period_days == 7
    assert rules[0].value == 3
    assert rules[0].currency is None


def test_monthly_spend_cap_is_money_not_a_count():
    """Regression: 'CHF 200 a month' was once compiled as 200 purchases/month."""
    m = compile_offline("Don't spend more than CHF 200 a month on groceries.")
    assert len(m.hard_rules) == 1
    rule = m.hard_rules[0]
    assert rule.field == "rolling.billing_amount_chf"
    assert rule.value == 200
    assert rule.currency == "CHF"
    assert rule.period_days == 30


def test_approximate_window_is_flagged_for_confirmation():
    m = compile_offline("Don't spend more than CHF 200 a month.")
    assert any("calendar month" in q for q in m.open_questions)


def test_exact_window_is_not_questioned():
    m = compile_offline("Buy lunch no more than 3 times a week. Ask me when uncertain.")
    assert not any("calendar" in q for q in m.open_questions)


# --------------------------------------------------------------------------
# Purity and isolation
# --------------------------------------------------------------------------

def test_compilation_is_deterministic():
    a = compile_offline(WORKED_EXAMPLE).to_viseca_dict()
    b = compile_offline(WORKED_EXAMPLE).to_viseca_dict()
    assert a == b


def test_instruction_is_not_mutated():
    text = WORKED_EXAMPLE
    before = str(text)
    compile_offline(text)
    assert text == before


def test_empty_instruction_is_rejected():
    for bad in ("", "   ", "\n"):
        with pytest.raises(ValueError):
            compile_offline(bad)
    with pytest.raises(ValueError):
        compile_offline(None)  # type: ignore[arg-type]


def test_source_instruction_is_preserved_for_audit():
    m = compile_offline(WORKED_EXAMPLE)
    assert m.source_instruction == WORKED_EXAMPLE


def test_every_clause_carries_a_reason():
    """Explainability is a judging criterion; an unexplained routing is a bug."""
    m = compile_offline(WORKED_EXAMPLE)
    assert m.clauses
    for c in m.clauses:
        assert c.reason.strip(), f"clause {c.text!r} has no reason"


def test_unresolvable_to_is_configurable():
    guidance_only = Config(backend="deterministic", unresolvable_to="guidance")
    questions_only = Config(backend="deterministic", unresolvable_to="open_question")

    g = compile_mandate(WORKED_EXAMPLE, config=guidance_only, backend=DeterministicBackend())
    q = compile_mandate(WORKED_EXAMPLE, config=questions_only, backend=DeterministicBackend())

    assert any("regularly" in x.casefold() for x in g.guidance)
    assert not any("regularly" in x.casefold() for x in g.open_questions)
    assert not any("regularly" in x.casefold() for x in q.guidance)
    assert any("regularly" in x.casefold() for x in q.open_questions)
