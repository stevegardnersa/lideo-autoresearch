# Summary: Wire Autoresearch Tool to Serverless Summariser

Spec written: `specs/integrate-serverless-summariser.md`. No design refs exist (CLI-only integration, no UI). Spec covers contract, wiring, auth, state flows, edge cases, parity, acceptance criteria.

## Current state (found)

- **Cloud function** (`cloud_function/`, committed 24b08df): GCP CF v2 `summarize` POST endpoint. Validates `source_md/model/system_prompt/user_prompt`, calls OpenRouter via stdlib urllib, parses JSON-schema output, optional deterministic scoring (`rubric`+`target_words`) and LLM judge (`judge:true`). Key resolution: body `api_key` → `LLM_API_KEY` env.
- **Batch worker** (`tools/batch_summarize.py`): already has `--function-url` CF mode BUT posts without auth (broken vs `--no-allow-unauthenticated` deployments), OIDC missing.
- **Autoresearch tool** (`autoresearch/` agent→optimizer→permutation_store, plus `core/run_candidate.py`): evaluation loop subprocesses `core/run_candidate.py <variant>`, which calls OpenRouter **directly from the local machine** via `OpenRouterClient.from_env(LLM_API_KEY)`. This is the gap — optimization runs still need a local API key; the CF is unused by the optimisation loop.
- Env drift: `.env` has `OPENROUTER_API_KEY`; docs/example say `LLM_API_KEY` (commit 4fdb5f8 standardized on LLM_API_KEY).

## Key design decisions in spec

1. **Transport abstraction** — new `core/cf_client.py::CloudFunctionClient` (urllib, stdlib-only) implementing the `OpenRouterClient` surface used by `run_candidate` (`chat_completion`, `get_credits`, `fetch_models`). `run_length_controlled_stage`/`invoke_generation` stay unchanged; multi-pass length-control = one CF request per pass. Lazy import so direct mode untouched.
2. **CF contract additions (backward-compatible)** — (a) `summary_md` field enabling judge-only/scoring-only mode (prompts optional then; needed because judge is a separate phase after stage checkpointing and CF is not a generic proxy); (b) `score:false` flag to skip server scoring on generation passes; (c) `judge_source_char_limit` default 32000 to fix the local-vs-CF judge truncation parity gap; (d) `meta.scoring_version` (scoring.py sha) + `judge_generation_cost` usage field.
3. **Judge parity** — CF mode judges through CF judge path (`parse_cf_judge_scores` helper, rationale preserved in trace; batch tool currently drops rationale — fixed). Deterministic dataset scoring stays local in `run_candidate` CF mode (byte-compatible with direct mode; CF `scoring` block informational only).
4. **Auth** — `AUTH_MODE` auto/oidc/env/none; oidc = cached `gcloud auth print-identity-token` (300s expiry skew, forced refresh+retry once on 401/403), `GCP_IDENTITY_TOKEN` env for CI; `none` for local functions-framework on :8080. Batch tool auth fixed to reuse shared token provider.
5. **Mode matrix** — `--mock` > `--function-url`/`FUNCTION_URL` > direct; no silent fallback. `run_candidate` gains `--function-url/--auth-mode/--function-timeout`; `autoresearch/agent.py` gains passthrough flags that export env into subprocess evaluations (no optimizer.py logic change).
6. **Edge cases speced** — 402-insufficient-credits surfaced from CF 500 wrapper → typed error → existing `wait_for_credits` timed-retry fallback; 401/403 refresh; token expiry mid-run; resume idempotency (stateless CF calls); scoring version mismatch = WARN + manifest flag, not abort; >500K-char source warning.

## Config / files

- New: `core/cf_client.py`; `specs/`.
- Touched: `cloud_function/handler.py`, `core/run_candidate.py`, `autoresearch/agent.py`, `tools/batch_summarize.py`, `.env.example`, `Makefile` (`cf-smoke` target), `.gcloudignore` (add `specs/`), README docs.
- Acceptance: 7 criteria — byte-identical summaries vs direct mode (seed=42), judge parity on slice, resume, optimizer e2e, `make cf-smoke`.

Rollout: CF deploy → client → local smoke/parity → auth test → agent wiring → direct-mode regression.