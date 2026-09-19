"""
risk.py
=======
The composition layer: folds risk_signals evidence into decide()'s hard-rules
verdict, producing the final approve / decline / step_up.

This is the keystone that connects three separate packages -- mandate_compiler
(Function 1), decision_engine (hard rules), and risk_signals (evidence) -- into
one working Function 2. It is the ONLY place that turns evidence into a
decision; risk_signals itself never decides, and hard_rules only knows the
mandate's compiled rules.

DESIGN, decided with the team (2026-09-19):

  ESCALATE-ONLY (monotonic). A risk signal can only make the decision MORE
  conservative, never less. Starting from decide()'s verdict, folding signals
  can turn approve -> step_up or approve/step_up -> decline, but can never
  soften a decline back to approve, or a step_up back to approve. Severity
  order: approve(0) < step_up(1) < decline(2); the final decision is the
  max-severity anyone argued for.

  CONTEXT-GATED DECLINES. A signal declines only when it contradicts something
  the CUSTOMER actually asked for -- it is never a decline just because of the
  signal's type. The one exception is `card_not_active`, which declines
  unconditionally: a dead card means the transaction cannot occur at all, so
  it is mandate-independent. Everything else that can decline
  (merchant_category_mismatch, an unfamiliar/lookalike seller) does so only
  when the mandate required that thing -- otherwise it is at most a step_up, or
  pure evidence. Returnability is the worked example of why: a customer who
  never asked for returnable does not want a purchase declined because a shop
  marked it non-returnable.

  UNTRUSTED TEXT NEVER DECIDES. Low-tier signals over merchant free text
  (suspected_injected_instructions, extracted return-window) attach to the
  evidence trail and can, at most, contribute to a step_up -- never a decline
  or an approve on their own. Letting merchant text drive the decision IS the
  attack, in both directions (talk the agent into approving, or into refusing).

REQUIREMENT DERIVATION -- the pluggable seam. To gate declines on "did the
customer ask for this?", the layer needs the mandate's requirements as
structured flags (expected categories, returnable required, known-seller
required). Function 1 leaves these as free-text `guidance`, so `derive_requirements`
below is a deliberately small KEYWORD stub. It is the piece the quarantined LLM
extractor (piece B) will replace -- same MandateRequirements output, a better
engine behind it. The stub is conservative: it only sets a requirement on a
confident keyword hit, so it never invents a constraint the customer didn't
state (which would cause a false decline).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from risk_signals import (
    Authorization as RSAuthorization,
    FamiliarMerchant,
    Item as RSItem,
    Merchant as RSMerchant,
    PriorAuthorization,
    check_attempt_velocity,
    check_card_status,
    check_fulfillment_terms,
    check_merchant_legitimacy,
    detect_duplicate,
    detect_lookalike_merchant,
    extract_return_window_days,
    parse_timestamp,
    scan_manipulated_text,
)

from .decide import Decision
from .ledger import Ledger

_SEVERITY = {"approve": 0, "step_up": 1, "decline": 2}


# ---------------------------------------------------------------------------
# 1. Requirements -- the keyword stub the LLM extractor (piece B) will replace
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MandateRequirements:
    """What the customer actually asked for, as structured flags the detectors
    can be gated on. Produced today by `derive_requirements` (keyword stub);
    later by the quarantined LLM extractor, unchanged shape."""

    expected_categories: tuple[str, ...] | None = None
    require_returnable: bool = False
    require_cancellable: bool = False
    require_known_seller: bool = False
    min_return_days: int | None = None
    requested_items: tuple[str, ...] = ()  # concrete items the customer asked for (LLM extractor fills this)


# A tiny, illustrative lexicon: guidance phrase cue -> merchant_category value.
# Deliberately small and conservative -- this is the brittle part the LLM
# extractor generalises. Categories are real values from merchants.csv.
_CATEGORY_CUES: tuple[tuple[str, str], ...] = (
    (r"\bsport(s|ing)?\b|\bathletic\b|\brunning\b", "sporting_goods"),
    (r"\bgrocer(y|ies)\b|\bsupermarket\b", "groceries"),
    (r"\bcloth(es|ing)\b|\bapparel\b|\bwear\b", "clothing"),
    (r"\belectronics?\b|\bgadget", "electronics"),
    (r"\bbooks?\b|\bbookshop\b|\bbookstore\b", "books"),
)
_RETURNABLE_CUES = re.compile(r"\breturn(able|s|ed)?\b|\bcan be returned\b|\brefund", re.IGNORECASE)
_CANCELLABLE_CUES = re.compile(r"\bcancel(l?able|led)?\b", re.IGNORECASE)
_KNOWN_SELLER_CUES = re.compile(
    r"\bused? before\b|\bbought from before\b|\bshops? i (?:use|have used)\b"
    r"|\bfamiliar\b|\bregular(ly)?\b|\bknown (?:shop|seller|merchant)s?\b"
    r"|\bspecialist\b",
    re.IGNORECASE,
)
_RETURN_DAYS_CUES = re.compile(r"return(?:able|ed)?\s+(?:with)?in\s+(\d{1,3})\s*days?|(\d{1,3})\s*[- ]day\s+return", re.IGNORECASE)


def derive_requirements(mandate: dict) -> MandateRequirements:
    """Keyword stub: read the mandate's guidance (+ open_questions) into
    structured requirement flags. Conservative -- only sets a flag on a clear
    hit, so it never fabricates a constraint the customer didn't state."""
    blob = " ".join(
        list(mandate.get("guidance", []))
        + list(mandate.get("open_questions", []))
        + [mandate.get("instruction", "") or ""]
    )

    categories: list[str] = []
    for pattern, category in _CATEGORY_CUES:
        if re.search(pattern, blob, re.IGNORECASE):
            categories.append(category)

    min_days = None
    m = _RETURN_DAYS_CUES.search(blob)
    if m:
        min_days = int(next(g for g in m.groups() if g))

    return MandateRequirements(
        expected_categories=tuple(dict.fromkeys(categories)) or None,
        require_returnable=bool(_RETURNABLE_CUES.search(blob)),
        require_cancellable=bool(_CANCELLABLE_CUES.search(blob)),
        require_known_seller=bool(_KNOWN_SELLER_CUES.search(blob)),
        min_return_days=min_days,
    )


# ---------------------------------------------------------------------------
# 2. Bridge -- live event -> risk_signals input types
# ---------------------------------------------------------------------------

def _as_bool(value) -> bool:
    return str(value).strip().lower() == "true"


def to_rs_authorization(event: dict) -> RSAuthorization:
    """Map a live/offline authorization event into a risk_signals.Authorization.
    Field names already mirror Viseca's, so this is a copy, not a translation."""
    auth = event["authorization"]
    m = auth.get("merchant", {})
    items = tuple(
        RSItem(
            item_id=it.get("item_id", ""),
            quantity=int(it.get("quantity", 1)),
            item_name=it.get("item_name", ""),
            item_category=it.get("item_category", ""),
            unit_price=it.get("unit_price"),
            currency=it.get("currency"),
            item_details=it.get("item_details", "") or "",
        )
        for it in auth.get("items", [])
    )
    return RSAuthorization(
        authorization_id=auth["authorization_id"],
        card_id=auth["card_id"],
        timestamp=parse_timestamp(auth["timestamp"]),
        merchant=RSMerchant(
            merchant_id=m.get("merchant_id", ""),
            merchant_name=m.get("merchant_name", ""),
            merchant_category=m.get("merchant_category", ""),
            merchant_country=m.get("merchant_country", ""),
            merchant_city=m.get("merchant_city", ""),
            availability=m.get("availability", ""),
            recurring_capable=_as_bool(m.get("recurring_capable", "false")),
        ),
        amount=float(auth.get("amount", auth["billing_amount_chf"])),
        currency=auth.get("currency", "CHF"),
        billing_amount_chf=float(auth["billing_amount_chf"]),
        items=items,
        card_status_at_attempt=auth.get("card_status_at_attempt", "active"),
        authority_status=auth.get("authority_status", "active"),
        recent_attempt_count_10m=int(auth.get("recent_attempt_count_10m", 0)),
        customer_device_id=auth.get("customer_device_id"),
        channel=auth.get("channel", ""),
        fulfillment_method=auth.get("fulfillment_method"),
        delivery_by=auth.get("delivery_by"),
        order_returnable=auth.get("order_returnable", "unknown"),
        order_cancellable=auth.get("order_cancellable", "unknown"),
        purchase_description=auth.get("purchase_description", "") or "",
        related_authorization_id=auth.get("related_authorization_id"),
        related_authorization_status=auth.get("related_authorization_status"),
    )


def ledger_to_priors(ledger: Ledger, card_id: str) -> list[PriorAuthorization]:
    """Turn the engine's own approved-purchase ledger into duplicate-detection
    priors WITH cart lines -- the high-confidence path (see ledger.approved_rows
    and the risk_signals README's duplicate section)."""
    priors: list[PriorAuthorization] = []
    for row in ledger.approved_rows(card_id):
        raw_items = row.get("items") or []
        items = tuple(
            RSItem(item_id=it.get("item_id", ""), quantity=int(it.get("quantity", 1)))
            for it in raw_items
        ) or None
        priors.append(
            PriorAuthorization(
                authorization_id=row["authorization_id"],
                timestamp=parse_timestamp(row["timestamp"]),
                merchant_id=row.get("merchant_id") or "",
                billing_amount_chf=row["billing_amount_chf"],
                status=row.get("status", "approved"),
                items=items,
                currency=row.get("currency"),
                card_id=card_id,
            )
        )
    return priors


def build_familiar(history_rows: list[dict], card_id: str) -> list[FamiliarMerchant]:
    """Build the card's familiar-merchant list from authorization_history rows:
    approved transactions grouped by merchant_id. `history_rows` are dicts with
    at least card_id, merchant_id, merchant_name, status (the columns of
    authorization_history.csv)."""
    counts: dict[str, list] = {}
    for row in history_rows:
        if row.get("card_id") != card_id:
            continue
        if str(row.get("status", "")).lower() != "approved":
            continue
        mid = row.get("merchant_id")
        if not mid:
            continue
        entry = counts.setdefault(mid, [row.get("merchant_name", ""), 0])
        entry[1] += 1
    return [FamiliarMerchant(merchant_id=mid, merchant_name=name, approved_count=n) for mid, (name, n) in counts.items()]


# ---------------------------------------------------------------------------
# 3. Runner -- call only the detectors the mandate actually invokes
# ---------------------------------------------------------------------------

def gather_signals(
    event: dict,
    *,
    requirements: MandateRequirements,
    ledger: Ledger | None = None,
    familiar: list[FamiliarMerchant] | None = None,
):
    """Run the relevant detectors and return the list of Signals that FIRED.

    Universal safety checks (card status, velocity, duplicate, injection scan)
    always run. Requirement-specific checks (category, fulfilment, lookalike)
    run only when the mandate asked for the thing they verify -- the
    context-gating principle."""
    rs_auth = to_rs_authorization(event)
    signals = []

    # Universal -- always relevant.
    signals.append(check_card_status(rs_auth))
    signals.append(check_attempt_velocity(rs_auth))
    signals.append(scan_manipulated_text(rs_auth))  # low tier; never decides

    if ledger is not None:
        priors = ledger_to_priors(ledger, rs_auth.card_id)
        if priors:
            signals.append(detect_duplicate(rs_auth, priors))

    # Requirement-gated.
    if requirements.expected_categories:
        signals.append(check_merchant_legitimacy(rs_auth, expected_categories=requirements.expected_categories))
    if requirements.require_returnable or requirements.require_cancellable or requirements.min_return_days is not None:
        signals.append(check_fulfillment_terms(
            rs_auth,
            require_returnable=requirements.require_returnable,
            require_cancellable=requirements.require_cancellable,
            min_return_days=requirements.min_return_days,
        ))
        if requirements.min_return_days is not None:
            signals.append(extract_return_window_days(rs_auth))  # low tier evidence
    if familiar is not None:
        signals.append(detect_lookalike_merchant(rs_auth, familiar))

    return [s for s in signals if s.fired]


# ---------------------------------------------------------------------------
# 4. Fold -- turn fired signals into an escalation, context-gated
# ---------------------------------------------------------------------------

def _classify(flag: str, requirements: MandateRequirements) -> str:
    """What a fired signal argues for: 'decline' | 'step_up' | 'evidence' |
    'ignore'. Context-gated per the module docstring."""
    if flag == "card_not_active":
        return "decline"  # unconditional -- the transaction cannot occur
    if flag == "merchant_category_mismatch":
        # Only fires when expected_categories was supplied, i.e. the customer
        # required a category -> a real contradiction -> decline.
        return "decline"
    if flag == "lookalike_merchant":
        return "decline" if requirements.require_known_seller else "step_up"
    if flag == "unfamiliar_merchant":
        return "step_up" if requirements.require_known_seller else "evidence"
    if flag in ("likely_duplicate", "attempt_burst", "merchant_channel_mismatch",
                "fulfillment_mismatch", "fulfillment_unverifiable", "fulfillment_not_applicable"):
        return "step_up"
    if flag == "likely_retry":
        return "ignore"  # a retry of a declined order is normal; never block
    if flag in ("possible_duplicate", "elevated_attempt_rate", "return_window_extracted",
                "suspected_injected_instructions"):
        return "evidence"  # attach to trail; never escalate on its own
    return "evidence"


def fold_signals(base: Decision, signals, requirements: MandateRequirements) -> Decision:
    """Escalate-only fold. Never softens `base`; only raises severity when a
    signal argues for it. Merges every fired signal's reasons + evidence into
    the returned Decision so the explainability trail is complete."""
    final_decision = base.decision
    escalation_reasons: list[str] = []
    evidence: list[str] = list(base.evidence)

    for sig in signals:
        action = _classify(sig.flag, requirements)
        evidence.extend(f"[{sig.confidence_tier}] {sig.name}: {r}" for r in sig.reasons)
        if action in ("decline", "step_up") and _SEVERITY[action] > _SEVERITY[final_decision]:
            final_decision = action
            escalation_reasons.append(f"{sig.flag} ({sig.confidence_tier} tier)")

    if final_decision == base.decision:
        # Nothing escalated; keep the base decision but carry the richer trail.
        return Decision(base.authorization_id, base.decision, list(base.reason_codes),
                        base.customer_message, evidence)

    reason_codes = list(base.reason_codes) + [f"risk_escalation:{final_decision}"] + \
        [f"signal:{r}" for r in escalation_reasons]
    message = (
        "This purchase needs your confirmation because of the risk signals below."
        if final_decision == "step_up"
        else "This purchase was declined because of the risk signals below."
    )
    return Decision(base.authorization_id, final_decision, reason_codes, message, evidence)


# ---------------------------------------------------------------------------
# 5. Public entry -- decide() + the risk fold, in one call
# ---------------------------------------------------------------------------

def _unrequested_items(event: dict, requirements: MandateRequirements) -> list[str]:
    """Basket line names that don't match anything the customer asked for.
    Only meaningful when `requested_items` is populated (the LLM extractor does
    this; the keyword path leaves it empty, so this is inert offline). Matching
    is a simple word-overlap check -- the smart part is the extractor knowing
    what was requested; the compare stays deterministic."""
    requested = [r.lower() for r in requirements.requested_items]
    if not requested:
        return []
    req_words = {w for r in requested for w in re.findall(r"[a-z0-9]+", r)}
    unmatched: list[str] = []
    for it in event.get("authorization", {}).get("items", []):
        name = (it.get("item_name") or "").lower()
        words = set(re.findall(r"[a-z0-9]+", name))
        if not (words & req_words):
            unmatched.append(it.get("item_name") or "")
    return unmatched


def compose_decision(
    base: Decision,
    event: dict,
    *,
    ledger: Ledger | None = None,
    familiar: list[FamiliarMerchant] | None = None,
    requirements: MandateRequirements | None = None,
    extractor=None,
) -> Decision:
    """Fold risk evidence into an already-computed hard-rules Decision.

    `base` is what decide() returned. Requirements come from, in order: an
    explicit `requirements` arg; else `extractor.requirements(mandate)` when an
    extractor is passed (the quarantined LLM extractor); else the keyword
    `derive_requirements`. The default (no extractor) is byte-for-byte the
    offline keyword behaviour."""
    mandate = event.get("mandate", {})
    if requirements is None:
        requirements = extractor.requirements(mandate) if extractor is not None else derive_requirements(mandate)

    signals = gather_signals(event, requirements=requirements, ledger=ledger, familiar=familiar)
    result = fold_signals(base, signals, requirements)

    # Add-on check -- unlocked by the LLM extractor's requested_items. Escalates
    # to step_up only (the customer may accept the extra item), never decline.
    addons = _unrequested_items(event, requirements)
    if addons and _SEVERITY["step_up"] > _SEVERITY[result.decision]:
        result = Decision(
            result.authorization_id, "step_up",
            list(result.reason_codes) + ["risk_escalation:step_up", "signal:unrequested_addon (medium tier)"],
            "This purchase needs your confirmation: it includes items you didn't ask for.",
            list(result.evidence) + [f"[medium] unrequested_addon: basket line {a!r} matches nothing you requested" for a in addons],
        )
    elif addons:
        result = Decision(result.authorization_id, result.decision, list(result.reason_codes), result.customer_message,
                          list(result.evidence) + [f"[medium] unrequested_addon: basket line {a!r} matches nothing you requested" for a in addons])
    return result


def decide_full(
    event: dict,
    *,
    ledger: Ledger | None = None,
    fx_rates: dict[str, float] | None = None,
    familiar: list[FamiliarMerchant] | None = None,
    requirements: MandateRequirements | None = None,
    extractor=None,
) -> Decision:
    """The full Function 2 in one call: hard rules (decide) THEN the risk fold.
    This is what the worker and the replay harness call. `decide()` stays the
    pure hard-rules entry point for anyone who wants only that. Pass `extractor`
    (from decision_engine.extractor.get_extractor) to derive requirements with
    the quarantined LLM; omit it for the deterministic keyword path."""
    from .decide import decide

    base = decide(event, ledger=ledger, fx_rates=fx_rates)
    return compose_decision(base, event, ledger=ledger, familiar=familiar,
                            requirements=requirements, extractor=extractor)
