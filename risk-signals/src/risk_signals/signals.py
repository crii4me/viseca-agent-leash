"""
The other risk signals, one independent pure function each.

Function 2 calls whichever of these it wants, in whatever order it wants. There
is deliberately no orchestrator and no shared state: a monolithic `check_all()`
would force the engine to pay for checks it does not need, and would make the
reason trail harder to attribute.

Every function returns a `Signal` and never a decision. Read each one's
`confidence_tier` before acting:

  high   - structured fields only. Safe for Function 2 to act on directly.
  medium - inferred from structured fields. Corroborate before declining.
  low    - heuristic over untrusted text. Evidence for an open_question ONLY.
"""

from __future__ import annotations

from .types import (
    FLAG_NONE,
    TIER_HIGH,
    TIER_MEDIUM,
    Authorization,
    FamiliarMerchant,
    Signal,
    make_signal,
)

# ---------------------------------------------------------------------------
# Card status - cheapest and most certain, so check it first
# ---------------------------------------------------------------------------

def check_card_status(auth: Authorization) -> Signal:
    """Any card or authority status other than `active` is a hard stop.

    Near-certain and costs one string comparison, so run it before anything
    expensive. Mirrors the "hard stops first, cheaply" pattern in the
    decision-engine notes.
    """
    problems: list[str] = []
    if auth.card_status_at_attempt != "active":
        problems.append(f"card_status_at_attempt is '{auth.card_status_at_attempt}'")
    if auth.authority_status != "active":
        problems.append(f"authority_status is '{auth.authority_status}'")

    if not problems:
        return make_signal(
            name="card_status",
            flag=FLAG_NONE,
            score=0.0,
            tier=TIER_HIGH,
            reasons=["Card and authority are both active."],
            matched_fields=["card_status_at_attempt", "authority_status"],
        )

    return make_signal(
        name="card_status",
        flag="card_not_active",
        score=1.0,
        tier=TIER_HIGH,
        reasons=[
            "Authorization arrived against a card or mandate that is not active: "
            + "; ".join(problems)
            + ".",
            "This is a structured fact, not an inference.",
        ],
        matched_fields=["card_status_at_attempt", "authority_status"],
        evidence={
            "card_status_at_attempt": auth.card_status_at_attempt,
            "authority_status": auth.authority_status,
        },
    )


# ---------------------------------------------------------------------------
# Attempt velocity
# ---------------------------------------------------------------------------

def check_attempt_velocity(
    auth: Authorization,
    *,
    elevated_threshold: int = 3,
    burst_threshold: int = 6,
) -> Signal:
    """Burst of attempts in the last 10 minutes - retry storm or hijacked agent.

    Reads Viseca's own `recent_attempt_count_10m`. Note its blind spot: the
    window is 10 minutes, so a resubmission 25 minutes later (AU0035/AU0036)
    shows a count of 0 here. Velocity and duplicate detection catch different
    things; run both.
    """
    count = auth.recent_attempt_count_10m

    if count >= burst_threshold:
        flag, score = "attempt_burst", 0.9
        note = (
            f"{count} attempts in 10 minutes is a burst pattern - consistent with "
            f"a retry storm or an agent that has lost control of its loop."
        )
    elif count >= elevated_threshold:
        flag, score = "elevated_attempt_rate", 0.55
        note = f"{count} attempts in 10 minutes is above the normal rate."
    else:
        return make_signal(
            name="attempt_velocity",
            flag=FLAG_NONE,
            score=0.0,
            tier=TIER_MEDIUM,
            reasons=[
                f"{count} attempt(s) in the last 10 minutes; below the "
                f"{elevated_threshold}-attempt threshold.",
                "Note: this counter has a 10-minute window and will not see a "
                "resubmission that arrives later than that.",
            ],
            matched_fields=["recent_attempt_count_10m"],
            evidence={"recent_attempt_count_10m": count},
        )

    return make_signal(
        name="attempt_velocity",
        flag=flag,
        score=score,
        tier=TIER_MEDIUM,
        reasons=[note],
        matched_fields=["recent_attempt_count_10m"],
        evidence={"recent_attempt_count_10m": count},
    )


# ---------------------------------------------------------------------------
# Fulfillment terms
# ---------------------------------------------------------------------------

def check_fulfillment_terms(
    auth: Authorization,
    *,
    require_returnable: bool = False,
    require_cancellable: bool = False,
    min_return_days: int | None = None,
) -> Signal:
    """Compare structured order terms against what the mandate assumed.

    Only reads STRUCTURED fields: `order_returnable`, `order_cancellable`,
    `fulfillment_method`, `delivery_by`. Each is one of `true` / `false` /
    `unknown` / `not_applicable`.

    Important limitation, and the reason `min_return_days` only produces a
    warning: **`order_returnable` is a flag, not a duration.** It cannot express
    "returnable within 14 days". A mandate that asks for a return WINDOW cannot
    be verified from structured data at all - the number of days appears only in
    `item_details`, which is untrusted merchant free text. See
    `text_heuristics.extract_return_window_days`, which surfaces that as
    low-confidence evidence, deliberately kept separate from this function.
    """
    reasons: list[str] = []
    matched: list[str] = []
    flag = FLAG_NONE
    score = 0.0

    if require_returnable:
        matched.append("order_returnable")
        if auth.order_returnable == "false":
            flag, score = "fulfillment_mismatch", 0.95
            reasons.append(
                "The mandate requires a returnable order, and order_returnable "
                "is 'false'. This is a direct, structured contradiction."
            )
        elif auth.order_returnable == "unknown":
            flag, score = "fulfillment_unverifiable", 0.6
            reasons.append(
                "The mandate requires a returnable order, but order_returnable "
                "is 'unknown' - the term was not supplied. This is unverified, "
                "not violated; the two deserve different handling."
            )
        elif auth.order_returnable == "not_applicable":
            flag, score = "fulfillment_not_applicable", 0.5
            reasons.append(
                "order_returnable is 'not_applicable', which the data dictionary "
                "defines as the term not applying to this fulfilment type "
                f"(fulfillment_method='{auth.fulfillment_method}'). A digital "
                "delivery cannot satisfy a returnability requirement."
            )
        else:
            reasons.append("order_returnable is 'true', as the mandate requires.")

    if require_cancellable:
        matched.append("order_cancellable")
        if auth.order_cancellable == "false":
            flag, score = "fulfillment_mismatch", max(score, 0.95)
            reasons.append(
                "The mandate requires a cancellable order and order_cancellable "
                "is 'false'."
            )
        elif auth.order_cancellable in ("unknown", "not_applicable"):
            if flag == FLAG_NONE:
                flag, score = "fulfillment_unverifiable", max(score, 0.6)
            reasons.append(
                f"order_cancellable is '{auth.order_cancellable}', so the "
                f"cancellation requirement cannot be confirmed."
            )
        else:
            reasons.append("order_cancellable is 'true', as the mandate requires.")

    if min_return_days is not None:
        reasons.append(
            f"The mandate asks for a return window of at least {min_return_days} "
            f"days. No structured field carries a return duration - "
            f"order_returnable is only true/false/unknown/not_applicable. This "
            f"requirement CANNOT be verified from trustworthy data. Treat any "
            f"day-count found in item_details as low-confidence evidence for an "
            f"open_question, never as grounds to decline on its own."
        )
        if flag == FLAG_NONE:
            flag, score = "fulfillment_unverifiable", max(score, 0.5)

    if not reasons:
        reasons.append("No fulfilment expectations were supplied to check against.")

    return make_signal(
        name="fulfillment_terms",
        flag=flag,
        score=score,
        tier=TIER_HIGH,  # structured fields only; the caveats above are explicit
        reasons=reasons,
        matched_fields=matched,
        evidence={
            "order_returnable": auth.order_returnable,
            "order_cancellable": auth.order_cancellable,
            "fulfillment_method": auth.fulfillment_method,
            "delivery_by": auth.delivery_by,
        },
    )


# ---------------------------------------------------------------------------
# Merchant legitimacy - the cheap structural half of the lookalike problem
# ---------------------------------------------------------------------------

def check_merchant_legitimacy(
    auth: Authorization,
    *,
    expected_categories: tuple[str, ...] | list[str] | None = None,
) -> Signal:
    """Structural merchant checks. Run this before the name-similarity pass.

    Two cheap questions: does the merchant's `availability` support the channel
    this purchase came through, and is `merchant_category` one the mandate
    expected? Both are structured reference data.
    """
    reasons: list[str] = []
    matched: list[str] = []
    flag = FLAG_NONE
    score = 0.0

    availability = auth.merchant.availability
    channel = auth.channel
    if availability and channel:
        matched.append("merchant.availability")
        online_channel = channel in ("ecommerce", "online")
        if online_channel and availability == "store":
            flag, score = "merchant_channel_mismatch", 0.8
            reasons.append(
                f"Purchase arrived through '{channel}' but the merchant's "
                f"availability is 'store' - it is not set up to sell online."
            )
        elif availability == "atm" and not online_channel:
            reasons.append("ATM merchant with a non-online channel; consistent.")
        else:
            reasons.append(
                f"Merchant availability '{availability}' is consistent with "
                f"channel '{channel}'."
            )

    if expected_categories:
        matched.append("merchant.merchant_category")
        expected = {c.strip().lower() for c in expected_categories}
        actual = auth.merchant.merchant_category.strip().lower()
        if actual not in expected:
            flag = "merchant_category_mismatch"
            score = max(score, 0.9)
            reasons.append(
                f"Merchant category is '{auth.merchant.merchant_category}', which "
                f"is not among the categories the mandate allows "
                f"({', '.join(sorted(expected))})."
            )
        else:
            reasons.append(
                f"Merchant category '{auth.merchant.merchant_category}' is allowed."
            )

    if not reasons:
        reasons.append("No merchant expectations were supplied to check against.")

    return make_signal(
        name="merchant_legitimacy",
        flag=flag,
        score=score,
        tier=TIER_HIGH,
        reasons=reasons,
        matched_fields=matched,
        evidence={
            "merchant_id": auth.merchant.merchant_id,
            "merchant_category": auth.merchant.merchant_category,
            "availability": availability,
            "channel": channel,
        },
    )


# ---------------------------------------------------------------------------
# Lookalike seller - the expensive name-similarity half
# ---------------------------------------------------------------------------

def levenshtein(a: str, b: str) -> int:
    """Edit distance. Hand-rolled to keep this package dependency-free."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,        # deletion
                    current[j - 1] + 1,     # insertion
                    previous[j - 1] + (ca != cb),  # substitution
                )
            )
        previous = current
    return previous[-1]


def name_similarity(a: str, b: str) -> float:
    """Normalised Levenshtein similarity in [0, 1], case- and space-insensitive."""
    x = "".join(a.lower().split())
    y = "".join(b.lower().split())
    if not x and not y:
        return 1.0
    longest = max(len(x), len(y))
    if longest == 0:
        return 1.0
    return 1.0 - levenshtein(x, y) / longest


def detect_lookalike_merchant(
    auth: Authorization,
    familiar: list[FamiliarMerchant] | tuple[FamiliarMerchant, ...],
    *,
    similarity_threshold: float = 0.85,
) -> Signal:
    """A merchant whose NAME closely resembles one the card knows, but whose
    `merchant_id` is different.

    This is the SCEN0004 lookalike case: `PixelHarbour` (ME0059) sitting beside
    the genuine `PixelHarbor` (ME0022). Note the decisive evidence is the ID and
    the transaction history, not the spelling - name similarity only explains
    *why* it is worth flagging.

    `familiar` is built by Function 2 from authorization history: approved
    transactions grouped by merchant_id.
    """
    current_id = auth.merchant.merchant_id
    current_name = auth.merchant.merchant_name

    known = {m.merchant_id for m in familiar}
    if current_id in known:
        prior = next(m for m in familiar if m.merchant_id == current_id)
        return make_signal(
            name="lookalike_merchant",
            flag=FLAG_NONE,
            score=0.0,
            tier=TIER_HIGH,
            reasons=[
                f"Merchant {current_id} ({current_name}) has {prior.approved_count} "
                f"prior approved transaction(s) on this card - it is known, not a "
                f"lookalike."
            ],
            matched_fields=["merchant.merchant_id"],
            evidence={"prior_approved_count": prior.approved_count},
        )

    best: FamiliarMerchant | None = None
    best_score = 0.0
    for candidate in familiar:
        score = name_similarity(current_name, candidate.merchant_name)
        if score > best_score:
            best, best_score = candidate, score

    if best is not None and best_score >= similarity_threshold:
        return make_signal(
            name="lookalike_merchant",
            flag="lookalike_merchant",
            score=min(1.0, best_score),
            tier=TIER_MEDIUM,
            reasons=[
                f"Merchant {current_id} ('{current_name}') has NO prior approved "
                f"transactions on this card.",
                f"Its name is {best_score:.0%} similar to '{best.merchant_name}' "
                f"({best.merchant_id}), which the card has used "
                f"{best.approved_count} time(s).",
                "A near-identical name with a different merchant_id and no history "
                "is the signature of a lookalike seller.",
            ],
            matched_fields=["merchant.merchant_name", "merchant.merchant_id"],
            evidence={
                "resembles_merchant_id": best.merchant_id,
                "resembles_merchant_name": best.merchant_name,
                "name_similarity": round(best_score, 4),
                "prior_approved_count": 0,
            },
        )

    return make_signal(
        name="lookalike_merchant",
        flag="unfamiliar_merchant",
        score=0.4,
        tier=TIER_MEDIUM,
        reasons=[
            f"Merchant {current_id} ('{current_name}') has no prior approved "
            f"transactions on this card, but its name does not closely resemble "
            f"any known merchant"
            + (
                f" (closest: '{best.merchant_name}' at {best_score:.0%})."
                if best
                else "."
            ),
            "Unfamiliar is not the same as fraudulent - a mandate that does not "
            "require a known seller should not decline on this alone.",
        ],
        matched_fields=["merchant.merchant_id"],
        evidence={
            "prior_approved_count": 0,
            "closest_known_name": best.merchant_name if best else None,
            "closest_name_similarity": round(best_score, 4) if best else None,
        },
    )
