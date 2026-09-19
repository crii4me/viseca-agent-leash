"""Function 2 -- decision engine. Purchase event -> approve / decline / step_up."""

from .decide import Decision, decide
from .hard_rules import CurrencyNotConvertible, HardRulesResult, RuleResult, evaluate_hard_rules
from .ledger import Ledger
from .extractor import (
    AnthropicExtractor,
    ExtractedTextFacts,
    KeywordExtractor,
    get_extractor,
)
from .risk import (
    MandateRequirements,
    build_familiar,
    compose_decision,
    decide_full,
    derive_requirements,
    fold_signals,
    gather_signals,
    to_rs_authorization,
)

__all__ = [
    "CurrencyNotConvertible",
    "Decision",
    "HardRulesResult",
    "Ledger",
    "RuleResult",
    "decide",
    "evaluate_hard_rules",
    # composition layer
    "MandateRequirements",
    "build_familiar",
    "compose_decision",
    "decide_full",
    "derive_requirements",
    "fold_signals",
    "gather_signals",
    "to_rs_authorization",
    # quarantined extractor (injection layer)
    "AnthropicExtractor",
    "ExtractedTextFacts",
    "KeywordExtractor",
    "get_extractor",
]
