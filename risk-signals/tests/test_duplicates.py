"""
Duplicate detector tests, driven by real Viseca rows.

The headline case is AU0035 / AU0036: the same order submitted twice, 25 minutes
apart, with `related_authorization_id` empty on both so there is no link to
follow.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from conftest import auth, merchant, prior, shifted

from risk_signals import (
    FLAG_LIKELY_DUPLICATE,
    FLAG_LIKELY_RETRY,
    FLAG_NONE,
    FLAG_POSSIBLE_DUPLICATE,
    DuplicateConfig,
    Item,
    PriorAuthorization,
    amount_similarity,
    candidate_window,
    detect_duplicate,
    jaccard_similarity,
    weighted_jaccard,
)

# ---------------------------------------------------------------------------
# The data really is what the blueprint says it is
# ---------------------------------------------------------------------------

def test_au0035_au0036_are_an_unlinked_near_duplicate_in_the_real_data():
    a, b = auth("AU0035"), auth("AU0036")

    assert a.card_id == b.card_id == "CA0039"
    assert a.merchant.merchant_id == b.merchant.merchant_id == "ME0022"
    assert a.billing_amount_chf == b.billing_amount_chf == 289.00
    assert a.currency == b.currency == "CHF"
    assert [i.item_id for i in a.items] == [i.item_id for i in b.items] == ["IT0017"]
    assert a.items[0].item_details == b.items[0].item_details

    gap = (b.timestamp - a.timestamp).total_seconds() / 60
    assert gap == 25.0

    # The whole reason this detector has to exist.
    assert a.related_authorization_id is None
    assert b.related_authorization_id is None

    # And Viseca's own velocity counter cannot see it either: 10-minute window,
    # 25-minute gap.
    assert b.recent_attempt_count_10m == 0

    # ME0022 is not recurring-capable, so no dampening applies.
    assert a.merchant.recurring_capable is False


# ---------------------------------------------------------------------------
# The positive case
# ---------------------------------------------------------------------------

def test_au0036_is_flagged_as_a_duplicate_of_au0035():
    signal = detect_duplicate(auth("AU0036"), [prior("AU0035")])

    assert signal.flag == FLAG_LIKELY_DUPLICATE
    assert signal.matched_against_authorization_id == "AU0035"
    assert signal.score >= 0.90
    assert signal.time_gap_minutes == 25.0
    assert signal.fired is True

    for expected in ("merchant_id", "billing_amount_chf", "currency", "item_ids"):
        assert expected in signal.matched_fields, f"missing {expected}"

    assert signal.confidence_tier == "high"
    assert any("identical" in r.lower() for r in signal.reasons)


def test_detection_is_symmetric_in_substance():
    """Order of arrival doesn't change that these two are the same order."""
    forward = detect_duplicate(auth("AU0036"), [prior("AU0035")])
    # Reverse: pretend AU0035 arrived second.
    reversed_current = shifted(auth("AU0035"), 50, "AU0035_LATE")
    backward = detect_duplicate(reversed_current, [prior("AU0036")])

    assert forward.flag == FLAG_LIKELY_DUPLICATE
    assert backward.flag == FLAG_LIKELY_DUPLICATE


def test_output_is_structured_evidence_not_a_decision():
    signal = detect_duplicate(auth("AU0036"), [prior("AU0035")])
    payload = signal.to_dict()

    assert set(payload) >= {
        "flag",
        "matched_against_authorization_id",
        "score",
        "matched_fields",
        "time_gap_minutes",
        "reasons",
    }
    # Nothing in the output may look like a decision.
    assert "decision" not in payload
    assert payload["flag"] not in ("approve", "decline", "step_up")


def test_a_clean_result_still_explains_itself():
    """No match must not mean silence."""
    signal = detect_duplicate(auth("AU0035"), [])
    assert signal.flag == FLAG_NONE
    assert signal.fired is False
    assert signal.reasons, "a 'none' result must still say what was checked"


# ---------------------------------------------------------------------------
# Degradation when the prior has no cart lines
# ---------------------------------------------------------------------------

def test_still_detected_from_context_recent_authorizations_shape_but_lower():
    """Viseca's context.recent_authorizations has no items and no currency.

    The detector must still work, score lower, and say why.
    """
    with_cart = detect_duplicate(auth("AU0036"), [prior("AU0035")])
    without_cart = detect_duplicate(auth("AU0036"), [prior("AU0035", with_items=False)])

    assert without_cart.matched_against_authorization_id == "AU0035"
    assert without_cart.fired is True
    assert without_cart.score < with_cart.score
    assert without_cart.confidence_tier == "medium"
    assert any("unavailable" in r.lower() for r in without_cart.reasons)
    assert "item_ids" not in without_cart.matched_fields


# ---------------------------------------------------------------------------
# Negative case 1 - REAL rows
# ---------------------------------------------------------------------------

def test_real_negative_au0013_is_not_a_duplicate_of_au0012():
    """AU0012 and AU0013: same card, same merchant, same item_id, same day.

    Two different shoes being considered - CHF 165.00 vs CHF 155.00, 6.1% apart.
    A wide window is used so this tests the AMOUNT gate rather than just falling
    out of the time window.
    """
    a, b = auth("AU0012"), auth("AU0013")
    assert a.merchant.merchant_id == b.merchant.merchant_id == "ME0028"
    assert [i.item_id for i in a.items] == [i.item_id for i in b.items] == ["IT0014"]
    relative = abs(a.billing_amount_chf - b.billing_amount_chf) / a.billing_amount_chf
    assert relative > 0.02, "this case only works if the prices differ beyond tolerance"

    wide = DuplicateConfig(lookback_minutes=600)
    signal = detect_duplicate(b, [prior("AU0012")], wide)

    assert signal.flag == FLAG_NONE
    assert signal.fired is False


def test_real_negative_also_rejected_by_the_default_time_window():
    signal = detect_duplicate(auth("AU0013"), [prior("AU0012")])
    assert signal.flag == FLAG_NONE
    assert "lookback window" in " ".join(signal.reasons)


# ---------------------------------------------------------------------------
# Negative case 2 - CONSTRUCTED (no real close-in-time recurring pair exists)
# ---------------------------------------------------------------------------

def _recurring_pair():
    """Two identical subscription charges 25 minutes apart.

    CONSTRUCTED, and labelled as such: real recurring charges in
    authorization_history.csv are ~30 days apart, so no genuine close-in-time
    pair exists to test the dampener with. Merchant ME0018 (NorthTone Digital,
    recurring_capable=true) and item IT0037 are both real; only the timing is
    synthetic. Deliberately identical in structure to AU0035/AU0036 so the ONLY
    difference is recurring_capable.
    """
    base = auth("AU0035")
    sub_item = Item(
        item_id="IT0037",
        quantity=1,
        item_name="Monthly media subscription",
        item_category="subscriptions",
        unit_price=10.95,
        currency="CHF",
        item_details="One month of a digital media streaming subscription",
    )
    first = replace(
        base,
        authorization_id="SYNTH_SUB_1",
        merchant=merchant("ME0018"),
        amount=10.95,
        billing_amount_chf=10.95,
        items=(sub_item,),
        purchase_description="Monthly media subscription",
    )
    second = shifted(first, 25, "SYNTH_SUB_2")
    return first, second


def test_constructed_negative_recurring_merchant_scores_visibly_lower():
    first, second = _recurring_pair()
    assert first.merchant.recurring_capable is True

    recurring_signal = detect_duplicate(
        second,
        [
            PriorAuthorization(
                authorization_id=first.authorization_id,
                timestamp=first.timestamp,
                merchant_id=first.merchant.merchant_id,
                billing_amount_chf=first.billing_amount_chf,
                status="approved",
                items=first.items,
                currency=first.currency,
                card_id=first.card_id,
            )
        ],
    )
    duplicate_signal = detect_duplicate(auth("AU0036"), [prior("AU0035")])

    # Structurally identical match, but suspicion is damped.
    assert recurring_signal.score < duplicate_signal.score
    assert recurring_signal.flag != FLAG_LIKELY_DUPLICATE
    assert recurring_signal.flag == FLAG_POSSIBLE_DUPLICATE
    assert any("recurring_capable" in r for r in recurring_signal.reasons)

    # "Visibly lower" - at least a 0.25 gap, not a rounding difference.
    assert duplicate_signal.score - recurring_signal.score > 0.25


# ---------------------------------------------------------------------------
# Retry vs duplicate - a re-attempt of a DECLINED order is not a double charge
# ---------------------------------------------------------------------------

def test_reattempt_of_a_declined_authorization_is_a_retry_not_a_duplicate():
    """AU0042 really does carry related_authorization_id = AU0037, declined.

    Even without that link, a near-match against a declined prior must not be
    reported as a duplicate charge - nobody was billed twice.
    """
    a42 = auth("AU0042")
    assert a42.related_authorization_id == "AU0037"
    assert a42.related_authorization_status == "declined"

    wide = DuplicateConfig(lookback_minutes=60 * 24 * 7, amount_tolerance=0.60)
    signal = detect_duplicate(a42, [prior("AU0037", status="declined")], wide)

    assert signal.flag in (FLAG_LIKELY_RETRY, FLAG_NONE)
    assert signal.flag != FLAG_LIKELY_DUPLICATE
    if signal.fired:
        assert any("re-attempt" in r for r in signal.reasons)


# ---------------------------------------------------------------------------
# Stage-level unit tests
# ---------------------------------------------------------------------------

def test_candidate_window_excludes_self_future_and_out_of_window():
    current = auth("AU0036")
    assert candidate_window(current, [prior("AU0036")]) == []          # self
    assert candidate_window(current, [prior("AU0035")]) != []          # 25 min
    tight = DuplicateConfig(lookback_minutes=10)
    assert candidate_window(current, [prior("AU0035")], tight) == []   # too old


def test_candidate_window_excludes_other_cards():
    current = auth("AU0036")  # CA0039
    other_card = prior("AU0012")  # CA0011
    assert candidate_window(current, [other_card]) == []


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ({"x"}, {"x"}, 1.0),
        ({"x"}, {"y"}, 0.0),
        ({"x", "y"}, {"x"}, 0.5),
        (set(), set(), 1.0),
    ],
)
def test_jaccard(a, b, expected):
    assert jaccard_similarity(a, b) == pytest.approx(expected)


def test_weighted_jaccard_counts_quantities():
    one = (Item(item_id="IT0017", quantity=1),)
    three = (Item(item_id="IT0017", quantity=3),)
    assert weighted_jaccard(one, one) == pytest.approx(1.0)
    # Presence-only Jaccard would call these identical; quantity-aware does not.
    assert weighted_jaccard(one, three) == pytest.approx(1 / 3)


def test_weighted_jaccard_handles_an_added_line():
    base = (Item(item_id="IT0017", quantity=1),)
    with_addon = (Item(item_id="IT0017", quantity=1), Item(item_id="IT0066", quantity=1))
    assert weighted_jaccard(base, with_addon) == pytest.approx(0.5)


@pytest.mark.parametrize(
    "a,b,tolerance,expected",
    [
        (289.0, 289.0, 0.02, 1.0),
        (100.0, 101.0, 0.02, pytest.approx(1.0 - 0.3 * (0.00990099 / 0.02), rel=1e-3)),
        (165.0, 155.0, 0.02, None),
    ],
)
def test_amount_similarity(a, b, tolerance, expected):
    result = amount_similarity(a, b, tolerance)
    if expected is None:
        assert result is None
    else:
        assert result == expected


def test_detector_is_pure_and_repeatable():
    current, priors = auth("AU0036"), [prior("AU0035")]
    first = detect_duplicate(current, priors).to_dict()
    second = detect_duplicate(current, priors).to_dict()
    assert first == second
    assert len(priors) == 1, "input list must not be mutated"
