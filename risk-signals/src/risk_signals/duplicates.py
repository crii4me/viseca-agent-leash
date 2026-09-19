"""
Duplicate / near-duplicate authorization detection.

Why this exists: AU0035 and AU0036 are the same real order submitted twice -
same card, same merchant, same CHF 289.00, identical cart line, 25 minutes
apart - and Viseca's `related_authorization_id` is EMPTY on both rows. There is
no structured field saying "this is a duplicate of that", so it has to be
inferred. Viseca's own `recent_attempt_count_10m` misses it too: that counter
looks back 10 minutes and the gap is 25.

Four stages, cheapest first, per the blueprint:

  1. Candidate window  - same card, earlier, inside the lookback window.
  2. Structured match  - merchant_id exact, amount within tolerance, currency.
  3. Item-set overlap  - quantity-aware Jaccard over cart lines.
  4. Time weighting    - a modifier on confidence, never a gate.

Everything here is pure. `detect_duplicate` reads no clock: "now" is the
current authorization's own timestamp.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .types import (
    FLAG_NONE,
    TIER_HIGH,
    TIER_MEDIUM,
    Authorization,
    Item,
    PriorAuthorization,
    Signal,
    make_signal,
)

FLAG_LIKELY_DUPLICATE = "likely_duplicate"
FLAG_POSSIBLE_DUPLICATE = "possible_duplicate"
FLAG_LIKELY_RETRY = "likely_retry"


@dataclass(frozen=True)
class DuplicateConfig:
    """Tuning knobs. Every threshold in the module is here, none are inline."""

    # Stage 1
    lookback_minutes: float = 60.0

    # Stage 2 - relative tolerance, so 0.02 means "within 2% of each other".
    # Non-zero so a re-quote with a slightly different fee still matches.
    amount_tolerance: float = 0.02

    # Stage 3 - weights. Merchant is binary and always 1.0 here (it is a gate in
    # stage 2), so it acts as a floor on the score of anything that gets this far.
    weight_merchant: float = 0.35
    weight_amount: float = 0.35
    weight_items: float = 0.30

    # Applied when the prior has no cart lines - i.e. it came from
    # `context.recent_authorizations`, which carries no items. We cap rather than
    # assume: less evidence must mean less confidence, not the same confidence.
    items_unavailable_factor: float = 0.85

    # Stage 4 - time is a modifier, not a gate, so the penalty is deliberately
    # small. Quadratic: confidence decays slowly for tight gaps and faster
    # toward the edge of the window.
    max_time_penalty: float = 0.15

    # A recurring-capable merchant (subscriptions, memberships) is expected to
    # bill the same amount repeatedly. That should lower suspicion, not raise it.
    recurring_damping: float = 0.35

    # A near-match against a DECLINED or CANCELLED prior is a retry, not a double
    # charge - nobody was billed twice. Real example: AU0042 re-quoting the
    # declined AU0037. Reported, but weighted well down.
    retry_damping: float = 0.50

    # Flag thresholds.
    likely_threshold: float = 0.85
    possible_threshold: float = 0.60


DEFAULT_CONFIG = DuplicateConfig()

_RETRY_STATUSES = frozenset({"declined", "cancelled"})


# ---------------------------------------------------------------------------
# Stage 3 helpers - pure set maths
# ---------------------------------------------------------------------------

def jaccard_similarity(a: set[str], b: set[str]) -> float:
    """|A n B| / |A u B|. Two empty sets are defined as 1.0 (identical)."""
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


def weighted_jaccard(items_a: tuple[Item, ...], items_b: tuple[Item, ...]) -> float:
    """Quantity-aware Jaccard over cart lines.

    sum(min(qty)) / sum(max(qty)) across the union of item_ids. Presence-only
    Jaccard would score "1 monitor" and "3 monitors" as identical carts, which
    is exactly the near-duplicate we would want to look at more closely.
    """
    count_a: Counter[str] = Counter()
    count_b: Counter[str] = Counter()
    for it in items_a:
        count_a[it.item_id] += max(1, int(it.quantity))
    for it in items_b:
        count_b[it.item_id] += max(1, int(it.quantity))

    if not count_a and not count_b:
        return 1.0

    keys = set(count_a) | set(count_b)
    numerator = sum(min(count_a.get(k, 0), count_b.get(k, 0)) for k in keys)
    denominator = sum(max(count_a.get(k, 0), count_b.get(k, 0)) for k in keys)
    return numerator / denominator if denominator else 1.0


def amount_similarity(a: float, b: float, tolerance: float) -> float | None:
    """1.0 for an exact match, decaying to 0.7 at the tolerance edge.

    Returns None when the two amounts are further apart than the tolerance -
    meaning "not a candidate", which is different from "a weak match".
    """
    if a <= 0 and b <= 0:
        return 1.0
    scale = max(abs(a), abs(b))
    if scale == 0:
        return 1.0
    relative_difference = abs(a - b) / scale
    if relative_difference > tolerance:
        return None
    if tolerance <= 0:
        return 1.0
    return 1.0 - 0.3 * (relative_difference / tolerance)


# ---------------------------------------------------------------------------
# Stage 1
# ---------------------------------------------------------------------------

def candidate_window(
    current: Authorization,
    priors: list[PriorAuthorization] | tuple[PriorAuthorization, ...],
    config: DuplicateConfig = DEFAULT_CONFIG,
) -> list[PriorAuthorization]:
    """Priors on the same card, strictly earlier, inside the lookback window.

    Exposed separately so Function 2 can reuse the blocking step, and so the
    window can be tested on its own.
    """
    out: list[PriorAuthorization] = []
    for prior in priors:
        if prior.authorization_id == current.authorization_id:
            continue
        if prior.card_id is not None and prior.card_id != current.card_id:
            continue
        gap = (current.timestamp - prior.timestamp).total_seconds() / 60.0
        if gap <= 0:
            continue  # same instant or in the future - not a prior
        if gap > config.lookback_minutes:
            continue
        out.append(prior)
    return out


# ---------------------------------------------------------------------------
# Stages 2-4, scored
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Match:
    prior: PriorAuthorization
    score: float
    matched_fields: tuple[str, ...]
    reasons: tuple[str, ...]
    time_gap_minutes: float
    item_score: float | None
    amount_score: float
    is_retry: bool


def _score_candidate(
    current: Authorization,
    prior: PriorAuthorization,
    config: DuplicateConfig,
) -> _Match | None:
    """Score one candidate, or None if it fails a stage-2 gate."""
    matched: list[str] = []
    reasons: list[str] = []

    # --- Stage 2: merchant (cheapest, most decisive) ----------------------
    if prior.merchant_id != current.merchant.merchant_id:
        return None
    matched.append("merchant_id")

    # --- Stage 2: amount ---------------------------------------------------
    amount_score = amount_similarity(
        current.billing_amount_chf, prior.billing_amount_chf, config.amount_tolerance
    )
    if amount_score is None:
        return None
    if current.billing_amount_chf == prior.billing_amount_chf:
        matched.append("billing_amount_chf")
        reasons.append(
            f"Identical billing amount (CHF {current.billing_amount_chf:.2f})."
        )
    else:
        matched.append("billing_amount_chf~")
        difference = abs(current.billing_amount_chf - prior.billing_amount_chf)
        reasons.append(
            f"Billing amounts within tolerance: CHF {current.billing_amount_chf:.2f} "
            f"vs CHF {prior.billing_amount_chf:.2f} (CHF {difference:.2f} apart)."
        )

    # --- Stage 2: currency -------------------------------------------------
    if prior.currency is None:
        reasons.append(
            "Currency not comparable: context.recent_authorizations carries no "
            "currency field. Compared CHF-billed amounts instead."
        )
    elif prior.currency != current.currency:
        return None
    else:
        matched.append("currency")

    # --- Stage 3: item set -------------------------------------------------
    if prior.items is None:
        item_score = None
        reasons.append(
            "Cart comparison unavailable: the prior authorization carries no item "
            "lines (context.recent_authorizations has no `items` field), so "
            "confidence is capped. Feeding this detector from Function 2's own "
            "ledger would resolve it."
        )
    else:
        item_score = weighted_jaccard(current.items, prior.items)
        current_ids = {i.item_id for i in current.items}
        prior_ids = {i.item_id for i in prior.items}
        set_score = jaccard_similarity(current_ids, prior_ids)
        if item_score >= 1.0:
            matched.extend(["item_ids", "item_quantities"])
            reasons.append(
                "Cart lines are identical, including quantities "
                f"({', '.join(sorted(current_ids)) or 'empty cart'})."
            )
        elif set_score > 0:
            matched.append("item_ids~")
            shared = sorted(current_ids & prior_ids)
            reasons.append(
                f"Carts overlap but are not identical (quantity-aware Jaccard "
                f"{item_score:.2f}); shared items: {', '.join(shared)}."
            )
        else:
            reasons.append("Carts share no items.")

    # --- Base score --------------------------------------------------------
    if item_score is None:
        available_weight = config.weight_merchant + config.weight_amount
        base = (
            config.weight_merchant * 1.0 + config.weight_amount * amount_score
        ) / available_weight
        base *= config.items_unavailable_factor
    else:
        base = (
            config.weight_merchant * 1.0
            + config.weight_amount * amount_score
            + config.weight_items * item_score
        )

    # --- Stage 4: time as a modifier --------------------------------------
    gap_minutes = (current.timestamp - prior.timestamp).total_seconds() / 60.0
    window_fraction = (
        gap_minutes / config.lookback_minutes if config.lookback_minutes > 0 else 0.0
    )
    time_multiplier = 1.0 - config.max_time_penalty * (window_fraction**2)
    score = base * time_multiplier
    reasons.append(
        f"Submitted {gap_minutes:.0f} minutes apart, inside the "
        f"{config.lookback_minutes:.0f}-minute lookback window."
    )

    # --- Dampeners ---------------------------------------------------------
    is_retry = prior.status in _RETRY_STATUSES
    if is_retry:
        score *= 1.0 - config.retry_damping
        reasons.append(
            f"The earlier authorization was {prior.status}, so this is a "
            f"re-attempt rather than a second charge - nobody was billed twice."
        )
    elif current.merchant.recurring_capable:
        score *= 1.0 - config.recurring_damping
        reasons.append(
            "Merchant is flagged recurring_capable, where repeat charges of the "
            "same amount are expected, so suspicion is reduced."
        )

    return _Match(
        prior=prior,
        score=max(0.0, min(1.0, score)),
        matched_fields=tuple(matched),
        reasons=tuple(reasons),
        time_gap_minutes=round(gap_minutes, 1),
        item_score=item_score,
        amount_score=amount_score,
        is_retry=is_retry,
    )


def detect_duplicate(
    current: Authorization,
    priors: list[PriorAuthorization] | tuple[PriorAuthorization, ...],
    config: DuplicateConfig = DEFAULT_CONFIG,
) -> Signal:
    """Is this authorization a resubmission of a recent one on the same card?

    Args:
        current: the authorization being evaluated.
        priors: recent authorizations - from `context.recent_authorizations`,
            or richer rows from Function 2's own ledger.
        config: thresholds; defaults are a 60-minute window and 2% tolerance.

    Returns:
        A `Signal`. Never a bare bool, and never a decision. `flag` is one of
        `likely_duplicate`, `possible_duplicate`, `likely_retry`, or `none`.
        A `none` result still explains what was checked.
    """
    candidates = candidate_window(current, priors, config)

    if not candidates:
        return make_signal(
            name="duplicate_purchase",
            flag=FLAG_NONE,
            score=0.0,
            tier=TIER_HIGH,
            reasons=[
                f"No earlier authorization on card {current.card_id} within the "
                f"{config.lookback_minutes:.0f}-minute lookback window."
            ],
            evidence={"candidates_considered": 0},
        )

    scored = [m for m in (_score_candidate(current, c, config) for c in candidates) if m]

    if not scored:
        return make_signal(
            name="duplicate_purchase",
            flag=FLAG_NONE,
            score=0.0,
            tier=TIER_HIGH,
            reasons=[
                f"{len(candidates)} recent authorization(s) on this card were "
                f"examined; none matched on merchant and amount."
            ],
            evidence={"candidates_considered": len(candidates)},
        )

    best = max(scored, key=lambda m: m.score)

    if best.is_retry:
        flag = FLAG_LIKELY_RETRY if best.score >= config.possible_threshold else FLAG_NONE
    elif best.score >= config.likely_threshold:
        flag = FLAG_LIKELY_DUPLICATE
    elif best.score >= config.possible_threshold:
        flag = FLAG_POSSIBLE_DUPLICATE
    else:
        flag = FLAG_NONE

    reasons = list(best.reasons)
    if flag == FLAG_NONE:
        reasons.append(
            f"Combined confidence {best.score:.2f} is below the "
            f"{config.possible_threshold:.2f} reporting threshold."
        )

    # Cart-level evidence is structured and trustworthy; without it we are
    # inferring from merchant and amount alone, so the tier drops.
    tier = TIER_HIGH if best.item_score is not None else TIER_MEDIUM

    return make_signal(
        name="duplicate_purchase",
        flag=flag,
        score=best.score,
        tier=tier,
        reasons=reasons,
        matched_against=best.prior.authorization_id,
        matched_fields=best.matched_fields,
        time_gap_minutes=best.time_gap_minutes,
        evidence={
            "candidates_considered": len(candidates),
            "candidates_matched": len(scored),
            "amount_score": round(best.amount_score, 4),
            "item_score": (
                round(best.item_score, 4) if best.item_score is not None else None
            ),
            "prior_status": best.prior.status,
            "merchant_recurring_capable": current.merchant.recurring_capable,
            "cart_detail_available": best.prior.has_cart_detail,
        },
    )
