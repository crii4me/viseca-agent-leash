"""
Tests for decision_engine.decide() -- the full approve/decline/step_up ladder.

Each rule set is produced by actually compiling an instruction through
Function 1 (mandate_compiler), so the tests exercise the real contract shape.
The ladder under test is documented in decide.py.
"""

from __future__ import annotations

from decision_engine import Ledger, decide
from mandate_compiler import compile_mandate


def _mandate_dict(instruction, **overrides):
    m = compile_mandate(instruction).to_viseca_dict()
    m.setdefault("status", "active")
    m.update(overrides)
    return m


def _event(mandate, *, billing_amount_chf=10.0, card_id="CA_T", timestamp="2026-09-19T09:00:00Z",
           authorization_id="AU_T", merchant_category="groceries"):
    return {
        "authorization": {
            "authorization_id": authorization_id,
            "card_id": card_id,
            "timestamp": timestamp,
            "billing_amount_chf": billing_amount_chf,
            "merchant": {"merchant_name": "Shop", "merchant_category": merchant_category, "merchant_country": "CH"},
            "items": [{"item_id": "IT1", "item_name": "thing", "quantity": 1}],
        },
        "mandate": mandate,
    }


class TestApprove:
    def test_within_cap_and_no_open_questions_approves(self):
        # eur instruction has NO open questions and decline-on-unsure policy;
        # use the office-supplies one but drop open_questions for a clean approve.
        mandate = _mandate_dict("Spend up to EUR 45 on office supplies. Decline if you're not sure.", open_questions=[])
        event = _event(mandate, billing_amount_chf=30.0)
        d = decide(event, ledger=Ledger(), fx_rates={"EUR": 0.93})
        assert d.decision == "approve"
        assert d.reason_codes == ["within_policy"]


class TestDeclineOnViolation:
    def test_over_cap_declines(self):
        mandate = _mandate_dict("Buy one ordinary grocery item for CHF 20 or less. Ask me when uncertain.")
        event = _event(mandate, billing_amount_chf=50.0)
        d = decide(event, ledger=Ledger())
        assert d.decision == "decline"
        assert d.reason_codes == ["hard_rule_violation"]
        assert d.evidence  # carries which rule failed


class TestStepUpOnUnresolvable:
    def test_missing_fx_rate_steps_up_not_declines(self):
        """Team decision: a rule that can't be checked hands to the human,
        it does NOT auto-decline. Here the EUR rule has no fx rate supplied."""
        mandate = _mandate_dict("Spend up to EUR 45 on office supplies. Decline if you're not sure.")
        event = _event(mandate, billing_amount_chf=10.0)
        d = decide(event, ledger=Ledger(), fx_rates={})  # no EUR rate
        assert d.decision == "step_up"
        assert d.reason_codes == ["rule_unverifiable"]


class TestUncertaintyPolicyFallback:
    def test_all_pass_with_open_questions_uses_ask_policy(self):
        """Worked example: under the cap, but 'regularly' is an open question
        and uncertainty_policy is 'ask' -> step_up."""
        mandate = _mandate_dict("Buy one ordinary grocery item for CHF 20 or less from a shop I use regularly. Ask me when uncertain.")
        assert mandate["open_questions"]  # sanity: it does have an open question
        assert mandate["uncertainty_policy"] == "ask"
        event = _event(mandate, billing_amount_chf=15.0)
        d = decide(event, ledger=Ledger())
        assert d.decision == "step_up"
        assert d.reason_codes == ["uncertainty_policy:ask"]

    def test_decline_policy_with_open_questions_declines(self):
        mandate = _mandate_dict(
            "Buy one ordinary grocery item for CHF 20 or less from a shop I use regularly. Decline when uncertain.",
        )
        # force the decline policy + keep the open question
        mandate["uncertainty_policy"] = "decline"
        assert mandate["open_questions"]
        event = _event(mandate, billing_amount_chf=15.0)
        d = decide(event, ledger=Ledger())
        assert d.decision == "decline"
        assert d.reason_codes == ["uncertainty_policy:decline"]

    def test_open_questions_stripped_means_approve(self):
        """Simulates a LIVE event (open_questions absent): the same under-cap
        purchase approves, because there's nothing left open to defer on."""
        mandate = _mandate_dict("Buy one ordinary grocery item for CHF 20 or less from a shop I use regularly. Ask me when uncertain.")
        mandate["open_questions"] = []  # as a live event would arrive
        event = _event(mandate, billing_amount_chf=15.0)
        d = decide(event, ledger=Ledger())
        assert d.decision == "approve"


class TestHardStops:
    def test_inactive_mandate_declines(self):
        mandate = _mandate_dict("Buy one ordinary grocery item for CHF 20 or less. Ask me when uncertain.")
        mandate["status"] = "revoked"
        event = _event(mandate, billing_amount_chf=5.0)
        d = decide(event, ledger=Ledger())
        assert d.decision == "decline"
        assert d.reason_codes == ["mandate_not_active"]

    def test_engine_error_fails_closed_to_decline(self):
        """A malformed event that makes evaluation throw must resolve to a
        definite decline, never propagate as a timeout."""
        broken = {"authorization": {"authorization_id": "AU_BROKEN"}}  # no mandate key
        d = decide(broken, ledger=Ledger())
        assert d.decision == "decline"
        assert d.reason_codes == ["engine_error"]


class TestApiPayload:
    def test_payload_has_required_fields(self):
        mandate = _mandate_dict("Buy one ordinary grocery item for CHF 20 or less. Ask me when uncertain.")
        event = _event(mandate, billing_amount_chf=5.0, authorization_id="AU_PAY")
        d = decide(event, ledger=Ledger())
        payload = d.to_api_payload(engine_version="0.1.0")
        assert payload["authorization_id"] == "AU_PAY"
        assert payload["decision"] in ("approve", "decline", "step_up")
        assert payload["engine_version"] == "0.1.0"
