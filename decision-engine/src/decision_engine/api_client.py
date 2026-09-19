"""
api_client.py
=============
The live worker: drives the Viseca sandbox end to end, using `decide()` as the
brain. No key is needed to import or unit-test this module; a key is needed
only to actually run a scenario against the hosted service (set TEAM_API_KEY).

Flow (technical_details.md sections 5-8):
    healthz -> bootstrap -> reference-data (fx rates)
    -> compile instruction via Function 1 -> POST /v1/mandates (draft)
    -> POST /v1/mandates/{draft_id}/confirm -> GET /v1/mandates/{mandate_id}
    -> POST /v1/scenario-runs
    -> loop: GET /v1/decision-requests/next?wait=25
             204 -> check run progress, poll again while work remains
             200 -> validate, idempotency-check, decide(), POST decision
    -> step_up decisions wait for a human answer, submitted via resolve()

TWO ADAPTERS THIS MODULE OWNS, and why:

  1. OPERATOR TRANSLATION (== -> =). mandate_compiler emits "==" for equality;
     the platform's /v1/mandates schema only accepts "=" (single). `to_api_mandate`
     rewrites every rule operator "==" -> "=" before submitting, so a compiled
     mandate isn't rejected. (The decision engine reads either form -- see
     hard_rules._OPERATORS -- so nothing breaks on the way back.)

  2. OPEN-QUESTIONS RE-INJECTION. Live events STRIP guidance and open_questions
     (technical_details.md: they "are absent from live events"). But the
     decision ladder's uncertainty-policy branch fires only when open_questions
     are present. So the worker fetches the confirmed mandate once
     (GET /v1/mandates/{id}), caches its open_questions + uncertainty_policy,
     and injects them into each event's mandate before calling decide(). Offline
     replay doesn't need this because it builds the mandate itself.

This module makes ZERO decisions of its own -- it only transports events to
decide() and decisions back to the API. All policy lives in decide.py.
"""

from __future__ import annotations

import copy
import os
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from mandate_compiler import compile_mandate

from .decide import Decision, decide
from .ledger import Ledger

DEFAULT_BASE_URL = "https://saw26api.ashyground-364e1d07.switzerlandnorth.azurecontainerapps.io"
ENGINE_VERSION = "decision-engine/0.1.0"


def to_api_mandate(mandate_dict: dict) -> dict:
    """Turn a Function-1 mandate (`compile_mandate(...).to_viseca_dict()`) into a
    /v1/mandates request body: same four fields, but with every rule operator
    "==" rewritten to "=" so the platform's schema accepts it."""
    body = copy.deepcopy(mandate_dict)
    for rule in body.get("hard_rules", []):
        if rule.get("operator") == "==":
            rule["operator"] = "="
    return body


class LeashApiError(RuntimeError):
    pass


@dataclass
class LeashClient:
    """Thin HTTP wrapper. Every call raises LeashApiError on an HTTP error so
    the worker loop can decide how to handle it, rather than silently
    continuing on a bad response."""

    base_url: str = field(default_factory=lambda: os.environ.get("LEASH_BASE_URL", DEFAULT_BASE_URL))
    api_key: str = field(default_factory=lambda: os.environ.get("TEAM_API_KEY", ""))
    timeout: float = 30.0

    def __post_init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})
        if self.api_key:
            self._session.headers.update({"Authorization": f"Bearer {self.api_key}"})

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        resp = self._session.request(method, f"{self.base_url}{path}", timeout=self.timeout, **kwargs)
        if resp.status_code >= 400:
            raise LeashApiError(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:500]}")
        return resp

    # -- setup -----------------------------------------------------------
    def healthz(self) -> dict:
        return self._request("GET", "/healthz").json()

    def bootstrap(self) -> dict:
        return self._request("GET", "/v1/bootstrap").json()

    def reference_data(self) -> dict:
        return self._request("GET", "/v1/reference-data").json()

    def fx_rates(self) -> dict[str, float]:
        """{currency -> rate to CHF}, pulled from /v1/reference-data. The exact
        JSON path isn't documented, so this probes the common shapes and, if
        none match, returns the pack's known fixed rates as a labelled
        fallback rather than an empty dict (which would fail-closed every
        non-CHF rule to step_up)."""
        data = self.reference_data()
        rates = _extract_fx_rates(data)
        if rates:
            return rates
        # Documented fixed rates (data/fx_rates.csv). If the live shape differs,
        # reconcile here on event day rather than shipping an empty map.
        return {"CHF": 1.0, "EUR": 0.95, "GBP": 1.12, "USD": 0.87}

    # -- mandate ---------------------------------------------------------
    def create_and_confirm_mandate(self, instruction: str) -> tuple[str, dict]:
        """Compile the instruction (Function 1), submit + confirm it, and return
        (mandate_id, the confirmed mandate as GET returns it -- including
        open_questions and uncertainty_policy)."""
        compiled = compile_mandate(instruction).to_viseca_dict()
        body = to_api_mandate(compiled)
        body["instruction"] = instruction  # the API stores the original wording; required field
        draft = self._request("POST", "/v1/mandates", json=body).json()
        draft_id = draft.get("draft_id")
        if not draft_id:
            raise LeashApiError(f"no draft_id in /v1/mandates response: {draft}")

        confirm = self._request("POST", f"/v1/mandates/{draft_id}/confirm", json={"confirmed": True}).json()
        mandate_id = confirm.get("mandate_id")
        if not mandate_id:
            raise LeashApiError(f"no mandate_id in confirm response: {confirm}")

        full = self._request("GET", f"/v1/mandates/{mandate_id}").json()
        return mandate_id, full

    # -- run -------------------------------------------------------------
    def start_scenario_run(self, scenario_id: str, mandate_id: str) -> str:
        run = self._request("POST", "/v1/scenario-runs", json={"scenario_id": scenario_id, "mandate_id": mandate_id}).json()
        run_id = run.get("run_id")
        if not run_id:
            raise LeashApiError(f"no run_id in scenario-run response: {run}")
        return run_id

    def run_progress(self, run_id: str) -> dict:
        return self._request("GET", f"/v1/scenario-runs/{run_id}").json()

    def poll_next(self, wait: int = 25) -> dict | None:
        """Long-poll for the next purchase. Returns the envelope dict on 200,
        or None on 204 (no work right now -- not necessarily run-finished)."""
        resp = self._request("GET", f"/v1/decision-requests/next?wait={wait}")
        if resp.status_code == 204:
            return None
        return resp.json()

    # -- decision / resolve ---------------------------------------------
    def submit_decision(self, decision: Decision) -> dict:
        return self._request(
            "POST", f"/v1/authorizations/{decision.authorization_id}/decision",
            json=decision.to_api_payload(engine_version=ENGINE_VERSION),
        ).json()

    def resolve(self, authorization_id: str, decision: str, customer_message: str = "", evidence: list | None = None) -> dict:
        """Submit a REAL customer's approve/decline after a step_up. Never call
        this to fake a human answer -- it's for the UI to call when the person
        actually responds."""
        return self._request(
            "POST", f"/v1/authorizations/{authorization_id}/resolve",
            json={"decision": decision, "customer_message": customer_message, "evidence": evidence or []},
        ).json()


def _extract_fx_rates(data: Any) -> dict[str, float]:
    """Best-effort pull of {currency -> rate_to_CHF} from the reference-data
    JSON, whose exact shape isn't documented. Tries a few plausible layouts;
    returns {} if none match so the caller can fall back."""
    node = data.get("data", data) if isinstance(data, dict) else data
    if not isinstance(node, dict):
        return {}
    candidate = node.get("fx_rates") or node.get("currency_rates") or node.get("rates")
    rates: dict[str, float] = {}
    if isinstance(candidate, dict):
        for k, v in candidate.items():
            try:
                rates[k] = float(v)
            except (TypeError, ValueError):
                continue
    elif isinstance(candidate, list):
        for row in candidate:
            if isinstance(row, dict) and str(row.get("to_currency", "CHF")) == "CHF":
                try:
                    rates[row["from_currency"]] = float(row["rate"])
                except (TypeError, ValueError, KeyError):
                    continue
    return rates


@dataclass
class Worker:
    """Orchestrates one scenario run end to end. Keeps its own Ledger and the
    per-run mandate context (open_questions + uncertainty_policy) that live
    events don't carry."""

    client: LeashClient
    ledger: Ledger = field(default_factory=Ledger)
    _mandate_ctx: dict = field(default_factory=dict)
    _fx_rates: dict[str, float] = field(default_factory=dict)

    def prepare(self, instruction: str, scenario_id: str) -> str:
        """Bootstrap, load fx rates, create+confirm the mandate, start the run.
        Returns run_id."""
        self.client.healthz()
        self.client.bootstrap()
        self._fx_rates = self.client.fx_rates()
        mandate_id, full_mandate = self.client.create_and_confirm_mandate(instruction)
        self._mandate_ctx = {
            "open_questions": full_mandate.get("open_questions", []),
            "uncertainty_policy": full_mandate.get("uncertainty_policy", "ask"),
            "status": full_mandate.get("status", "active"),
        }
        return self.client.start_scenario_run(scenario_id, mandate_id)

    def _hydrate(self, event: dict) -> dict:
        """Inject the open_questions / uncertainty_policy / status that live
        events strip, so decide()'s ladder has what it needs."""
        event = copy.deepcopy(event)
        mandate = event.setdefault("mandate", {})
        mandate.setdefault("uncertainty_policy", self._mandate_ctx.get("uncertainty_policy", "ask"))
        mandate.setdefault("status", self._mandate_ctx.get("status", "active"))
        if not mandate.get("open_questions"):
            mandate["open_questions"] = list(self._mandate_ctx.get("open_questions", []))
        return event

    def handle_one(self, envelope: dict) -> Decision | None:
        """Decide one polled envelope and submit the result. Returns the
        Decision, or None if it was a duplicate already handled."""
        event = envelope.get("data", envelope)
        auth = event.get("authorization", {})
        auth_id = auth.get("authorization_id")

        if auth_id and self.ledger.already_handled(auth_id):
            return None  # reconcile upstream; never decide or count twice
        if auth_id:
            self.ledger.record_seen(auth_id)

        event = self._hydrate(event)
        d = decide(event, ledger=self.ledger, fx_rates=self._fx_rates)
        if d.decision == "approve":
            self.ledger.record_approved(event)  # only FINAL approvals count
        self.client.submit_decision(d)
        return d

    def run(self, instruction: str, scenario_id: str, *, max_polls: int = 200, poll_wait: int = 25) -> list[Decision]:
        """Full loop for one scenario. Stops when the run reports no work
        remaining or max_polls is hit. step_up decisions are submitted and left
        pending for a human (the UI calls client.resolve when they answer)."""
        run_id = self.prepare(instruction, scenario_id)
        decisions: list[Decision] = []
        for _ in range(max_polls):
            envelope = self.client.poll_next(wait=poll_wait)
            if envelope is None:
                progress = self.client.run_progress(run_id)
                if _run_finished(progress):
                    break
                continue
            d = self.handle_one(envelope)
            if d is not None:
                decisions.append(d)
        return decisions


def _run_finished(progress: dict) -> bool:
    """Heuristic over /v1/scenario-runs/{run_id} counters -- the exact field
    names aren't documented, so this checks the common ones and treats an
    explicit 'complete'/'finished' status, or delivered==total, as done.
    Reconcile against the real bootstrap/run shape on event day."""
    node = progress.get("data", progress) if isinstance(progress, dict) else {}
    status = str(node.get("status", "")).lower()
    if status in ("complete", "completed", "finished", "done"):
        return True
    delivered = node.get("delivered") or node.get("events_delivered")
    total = node.get("total") or node.get("event_count")
    if isinstance(delivered, int) and isinstance(total, int) and total > 0:
        return delivered >= total
    return False
