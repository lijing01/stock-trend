# Remove Candidate Trade Plan Display and Gating Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task.

## Goal

Remove the candidate report's execution-plan fields and decisions that are not part of candidate discovery. In `/candidates`, do not display or use target prices, R:R, position sizing, target-source audits, or the complete trade-plan gate. Preserve the reusable trade-plan implementation for other workflows.

## Scope and constraints

- Scope changes to `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`, its focused tests, and the current behavior documentation.
- Keep the existing candidate market/data/Wyckoff/sector gates and recommendation-count limits.
- Keep the existing table-width UI changes intact.
- Do not remove or change `core/candidate_trade_plan.py` or the generic optional trade-plan path in `stock_scanner.py`.
- Do not regenerate golden snapshots.

## Implementation steps

1. Add regression coverage in `tests/test_daily_candidates.py` that proves a candidate without a trade plan can still enter the appropriate recommendation bucket when all remaining gates pass, and that Markdown/HTML candidate reports omit trade-plan, target, R:R, position, and target-audit content even when fixture data contains it.
2. In `daily_candidates.py`, stop passing trade-plan policy during the daily scan, remove candidate-only trade-plan construction/audit/rendering, and remove trade-plan promotion checks and reasons from candidate classification.
3. Remove the candidate report's portfolio-position policy output while retaining recommendation-mode and quantity limits; clean dead labels/helpers and update the observation/report text.
4. Update `SKILL.md` and the optimization design document so they no longer describe candidate trade plans, targets, R:R, or position sizing as current `/candidates` output or eligibility gates. Keep historical/general trade-plan references only where they are explicitly future work or other-workflow scope.
5. Run the focused candidate tests, both required stock-trend quality gates, `git diff --check`, and inspect the final diff/status for scope and preservation of unrelated changes.

## Verification criteria

- A candidate's `/candidates` bucket is determined without `trade_plan`, target, R:R, or position fields.
- Generated Markdown and HTML contain none of the removed candidate-plan labels or values.
- Existing generic trade-plan tests and the previously implemented table-width tests continue to pass.
- No golden snapshot is changed.
