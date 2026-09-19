"""
hard_rules.py
=============
Function 2 (decision engine): deterministic evaluator for the `hard_rules[]`
array that Function 1 (`/mandate-compiler`) produces, per
`/contracts/mandate.schema.json`.

SCOPE: this module answers exactly one question -- "does this purchase comply
with every hard_rule in this mandate?" -- and nothing about approve / decline
/ step_up. See this folder's README, "Design shape worth copying", for how a
compliant result here plugs into the full decision (hard stops first,
denylist-before-allowlist, `uncertainty_policy` as the fallback when nothing
else resolves the case, fail-closed on any error).

SOURCE OF TRUTH, NOT A FORK: this module imports `FIELDS` from
`mandate_compiler.models` instead of re-declaring the field vocabulary, so
Function 1 and Function 2 cannot silently drift apart. If the compiler adds,
renames or reclassifies a field, this module either already handles it
(`rolling.*` / `authorization.*` are resolved generically, keyed by name) or
raises loudly the first time that field is actually used -- never silently.

THE TWO NAMESPACES (`contracts/README.md`, "thing #1" that will bite you):
  - `authorization.*` fields are read straight off THIS event. scope=purchase.
  - `rolling.*` fields are aggregates over the caller's OWN ledger (see
    `ledger.py`), computed over the trailing `period_days`, summing/counting
    only strictly-earlier rows, per card. scope=period. Never read off the
    event itself.
`mandate_compiler.models.HardRule` already enforces field/scope agreement at
COMPILE time, so a well-formed mandate never mixes them -- but this module
checks again anyway at EVALUATE time, because it must not assume every caller
compiled its mandate through Function 1 (or that the JSON wasn't hand-edited
or tampered with in transit).

CURRENCY (`contracts/README.md`, "thing #2"): a rule's `value` is denominated
in its `currency`, which the field name does NOT promise is CHF. Both money
fields in this vocabulary (`authorization.billing_amount_chf` and
`rolling.billing_amount_chf`) are themselves always CHF-denominated, so this
module converts the RULE's value into CHF via the caller-supplied `fx_rates`
before comparing. A currency `fx_rates` doesn't cover is FAIL-CLOSED --
`status="unresolvable"`, which counts as non-compliant -- never silently
skipped. "A cap that can't be compared is not a cap that was satisfied."

OPERATORS (`contracts/README.md`, "thing #3"): exactly
`<=, <, >=, >, ==, !=, in, not_in` -- note `==`, not `=`. `in` / `not_in` take
a list value; every other operator takes a scalar.
`mandate_compiler.models.HardRule` already enforces this shape at compile
time; this module re-checks defensively rather than trusting the caller.

PROVISIONAL FIELDS (`mandate-compiler/README.md`, event-day checklist item
#2): of the 7 fields in `FIELDS`, only `authorization.billing_amount_chf` is
confirmed against Viseca's own worked example. The rest --
`authorization.merchant.{name,category,country}`, `authorization.item_count`,
and both `rolling.*` fields -- are the compiler's best guess and have NOT
been reconciled against the live `authorization_event.schema.json`. This
module's best-effort mapping for each one lives in
`_resolve_authorization_field()` below, with the specific mismatches spelled
out there (e.g. the live event's merchant object uses `merchant_name`, not
`merchant.name`). Every provisional field this module evaluates gets flagged
in `RuleResult.detail`, so nothing ships on an unverified assumption
silently. CONFIRM THESE MAPPINGS ON EVENT DAY before relying on anything but
`authorization.billing_amount_chf`.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Callable

from mandate_compiler.models import FIELDS

from .ledger import Ledger


class CurrencyNotConvertible(Exception):
    """A rule's currency has no known fx_rate to the field's native currency."""


# ---------------------------------------------------------------------------
# authorization.* : read straight off the event's `authorization` object.
#
# PROVISIONAL MAPPING WARNING (see module docstring): mandate_compiler's
# field names do not literally match Viseca's authorization_event.schema.json
# field names for anything but billing_amount_chf.
#   - The event's merchant object uses merchant_name / merchant_category /
#     merchant_country (flat, prefixed) -- not merchant.name / .category /
#     .country. Mapped below; RECONCILE ON EVENT DAY.
#   - item_count does not exist on the event at all. items[] does. This maps
#     item_count to len(items) -- i.e. NUMBER OF LINES, NOT TOTAL QUANTITY
#     SUMMED ACROSS LINES. That is an assumption, not a confirmed fact --
#     flag it if the real semantics turn out to be "total units purchased".
# ---------------------------------------------------------------------------

def _resolve_authorization_field(field_name: str, auth: dict) -> Any:
    if field_name == "authorization.billing_amount_chf":
        return auth["billing_amount_chf"]
    if field_name == "authorization.merchant.name":
        return auth["merchant"]["merchant_name"]
    if field_name == "authorization.merchant.category":
        return auth["merchant"]["merchant_category"]
    if field_name == "authorization.merchant.country":
        return auth["merchant"]["merchant_country"]
    if field_name == "authorization.item_count":
        return len(auth["items"])
    raise KeyError(field_name)


def _resolve_rolling_field(field_name: str, ledger: Ledger, card_id: str, as_of: str, period_days: int, auth: dict) -> Any:
    """`ledger.*_in_window()` aggregates strictly-earlier rows only (by
    design -- see ledger.py). A cap check needs "would allowing THIS
    purchase breach the window", so the current purchase's own contribution
    (its own amount, or 1 purchase) is added on top here -- the same
    prior-plus-this-one convention the compiler's own reference examples use
    for billing_amount_chf, extended to purchase_count. Omitting this would
    silently let an unbounded number of purchases through: a purchase-count
    check that only ever looks at PRIOR count and never itself is never
    included in what it's counting.
    """
    if field_name == "rolling.billing_amount_chf":
        return ledger.spend_in_window(card_id, as_of, period_days) + auth["billing_amount_chf"]
    if field_name == "rolling.purchase_count":
        return ledger.count_in_window(card_id, as_of, period_days) + 1
    raise KeyError(field_name)


# Both money fields in the vocabulary (authorization.billing_amount_chf and
# rolling.billing_amount_chf, the latter built by summing the former) are
# always CHF-denominated -- so "convert to the field's native currency"
# always means "convert to CHF" for this vocabulary.
_MONEY_FIELDS = frozenset(name for name, spec in FIELDS.items() if spec.kind == "money")


def _convert_to_chf(value: float, currency: str, fx_rates: dict[str, float]) -> float:
    """Converts a rule's (value, currency) into CHF.

    `fx_rates` maps an ISO currency code to the rate that turns 1 unit of it
    into CHF (amount_in_currency * rate == amount_in_CHF) -- the same
    convention the challenge's own data pack (fx_rates.csv) uses, so a caller
    testing offline can pass that file's rows straight through.
    """
    if currency == "CHF":
        return value
    if currency not in fx_rates:
        raise CurrencyNotConvertible(f"no fx_rate for {currency!r}")
    return value * fx_rates[currency]


@dataclass
class RuleResult:
    rule: dict
    status: str  # "satisfied" | "violated" | "unresolvable"
    actual_value: Any = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "satisfied"


@dataclass
class HardRulesResult:
    compliant: bool
    rule_results: list[RuleResult] = dc_field(default_factory=list)

    def violations(self) -> list[RuleResult]:
        return [r for r in self.rule_results if not r.ok]


# NOTE on '=' vs '==': the LIVE event schema and the /v1/mandates POST body
# both use single '=' for equality (authorization_event.schema.json's
# mandate_rule.operator enum is exactly ["<","<=","=","!=",">",">=","in","not_in"]).
# mandate_compiler.models.Operator.EQ, however, emits "==". So a live event
# carries "=" while an offline mandate compiled through Function 1 carries "==".
# Both mean the same thing; this table accepts BOTH so the engine is correct
# whichever side produced the mandate. The api_client separately translates
# "==" -> "=" when SUBMITTING a compiled mandate to /v1/mandates, because the
# platform's own schema would reject "==".
_OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "<": lambda a, v: a < v,
    "<=": lambda a, v: a <= v,
    "=": lambda a, v: a == v,
    "==": lambda a, v: a == v,
    "!=": lambda a, v: a != v,
    ">": lambda a, v: a > v,
    ">=": lambda a, v: a >= v,
    "in": lambda a, v: a in v,
    "not_in": lambda a, v: a not in v,
}


def evaluate_hard_rules(
    event: dict,
    *,
    ledger: Ledger | None = None,
    fx_rates: dict[str, float] | None = None,
) -> HardRulesResult:
    """Evaluates every rule in `event["mandate"]["hard_rules"]` against `event`.

    Returns `HardRulesResult(compliant=True)` only if every rule resolves to
    `"satisfied"`. Fail-closed throughout: any rule that can't be checked --
    unknown field, scope/field mismatch, missing data, unconvertible currency
    -- counts as non-compliant, with `RuleResult.status == "unresolvable"`
    and `.detail` explaining exactly why.

    `ledger` is required if any rule has `scope == "period"` (i.e. targets a
    `rolling.*` field) -- pass `None` only for purchase-scoped-only mandates;
    a period-scope rule with no ledger raises `ValueError` rather than
    silently treating the window as unbounded.

    `fx_rates` is required whenever a rule's `currency` differs from CHF -- a
    money rule with no matching rate is `"unresolvable"` (fail-closed), never
    silently skipped or assumed to be CHF.
    """
    auth = event["authorization"]
    card_id = auth.get("card_id")
    as_of = auth.get("timestamp")
    rules = event["mandate"]["hard_rules"]
    fx_rates = fx_rates or {}

    results = [_evaluate_one_rule(rule, auth, card_id, as_of, ledger, fx_rates) for rule in rules]
    return HardRulesResult(compliant=all(r.ok for r in results), rule_results=results)


def _evaluate_one_rule(
    rule: dict,
    auth: dict,
    card_id: str | None,
    as_of: str | None,
    ledger: Ledger | None,
    fx_rates: dict[str, float],
) -> RuleResult:
    field_name = rule["field"]
    operator = rule["operator"]
    value = rule["value"]
    scope = rule.get("scope", "purchase")
    currency = rule.get("currency")
    period_days = rule.get("period_days")

    # 1. Field must be in the compiler's own known vocabulary -- a compliant
    #    mandate can never name anything else. This is the same guard
    #    mandate_compiler.models.HardRule applies at compile time; re-applied
    #    here because this module must not trust the caller compiled through
    #    Function 1, or that the JSON wasn't altered in transit.
    spec = FIELDS.get(field_name)
    if spec is None:
        return RuleResult(
            rule, "unresolvable",
            detail=(
                f"field {field_name!r} is not in mandate_compiler's known vocabulary -- "
                "a compliant mandate never produces this; refusing to guess, fail-closed"
            ),
        )

    # 2. scope must agree with the field's own namespace (authorization.* <->
    #    purchase, rolling.* <-> period) -- same coherence check the compiler
    #    already enforces at compile time, re-applied defensively.
    if spec.scope.value != scope:
        return RuleResult(
            rule, "unresolvable",
            detail=(
                f"field {field_name!r} is {spec.scope.value}-scoped but the rule "
                f"declares scope={scope!r} -- a compliant mandate never does this"
            ),
        )

    provisional_note = "" if spec.confirmed else " [PROVISIONAL FIELD -- unreconciled against the live event schema]"

    # 3. Resolve the actual value: authorization.* off the event, rolling.*
    #    off the ledger.
    if scope == "purchase":
        try:
            actual = _resolve_authorization_field(field_name, auth)
        except KeyError:
            return RuleResult(
                rule, "unresolvable",
                detail=f"field {field_name!r} not present on this event's authorization object -- fail-closed" + provisional_note,
            )
    else:  # scope == "period"
        if ledger is None:
            raise ValueError(
                f"rule on {field_name!r} has scope='period' but no ledger was passed to "
                "evaluate_hard_rules() -- cannot compute a rolling window without one"
            )
        if card_id is None or as_of is None:
            return RuleResult(
                rule, "unresolvable",
                detail="event.authorization is missing card_id or timestamp -- cannot look up a rolling window without both",
            )
        if period_days is None:
            return RuleResult(
                rule, "unresolvable",
                detail="scope='period' rule has no period_days -- a compliant mandate always sets one",
            )
        try:
            actual = _resolve_rolling_field(field_name, ledger, card_id, as_of, period_days, auth)
        except KeyError:
            return RuleResult(rule, "unresolvable", detail=f"no ledger aggregation implemented for {field_name!r}")

    # 4. Currency: convert the RULE's value into the field's native currency
    #    (CHF, for both money fields in this vocabulary) before comparing.
    if field_name in _MONEY_FIELDS:
        if currency is None:
            return RuleResult(
                rule, "unresolvable", actual_value=actual,
                detail=f"field {field_name!r} is a money field but the rule carries no currency -- a compliant mandate always sets one, fail-closed",
            )
        try:
            value = _convert_to_chf(value, currency, fx_rates)
        except CurrencyNotConvertible as exc:
            return RuleResult(
                rule, "unresolvable", actual_value=actual,
                detail=(
                    f"{exc} -- cannot convert rule value ({rule['value']} {currency}) to compare "
                    f"against {field_name!r} (always CHF); fail-closed, not silently skipped"
                ),
            )

    # 5. Compare.
    compare = _OPERATORS.get(operator)
    if compare is None:
        return RuleResult(rule, "unresolvable", actual_value=actual, detail=f"unknown operator {operator!r}")
    try:
        satisfied = compare(actual, value)
    except TypeError as exc:
        return RuleResult(
            rule, "unresolvable", actual_value=actual,
            detail=f"operator {operator!r} not comparable between {actual!r} and {value!r}: {exc}",
        )

    status = "satisfied" if satisfied else "violated"
    detail = f"{field_name} {operator} {value!r} (actual={actual!r})" + provisional_note
    return RuleResult(rule, status, actual_value=actual, detail=detail)
