"""Versioned contracts shared by candidate scanning and replay."""

from typing import NotRequired, TypedDict


CANDIDATE_SCORE_LEDGER_SCHEMA = "candidate-score-ledger/v1"
CANDIDATE_TRADE_PLAN_SCHEMA = "candidate-trade-plan/v1"


class CandidateDataQuality(TypedDict, total=False):
    eligible: bool
    reasons: list[str]
    coverage_factor: float
    freshness_factor: float


class Candidate(TypedDict, total=False):
    code: str
    composite_score: float
    quality_adjusted_score: float
    execution_priority_score: float
    data_quality: CandidateDataQuality
    sector_actionable: bool
    sector_capital_evidence: str
    wyckoff: dict
    score_eligible: bool
    observation_reasons: NotRequired[list[str]]
