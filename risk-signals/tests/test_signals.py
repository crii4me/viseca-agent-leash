"""The independent risk signals, tested against real Viseca rows."""

from __future__ import annotations

from dataclasses import replace

import pytest
from conftest import auth, familiar

from risk_signals import (
    FLAG_NONE,
    TIER_LOW,
    check_attempt_velocity,
    check_card_status,
    check_fulfillment_terms,
    check_merchant_legitimacy,
    detect_lookalike_merchant,
    extract_return_window_days,
    levenshtein,
    name_similarity,
    scan_manipulated_text,
)

# ---------------------------------------------------------------------------
# Card status
# ---------------------------------------------------------------------------

def test_active_card_produces_no_flag():
    signal = check_card_status(auth("AU0035"))
    assert signal.flag == FLAG_NONE
    assert signal.confidence_tier == "high"


def test_blocked_card_is_a_high_confidence_flag():
    blocked = replace(auth("AU0035"), card_status_at_attempt="blocked")
    signal = check_card_status(blocked)
    assert signal.flag == "card_not_active"
    assert signal.score == 1.0
    assert signal.confidence_tier == "high"


def test_revoked_authority_is_caught_too():
    revoked = replace(auth("AU0035"), authority_status="revoked")
    assert check_card_status(revoked).flag == "card_not_active"


# ---------------------------------------------------------------------------
# Attempt velocity
# ---------------------------------------------------------------------------

def test_real_rows_have_no_velocity_problem():
    signal = check_attempt_velocity(auth("AU0036"))
    assert signal.flag == FLAG_NONE
    assert auth("AU0036").recent_attempt_count_10m == 0


def test_velocity_thresholds():
    base = auth("AU0035")
    assert check_attempt_velocity(replace(base, recent_attempt_count_10m=4)).flag == (
        "elevated_attempt_rate"
    )
    assert check_attempt_velocity(replace(base, recent_attempt_count_10m=9)).flag == (
        "attempt_burst"
    )


def test_velocity_states_its_own_blind_spot():
    signal = check_attempt_velocity(auth("AU0036"))
    assert any("10-minute window" in r for r in signal.reasons)


# ---------------------------------------------------------------------------
# Fulfilment terms
# ---------------------------------------------------------------------------

def test_non_returnable_order_contradicts_a_returnable_requirement():
    """AU0014 is the real final-sale row: order_returnable == 'false'."""
    a = auth("AU0014")
    assert a.order_returnable == "false"
    signal = check_fulfillment_terms(a, require_returnable=True)
    assert signal.flag == "fulfillment_mismatch"
    assert signal.score >= 0.9


def test_unknown_return_term_is_unverifiable_not_violated():
    """AU0016 is the real 'return policy not stated' row."""
    a = auth("AU0016")
    assert a.order_returnable == "unknown"
    signal = check_fulfillment_terms(a, require_returnable=True)
    assert signal.flag == "fulfillment_unverifiable"
    assert signal.flag != "fulfillment_mismatch"
    assert any("not violated" in r for r in signal.reasons)


def test_returnable_order_satisfies_the_requirement():
    a = auth("AU0012")
    assert a.order_returnable == "true"
    assert check_fulfillment_terms(a, require_returnable=True).flag == FLAG_NONE


def test_a_return_window_in_days_cannot_be_verified_from_structured_data():
    """The AU0015 trap: order_returnable is true, but it carries no duration."""
    a = auth("AU0015")
    assert a.order_returnable == "true"
    signal = check_fulfillment_terms(a, require_returnable=True, min_return_days=14)
    assert signal.flag == "fulfillment_unverifiable"
    assert any("CANNOT be verified" in r for r in signal.reasons)


# ---------------------------------------------------------------------------
# Merchant legitimacy
# ---------------------------------------------------------------------------

def test_category_outside_the_mandate_is_flagged():
    signal = check_merchant_legitimacy(
        auth("AU0035"), expected_categories=("groceries",)
    )
    assert signal.flag == "merchant_category_mismatch"
    assert "electronics" in " ".join(signal.reasons)


def test_allowed_category_passes():
    signal = check_merchant_legitimacy(
        auth("AU0035"), expected_categories=("electronics", "software")
    )
    assert signal.flag == FLAG_NONE


def test_store_only_merchant_selling_online_is_a_channel_mismatch():
    a = auth("AU0035")
    store_only = replace(a, merchant=replace(a.merchant, availability="store"))
    assert check_merchant_legitimacy(store_only).flag == "merchant_channel_mismatch"


# ---------------------------------------------------------------------------
# Lookalike seller - the real PixelHarbor / PixelHarbour pair
# ---------------------------------------------------------------------------

def test_the_lookalike_pair_really_is_in_the_data():
    genuine = auth("AU0035").merchant
    impostor = auth("AU0039").merchant
    assert genuine.merchant_name == "PixelHarbor"
    assert impostor.merchant_name == "PixelHarbour"
    assert genuine.merchant_id != impostor.merchant_id
    assert levenshtein("pixelharbor", "pixelharbour") == 1


def test_known_merchant_is_not_flagged():
    signal = detect_lookalike_merchant(auth("AU0035"), familiar("CA0039"))
    assert signal.flag == FLAG_NONE
    assert dict(signal.evidence)["prior_approved_count"] == 6


def test_pixelharbour_is_flagged_as_a_lookalike():
    signal = detect_lookalike_merchant(auth("AU0039"), familiar("CA0039"))
    assert signal.flag == "lookalike_merchant"
    evidence = dict(signal.evidence)
    assert evidence["resembles_merchant_id"] == "ME0022"
    assert evidence["prior_approved_count"] == 0
    assert signal.score > 0.85


def test_an_unfamiliar_but_dissimilar_merchant_is_reported_more_gently():
    """AU0044 is Circuit and Pine - new to this card, but no name resemblance."""
    signal = detect_lookalike_merchant(auth("AU0044"), familiar("CA0039"))
    assert signal.flag == "unfamiliar_merchant"
    assert signal.score < 0.6
    assert any("not the same as fraudulent" in r for r in signal.reasons)


@pytest.mark.parametrize(
    "a,b,floor",
    [("PixelHarbor", "PixelHarbour", 0.9), ("PixelHarbor", "Circuit and Pine", 0.0)],
)
def test_name_similarity_behaves(a, b, floor):
    assert name_similarity(a, b) >= floor
    assert name_similarity(a, a) == 1.0


# ---------------------------------------------------------------------------
# Manipulated free text - LOW confidence, evidence only
# ---------------------------------------------------------------------------

def test_clean_product_copy_is_not_flagged():
    signal = scan_manipulated_text(auth("AU0035"))
    assert signal.flag == FLAG_NONE
    assert signal.confidence_tier == TIER_LOW


def test_au0037_fake_preauthorisation_claim_is_detected():
    signal = scan_manipulated_text(auth("AU0037"))
    assert signal.flag == "suspected_injected_instructions"
    categories = dict(signal.evidence)["categories"]
    assert "claims_authority_it_cannot_have" in categories
    assert "addresses_an_automated_agent" in categories


def test_au0040_ignore_previous_instructions_is_detected():
    signal = scan_manipulated_text(auth("AU0040"))
    assert signal.flag == "suspected_injected_instructions"
    categories = dict(signal.evidence)["categories"]
    assert "attempts_to_override_instructions" in categories


def test_injected_text_is_always_low_tier_and_never_certain():
    """The signal must never be strong enough to stand alone as a decision."""
    for auth_id in ("AU0037", "AU0040"):
        signal = scan_manipulated_text(auth(auth_id))
        assert signal.confidence_tier == TIER_LOW
        assert signal.score < 1.0
        assert any("Do NOT decline on it alone" in r for r in signal.reasons)


def test_au0040_is_legitimate_on_its_structured_facts():
    """The point of the whole trust model.

    AU0040 carries text demanding immediate approval. It is also CHF 299 from a
    merchant with 6 prior approved transactions, against a CHF 400 cap - fine on
    its own facts. The text signal is evidence; it must not be what decides.
    """
    a = auth("AU0040")
    assert a.billing_amount_chf == 299.00
    assert a.merchant.merchant_id == "ME0022"

    merchant_signal = detect_lookalike_merchant(a, familiar("CA0039"))
    assert merchant_signal.flag == FLAG_NONE  # known seller
    assert check_card_status(a).flag == FLAG_NONE
    assert check_attempt_velocity(a).flag == FLAG_NONE

    text_signal = scan_manipulated_text(a)
    assert text_signal.fired is True
    assert text_signal.confidence_tier == TIER_LOW  # cannot carry a decision


# ---------------------------------------------------------------------------
# Return-window extraction - evidence for the structured/untrusted conflict
# ---------------------------------------------------------------------------

def test_seven_day_window_is_extracted_from_au0015():
    a = auth("AU0015")
    assert "7 days" in a.items[0].item_details
    signal = extract_return_window_days(a)
    assert signal.flag == "return_window_extracted"
    assert dict(signal.evidence)["min_days_found"] == 7
    assert dict(signal.evidence)["structured_order_returnable"] == "true"
    assert signal.confidence_tier == TIER_LOW


def test_thirty_day_window_is_extracted_from_au0012():
    signal = extract_return_window_days(auth("AU0012"))
    assert dict(signal.evidence)["min_days_found"] == 30


def test_final_sale_reads_as_zero_days():
    signal = extract_return_window_days(auth("AU0014"))
    assert dict(signal.evidence)["min_days_found"] == 0
    assert any("final sale" in r.lower() for r in signal.reasons)


def test_unstated_policy_is_reported_without_inventing_a_number():
    signal = extract_return_window_days(auth("AU0016"))
    assert dict(signal.evidence)["min_days_found"] is None
    assert any("did not state" in r for r in signal.reasons)


def test_extraction_never_compares_against_the_mandate():
    """This module reports the fact; Function 2 decides what it means."""
    signal = extract_return_window_days(auth("AU0015"))
    assert any("does not compare" in r for r in signal.reasons)
    assert signal.flag != "decline"
