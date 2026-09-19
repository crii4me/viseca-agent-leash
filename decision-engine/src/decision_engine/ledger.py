"""
ledger.py
=========
Function 2's own record of prior APPROVED purchases, per card -- what
`rolling.*` hard_rule fields aggregate over. `/contracts/README.md`, "thing
#1", is explicit that a `rolling.*` field is never read off the incoming
event: it is an aggregate Function 2 computes from its OWN ledger over the
rule's `period_days`.

RECORD-APPROVED TIMING: only a FINAL approval belongs in this ledger. A
purchase that merely triggered a step_up must not be recorded until /resolve
actually confirms it -- call `record_approved()` when the decision is final,
never when a step_up is only offered. See `/decision-engine/README.md`'s test
list ("step-up requested but never answered ... decline on timeout, never
silently approve") -- an unresolved step-up must never inflate a later
rolling-window check either.

WINDOWING: `spend_in_window` / `count_in_window` sum or count only
STRICTLY-EARLIER rows (`window_start <= ts < as_of`), scoped per `card_id` --
so a rolling rule never counts the purchase it is currently evaluating, and
one card's history never leaks into another card's window.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


@dataclass
class Ledger:
    """In-memory ledger of approved purchases, scoped per `card_id`.

    Swap the storage for a real database in production (or for the
    platform's own history, if `/v1/bootstrap` or the event's own
    `context` ever exposes one) -- what `hard_rules.py` actually depends on
    is the aggregation contract above (strictly-earlier rows, per card,
    trailing `period_days`), not this in-memory implementation.
    """

    _by_card: dict[str, list[dict]] = field(default_factory=dict)
    _seen_authorization_ids: set[str] = field(default_factory=set)

    def already_handled(self, authorization_id: str) -> bool:
        """True if this authorization_id was already delivered and decided.

        The platform can redeliver the same authorization on a retry; a
        compliant engine must not double-decide or double-count it toward a
        rolling window (`/decision-engine/README.md` test list, idempotency).
        """
        return authorization_id in self._seen_authorization_ids

    def record_seen(self, authorization_id: str) -> None:
        self._seen_authorization_ids.add(authorization_id)

    def record_approved(self, event: dict) -> None:
        """Records a FINAL approval. Never call this for a step_up offer that
        hasn't been confirmed -- see the module docstring."""
        auth = event["authorization"]
        card_id = auth["card_id"]
        self._by_card.setdefault(card_id, []).append(
            {
                "authorization_id": auth["authorization_id"],
                "timestamp": auth["timestamp"],
                "billing_amount_chf": auth["billing_amount_chf"],
            }
        )

    def _rows_in_window(self, card_id: str, as_of: str, period_days: int) -> list[dict]:
        as_of_dt = _parse_ts(as_of)
        window_start = as_of_dt - timedelta(days=period_days)
        return [
            row
            for row in self._by_card.get(card_id, [])
            if window_start <= _parse_ts(row["timestamp"]) < as_of_dt
        ]

    def spend_in_window(self, card_id: str, as_of: str, period_days: int) -> float:
        """Backs `rolling.billing_amount_chf`."""
        return sum(row["billing_amount_chf"] for row in self._rows_in_window(card_id, as_of, period_days))

    def count_in_window(self, card_id: str, as_of: str, period_days: int) -> int:
        """Backs `rolling.purchase_count`."""
        return len(self._rows_in_window(card_id, as_of, period_days))
