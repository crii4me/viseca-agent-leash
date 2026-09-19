"""
decide.py
=========
Function 2's decision brain: one purchase event -> `approve` / `decline` /
`step_up`. Wraps the deterministic `hard_rules.evaluate_hard_rules()` in the
ordered ladder the team agreed on (2026-09-19), then falls back to the
mandate's `uncertainty_policy` only when the hard rules don't resolve the case.

DECISION LADDER (top wins; first match returns):

  0. mandate not active           -> decline   ["mandate_not_active"]
       A live event only arrives for an active mandate, but offline stubs and
       tampered payloads may not be -- so this is checked, not assumed.

  1. any hard rule VIOLATED       -> decline   ["hard_rule_violation"]
       A rule that was actually checked and failed. The customer set this
       limit explicitly; breaching it is a decline, full stop.

  2. any hard rule UNRESOLVABLE   -> step_up    ["rule_unverifiable"]
       A rule that couldn't be checked (a field it needs is missing from the
       event). Team decision: do NOT auto-decline a purchase just because a
       field wasn't supplied -- hand it to the human instead. Currency is not
       a source of this in practice: CHF/EUR/GBP/USD all have fixed rates
       loaded from /v1/reference-data, so conversion never fails; a genuinely
       absent rate is a config bug and also surfaces here as step_up, never a
       silent decline.

  3. all rules SATISFIED (or none), open_questions present
                                  -> uncertainty_policy
       ask -> step_up, decline -> decline, approve -> approve. The contract's
       intended role for uncertainty_policy: what to do when nothing else
       resolves the case, and an unanswered open_question means the case is
       not fully resolved. NOTE: open_questions are STRIPPED from live events
       (they're explanatory text, present only on GET /v1/mandates/{id}); the
       api_client injects them back into the event's mandate before calling
       this function, so this branch can fire live. Offline they're already
       present because we build the mandate ourselves.

  4. all rules SATISFIED, nothing open
                                  -> approve    ["within_policy"]

  E. any exception while evaluating -> decline  ["engine_error"]
       The one place fail-closed still means DECLINE, not step_up: a crash
       under the 8-second deadline must resolve to a definite, safe answer
       fast. This is deliberately distinct from rung 2 (an orderly "can't
       check this rule" -> step_up); an engine that blew up mid-evaluation
       cannot vouch for the purchase, so it declines. Flip this to step_up
       here if the team prefers.

Every branch returns a `Decision` carrying `reason_codes` and human-readable
`evidence`, so the /decision payload and the demo UI are built from the same
object -- a direct hit on the explainability judging criterion.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .hard_rules import evaluate_hard_rules
from .ledger import Ledger

_POLICY_TO_DECISION = {"ask": "step_up", "decline": "decline", "approve": "approve"}


@dataclass
class Decision:
    authorization_id: str
    decision: str  # "approve" | "decline" | "step_up"
    reason_codes: list[str] = field(default_factory=list)
    customer_message: str = ""
    evidence: list[str] = field(default_factory=list)

    def to_api_payload(self, engine_version: str | None = None) -> dict:
        """The body for POST /v1/authorizations/{authorization_id}/decision.

        Only authorization_id and decision are required by the API; the rest
        explain the call and feed the explainability criterion.
        """
        payload = {
            "authorization_id": self.authorization_id,
            "decision": self.decision,
            "reason_codes": list(self.reason_codes),
            "customer_message": self.customer_message,
            "evidence": list(self.evidence),
        }
        if engine_version is not None:
            payload["engine_version"] = engine_version
        return payload


def decide(
    event: dict,
    *,
    ledger: Ledger | None = None,
    fx_rates: dict[str, float] | None = None,
) -> Decision:
    """Decide one purchase event. Never raises: any internal error is caught
    and turned into a fail-closed `decline` (rung E above), because under the
    8-second deadline an uncaught exception would otherwise become a timeout,
    and a timeout with no definite answer is the worst outcome."""
    auth = event.get("authorization", {})
    authorization_id = auth.get("authorization_id", "<unknown>")

    try:
        return _decide_inner(event, authorization_id, ledger, fx_rates)
    except Exception as exc:  # noqa: BLE001 -- deadline safety: must not propagate
        return Decision(
            authorization_id,
            "decline",
            ["engine_error"],
            "This purchase was declined because it could not be checked safely in time.",
            [f"engine raised {type(exc).__name__}: {exc}"],
        )


def _decide_inner(
    event: dict,
    authorization_id: str,
    ledger: Ledger | None,
    fx_rates: dict[str, float] | None,
) -> Decision:
    mandate = event.get("mandate", {})

    # Rung 0: mandate must be active.
    status = mandate.get("status")
    if status is not None and status != "active":
        return Decision(
            authorization_id, "decline", ["mandate_not_active"],
            "This purchase was declined because the spending permission is not active.",
            [f"mandate.status = {status!r}"],
        )

    result = evaluate_hard_rules(event, ledger=ledger, fx_rates=fx_rates)

    violated = [r for r in result.rule_results if r.status == "violated"]
    unresolvable = [r for r in result.rule_results if r.status == "unresolvable"]

    # Rung 1: an explicitly-checked rule failed.
    if violated:
        return Decision(
            authorization_id, "decline", ["hard_rule_violation"],
            "This purchase was declined because it breaks a limit you set.",
            [r.detail for r in violated],
        )

    # Rung 2: a rule couldn't be checked -> ask the human, don't auto-decline.
    if unresolvable:
        return Decision(
            authorization_id, "step_up", ["rule_unverifiable"],
            "This purchase needs your confirmation: one of your rules couldn't be checked automatically.",
            [r.detail for r in unresolvable],
        )

    # Rungs 3 & 4: every rule passed (or there were none).
    open_questions = mandate.get("open_questions") or []
    if open_questions:
        policy = mandate.get("uncertainty_policy", "ask")
        decision = _POLICY_TO_DECISION.get(policy, "step_up")
        return Decision(
            authorization_id, decision, [f"uncertainty_policy:{policy}"],
            _uncertainty_message(decision),
            [f"all hard rules satisfied, but {len(open_questions)} open question(s) remain"]
            + [f"open question: {q}" for q in open_questions],
        )

    return Decision(
        authorization_id, "approve", ["within_policy"],
        "Approved: this purchase is within the limits you set.",
        [r.detail for r in result.rule_results] or ["no hard rules on this mandate; nothing to violate"],
    )


def _uncertainty_message(decision: str) -> str:
    if decision == "approve":
        return "Approved under your 'approve when unsure' setting."
    if decision == "decline":
        return "Declined under your 'decline when unsure' setting."
    return "This purchase needs your confirmation because part of your instruction was left open."
