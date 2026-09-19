"""
Deterministic, offline clause classifier.

This is both the no-API-key development path and the production fallback the
challenge brief asks for ("low-latency, small models with a deterministic
fallback"). It is tuned for *precision on hard rules*, not recall: it only emits
a hard rule when it has matched an explicit comparator, an explicit number, and
- for money - an explicit currency. Everything it cannot pin down that precisely
becomes guidance, and anything that looks like a missing threshold becomes an
open question.

That asymmetry is the whole point. A missed hard rule costs us a step-up prompt.
A wrong hard rule silently spends someone's money against a constraint they never
agreed to.
"""

from __future__ import annotations

import re

from ..config import Config
from ..models import Bucket, Clause, Operator, Scope, UncertaintyPolicy
from ..normalize import (
    as_guidance,
    is_filler,
    meaningful,
    remove_span,
    split_clauses,
    tidy,
)
from . import BackendResult, safe_rule

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

_CURRENCY = {
    "chf": "CHF", "fr": "CHF", "franc": "CHF", "francs": "CHF", "franken": "CHF",
    "eur": "EUR", "euro": "EUR", "euros": "EUR", "€": "EUR",
    "usd": "USD", "dollar": "USD", "dollars": "USD", "$": "USD",
    "gbp": "GBP", "pound": "GBP", "pounds": "GBP", "£": "GBP",
}
_CUR_ALT = "|".join(sorted((re.escape(k) for k in _CURRENCY), key=len, reverse=True))

_NUMBER_WORDS = {
    "once": 1, "twice": 2, "thrice": 3,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_NUM_WORD_ALT = "|".join(_NUMBER_WORDS)

_PERIOD_DAYS = {
    "day": 1, "days": 1, "daily": 1,
    "week": 7, "weeks": 7, "weekly": 7,
    "fortnight": 14, "fortnightly": 14,
    "month": 30, "months": 30, "monthly": 30,
    "year": 365, "years": 365, "yearly": 365, "annually": 365, "annum": 365,
}
# Windows whose day-count is an approximation worth confirming with the customer.
# A "month" is not 30 days and a "year" is not 365 for billing purposes; the
# customer may well mean a calendar period that resets, which is a different
# rule. We compile the rolling reading and ask rather than deciding silently.
_APPROXIMATE_PERIODS = {"month", "months", "monthly", "year", "years", "yearly",
                        "annually", "annum"}
_CALENDAR_NAME = {
    "month": "month", "months": "month", "monthly": "month",
    "year": "year", "years": "year", "yearly": "year",
    "annually": "year", "annum": "year",
}

# Comparators that mean strictly-less vs less-or-equal. Getting this wrong by one
# rappen is exactly the kind of silent error that loses a judging point.
_STRICT = {"under", "below", "less than", "lower than", "cheaper than", "beneath"}
_INCLUSIVE = {
    "no more than", "not more than", "at most", "up to", "maximum of", "maximum",
    "max of", "max", "no higher than", "no greater than", "or less", "or under",
    "or below", "or cheaper", "or fewer",
    # "at or below CHF 120" must NOT be read as the bare "below CHF 120": the
    # "at or" makes the limit inclusive, and dropping it declines a purchase
    # priced exactly at the stated limit. These are listed explicitly because
    # _CMP_BEFORE is length-sorted, so the compound phrase is tried before the
    # bare comparator it contains.
    "at or below", "at or under", "at or less than",
}

_NUM = r"\d{1,9}(?:['’]\d{3})*(?:[.,]\d{1,2})?"
_MONEY = (
    rf"(?:(?P<cur_a>{_CUR_ALT})\s*(?P<val_a>{_NUM})"
    rf"|(?P<val_b>{_NUM})\s*(?P<cur_b>{_CUR_ALT})\b)"
)

_CMP_BEFORE = "|".join(
    sorted((re.escape(c) for c in _STRICT | _INCLUSIVE if not c.startswith("or ")),
           key=len, reverse=True)
)
_CMP_AFTER = "|".join(
    sorted((re.escape(c) for c in _INCLUSIVE if c.startswith("or ")) , key=len, reverse=True)
) + r"|maximum|max"

# amount, comparator first:  "under CHF 50", "no more than 30 euros"
_RE_AMOUNT_PRE = re.compile(rf"\b(?P<cmp>{_CMP_BEFORE})\s+{_MONEY}", re.IGNORECASE)
# amount, comparator last:   "CHF 20 or less", "20 francs max"
_RE_AMOUNT_POST = re.compile(rf"{_MONEY}\s+(?P<cmp>{_CMP_AFTER})\b", re.IGNORECASE)
# negated comparator: "don't spend more than CHF 200", "never go over EUR 50".
# Handled separately because the negation flips the operator, and getting that
# backwards would turn a spending cap into a spending floor.
_RE_AMOUNT_NEG = re.compile(
    r"\b(?:don(?:'|’)?t|do not|never|avoid|without going)\s+(?:\w+\s+){0,2}?"
    rf"(?:more than|over|above|exceed(?:ing)?|beyond)\s+{_MONEY}",
    re.IGNORECASE,
)
# bare amount with no currency anywhere: "for 20 or less"
_RE_BARE_AMOUNT = re.compile(
    rf"\b(?:(?P<cmp1>{_CMP_BEFORE})\s+(?P<val1>{_NUM})"
    rf"|(?P<val2>{_NUM})\s+(?P<cmp2>{_CMP_AFTER}))\b",
    re.IGNORECASE,
)

_PERIOD_ALT = "|".join(sorted(_PERIOD_DAYS, key=len, reverse=True))
# "a week", "per month", "every 3 days", "any seven days", "weekly".
# `any` is in the determiner list and the count accepts number words so that
# "across any seven days" is recognised; without both, a rolling window stated
# in words compiles to a per-purchase cap and is never enforced.
_RE_PERIOD = re.compile(
    rf"\b(?:(?:a|an|any|per|each|every)\s+(?:(?P<n>\d+|{_NUM_WORD_ALT})\s+)?"
    rf"(?P<unit>{_PERIOD_ALT})"
    rf"|(?P<bare>daily|weekly|fortnightly|monthly|yearly|annually))\b",
    re.IGNORECASE,
)

# Words that mark an amount as a SUM over a window rather than a per-purchase
# price. Only when one of these is present will a window stated *before* the
# amount be treated as a rolling cap - otherwise "buy lunch every day for up to
# CHF 25" would wrongly become CHF 25 of lunch per day instead of per lunch.
_RE_AGGREGATE = re.compile(
    r"\b(?:total|totals|totalling|altogether|combined|cumulative|"
    r"in all|all together|sum|across)\b",
    re.IGNORECASE,
)

# "no more than 3 times a week", "twice a month", "at most once per day"
_RE_COUNT = re.compile(
    rf"\b(?:(?P<cmp>{_CMP_BEFORE})\s+)?"
    rf"(?P<n>\d{{1,4}}|{_NUM_WORD_ALT})\s*"
    rf"(?:times?\s*)?"
    rf"(?=(?:a|an|per|each|every)\s+(?:\d+\s+)?(?:{_PERIOD_ALT})\b)",
    re.IGNORECASE,
)

_RE_UNCERTAIN_TRIGGER = re.compile(
    r"\b(?:uncertain|unsure|not sure|in doubt|doubt|unclear|ambiguous|"
    r"can(?:'|’)?t tell|don(?:'|’)?t know|borderline|edge case)\b",
    re.IGNORECASE,
)
_RE_ASK = re.compile(
    r"\b(?:ask me|ask first|ask before|check with me|confirm with me|confirm first|"
    r"come to me|get my (?:approval|ok|okay)|prompt me|check in with me|"
    r"double[- ]?check with me)\b",
    re.IGNORECASE,
)
_RE_DECLINE = re.compile(
    r"\b(?:decline|refuse|reject|block it|say no|don(?:'|’)?t buy|do not buy|"
    r"don(?:'|’)?t purchase|do not purchase|skip it|walk away)\b",
    re.IGNORECASE,
)
_RE_APPROVE = re.compile(
    r"\b(?:approve|go ahead|allow it|let it through|proceed|"
    r"use your (?:own )?judg(?:e)?ment|decide yourself)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Vague qualifiers, split into two tiers.
#
# Tier A describes a KIND of thing. An agent can exercise that judgment per
# purchase from item metadata, so it is guidance and nothing more.
#
# Tier B asserts a QUANTIFIED RELATIONSHIP OVER HISTORY ("regularly", "usually",
# "trusted"). To enforce it the system would have to measure something against a
# threshold nobody supplied - so it is guidance AND an open question. That line
# is the reason "ordinary grocery item" and "a shop I use regularly" land in
# different places despite both being fuzzy.
# ---------------------------------------------------------------------------
_TIER_A = {
    "ordinary", "normal", "typical", "standard", "basic", "simple", "everyday",
    "plain", "regular-sized", "modest", "small", "sensible-looking", "cheap",
}

_TIER_B: dict[str, tuple[str, str]] = {
    "regularly": (
        "'regularly' sets a frequency threshold that the instruction never states.",
        "You limited purchases to shops you use regularly, but 'regularly' isn't "
        "defined. What should qualify a shop as regular - a minimum number of past "
        "purchases (say 3 or more), or any shop used within a recent window (say "
        "the last 90 days)?",
    ),
    "usually": (
        "'usually' describes a historical pattern with no threshold attached.",
        "What counts as a shop or item you 'usually' use? A share of past purchases, "
        "or a recent-use window?",
    ),
    "normally": (
        "'normally' describes a historical pattern with no threshold attached.",
        "What counts as 'normally' here - how far back should the system look, and "
        "how many prior purchases make something normal for you?",
    ),
    "often": (
        "'often' is a frequency qualifier with no threshold given.",
        "How often is 'often'? Give a number of purchases and a window (e.g. 4+ "
        "times in 30 days) so this can be checked.",
    ),
    "frequently": (
        "'frequently' is a frequency qualifier with no threshold given.",
        "How frequently is 'frequently'? A count and a time window would make this "
        "enforceable.",
    ),
    "occasionally": (
        "'occasionally' is a frequency qualifier with no threshold given.",
        "What upper bound does 'occasionally' imply - at most how many times, over "
        "what window?",
    ),
    "trusted": (
        "'trusted' implies a list or history the instruction does not supply.",
        "Which merchants count as trusted? A named list, or any merchant you have "
        "bought from before?",
    ),
    "familiar": (
        "'familiar' implies a history the instruction does not define.",
        "What makes a merchant familiar - any prior purchase, or some minimum "
        "number of them?",
    ),
    "known": (
        "'known' implies a list or history the instruction does not supply.",
        "Which merchants count as known to you - a specific list, or anywhere with "
        "prior purchase history?",
    ),
    "reasonable": (
        "'reasonable' is an open-ended judgment with no comparable value.",
        "What makes a price reasonable? An amount cap, or a percentage over a "
        "typical price, would turn this into something checkable.",
    ),
    "sensible": (
        "'sensible' is an open-ended judgment with no comparable value.",
        "What would make a purchase sensible or not? A concrete limit would let "
        "this be enforced rather than guessed.",
    ),
    "appropriate": (
        "'appropriate' is an open-ended judgment with no comparable value.",
        "What makes a purchase appropriate here? Naming a category or a cap would "
        "make this checkable.",
    ),
    "affordable": (
        "'affordable' is relative to a budget the instruction does not state.",
        "Affordable relative to what - a per-purchase cap, or a budget over a "
        "period?",
    ),
    "a few": (
        "'a few' is a count with no exact value.",
        "How many is 'a few' - and over what period?",
    ),
    "several": (
        "'several' is a count with no exact value.",
        "How many is 'several' - and over what period?",
    ),
    "necessary": (
        "'necessary' is a judgment the system cannot evaluate from event data.",
        "What makes a purchase necessary? If there is a category or a list that "
        "captures it, that would be enforceable.",
    ),
    "urgent": (
        "'urgent' is a judgment the system cannot evaluate from event data.",
        "How should urgency be determined from a purchase request? Without a "
        "signal in the event, this can only be guessed.",
    ),
}
_RE_TIER_B = re.compile(
    r"\b(?:" + "|".join(sorted((re.escape(t) for t in _TIER_B), key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
_RE_TIER_A = re.compile(
    r"\b(?:" + "|".join(sorted((re.escape(t) for t in _TIER_A), key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Conditions the customer plainly expects ENFORCED, for which no field exists.
#
# These are not descriptions of the kind of thing to buy ("an ordinary grocery
# item") - they are gates: "only if the order can be returned", "from shops I
# have used before". Filing them as silent guidance is the worst outcome: the
# customer stated a condition, and the system neither enforces it nor admits it
# cannot. They become open_questions instead - NON-blocking, because Function 2
# does enforce most of them through its risk layer, so the question is about
# precision, not about whether a purchase can be judged at all.
# ---------------------------------------------------------------------------
_ENFORCEABLE_INTENT = re.compile(
    r"\b(?:only\s+(?:if|from|at|when|with|where)|must\s+(?:be|have|not)|"
    r"has\s+to\s+be|needs?\s+to\s+be|provided\s+that|as\s+long\s+as|"
    r"so\s+long\s+as|pause\s+anything|block\s+anything|flag\s+anything|"
    r"stop\s+anything)\b",
    re.IGNORECASE,
)

# (topic pattern, the specific question to ask). A specific, answerable question
# beats a generic "we could not enforce this" every time.
_UNENFORCEABLE_TOPICS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(r"\breturn(?:ed|able|s|ing)?\b", re.IGNORECASE),
        "You made returnability a condition. The payment event reports only "
        "whether an order is returnable at all (true / false / unknown) - no "
        "field carries the return window in days. Is 'returnable at all' close "
        "enough, or should an order with no confirmed window be refused?",
    ),
    (
        re.compile(
            r"\b(?:used\s+before|bought\s+from\s+before|shopped\s+(?:at|with)\s+"
            r"before|used\s+previously|previously\s+used|been\s+to\s+before)\b",
            re.IGNORECASE,
        ),
        "You limited this to shops you have used before. How should that be "
        "decided - any single previous purchase at the same shop, or a minimum "
        "number of them, and over what period?",
    ),
    (
        re.compile(
            r"\b(?:someone\s+other\s+than\s+me|somebody\s+else|not\s+me|"
            r"driving\s+the\s+session|session)\b",
            re.IGNORECASE,
        ),
        "You asked to pause anything that looks like someone else is driving "
        "the session. The event carries a device id and a count of recent "
        "attempts - which of those should count as suspicious, and at what "
        "threshold?",
    ),
    (
        re.compile(
            r"\b(?:specialist|specialty)\b.*?\b(?:retailer|shop|store|seller)\b"
            r"|\bkind\s+of\s+(?:shop|store|retailer|seller)\b",
            re.IGNORECASE,
        ),
        "You restricted this to a type of retailer. Merchants carry only a "
        "broad category label (groceries, sporting_goods, electronics, ...) - "
        "which categories should count as acceptable here?",
    ),
]


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_number(raw: str) -> float | None:
    """Parse '1'000', '20', '19.50', '19,50' into a float."""
    if raw is None:
        return None
    s = raw.strip().replace("’", "").replace("'", "")
    if "," in s and "." in s:
        s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = f"{head}.{tail}" if len(tail) in (1, 2) else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _parse_count(raw: str) -> int | None:
    raw = (raw or "").strip().lower()
    if raw in _NUMBER_WORDS:
        return _NUMBER_WORDS[raw]
    try:
        return int(raw)
    except ValueError:
        return None


def _operator_for(cmp_text: str) -> Operator:
    c = re.sub(r"\s+", " ", (cmp_text or "").strip().lower())
    return Operator.LT if c in _STRICT else Operator.LTE


def _find_period(text: str) -> tuple[int, str, tuple[int, int]] | None:
    """Return (period_days, matched_unit_word, span) for the first period phrase."""
    m = _RE_PERIOD.search(text)
    if not m:
        return None
    bare = m.group("bare")
    if bare:
        unit = bare.lower()
        return _PERIOD_DAYS[unit], unit, m.span()
    unit = m.group("unit").lower()
    # The count may be a digit ("every 3 days") or a word ("any seven days").
    raw_n = m.group("n")
    mult = _parse_count(raw_n) if raw_n else 1
    if not mult or mult < 1:
        mult = 1
    return _PERIOD_DAYS[unit] * mult, unit, m.span()


def _money_from(m: re.Match) -> tuple[float | None, str | None]:
    cur = m.groupdict().get("cur_a") or m.groupdict().get("cur_b")
    val = m.groupdict().get("val_a") or m.groupdict().get("val_b")
    currency = _CURRENCY.get((cur or "").strip().lower()) if cur else None
    return _parse_number(val) if val else None, currency


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------

class DeterministicBackend:
    """Regex + rule based. No network, no key, no nondeterminism."""

    name = "deterministic"

    def analyze(self, instruction: str, config: Config) -> BackendResult:
        result = BackendResult(model=None)
        policy: UncertaintyPolicy | None = None

        for clause in split_clauses(instruction):
            found_policy = self._match_uncertainty(clause, result)
            if found_policy is not None:
                policy = found_policy
                continue

            consumed: list[tuple[int, int]] = []

            # Order matters: a count-per-period ("3 times a week") must be tried
            # before the money matchers so it is not mistaken for a bare amount.
            span = self._match_count(clause, result, config)
            if span:
                consumed.append(span)

            span = self._match_amount(clause, result, config, already=consumed)
            if span:
                consumed.append(span)

            if not consumed:
                span = self._match_bare_amount(clause, result)
                if span:
                    consumed.append(span)

            residual = clause
            for start, end in sorted(consumed, reverse=True):
                residual = remove_span(residual, start, end)

            self._match_residual(residual, clause, result, config)

        result.uncertainty_policy = policy
        return result

    # -- matchers ----------------------------------------------------------

    def _match_uncertainty(self, clause: str, out: BackendResult) -> UncertaintyPolicy | None:
        has_trigger = bool(_RE_UNCERTAIN_TRIGGER.search(clause))
        short = len(re.findall(r"[A-Za-z']+", clause)) <= 6

        for regex, policy, label in (
            (_RE_DECLINE, UncertaintyPolicy.DECLINE, "decline"),
            (_RE_ASK, UncertaintyPolicy.ASK, "ask"),
            (_RE_APPROVE, UncertaintyPolicy.APPROVE, "approve"),
        ):
            m = regex.search(clause)
            if not m:
                continue
            if not (has_trigger or short):
                continue
            out.clauses.append(
                Clause(
                    text=clause,
                    bucket=Bucket.UNCERTAINTY_POLICY,
                    reason=(
                        f"Names what to do when the outcome is uncertain "
                        f"(matched {m.group(0)!r}), so it sets uncertainty_policy "
                        f"rather than adding a rule."
                    ),
                    guidance=None,
                    confidence=1.0 if has_trigger else 0.75,
                )
            )
            return policy
        return None

    def _match_count(
        self, clause: str, out: BackendResult, config: Config
    ) -> tuple[int, int] | None:
        m = _RE_COUNT.search(clause)
        if not m:
            return None

        # "CHF 200 a month" is a spending cap, not a count of 200 purchases.
        # Without this guard the count matcher eats the number before the money
        # matcher ever sees it, and the currency is lost entirely.
        before = clause[: m.start("n")].rstrip()
        if re.search(rf"(?:^|[^A-Za-z]) *(?:{_CUR_ALT})$", before, re.IGNORECASE):
            return None

        n = _parse_count(m.group("n"))
        period = _find_period(clause[m.end():])
        if n is None or period is None:
            return None
        period_days, unit, _ = period

        op = _operator_for(m.group("cmp")) if m.group("cmp") else Operator.LTE
        rule, err = safe_rule(
            field="rolling.purchase_count",
            operator=op,
            value=n,
            scope=Scope.PERIOD,
            period_days=period_days,
        )
        span = (m.start(), m.end() + _RE_PERIOD.search(clause[m.end():]).end())
        text = tidy(clause[span[0]:span[1]])

        if rule is None:
            out.clauses.append(
                Clause(text=text, bucket=Bucket.GUIDANCE, reason=f"Could not compile: {err}",
                       guidance=as_guidance(text))
            )
            return span

        out.clauses.append(
            Clause(
                text=text,
                bucket=Bucket.HARD_RULE,
                reason=(
                    f"Explicit count ({n}) over an explicit window ({unit} -> "
                    f"{period_days}d), so it reduces to a countable check against "
                    f"the rolling ledger."
                ),
                rule=rule,
            )
        )
        if unit in _APPROXIMATE_PERIODS:
            out.clauses.append(
                Clause(
                    text=text,
                    bucket=Bucket.OPEN_QUESTION,
                    reason=f"'{unit}' was compiled to a rolling {period_days}-day window.",
                    question=(
                        f"You said '{unit}' for how often. It was compiled to a rolling "
                        f"{period_days}-day window. Did you mean a calendar "
                        f"{_CALENDAR_NAME.get(unit, unit)} that resets on a fixed date, "
                        f"or is a rolling window what you had in mind?"
                    ),
                    confidence=0.6,
                )
            )
        return span

    def _match_amount(
        self, clause: str, out: BackendResult, config: Config,
        already: list[tuple[int, int]],
    ) -> tuple[int, int] | None:
        # (regex, forced_operator). The negated form is tried first: "don't spend
        # more than CHF 200" must compile to <=, not to the > that "more than"
        # would mean on its own.
        for regex, forced in (
            (_RE_AMOUNT_NEG, Operator.LTE),
            (_RE_AMOUNT_PRE, None),
            (_RE_AMOUNT_POST, None),
        ):
            for m in regex.finditer(clause):
                if any(s <= m.start() < e or s < m.end() <= e for s, e in already):
                    continue
                value, currency = _money_from(m)
                if value is None:
                    continue
                if currency is None:
                    return self._unknown_currency(clause, m, out)

                op = forced if forced is not None else _operator_for(m.group("cmp"))
                text = tidy(m.group(0))
                span = m.span()
                period_days = unit = None

                # A window stated AFTER the amount: "CHF 200 a month".
                period = _find_period(clause[m.end():m.end() + 24])
                if period:
                    period_days, unit, pspan = period
                    # Consume the period phrase too, so it does not survive into
                    # the residual as a stray "a month".
                    span = (m.start(), m.end() + pspan[1])
                elif _RE_AGGREGATE.search(clause):
                    # A window stated BEFORE the amount: "keep the total across
                    # any seven days at or below CHF 300". Only consulted when the
                    # clause marks the amount as an aggregate, so a per-purchase
                    # price that merely sits near a frequency ("buy lunch every
                    # day for up to CHF 25") is not turned into a rolling cap.
                    before = _find_period(clause[: m.start()])
                    if before:
                        period_days, unit, pspan = before
                        span = (pspan[0], m.end())

                if period_days is not None:
                    field, scope, pd = "rolling.billing_amount_chf", Scope.PERIOD, period_days
                    text = tidy(clause[span[0]:span[1]])
                    why = (
                        f"Explicit amount and currency, attached to a window "
                        f"({unit} -> {period_days}d), so it is a rolling spend cap."
                    )
                else:
                    field, scope, pd, unit = "authorization.billing_amount_chf", Scope.PURCHASE, None, None
                    why = (
                        "Explicit comparator, amount and currency with no period "
                        "attached, so it is a per-purchase cap - directly comparable."
                    )

                rule, err = safe_rule(
                    field=field, operator=op, value=value,
                    currency=currency, scope=scope, period_days=pd,
                )
                if rule is None:
                    out.clauses.append(
                        Clause(text=text, bucket=Bucket.GUIDANCE,
                               reason=f"Could not compile: {err}", guidance=as_guidance(text))
                    )
                    return span

                out.clauses.append(
                    Clause(text=text, bucket=Bucket.HARD_RULE, reason=why, rule=rule)
                )
                if currency != "CHF":
                    out.warnings.append(
                        f"Rule value is in {currency}, while "
                        f"{field} is CHF-denominated. Function 2 must convert via "
                        f"fx_rates before comparing, and must fail closed (decline) "
                        f"if it cannot."
                    )
                if unit in _APPROXIMATE_PERIODS:
                    out.clauses.append(
                        Clause(
                            text=text, bucket=Bucket.OPEN_QUESTION,
                            reason=f"'{unit}' was compiled to a rolling {pd}-day window.",
                            question=(
                                f"You said '{unit}' for the spending cap. It was compiled "
                                f"to a rolling {pd}-day window. Did you mean a calendar "
                                f"{_CALENDAR_NAME.get(unit, unit)} that resets on a fixed "
                                f"date, or is a rolling window what you had in mind?"
                            ),
                            confidence=0.6,
                        )
                    )
                return span
        return None

    def _match_bare_amount(self, clause: str, out: BackendResult) -> tuple[int, int] | None:
        """A number with a comparator but no currency. Ask; never assume CHF."""
        m = _RE_BARE_AMOUNT.search(clause)
        if not m:
            return None
        raw = m.group("val1") or m.group("val2")
        value = _parse_number(raw)
        if value is None:
            return None
        return self._unknown_currency(clause, m, out, value=value)

    def _unknown_currency(
        self, clause: str, m: re.Match, out: BackendResult, value: float | None = None
    ) -> tuple[int, int]:
        text = tidy(m.group(0))
        if value is None:
            value, _ = _money_from(m)
        out.clauses.append(
            Clause(
                text=text,
                bucket=Bucket.OPEN_QUESTION,
                reason=(
                    "An amount limit was stated without a currency. Assuming one "
                    "would silently change what the customer authorised."
                ),
                question=(
                    f"You set a limit of {value:g} but didn't say which currency. "
                    f"Is that CHF? (The card settles in CHF, so a limit in another "
                    f"currency will be converted at the day's rate.)"
                ),
                confidence=0.9,
                # Blocking: without a currency the amount limit cannot be
                # compared against anything, so no purchase can be evaluated
                # against it. This one genuinely must be answered first.
                blocking=True,
            )
        )
        out.clauses.append(
            Clause(
                text=text,
                bucket=Bucket.GUIDANCE,
                reason="Kept as guidance so the intent survives until the currency is confirmed.",
                guidance=f"Customer stated a limit of {value:g}, currency unconfirmed.",
                confidence=0.9,
            )
        )
        return m.span()

    def _match_residual(
        self, residual: str, source: str, out: BackendResult, config: Config
    ) -> None:
        """Whatever is left after the comparable parts were lifted out."""
        text = as_guidance(residual)
        if not text or not meaningful(text) or is_filler(text):
            return

        tier_b = _RE_TIER_B.search(residual)
        tier_a = _RE_TIER_A.search(residual)

        if tier_b:
            term = tier_b.group(0).lower()
            why, question = _TIER_B[term]
            if config.unresolvable_to in ("guidance", "both"):
                out.clauses.append(
                    Clause(
                        text=tidy(residual),
                        bucket=Bucket.GUIDANCE,
                        reason=(
                            f"Shapes judgment but has no comparable field or threshold. "
                            f"{why}"
                        ),
                        guidance=text,
                        confidence=0.8,
                    )
                )
            if config.unresolvable_to in ("open_question", "both"):
                out.clauses.append(
                    Clause(
                        text=tidy(residual),
                        bucket=Bucket.OPEN_QUESTION,
                        reason=why,
                        question=question,
                        confidence=0.85,
                    )
                )
            return

        # A stated CONDITION with no field behind it. Not a description of what
        # to buy - a gate the customer expects applied. Say so instead of
        # filing it silently as guidance.
        topic_question = next(
            (q for pat, q in _UNENFORCEABLE_TOPICS if pat.search(residual)), None
        )
        if topic_question or _ENFORCEABLE_INTENT.search(residual):
            question = topic_question or (
                f"You asked for: \"{text}\". Nothing the payment system reports "
                f"settles this, so it cannot be enforced as a hard rule. Should "
                f"it block a purchase outright, or is it context for judgment?"
            )
            out.clauses.append(
                Clause(
                    text=tidy(residual),
                    bucket=Bucket.GUIDANCE,
                    reason=(
                        "States a condition the customer expects enforced, but no "
                        "field on the event settles it. Kept as guidance so it can "
                        "still inform judgment."
                    ),
                    guidance=text,
                    confidence=0.8,
                )
            )
            out.clauses.append(
                Clause(
                    text=tidy(residual),
                    bucket=Bucket.OPEN_QUESTION,
                    reason=(
                        "A stated condition with no matching field. Filing it as "
                        "silent guidance would neither enforce it nor admit that "
                        "it cannot be enforced."
                    ),
                    question=question,
                    confidence=0.8,
                    # Non-blocking: Function 2's risk layer enforces most of
                    # these through derived requirements, so the question is
                    # about precision, not about whether the purchase can be
                    # judged at all. Blocking here would re-create the step_up
                    # parade that Bug 5 and Question 1 just removed.
                    blocking=False,
                )
            )
            return

        reason = (
            f"Describes the kind of purchase intended ('{tier_a.group(0)}'), which an "
            f"agent can judge per purchase from item data. No comparable field to "
            f"hang a hard rule on."
            if tier_a
            else "No comparator or measurable field, so it informs judgment rather "
                 "than gating it."
        )
        out.clauses.append(
            Clause(text=tidy(residual), bucket=Bucket.GUIDANCE, reason=reason,
                   guidance=text, confidence=0.9)
        )
