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
def test_canonical_output_has_only_the_contract_fields(fx):
    m = compile_offline(fx["instruction"])
    assert set(m.to_viseca_dict()) == {
        "hard_rules",
        "uncertainty_policy",
        "guidance",
        "open_questions",
        "blocking_open_questions",
    }


@pytest.mark.parametrize("fx", FIXTURES, ids=[f["id"] for f in FIXTURES])
def test_blocking_questions_are_a_subset_of_open_questions(fx):
    """The two lists must never drift: blocking is a filter, not a second source."""
    m = compile_offline(fx["instruction"])
    d = m.to_viseca_dict()
    assert set(d["blocking_open_questions"]) <= set(d["open_questions"])


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


@pytest.mark.parametrize(
    "instruction,must_mention",
    [
        (
            "Replace my worn road-running shoes in size 43. Buy only from a "
            "specialist sports retailer, only if the order can be returned "
            "within 14 days or more, and pay no more than CHF 200. Ask me when "
            "uncertain.",
            ["returnab", "categor"],
        ),
        (
            "The agent may buy clothing for me, up to CHF 250 per order, from "
            "shops I have used before. Pause anything that looks like someone "
            "other than me is driving the session. Ask me when uncertain.",
            ["used before", "session"],
        ),
    ],
)
def test_stated_conditions_with_no_field_become_open_questions(instruction, must_mention):
    """Regression for Bug 3.

    SCEN0002 and SCEN0003 previously produced ZERO open questions, even though
    'only if the order can be returned', 'from shops I have used before' and
    'someone other than me driving the session' are all conditions the customer
    expects enforced. Filing them as silent guidance is the worst outcome: not
    enforced, and not admitted as unenforceable.
    """
    m = compile_offline(instruction)
    assert m.open_questions, "stated conditions were filed as silent guidance"
    blob = " ".join(m.open_questions).casefold()
    for needle in must_mention:
        assert needle in blob, f"no open question mentions {needle!r}: {m.open_questions}"


def test_unenforceable_condition_questions_are_not_blocking():
    """They must not re-create the step_up parade Bug 5 removed.

    Function 2's risk layer does enforce most of these via derived
    requirements, so the question is about precision, not about whether the
    purchase can be judged at all.
    """
    m = compile_offline(
        "Buy only from a specialist sports retailer, only if the order can be "
        "returned within 14 days or more, and pay no more than CHF 200."
    )
    assert m.open_questions
    assert m.blocking_open_questions == []


def test_plain_descriptions_do_not_become_open_questions():
    """Guard against the new check over-firing on ordinary guidance."""
    m = compile_offline("Buy one ordinary grocery item for CHF 20 or less.")
    joined = " ".join(m.open_questions).casefold()
    assert "ordinary grocery item" not in joined
    assert "cannot be enforced" not in joined


def test_unevaluable_constraint_never_becomes_a_hard_rule():
    m = compile_offline("Only buy from shops with at least 4 stars.")
    assert m.hard_rules == []


def test_fully_vague_instruction_yields_no_rules_but_does_yield_questions():
    m = compile_offline("Buy whatever seems reasonable.")
    assert m.hard_rules == []
    assert len(m.open_questions) >= 1


def test_regularly_question_is_not_blocking():
    """Regression for SCEN0000.

    'A shop I use regularly' is unanswerable, but it does not stop the CHF 20
    cap being checked. Marking it blocking makes the simplest purchase in the
    whole data set return step_up.
    """
    m = compile_offline(WORKED_EXAMPLE)
    assert any("regularly" in q.casefold() for q in m.open_questions)
    assert m.blocking_open_questions == [], m.blocking_open_questions


def test_missing_currency_question_is_blocking():
    """The opposite case: without a currency the rule cannot be compared at all."""
    m = compile_offline("Keep each purchase to 30 or less.")
    assert m.blocking_open_questions, m.open_questions
    assert any("currency" in q.casefold() for q in m.blocking_open_questions)


def test_defaulted_uncertainty_policy_question_is_not_blocking():
    """Otherwise every mandate that omits a policy would step_up forever."""
    m = compile_offline("Spend up to CHF 10 on coffee.")
    assert m.open_questions
    assert m.blocking_open_questions == []


def test_approximate_window_question_is_not_blocking():
    m = compile_offline("Don't spend more than CHF 200 a month.")
    assert any("calendar month" in q for q in m.open_questions)
    assert m.blocking_open_questions == []


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
        ("Spend at or below CHF 20.", Operator.LTE, 20),
        ("Spend at or under CHF 20.", Operator.LTE, 20),
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


@pytest.mark.parametrize(
    "text,operator,admits_the_boundary",
    [
        # Inclusive phrasings: a purchase priced EXACTLY at the limit is allowed.
        ("Keep each order at or below CHF 120.", Operator.LTE, True),
        ("Keep each order at or under CHF 120.", Operator.LTE, True),
        ("Keep each order to CHF 120 or less.", Operator.LTE, True),
        ("Keep each order to no more than CHF 120.", Operator.LTE, True),
        ("Keep each order to at most CHF 120.", Operator.LTE, True),
        # Strict phrasings: a purchase priced EXACTLY at the limit is refused.
        ("Keep each order below CHF 120.", Operator.LT, False),
        ("Keep each order under CHF 120.", Operator.LT, False),
        ("Keep each order less than CHF 120.", Operator.LT, False),
    ],
)
def test_boundary_value_in_both_directions(text, operator, admits_the_boundary):
    """Regression for SCEN0001 / AU0003.

    AU0003 is priced at exactly CHF 120.00 under a 'at or below CHF 120' mandate
    and was being declined, because 'at or below' was read as the bare 'below'.
    An off-by-one here refuses a purchase the customer authorised in plain words,
    so both directions are pinned: inclusive admits the boundary, strict does not.
    """
    m = compile_offline(text)
    assert len(m.hard_rules) == 1, f"{text!r} produced {m.hard_rules}"
    rule = m.hard_rules[0]
    assert rule.operator is operator, f"{text!r} -> {rule.operator}"
    assert rule.value == 120

    exactly_at_limit = 120.00
    allowed = (
        exactly_at_limit <= rule.value
        if rule.operator is Operator.LTE
        else exactly_at_limit < rule.value
    )
    assert allowed is admits_the_boundary


def test_scen0001_both_limits_are_inclusive():
    """The real SCEN0001 instruction: both caps use 'at or below'."""
    m = compile_offline(
        "Order our household groceries for delivery. Keep each order at or below "
        "CHF 120 including delivery, and keep the total across any seven days at "
        "or below CHF 300. Ask me when uncertain."
    )
    assert len(m.hard_rules) == 2, m.hard_rules
    assert all(r.operator is Operator.LTE for r in m.hard_rules), [
        r.describe() for r in m.hard_rules
    ]
    assert sorted(r.value for r in m.hard_rules) == [120, 300]


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


def test_scen0001_seven_day_total_is_a_rolling_rule():
    """Regression for SCEN0001: the weekly budget must not be a 2nd per-order cap.

    'keep the total across any seven days at or below CHF 300' states its window
    in words, and BEFORE the amount. Both of those defeated the original matcher,
    so the rule compiled to scope=purchase and never fired - every single order
    in the scenario is under CHF 300 on its own.
    """
    m = compile_offline(
        "Order our household groceries for delivery. Keep each order at or below "
        "CHF 120 including delivery, and keep the total across any seven days at "
        "or below CHF 300. Ask me when uncertain."
    )
    assert len(m.hard_rules) == 2, [r.describe() for r in m.hard_rules]

    per_order = [r for r in m.hard_rules if r.scope is Scope.PURCHASE]
    rolling = [r for r in m.hard_rules if r.scope is Scope.PERIOD]

    assert len(per_order) == 1 and len(rolling) == 1, [r.describe() for r in m.hard_rules]

    assert per_order[0].field == "authorization.billing_amount_chf"
    assert per_order[0].value == 120
    assert per_order[0].period_days is None

    assert rolling[0].field == "rolling.billing_amount_chf"
    assert rolling[0].value == 300
    assert rolling[0].period_days == 7
    assert rolling[0].operator is Operator.LTE


@pytest.mark.parametrize(
    "text,expect_period_days",
    [
        ("Keep the total across any seven days at or below CHF 300.", 7),
        ("Keep the total across any 7 days under CHF 300.", 7),
        ("Keep the combined total over any three days below CHF 100.", 3),
        ("Spend no more than CHF 200 a month in total.", 30),
    ],
)
def test_aggregate_windows_stated_before_the_amount(text, expect_period_days):
    m = compile_offline(text)
    rolling = [r for r in m.hard_rules if r.scope is Scope.PERIOD]
    assert len(rolling) == 1, [r.describe() for r in m.hard_rules]
    assert rolling[0].period_days == expect_period_days


@pytest.mark.parametrize(
    "text",
    [
        # A frequency stated BEFORE a price, with no aggregate word, is not a
        # budget for the period - it is the price of each item.
        "Buy lunch every day for up to CHF 25.",
        "Order a coffee each morning for no more than CHF 6.",
        "Spend up to CHF 20.",
    ],
)
def test_frequency_near_a_price_does_not_become_a_rolling_cap(text):
    """Guard against the backwards window search over-firing.

    Note the contrast with `test_explicit_per_period_price_is_still_rolling`:
    "CHF 6 a day" (window AFTER the amount) genuinely states a daily budget and
    SHOULD be rolling. Only a window sitting before an unmarked price is
    ambiguous, and that is the case this guards.
    """
    m = compile_offline(text)
    for rule in m.hard_rules:
        if rule.field == "rolling.billing_amount_chf":
            raise AssertionError(
                f"{text!r} wrongly compiled a rolling spend cap: {rule.describe()}"
            )


@pytest.mark.parametrize(
    "text,expect_period_days",
    [
        ("Order a coffee each morning for no more than CHF 6 a day.", 1),
        ("Don't spend more than CHF 200 a month on groceries.", 30),
    ],
)
def test_explicit_per_period_price_is_still_rolling(text, expect_period_days):
    """A window stated AFTER the amount is an explicit budget for that window."""
    m = compile_offline(text)
    rolling = [r for r in m.hard_rules if r.scope is Scope.PERIOD]
    assert len(rolling) == 1, [r.describe() for r in m.hard_rules]
    assert rolling[0].period_days == expect_period_days


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


@pytest.mark.parametrize("fx", FIXTURES, ids=[f["id"] for f in FIXTURES])
def test_guidance_is_never_garbled(fx):
    """Regression for Bug 4.

    Guidance was built by cutting the matched span out of the sentence, which
    left the connectives that pointed at it dangling: "Keep the total across",
    "Keep each order at or including delivery", a bare "Only". None of that
    should ever reach a customer.
    """
    m = compile_offline(fx["instruction"])
    dangling = {
        "across", "over", "within", "during", "at", "or", "and", "but", "of",
        "to", "for", "in", "on", "with", "by", "than", "up", "per",
    }
    for g in m.guidance:
        words = g.strip().casefold().split()
        assert words, "empty guidance line"
        assert words[-1] not in dangling, f"guidance ends on a dangling word: {g!r}"
        assert g.strip().casefold() not in {"only", "just", "per order"}, (
            f"guidance is pure scaffolding: {g!r}"
        )


@pytest.mark.parametrize(
    "instruction,not_wanted",
    [
        (
            "Order our household groceries for delivery. Keep each order at or "
            "below CHF 120 including delivery, and keep the total across any "
            "seven days at or below CHF 300. Ask me when uncertain.",
            ["keep the total across", "at or including"],
        ),
        (
            "Replace my worn road-running shoes in size 43. Buy only from a "
            "specialist sports retailer, and pay no more than CHF 200.",
            ["only"],
        ),
    ],
)
def test_specific_garbled_strings_are_gone(instruction, not_wanted):
    m = compile_offline(instruction)
    lowered = [g.strip().casefold() for g in m.guidance]
    for bad in not_wanted:
        assert bad not in lowered, f"{bad!r} still present in {m.guidance}"


def test_rule_summaries_render_from_the_structured_rule():
    """Customer-facing rule text comes from a template, not the sentence."""
    m = compile_offline(
        "Order our household groceries for delivery. Keep each order at or below "
        "CHF 120 including delivery, and keep the total across any seven days at "
        "or below CHF 300. Ask me when uncertain."
    )
    assert m.rule_summaries == [
        "Each purchase must be no more than CHF 120.00.",
        "Total spending over any 7 days must be no more than CHF 300.00.",
    ]


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
