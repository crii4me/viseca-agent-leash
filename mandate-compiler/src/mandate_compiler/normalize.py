"""Text helpers for clause splitting and tidying. Pure string work, no model calls."""

from __future__ import annotations

import re

# Split points that reliably separate independent constraints in spending
# instructions. Kept deliberately conservative - over-splitting produces clause
# fragments that read badly in guidance text.
_SPLIT = re.compile(
    r"(?:(?<=[.!?])\s+)"          # sentence boundary
    r"|\s*;\s*"                   # semicolon
    r"|,\s+(?:and\s+|but\s+|then\s+)?"  # comma, optionally followed by a conjunction
    r"|\s+\band\s+(?=\w)"         # bare 'and'
    r"|\s+\bbut\s+(?=\w)"         # bare 'but'
    r"|\s+\bfrom\s+(?=a\b|an\b|the\b|shops?\b|stores?\b|merchants?\b)"  # 'from a shop ...'
    r"|\s+\bat\s+(?=a\b|an\b|the\b|shops?\b|stores?\b|merchants?\b)",   # 'at a shop ...'
    flags=re.IGNORECASE,
)

# Leading imperatives to strip when turning a span into readable guidance.
# `\b\s*` rather than `\s+` so a bare trailing verb ("Only buy", left behind when
# a rule was lifted out) reduces to nothing and gets dropped as noise.
# `\b\s*` rather than `\s+`: "Buy only" leaves a bare "only" once the verb is
# stripped, and a trailing-whitespace requirement would let it through as
# guidance reading simply "Only".
_LEAD_ONLY = re.compile(r"^(?:only|just)\b\s*", flags=re.IGNORECASE)
_LEAD_VERBS = re.compile(
    r"^(?:please\s+)?(?:you\s+(?:may|can|should)\s+)?"
    r"(?:only\s+|just\s+)?(?:buy|purchase|order|get|spend|pay|use|shop|book)\b\s*",
    flags=re.IGNORECASE,
)

# Residual fragments that carry no information once the comparable part has been
# lifted out. Emitting these as guidance just adds noise for Function 2 to weigh.
_FILLER = {
    "each time", "every time", "per purchase", "per transaction", "per item",
    "at a time", "a time", "in total", "total", "each", "each one", "apiece",
    "it", "them", "that", "this", "one", "a month", "a week", "a day", "a year",
}

_TRAILING_PREP = re.compile(r"\s+(?:for|from|at|of|on|in|with|to|by)\s*$", flags=re.IGNORECASE)

# Connectives left pointing at nothing once a matched span is cut out of the
# middle of a sentence - "keep the total across |any seven days at or below CHF
# 300|" leaves a trailing "across". Stripped repeatedly, because removing one
# can expose another ("... at or" -> "... at" -> "").
_TRAILING_DANGLE = re.compile(
    r"\s*\b(?:across|over|within|during|under|up|than|at|or|and|but|of|to|for|"
    r"from|in|on|with|by|is|are|be)\s*$",
    flags=re.IGNORECASE,
)

# Sentence scaffolding that carries no instruction of its own. A residual made
# ENTIRELY of these words says nothing the compiled rule does not already say,
# so it is dropped rather than shown to the customer as guidance.
_SCAFFOLD_WORDS = {
    "keep", "kept", "each", "every", "the", "a", "an", "any", "all",
    "total", "totals", "order", "orders", "purchase", "purchases",
    "spend", "spending", "spent", "pay", "paying", "cost", "costs",
    "at", "or", "and", "but", "of", "to", "for", "in", "on", "with", "by",
    "up", "than", "across", "over", "within", "during", "under", "per",
    "is", "are", "be", "it", "them", "that", "this", "no", "not",
}
_LEADING_PREP = re.compile(r"^(?:for|from|at|of|on|in|with|to|by)\s+", flags=re.IGNORECASE)
_WS = re.compile(r"\s+")


def split_clauses(instruction: str) -> list[str]:
    """Break an instruction into candidate clauses.

    Each clause is then classified independently. Empty and punctuation-only
    fragments are dropped.
    """
    parts = _SPLIT.split(instruction or "")
    out: list[str] = []
    for p in parts:
        p = tidy(p)
        if p and re.search(r"[A-Za-z0-9]", p):
            out.append(p)
    return out


def tidy(text: str) -> str:
    """Collapse whitespace and strip stray punctuation from a span."""
    t = _WS.sub(" ", (text or "").strip())
    t = t.strip(" ,.;:!?-")
    return t.strip()


def as_guidance(text: str) -> str:
    """Turn a raw span into a readable guidance line.

    Strips the imperative lead ("Buy one ordinary grocery item" -> "one ordinary
    grocery item") and dangling prepositions left behind when a hard rule was
    lifted out of the middle of a clause.
    """
    t = tidy(text)
    t = _LEAD_VERBS.sub("", t)
    t = _LEAD_ONLY.sub("", t)
    t = _LEADING_PREP.sub("", t)
    t = _TRAILING_PREP.sub("", t)

    # Strip dangling connectives until none are left; removing one can expose
    # the next.
    while True:
        stripped = _TRAILING_DANGLE.sub("", t)
        if stripped == t:
            break
        t = stripped

    t = tidy(t)
    if not t:
        return ""
    return t[0].upper() + t[1:]


def is_filler(text: str) -> bool:
    """True if a residual fragment is pure connective tissue, not guidance.

    Two ways to qualify: an exact match against a known filler phrase, or being
    made up entirely of sentence scaffolding. The second catches leftovers like
    "Keep the total", which reads like guidance but only restates the rule that
    was just extracted from the same clause.
    """
    cleaned = tidy(text).casefold()
    if cleaned in _FILLER:
        return True
    words = re.findall(r"[a-z0-9']+", cleaned)
    return bool(words) and all(w in _SCAFFOLD_WORDS for w in words)


def remove_span(text: str, start: int, end: int) -> str:
    """Cut [start:end) out of text and tidy what is left."""
    return tidy(text[:start] + " " + text[end:])


def meaningful(text: str, min_words: int = 1) -> bool:
    """True if a residual fragment carries enough content to be worth keeping.

    One word is enough: "lunch" and "groceries" are real guidance about what may
    be bought, and dropping them would lose the only category signal in the
    instruction. Pure connectives are filtered by `is_filler` instead.
    """
    words = [w for w in re.findall(r"[A-Za-z0-9']+", text or "")]
    return len(words) >= min_words
