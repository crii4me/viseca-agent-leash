"""
Viseca mandate format - the output contract of Function 1 (the mandate compiler).

This module is the single source of truth for the shape Function 2 consumes.
Nothing here performs I/O or calls a model; it is pure data + validation, so it
can be imported by the decision engine without dragging in an LLM dependency.

Two invariants are enforced here rather than left to convention, because both
are failure modes called out in the working notes:

1.  A hard rule may only name a field from the known vocabulary below. A clause
    that wants a field we cannot evaluate is not a hard rule - it is guidance
    plus an open question. This is what stops "everything becomes a brittle
    hard rule".

2.  `scope` and the field namespace must agree. `authorization.*` fields are read
    straight off the incoming event (scope=purchase). `rolling.*` fields are
    aggregates Function 2 computes from its own ledger over `period_days`
    (scope=period). Mixing them is the "rolling-period aggregation computed over
    the wrong window" bug, caught at compile time instead of at 3am.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Operator(str, Enum):
    """Comparison operators a hard rule may use."""

    LTE = "<="
    LT = "<"
    GTE = ">="
    GT = ">"
    EQ = "=="
    NEQ = "!="
    IN = "in"
    NOT_IN = "not_in"


class Scope(str, Enum):
    """Whether a rule is checked against one purchase or a rolling window."""

    PURCHASE = "purchase"
    PERIOD = "period"


class UncertaintyPolicy(str, Enum):
    """What Function 2 does when nothing else resolves a case."""

    ASK = "ask"
    DECLINE = "decline"
    APPROVE = "approve"


class Bucket(str, Enum):
    """Which of the four output buckets a source clause was routed to."""

    HARD_RULE = "hard_rule"
    GUIDANCE = "guidance"
    OPEN_QUESTION = "open_question"
    UNCERTAINTY_POLICY = "uncertainty_policy"


FieldKind = Literal["money", "count", "text", "category"]


@dataclass(frozen=True)
class FieldSpec:
    """One evaluable field, and how confident we are that Viseca actually has it."""

    name: str
    scope: Scope
    kind: FieldKind
    confirmed: bool
    note: str


# ---------------------------------------------------------------------------
# Field vocabulary
#
# CONFIRMED fields are ones we can cite from Viseca's own worked example.
# PROVISIONAL fields are our best guess at the event shape and MUST be
# reconciled against data/schemas/authorization_event.schema.json from
# github.com/START-Hack/viseca-2026 on event day. Until then, a rule naming a
# provisional field still compiles, but `Mandate.provisional_fields_used`
# reports it so nothing ships on an unverified assumption by accident.
# ---------------------------------------------------------------------------
FIELDS: dict[str, FieldSpec] = {
    f.name: f
    for f in [
        FieldSpec(
            "authorization.billing_amount_chf",
            Scope.PURCHASE,
            "money",
            confirmed=True,
            note=(
                "Billing amount of the single purchase, denominated in CHF. "
                "NOTE: `value` is expressed in the rule's `currency`, which is "
                "NOT necessarily CHF. Function 2 must convert via fx_rates "
                "before comparing. A rule whose currency cannot be converted "
                "must fail closed (decline), never be silently skipped."
            ),
        ),
        FieldSpec(
            "authorization.merchant.name",
            Scope.PURCHASE,
            "text",
            confirmed=False,
            note="Merchant display name. Untrusted text - never let it set a rule value.",
        ),
        FieldSpec(
            "authorization.merchant.category",
            Scope.PURCHASE,
            "category",
            confirmed=False,
            note="Merchant category / MCC bucket.",
        ),
        FieldSpec(
            "authorization.merchant.country",
            Scope.PURCHASE,
            "category",
            confirmed=False,
            note="ISO country code of the merchant.",
        ),
        FieldSpec(
            "authorization.item_count",
            Scope.PURCHASE,
            "count",
            confirmed=False,
            note=(
                "Number of line items on this authorization. Derived from "
                "purchase_attempt_items.csv; confirm it is exposed on the event."
            ),
        ),
        FieldSpec(
            "rolling.billing_amount_chf",
            Scope.PERIOD,
            "money",
            confirmed=False,
            note=(
                "Sum of billing amounts over the trailing `period_days`, per card. "
                "Function 2 computes this from its own ledger, summing only "
                "strictly-earlier rows."
            ),
        ),
        FieldSpec(
            "rolling.purchase_count",
            Scope.PERIOD,
            "count",
            confirmed=False,
            note=(
                "Count of authorizations over the trailing `period_days`, per card. "
                "Same strictly-earlier-rows rule as above."
            ),
        ),
    ]
}

CONFIRMED_FIELDS = frozenset(n for n, f in FIELDS.items() if f.confirmed)
PROVISIONAL_FIELDS = frozenset(n for n, f in FIELDS.items() if not f.confirmed)

_ISO_4217 = re.compile(r"^[A-Z]{3}$")


class HardRule(BaseModel):
    """A single mechanically-checkable constraint.

    Only clauses that reduce to a clean, comparable check belong here. If you
    find yourself wanting a `maybe` or a `roughly`, it is guidance.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    field: str = Field(description="Dotted field path; must be in the known vocabulary.")
    operator: Operator
    value: float | int | str | list[str]
    currency: str | None = Field(
        default=None,
        description="ISO 4217 code that `value` is denominated in. Required for money fields.",
    )
    scope: Scope = Scope.PURCHASE
    period_days: int | None = Field(
        default=None,
        description="Trailing window in days. Required when scope=period, forbidden otherwise.",
    )

    @field_validator("field")
    @classmethod
    def _known_field(cls, v: str) -> str:
        if v not in FIELDS:
            known = ", ".join(sorted(FIELDS))
            raise ValueError(
                f"unknown field {v!r}; a hard rule may only name an evaluable field. "
                f"Known fields: {known}"
            )
        return v

    @field_validator("currency")
    @classmethod
    def _iso_currency(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.upper()
        if not _ISO_4217.match(v):
            raise ValueError(f"currency must be a 3-letter ISO 4217 code, got {v!r}")
        return v

    @model_validator(mode="after")
    def _coherent(self) -> HardRule:
        spec = FIELDS[self.field]

        if spec.scope is not self.scope:
            raise ValueError(
                f"field {self.field!r} is a {spec.scope.value}-scoped field but the "
                f"rule declares scope={self.scope.value}. `authorization.*` fields are "
                f"per-purchase; `rolling.*` fields are per-period."
            )

        if self.scope is Scope.PERIOD:
            if self.period_days is None:
                raise ValueError("scope=period requires period_days")
            if self.period_days <= 0:
                raise ValueError(f"period_days must be positive, got {self.period_days}")
        elif self.period_days is not None:
            raise ValueError("period_days is only meaningful when scope=period")

        if spec.kind == "money":
            if self.currency is None:
                raise ValueError(f"field {self.field!r} is monetary and requires a currency")
            if not isinstance(self.value, (int, float)) or isinstance(self.value, bool):
                raise ValueError(f"monetary field {self.field!r} requires a numeric value")
        elif spec.kind == "count":
            if isinstance(self.value, bool) or not isinstance(self.value, int):
                raise ValueError(f"count field {self.field!r} requires an integer value")
            if self.currency is not None:
                raise ValueError(f"count field {self.field!r} must not carry a currency")

        if self.operator in (Operator.IN, Operator.NOT_IN) and not isinstance(self.value, list):
            raise ValueError(f"operator {self.operator.value!r} requires a list value")
        if self.operator not in (Operator.IN, Operator.NOT_IN) and isinstance(self.value, list):
            raise ValueError(f"operator {self.operator.value!r} does not take a list value")

        return self

    def describe_plain(self) -> str:
        """Customer-facing sentence, rendered from the structured fields.

        Built from {operator, value, currency, scope, period_days} with a
        template - never by editing the customer's original sentence. Text
        produced by cutting a matched span out of a sentence leaves the
        connectives that pointed at it dangling ("Keep the total across"),
        which is how garbled guidance reached the customer. A template cannot
        produce that.
        """
        spec = FIELDS[self.field]

        if spec.kind == "money":
            subject = (
                "Each purchase"
                if self.scope is Scope.PURCHASE
                else f"Total spending over any {self.period_days} days"
            )
        elif spec.kind == "count":
            subject = (
                "The number of items on a purchase"
                if self.scope is Scope.PURCHASE
                else f"The number of purchases in any {self.period_days} days"
            )
        else:
            subject = self.field.rsplit(".", 1)[-1].replace("_", " ").capitalize()

        phrase = {
            Operator.LTE: "no more than",
            Operator.LT: "less than",
            Operator.GTE: "at least",
            Operator.GT: "more than",
            Operator.EQ: "exactly",
            Operator.NEQ: "anything other than",
            Operator.IN: "one of",
            Operator.NOT_IN: "none of",
        }[self.operator]

        if isinstance(self.value, list):
            value = ", ".join(str(v) for v in self.value)
        elif self.currency and isinstance(self.value, (int, float)):
            value = f"{self.currency} {float(self.value):.2f}"
        elif isinstance(self.value, float):
            value = f"{self.value:g}"
        else:
            value = str(self.value)

        return f"{subject} must be {phrase} {value}."

    def describe(self) -> str:
        """Technical form, for reasons[] strings and debug output."""
        val = self.value
        if isinstance(val, list):
            val = "[" + ", ".join(str(x) for x in val) + "]"
        money = f" {self.currency}" if self.currency else ""
        window = f" over {self.period_days}d" if self.scope is Scope.PERIOD else ""
        return f"{self.field} {self.operator.value} {val}{money}{window}"


class Clause(BaseModel):
    """Provenance for one source span: where it went, and why.

    A single span may produce two clauses (e.g. guidance AND an open question)
    when it shapes judgment but is also under-specified.
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(description="The span of the original instruction this came from.")
    bucket: Bucket
    reason: str = Field(description="Why this span was routed to this bucket.")
    rule: HardRule | None = None
    guidance: str | None = None
    question: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    blocking: bool = Field(
        default=False,
        description=(
            "Open questions only. True when the question must be answered before "
            "a purchase can be evaluated at all - e.g. an amount limit whose "
            "currency is unknown, so the rule cannot be compared. False when the "
            "question is about context the stated limits do not depend on, such "
            "as what 'a shop I use regularly' means: those attach as evidence "
            "but must not force a step_up on a purchase that passes every rule."
        ),
    )


class CompilerMeta(BaseModel):
    """How this mandate was produced. Not part of Viseca's mandate contract."""

    model_config = ConfigDict(extra="forbid")

    backend: str
    model: str | None = None
    elapsed_ms: float | None = None
    warnings: list[str] = Field(default_factory=list)


class Mandate(BaseModel):
    """The compiled mandate.

    The four canonical Viseca fields are `hard_rules`, `uncertainty_policy`,
    `guidance` and `open_questions`. Everything else is provenance we add for
    explainability; `to_viseca_dict()` emits the canonical four alone.
    """

    model_config = ConfigDict(extra="forbid")

    hard_rules: list[HardRule] = Field(default_factory=list)
    uncertainty_policy: UncertaintyPolicy = UncertaintyPolicy.ASK
    guidance: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)

    # --- provenance (ours, not Viseca's) ---
    source_instruction: str = ""
    clauses: list[Clause] = Field(default_factory=list)
    meta: CompilerMeta | None = None

    def to_viseca_dict(self) -> dict:
        """The contract fields, JSON-ready. This is what Function 2 gets.

        `open_questions` keeps its original shape - a flat list of strings -
        because that is what Viseca's live API returns on GET /v1/mandates/{id}.
        `blocking_open_questions` is an additive subset of it: the questions that
        must be answered before a purchase can be evaluated at all. A consumer
        that does not know the field simply ignores it and behaves as before.
        """
        return {
            "hard_rules": [
                r.model_dump(mode="json", exclude_none=True) for r in self.hard_rules
            ],
            "uncertainty_policy": self.uncertainty_policy.value,
            "guidance": list(self.guidance),
            "open_questions": list(self.open_questions),
            "blocking_open_questions": list(self.blocking_open_questions),
        }

    @property
    def blocking_open_questions(self) -> list[str]:
        """The subset of `open_questions` that must be answered before deciding.

        Derived from clause provenance so the two lists cannot drift: a question
        is blocking only if the clause that produced it said so.
        """
        blocking = [
            c.question
            for c in self.clauses
            if c.bucket is Bucket.OPEN_QUESTION and c.blocking and c.question
        ]
        seen: set[str] = set()
        out: list[str] = []
        for q in blocking:
            if q.casefold() not in seen:
                seen.add(q.casefold())
                out.append(q)
        return out

    @property
    def provisional_fields_used(self) -> list[str]:
        """Hard-rule fields that are our guess, not Viseca-confirmed. Verify on event day."""
        return sorted({r.field for r in self.hard_rules if r.field in PROVISIONAL_FIELDS})

    @property
    def guidance_text(self) -> str:
        """Guidance as a single free-text block, if a consumer wants it that way."""
        return "\n".join(self.guidance)

    @property
    def rule_summaries(self) -> list[str]:
        """Plain-English rendering of every hard rule, for a customer or a demo.

        Template-rendered from the structured rules, so it cannot inherit the
        garbling that comes from editing the original sentence.
        """
        return [r.describe_plain() for r in self.hard_rules]
