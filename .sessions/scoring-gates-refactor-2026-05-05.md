# Scoring Gates Refactor Session

**Date:** 2026-05-05
**Tool:** opencode

## Goals

Refactor scoring gates from per-candidate `ScoringGatesOverride` to centralized benchmark manifest profiles.

## Changes Made

### `benchmark_version.json`
Added `scoring_gates` dict with profile-specific gate values:
- `default`: min_faithfulness=0.70, min_concept_coverage=0.60
- `30m`: min_faithfulness=0.50, min_concept_coverage=0.15
- `60m`: min_faithfulness=0.60, min_concept_coverage=0.50

### `core/versioning.py`
Added `scoring_gates` to `DEFAULT_BENCHMARK_MANIFEST` with same profile keys.

### `core/run_candidate.py:2017-2035`
Replaced per-spec `ScoringGatesOverride` logic with profile-based lookup from `benchmark_manifest`:
- `30m_*` profiles → `30m` gate key
- `60m_*` profiles → `60m` gate key
- bare `30m`/`60m` → `default` gate key

### `candidate_spec.py`
- Removed `ScoringGatesOverride` dataclass (lines 88-95)
- Removed `scoring_gates_override: Optional[ScoringGatesOverride] = None` from `CandidateSpec`
- Removed all 28 `scoring_gates_override=ScoringGatesOverride(...)` blocks from individual candidate specs

### `tools/add_candidate.py`
Removed `scoring_gates_override` from generated candidate dict template.

## Verification

All checks passed:
```
30m gates: faithfulness=0.5, coverage=0.15
60m gates: faithfulness=0.6, coverage=0.5
default gates: faithfulness=0.7, coverage=0.6
```

## Example Usage

```bash
python core/run_candidate.py --bench chapter_fast --profile 30m_deepseek_notthinking --benchmark-manifest benchmark_version.json --mock --write-results
```