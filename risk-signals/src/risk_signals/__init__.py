"""
risk_signals - decision-neutral risk evidence for the Viseca control layer.

A prototype hand-off module for Function 2 (the decision engine). It computes
EVIDENCE about an authorization; it never decides anything, never calls an API,
and never mutates state.

    from risk_signals import Authorization, PriorAuthorization, detect_duplicate

    signal = detect_duplicate(current_event, context_recent_authorizations)
    if signal.fired:
        print(signal.flag, signal.score, signal.reasons)

Every detector returns a `Signal`. Read `Signal.confidence_tier` before acting:

    high   - structured fields only; safe to act on directly
    medium - inferred from structured fields; corroborate first
    low    - heuristic over untrusted text; open_question ONLY, never a decision

Zero runtime dependencies. Copy `src/risk_signals/` anywhere and import it.
"""

from .duplicates import (
    DEFAULT_CONFIG,
    FLAG_LIKELY_DUPLICATE,
    FLAG_LIKELY_RETRY,
    FLAG_POSSIBLE_DUPLICATE,
    DuplicateConfig,
    amount_similarity,
    candidate_window,
    detect_duplicate,
    jaccard_similarity,
    weighted_jaccard,
)
from .signals import (
    check_attempt_velocity,
    check_card_status,
    check_fulfillment_terms,
    check_merchant_legitimacy,
    detect_lookalike_merchant,
    levenshtein,
    name_similarity,
)
from .text_heuristics import extract_return_window_days, scan_manipulated_text
from .types import (
    FLAG_NONE,
    TIER_HIGH,
    TIER_LOW,
    TIER_MEDIUM,
    Authorization,
    FamiliarMerchant,
    Item,
    Merchant,
    PriorAuthorization,
    Signal,
    make_signal,
    parse_timestamp,
)

__all__ = [
    # types
    "Authorization",
    "PriorAuthorization",
    "Item",
    "Merchant",
    "FamiliarMerchant",
    "Signal",
    "make_signal",
    "parse_timestamp",
    # duplicate detection
    "detect_duplicate",
    "candidate_window",
    "jaccard_similarity",
    "weighted_jaccard",
    "amount_similarity",
    "DuplicateConfig",
    "DEFAULT_CONFIG",
    "FLAG_LIKELY_DUPLICATE",
    "FLAG_POSSIBLE_DUPLICATE",
    "FLAG_LIKELY_RETRY",
    # other signals
    "check_card_status",
    "check_attempt_velocity",
    "check_fulfillment_terms",
    "check_merchant_legitimacy",
    "detect_lookalike_merchant",
    "levenshtein",
    "name_similarity",
    # untrusted-text heuristics (LOW confidence)
    "scan_manipulated_text",
    "extract_return_window_days",
    # tiers
    "TIER_HIGH",
    "TIER_MEDIUM",
    "TIER_LOW",
    "FLAG_NONE",
]

__version__ = "0.1.0"
