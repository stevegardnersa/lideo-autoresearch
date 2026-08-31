# Summary: Implemented — Wire Autoresearch Tool to Serverless Summariser

Spec: `specs/integrate-serverless-summariser.md`. Implemented + verified. No deployment performed (out of scope).

## Delivered

| File | Change |
|------|--------|
| `core/cf_client.py` (new) | `CloudFunctionClient` (urllib, stdlib-only), `AuthTokenProvider` (oidc/env/none + JWT exp cache, 300s skew, 401/403 force-refresh-once), `parse_cf_judge_scores`, scoring-version mismatch flag. Raises for `get_credits`/`fetch_models`; `estimate_uncached_cost` returns server cost. |
| `cloud_function/handler.py` | Added judge/scoring-only `summary_md` mode, `score` bool, `judge_source_char_limit`, `usage.judge_generation_cost`, `meta` block (scoring_version/handler_version/deployed_at_utc). Judge now runs outside the scoring gate + source truncation parity fix. |
| `cloud_function/main.py` + **`main.py`** | Conditional prompt validation (optional when `summary_md` present); 400 `judge or rubric+target_words required` when `summary_md` w/o judge/rubric. **Root `main.py` synced** — it is the entrypoint a `gcloud functions deploy --source .` actually picks; leaving it stale would 400 every judge-only request. |
| `core/run_candidate.py` | `--function-url/--auth-mode/--function-timeout` (env defaults FUNCTION_URL/AUTH_MODE/CF_TIMEOUT); `make_client` CF branch (mock > CF > direct); CF branch in `invoke_generation` (score=False per pass) and `judge_if_requested` (judge-only POST → `parse_cf_judge_scores`); snapshot capture skip + manifest `generation_transport/function_url/auth_mode/scoring_version_mismatch/cloud_function_meta`; credits fallback log; results.tsv `transport` column (appended at END so legacy rows stay aligned) + header auto-migration for pre-existing results.tsv. |
| `tools/batch_summarize.py` | CF calls now carry OIDC bearer via shared `AuthTokenProvider` (`--auth-mode`); `judge_rationale` kept in trace. |
| `autoresearch/agent.py` | `--function-url/--auth-mode/--function-timeout` export FUNCTION_URL/AUTH_MODE/CF_TIMEOUT into child env before evaluate_variant* (optimizer.py untouched). |
| `.env.example`, `Makefile` (`cf-smoke`), `.gcloudignore` (`specs/`), `cloud_function/README.md`, `cloud_function/cf_smoke.py` (new) | Config surface + no-LLM-cost smoke. `cf-smoke` uses dynamic port + `LLM_API_KEY=sk-placeholder`, exercises scoring-only + validation; passes. |

## Verified

- CF client vs fake server: request body construction (generation + judge-only), response unwrap, usage mapping, `scoring_version_mismatch`, insufficient-credits → `OpenRouterInsufficientCreditsError`.
- Handler: 6 paths (gen+judge+scoring; judge-only zero-tokens + generation_cost=judge cost; scoring-only; score:false skip; raw-text gen; judge-error degradation).
- Full `run_candidate` CF run vs fake server: transport/auth_mode recorded, judge_scores + rationale in samples/trace, no local `LLM_API_KEY` needed.
- Auth: env/none/auto resolution, missing env token raises, JWT exp decode, batch `_call_cf` sends Bearer.
- `make cf-smoke` passes against BOTH entrypoints (cloud_function/main.py and root main.py).
- Mock/direct regression: `--mock` full run OK; results.tsv legacy rows intact (transport='').

## Deviations / notes from spec

- **Root `main.py`** added to touched files (see above) — not listed in spec §config, required for deployed contract.
- **`transport` column appended after `notes`** (not before) to keep pre-existing results.tsv rows aligned; `append_results_tsv` auto-migrates old headers.
- AC1/AC2/AC3/AC5 parity & deployed-URL checks need real LLM/CF + populated `data/chapter_notes.jsonl` — exercised via fake-server stand-ins; not run against live endpoints (no deploy/credits).
- `make cf-smoke` originally specced with fixed :8080 — made port-dynamic (8080 may be occupied).