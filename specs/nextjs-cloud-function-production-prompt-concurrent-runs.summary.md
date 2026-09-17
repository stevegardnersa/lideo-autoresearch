# Summary: Next.js Consumer of the `summarize` Cloud Function

Spec written: `specs/nextjs-cloud-function-production-prompt-concurrent-runs.md`. No `design_ref/` assets exist, so no visual/UI contract — integration-only. Consumer: a separate Next.js App Router project. Provider: this repo's GCP Cloud Function v2 `summarize`.

## Core decisions

1. **Production prompt = omit the override fields.** Send `source_md` + `model` + client-composed `user_prompt` only. `handler._resolve_prompt` then resolves `slots.chapter.general.production` from the CF's bundled registry (`registry_sha 986960ed…`, currently `chapter-p16` / fp `f3b9ba81d23cd37c`) and returns `meta.prompt_source="production_default"`. Sending `system_prompt` becomes a `"request"` override; sending `prompt_version` pins a version the **CF's** registry must know (unknown → 422 with `allowed`). `user_prompt` is always required — the CF never composes it. Never cache prompt text: production is promoted Python-side (`sync_prompts.py` + deploy) with no Next.js redeploy, so log `meta.prompt_version/fingerprint/registry_sha`.

2. **Auth is server-only OIDC.** CF is `--no-allow-unauthenticated`; use `google-auth-library` `GoogleAuth().getIdTokenClient(FUNCTION_URL)` (audience = function URL), which caches/refreshes tokens. On GCP use attached-SA ADC; off-GCP use an invoker SA key as a server secret. Refresh once on 401/403. Never expose keys/tokens to the browser or accept `api_key`/`system_prompt_override`/`allow_prompt_override` from it. Node runtime required.

3. **Concurrency, two layers.** (a) Next.js bounded pool over CF requests (default 4, env `CF_CONCURRENCY`, 1–16) with one shared `AbortController`; (b) CF/Cloud Run autoscaling (`--concurrency`, `--max-instances`, verify via describe; gen2 per-instance concurrency is low unless raised). CF calls are stateless with **no idempotency key** — concurrent safe, but retries bill new generations; dedupe identical work and never retry a 200.

4. **Retry taxonomy:** transport/timeout/408/429/500/502/503/504 and model-unavailable → backoff+jitter (max 3 retries); 401/403 → refresh once else abort batch; 400/404/405/422 permanent; 402 insufficient credits → abort batch. Adaptive halving on 429. Cost (`maxBatchCostUsd`) and duration guards.

5. **Partial failure preserved.** Per-item `{ok,id,data|error,attempts,latencyMs}` keyed by caller id, aggregate `status: complete|partial|aborted|budget_exceeded` with cost/token/wall totals. `judge_error` is a degraded, not failed, result.

6. **Route patterns:** single `/api/summarize`; synchronous bounded batch for small N; durable job + poll/SSE for full books because serverless `maxDuration` is far below a large fan-out. Timeout `CF_TIMEOUT_MS ≥ 600s` to avoid duplicate billed generations.

## Coverage

Req/resp schema, rubric shape, reasoning precedence, `summary_md` judge/scoring-only mode, TS data model, validation rules, state flows (loading/empty/error/offline/cancelled), error table, observability, performance targets, a11y notes if progress UI built, server-only env table, tests, 8 acceptance criteria, non-goals, open questions.

## Files

- New: `specs/nextjs-cloud-function-production-prompt-concurrent-runs.md`, this summary.
- No code changed. Source of truth referenced: `cloud_function/handler.py`, `main.py`, `prompts/registry.json`, `core/cf_client.py`, `tools/sync_prompts.py`, `specs/system-prompt-v2-versioned-routing.md`.
