"""
Input and output types for the risk-signal detectors.

Everything here is frozen. A detector takes these in and returns a `Signal`;
nothing is mutated, nothing is cached, no clock is read, no network is touched.
Two calls with the same inputs always produce the same output.

The one thing worth reading carefully is `PriorAuthorization`. Viseca's live
event gives you far less history than the blueprint assumes - see its docstring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def parse_timestamp(value: str | datetime) -> datetime:
    """Parse an ISO-8601 timestamp into a timezone-aware UTC datetime.

    Viseca timestamps end in `Z`, which `datetime.fromisoformat` rejects before
    Python 3.11, so normalise it explicitly rather than depending on the version.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Item:
    """One cart line."""

    item_id: str
    quantity: int = 1
    item_name: str = ""
    item_category: str = ""
    unit_price: float | None = None
    currency: str | None = None
    item_details: str = ""


@dataclass(frozen=True)
class Merchant:
    """Merchant reference data, as carried on the authorization event."""

    merchant_id: str
    merchant_name: str = ""
    merchant_category: str = ""
    merchant_country: str = ""
    merchant_city: str = ""
    availability: str = ""  # store | online | store_and_online | atm
    recurring_capable: bool = False


@dataclass(frozen=True)
class Authorization:
    """The incoming authorization event under evaluation.

    Field names mirror Viseca's `authorization` object so mapping from a live
    event is a straight copy, not a translation.
    """

    authorization_id: str
    card_id: str
    timestamp: datetime
    merchant: Merchant
    amount: float
    currency: str
    billing_amount_chf: float
    items: tuple[Item, ...] = ()

    # Structured fields the non-duplicate signals read.
    card_status_at_attempt: str = "active"
    authority_status: str = "active"
    recent_attempt_count_10m: int = 0
    customer_device_id: str | None = None
    channel: str = ""
    fulfillment_method: str | None = None
    delivery_by: str | None = None
    order_returnable: str = "unknown"  # true | false | unknown | not_applicable
    order_cancellable: str = "unknown"
    purchase_description: str = ""
    related_authorization_id: str | None = None
    related_authorization_status: str | None = None


@dataclass(frozen=True)
class PriorAuthorization:
    """One earlier authorization, used as a duplicate candidate.

    IMPORTANT - two different sources, two different levels of detail:

    1. `context.recent_authorizations` from the live Viseca event. Its schema is
       `additionalProperties: false` over exactly five fields: authorization_id,
       timestamp, merchant_id, billing_amount_chf, status. There are NO cart
       lines and NO currency. Built from this source, `items` and `currency`
       are None.

    2. Function 2's own ledger. If the decision engine records the full event
       for every authorization it decides on, it can populate `items` and
       `currency` too.

    The detector works with either. With `items=None` it skips the item-set
    stage, caps the score, and says so in `reasons` - it never guesses at cart
    contents it was not given. Feeding it source 2 measurably raises confidence,
    which is the argument for keeping that ledger.
    """

    authorization_id: str
    timestamp: datetime
    merchant_id: str
    billing_amount_chf: float
    status: str = "approved"  # approved | declined | pending | cancelled

    # Present only when fed from Function 2's own ledger.
    items: tuple[Item, ...] | None = None
    currency: str | None = None
    card_id: str | None = None

    @property
    def has_cart_detail(self) -> bool:
        return self.items is not None


@dataclass(frozen=True)
class FamiliarMerchant:
    """A merchant this card has transacted with before.

    Derived by Function 2 from authorization history - counting approved
    transactions per merchant_id. Used for the lookalike-seller check.
    """

    merchant_id: str
    merchant_name: str
    approved_count: int = 0


# Confidence tiers. These tell Function 2 how much weight a signal deserves,
# independent of its numeric score - a 0.9 from a text heuristic is not the same
# kind of thing as a 0.9 from a structured field comparison.
TIER_HIGH = "high"      # structured fields only; safe to act on directly
TIER_MEDIUM = "medium"  # inferred from structured fields; corroborate first
TIER_LOW = "low"        # heuristic over untrusted text; open_question ONLY

FLAG_NONE = "none"


@dataclass(frozen=True)
class Signal:
    """Decision-neutral evidence produced by one detector.

    This is never a decision. `flag` describes what was observed, not what to do
    about it. Turning a flag into approve / decline / step_up is Function 2's
    job, under the mandate's uncertainty_policy.

    A detector that finds nothing still returns a Signal, with `flag="none"` and
    a reason saying what it checked. Silence is not an answer.
    """

    name: str
    flag: str
    score: float
    confidence_tier: str
    reasons: tuple[str, ...] = ()
    matched_against_authorization_id: str | None = None
    matched_fields: tuple[str, ...] = ()
    time_gap_minutes: float | None = None
    evidence: tuple[tuple[str, object], ...] = ()

    @property
    def fired(self) -> bool:
        """True when this signal observed something worth Function 2's attention."""
        return self.flag != FLAG_NONE

    def to_dict(self) -> dict:
        """JSON-ready form, for attaching to a decision's evidence trail."""
        out: dict = {
            "name": self.name,
            "flag": self.flag,
            "score": round(self.score, 4),
            "confidence_tier": self.confidence_tier,
            "matched_against_authorization_id": self.matched_against_authorization_id,
            "matched_fields": list(self.matched_fields),
            "time_gap_minutes": self.time_gap_minutes,
            "reasons": list(self.reasons),
        }
        if self.evidence:
            out["evidence"] = dict(self.evidence)
        return out


def make_signal(
    name: str,
    flag: str,
    score: float,
    tier: str,
    reasons: list[str] | tuple[str, ...] = (),
    matched_against: str | None = None,
    matched_fields: list[str] | tuple[str, ...] = (),
    time_gap_minutes: float | None = None,
    evidence: dict | None = None,
) -> Signal:
    """Build a Signal, clamping the score and freezing the collections."""
    return Signal(
        name=name,
        flag=flag,
        score=max(0.0, min(1.0, float(score))),
        confidence_tier=tier,
        reasons=tuple(reasons),
        matched_against_authorization_id=matched_against,
        matched_fields=tuple(matched_fields),
        time_gap_minutes=time_gap_minutes,
        evidence=tuple(sorted((evidence or {}).items())),
    )
