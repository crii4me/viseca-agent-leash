"""
Tests for the api_client adapters that don't need a live key or network:
  - to_api_mandate: == -> = operator translation before submit
  - Worker._hydrate: re-injecting open_questions/uncertainty_policy that live
    events strip, so decide()'s uncertainty branch can still fire live.
"""

from __future__ import annotations

from decision_engine.api_client import LeashClient, Worker, to_api_mandate
from mandate_compiler.models import HardRule


def test_operator_double_equals_is_translated_to_single_for_the_api():
    # Build a mandate carrying an equality rule (compiler emits "==").
    eq_rule = HardRule(
        field="authorization.merchant.country", operator="==", value="CH"
    ).model_dump(mode="json", exclude_none=True)
    assert eq_rule["operator"] == "=="  # what Function 1 produces

    mandate = {"hard_rules": [eq_rule], "uncertainty_policy": "ask", "guidance": [], "open_questions": []}
    api_body = to_api_mandate(mandate)
    assert api_body["hard_rules"][0]["operator"] == "="  # what the platform accepts
    # original is untouched (deepcopy)
    assert mandate["hard_rules"][0]["operator"] == "=="


def test_non_equality_operators_are_left_alone():
    mandate = {
        "hard_rules": [
            {"field": "authorization.billing_amount_chf", "operator": "<=", "value": 20.0, "currency": "CHF", "scope": "purchase"},
        ],
        "uncertainty_policy": "ask", "guidance": [], "open_questions": [],
    }
    api_body = to_api_mandate(mandate)
    assert api_body["hard_rules"][0]["operator"] == "<="


def test_hydrate_injects_open_questions_that_live_events_strip():
    worker = Worker(client=LeashClient(base_url="http://unused", api_key=""))
    worker._mandate_ctx = {"open_questions": ["what counts as 'regularly'?"], "uncertainty_policy": "ask", "status": "active"}

    # A live event: mandate present but open_questions absent.
    live_event = {
        "authorization": {"authorization_id": "AU_LIVE", "card_id": "CA", "timestamp": "2026-09-19T09:00:00Z", "billing_amount_chf": 10.0},
        "mandate": {"hard_rules": [], "uncertainty_policy": "ask"},
    }
    hydrated = worker._hydrate(live_event)
    assert hydrated["mandate"]["open_questions"] == ["what counts as 'regularly'?"]
    assert hydrated["mandate"]["status"] == "active"
    # original event untouched (deepcopy)
    assert "open_questions" not in live_event["mandate"]
