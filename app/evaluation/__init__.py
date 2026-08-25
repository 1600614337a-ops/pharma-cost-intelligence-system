"""Automated acceptance gates for governed analysis outputs."""

from .llm_golden import (
    GOLDEN_LLM_SCENARIOS,
    evaluate_llm_contract,
    protected_contract_fingerprint,
)

__all__ = [
    "GOLDEN_LLM_SCENARIOS",
    "evaluate_llm_contract",
    "protected_contract_fingerprint",
]
