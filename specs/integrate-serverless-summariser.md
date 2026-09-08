# Spec: Wire Autoresearch Tool to the Serverless Summariser

**Status:** Draft
**Scope:** Integration of the deployed serverless summariser (GCP Cloud Functions v2, `cloud_function/`) into the autoresearch evaluation pipeline (`autoresearch/` → `core/run_candidate.py`). No visual/UI changes (`dashboard/` untouched). No changes to `tools/batch_summarize.py` behavior beyond auth dedupe.
**Config files affected by this spec:** `core/cf_client.py` (new), `core/run_candidate.py`, `autoresearch/agent.py`, `tools/batch_summarize.py`, `cloud_function/handler.py`, `cloud_function/main.py`, `.env.example`, `Makefile`, `.gcloudignore`.

---

## 1. Goals

1. Route all chapter-summary LLM generations made by the autoresearch evaluation loop through the deployed Cloud Function, so the local machine never holds the provider API key during optimization runs.
2. Keep run artifacts, scoring, and optimizer decisions **byte-compatible** with direct-API mode. A variant evaluated via CF must produce the same `SummarySample`, `score_dataset` output, and `EvaluationResult.composite_score` as the same variant evaluated directly (seeded generation ⇒ identical `summary_md`).
3. Preserve existing offline/dev paths: direct API mode, mock mode, local `functions-framework` dev server. Mode selection must be explicit and default-safe (a run must never silently switch transport).
4. Preserve judge parity: judge scores produced through CF must match the local `judge_summary_absolute` result modulo the source-truncation parity fix (see §7.4).

## 2. Non-Goals

- Not turning the CF into a generic OpenAI-compatible proxy. The CF stays a chapter-summary endpoint; judge/scoring requests use the CF's own judge path, not a passthrough request body.
- Not changing the permutation store format, optimization strategies (hill-climb/grid-search), or composite formula.
- Not deploying the CF from this spec (deployment runbook already exists: `.notes/setup-cloud-function.md`, `cloud_function/README.md`).
- Not changing the dashboard notes/auto-tagger pipeline.

## 3. Architecture

### 3.1 Component map (CF mode)

```
data/chapter_notes.jsonl
      │
      ▼
autoresearch/agent.py ── flags --function-url / --auth-mode ──┐
      │  (sets FUNCTION_URL, AUTH_MODE in os.environ)         │
      ▼                                                        │
autoresearch/optimizer.py ── subprocess (env inherited) ───────┤
      │  python core/run_candidate.py <variant>                │
      ▼                                                        │
core/run_candidate.py                                          │
      │  make_client() ── FUNCTION_URL set ─────────────────────┘
      ▼
core/cf_client.py  ← NEW. urllib-based transport.
      │  POST {source_md, model, system_prompt, user_prompt,
      │        thinking, use_json_schema, target_words,
      │        score, judge, judge_model, judge_source_char_limit,
      │        rubric, summary_md?}
      │  Authorization: Bearer <OIDC token>   (auth-mode oidc)
      ▼
cloud_function/main.py → cloud_function/handler.py → core/openrouter_client.py
      │                                            → core/judge.py (judge=true)
      │                                            → scoring.py (score=true)
      ▼
HTTP 200 {summary, usage, scoring?, judge_scores?, meta}
      │
      ▼
GenerationResult / SummarySample (identical shape to direct mode)
      → score_dataset() → runs/<run>/manifest.json → optimizer parses composite
```

### 3.2 Transport mode matrix

| Mode | Selection | LLM calls | API key needed locally | Delegate scoring | Delegate judge |
|------|-----------|-----------|------------------------|------------------|----------------|
| **Cloud Function** | `--function-url URL` or env `FUNCTION_URL` non-empty | CF HTTP (server-side) | No | No (local `score_dataset`; CF `score` block informational) | Yes (CF judge path) |
| **Direct API** | no FUNCTION_URL + `LLM_API_KEY` available | OpenRouterClient (urllib) | Yes | — (existing) | Local (existing) |
| **Mock** | `--mock` | none, `extractive_mock_summary` | No | — | Skip |

Precedence: `--mock` > `--function-url`/`FUNCTION_URL` > direct. A run with FUNCTION_URL set but unreachable must fail loudly (items marked FAIL), never silently fall back to direct.

## 4. Cloud Function contract additions (required)

The deployed `cloud_function/handler.py` must gain four backward-compatible fields. All remain stdlib-only.

### 4.1 `summary_md` (optional string) — judge-only / scoring-only mode

When `body["summary_md"]` is a non-empty string the function **skips LLM generation** entirely:
- Validation changes: `system_prompt` and `user_prompt` are required only when `summary_md` is absent/empty.
- Flow: parse rubric → (if `target_words > 0` and rubric present) run `score_sample` on the provided summary as `SummarySample` (passes_used=1) → (if `judge=true`) run `judge_summary_absolute` against the provided summary → return same response shape.
- Semantics: the `model` field is still required but unused for generation; used only to tag `usage.model_id` with the generation model for trace clarity.
- `usage` block: all token fields `0`, `generation_cost` = judge call cost only when judge ran (see `judge_scores` flow already returning the generation cost of the judge call — keep this: judge cost lands in `usage.generation_cost`). Add `usage.judge_generation_cost` mirror (float, 0.0 if no judge).

### 4.2 `score` (optional bool, default true)

When `score: false`, skip the deterministic scoring block even if rubric + `target_words` are present (used by generation passes inside the multi-pass length-control loop to avoid wasted server cycles; final dataset scoring is always computed locally). Response omits `scoring` key when skipped.

### 4.3 `judge_source_char_limit` (optional int, default 32000)

Applied as `source_md[:limit]` inside the judge call. Matches local `core/run_candidate.py --judge-source-char-limit` default (32000). Fixes the current parity gap where the CF judges the full chapter while local mode truncates.

### 4.4 `meta` block in success response

```json
"meta": {
  "scoring_version": "<sha256 of scoring.py at deploy>",
  "handler_version": "<sha256 of cloud_function/handler.py at deploy>",
  "deployed_at_utc": "ISO-8601"
}
```
`run_candidate` CF mode compares `scoring_version` against the local sha256 of `scoring.py`; on mismatch print a WARN and record `scoring_version_mismatch: true` in the run manifest (no abort).

### 4.5 Validation rules (final contract)

| Field | Required | Type | Rules |
|-------|----------|------|-------|
| `source_md` | yes | string | non-empty after strip; also used as judge source (truncated per 4.3) |
| `model` | yes | string | non-empty; generation model (or trace label in judge-only mode) |
| `system_prompt` | conditional | string | required iff `summary_md` absent |
| `user_prompt` | conditional | string | required iff `summary_md` absent |
| `summary_md` | no | string | presence ⇒ judge/scoring-only mode |
| `api_key` / `base_url` | no | string | override precedence unchanged |
| `thinking` | no | bool | default false |
| `use_json_schema` | no | bool | default true |
| `target_words` | no | int | >0 + rubric ⇒ scoring (unless `score:false`) |
| `score` | no | bool | default true |
| `judge` | no | bool | default false; requires `judge_model` + rubric |
| `judge_model` | no | string | default `openai/gpt-4o-mini` |
| `judge_source_char_limit` | no | int | default 32000 |
| `rubric` | no | object | 7 list fields, all optional, same parsing as today |

Error responses unchanged: 405 / 400 / 500 with `{"success": false, "error": "..."}`.

## 5. New module: `core/cf_client.py`

### 5.1 `class CloudFunctionClient`

Stdlib only (`urllib`). Implements the same surface as `OpenRouterClient` for the parts `core/run_candidate.py` and `core/judge.py` use.

```python
CloudFunctionClient(
    *,
    function_url: str,                    # required
    auth_mode: str = "oidc",              # "oidc" | "none" | "env"
    timeout: int = 600,                   # per-request; CF max 3600
    max_retries: int = 3,
    api_key: str = "",                    # optional static bearer token upload
)
```

Methods:
- `chat_completion(payload, *, source_md="", judge=False, judge_model="", judge_source_char_limit=32000, summary_md="", score=True, rubric=None, target_words=0, thinking=None, use_json_schema=None) -> GenerationResult`
  Builds the CF JSON body from the OpenAI-style `payload` (extract `model`, `messages[0].content` → `system_prompt`, `messages[1].content` → `user_prompt`, `extra_body.thinking.type`, `response_format`) merged with the transport kwargs. POSTs. On 2xx:
  - Unwrap `summary.summary_md` → `GenerationResult.summary_md`, `estimated_visible_words` → `estimated_visible_words`, `summary_md` also into `raw_content`, `raw_response` = full JSON.
  - Map `usage` dict → `UsageRecord` (all 10 existing fields are present in CF output).
  - Expose judge outcome: `resp["judge_scores"]` (incl. `rationale`) stored on the `raw_response`; helper `parse_cf_judge_scores(response) -> Optional[(JudgeScores, str rationale)]` (see §5.3).
  - Re-raise provider terminal errors embedded in CF 500 payloads as typed errors: if `error` contains `insufficient credits` → `OpenRouterInsufficientCreditsError`; otherwise `OpenRouterHTTPError(500, path=function_url, ...)`.
- Error taxonomy on transport level (mirrors `OpenRouterClient._request_json`):
  - 401/403 → `OpenRouterHTTPError`; auth layer refreshes token and retries once before raising.
  - 404 → `OpenRouterHTTPError` (function not deployed/wrong region); no retry.
  - 408/429/500/502/503/504 → retry with `min(2**attempt, 8)` backoff up to `max_retries`, then raise.
  - Network error (`URLError`) → `OpenRouterAPIError("Network error for <url>: ...")`, retry same backoff policy.
- `get_credits(*, api_key_override="")` → raises `OpenRouterAPIError("credits endpoint not available in Cloud Function mode")`. This lets the existing `wait_for_credits` fall through to its timed-retry branch unchanged.
- `fetch_models(*, refresh=False)` → raises `OpenRouterAPIError("model catalog not available in Cloud Function mode")`. `run_candidate` must skip catalog/pricing snapshot capture in CF mode (see §6.4).
- `estimate_uncached_cost(...)` → return the provided `usage.generation_cost` (CF already reports server-estimated `uncached_generation_cost`).
- `supports_parameter(...)` → `False`.

### 5.2 Auth

`auth_mode` resolution:

| Mode | Behavior |
|------|----------|
| `oidc` (default when URL scheme is `https://`) | Token sources in order: (1) env `GCP_IDENTITY_TOKEN` (CI), (2) `gcloud auth print-identity-token` subprocess. Token cached; decode `exp` from JWT payload (base64url segment 1); refresh when `exp - now < 300s`. On 401/403 from CF: force refresh once, retry once. `gcloud` missing → raise `OpenRouterAPIError` with install hint. |
| `env` | Send `Authorization: Bearer $GCP_IDENTITY_TOKEN`; missing env → raise. No caching/refresh. |
| `none` | No auth header. Default when URL scheme is `http://` (local `functions-framework`). `https://` + `none` prints a WARN. |

Extension note (out of scope v1): if `google.auth` importable and `GOOGLE_APPLICATION_CREDENTIALS` set, ADC may be used instead of the gcloud subprocess. Keep behind a separate `AUTH_MODE=adc`.

### 5.3 Judge result parsing

```python
def parse_cf_judge_scores(response: Mapping) -> Optional[Tuple[JudgeScores, str]]:
    """Return (scores, rationale) or None. rationale = resp['judge_scores'].get('rationale','')."""
```
Used by `run_candidate` to populate `sample.judge_scores` and `trace["judge_rationale"]` in CF mode (mirrors `tools/batch_summarize.py::_parse_cf_response` which currently drops rationale — fix that too, §8).

## 6. `core/run_candidate.py` integration

### 6.1 CLI flags (new)

| Flag | Default | Purpose |
|------|---------|---------|
| `--function-url` | env `FUNCTION_URL` or `""` | Cloud Function URL; non-empty ⇒ CF mode |
| `--auth-mode` | auto (`oidc` for https, `none` for http) | `oidc` \| `none` \| `env` |
| `--function-timeout` | 600 | HTTP timeout for CF calls |

### 6.2 `make_client(args)`

```
--mock                                   → None
args.function_url or env FUNCTION_URL    → CloudFunctionClient(...)
else                                    → OpenRouterClient.from_env(...)  (unchanged)
```

### 6.3 Generation passes

`invoke_generation(client, request_body, *, mock_source_md, target_words, current_summary_md)`:
- CF mode: call `client.chat_completion(request_body, source_md=mock_source_md, score=False, target_words=0, thinking=<from request>, use_json_schema=<from request>)`. `score=False` on every pass; deterministic scoring is local-only in `run_candidate` CF mode (parity with direct mode where scoring is always local).
- The multi-pass length-control loop (`run_length_controlled_stage`) is byte-identical: each repair pass is one CF request; `passes_used`, `first_pass_summary_md`, checkpoints, and raw_responses (now the CF JSON dicts) behave as today.
- Mock path unchanged (`client is None`).

### 6.4 `judge_if_requested` (CF mode)

When client is a `CloudFunctionClient` and `judge_model` set:
1. POST judge-only request: `{source_md: source_md[:judge_source_char_limit], summary_md, judge: true, judge_model, rubric: <asdict>, score: false, model: <generation model or judge_model>, system_prompt: "", user_prompt: "", use_json_schema: true}`. Emptiness rule per §4.1: prompts optional when `summary_md` present.
2. Parse via `parse_cf_judge_scores`; build `AbsoluteJudgeResult(scores=..., rationale=..., raw_response=cf_json)`.
3. If `resp["judge_error"]` present → log `Judge error: ...` and return `None` (exactly the current local semantics).
4. Resume semantics preserved: judge stays a separate phase after `stage_run` checkpointing; a crash between stage and judge re-runs only the judge call (idempotent, stateless).

### 6.5 Snapshots & manifest

- `capture_openrouter_snapshots`: skip when CF mode (no `/models`); record `generation_transport: "cloud_function"`, `function_url`, `auth_mode`, and `scoring_version_mismatch` in `run_manifest` instead.
- `results.tsv` row: add `transport` column (values `cloud_function` | `direct` | `mock`). Header bump tolerated; `tools/leaderboard.py` reads by column name — verify and update parser if strict.
- `--wait-for-credits`: works via timed-retry fallback; log `[credits] management-key polling unavailable in CF mode`.

## 7. Parity & validation rules

| # | Rule | Mechanism |
|---|------|-----------|
| 7.1 | Same `summary_md` in CF vs direct mode | Same provider/model + `seed=42`/`temperature=0.2`/`max_tokens=8192` — `_build_openrouter_payload` in handler matches `_build_chat_payload` in run_candidate. Acceptance test asserts equality on a slice. |
| 7.2 | Same UsageRecord shape | CF `_usage_dict` already emits all 10 fields read by `_parse_cf_response`. |
| 7.3 | Same dataset scores | Always computed locally from `SummarySample` + rubric via `score_dataset(DEFAULT_SCORING_CONFIG)` in both modes. CF `scoring` block is not consumed by run_candidate. |
| 7.4 | Same judge scores | CF judge delegates to the same `core/judge.py::judge_summary_absolute`; `judge_source_char_limit` (4.3) closes the truncation gap. `judge_model` default identical (`openai/gpt-4o-mini`). |
| 7.5 | Version pinning | `meta.scoring_version` compared to local `scoring.py` sha; mismatch ⇒ WARN + manifest flag. |
| 7.6 | Thinking flag parity | `spec.chapter_stage.extra_body.thinking.type` mapped to CF `thinking` bool in `chat_completion`. |

## 8. `tools/batch_summarize.py` changes (auth + dedupe only)

1. Replace inline `_call_cf` (currently posts **without auth** — 403 against an authenticated deployment) with `core/cf_client.py::CloudFunctionClient` transport so OIDC auth works. Keep `httpx` as the async engine: add optional thin async wrapper in `cf_client` or keep `_call_cf` but have it call the shared `AuthTokenProvider` (token cache lives in `cf_client.AuthTokenProvider` and is importable).
2. `_parse_cf_response` keeps `judge_scores.rationale` (add `rationale` field to the `JudgeScores`-adjacent trace; do not splice into `JudgeScores` — store in `trace`).
3. No other behavioral change; CF-scoring consumption stays as-is for this tool.

## 9. Autoresearch wiring (`autoresearch/agent.py`, `optimizer.py`)

- New flags on `autoresearch/agent.py`: `--function-url`, `--auth-mode`, `--function-timeout`. When set, they write `FUNCTION_URL`/`AUTH_MODE`/`CF_TIMEOUT` into `os.environ` before any `evaluate_variant*` call. `evaluate_variant_tempfile` subprocess inherits env → `core/run_candidate.py` picks CF mode automatically. No logic change in `optimizer.py`.
- `dry_run` path unaffected (no subprocess).
- Temp-dir evaluation: `_create_temp_spec_file` copies spec into tmpdir; CF env vars still inherited (child inherits `os.environ`). The child's `os.getcwd()` is tmpdir but `ROOT` resolves from file path — unchanged behavior.
- Report/permutation-store I/O unchanged; `cost` recorded from CF usage `uncached_generation_cost`.

## 10. Config surface

`.env.example` additions:

```
# ── Cloud Function transport (autoresearch/run_candidate) ──
# FUNCTION_URL=https://us-central1-<PROJECT>.cloudfunctions.net/summarize
# AUTH_MODE=auto                # auto | oidc | env | none
# GCP_IDENTITY_TOKEN=           # static OIDC token for CI (AUTH_MODE=env)
```

`autoresearch/agent.py` docstring + `cloud_function/README.md` updated with the `--function-url` usage examples.

`Makefile` additions:

```makefile
cf-smoke:   ## local functions-framework smoke (no auth)
	$(PYTHON) -m pip show functions-framework >/dev/null || $(PYTHON) -m pip install functions-framework
	functions-framework --target summarize --signature-type http --source cloud_function/main.py --port 8080 &
	$(PYTHON) -c "import json,urllib.request; ..."  # posts sample_request.json, asserts summary key
```

`.gcloudignore`: add `specs/` (docs must not be uploaded in deploy source).

## 11. State flows

| Flow | Behavior (CF mode) |
|------|--------------------|
| New run | `state.json` created with `status: running`, records `function_url`, `auth_mode` (same as today + transport fields). |
| Per-item success | `samples.jsonl` append + `completed_item_keys` update; sample record carries `judge_scores` (CF) and `trace.judge_rationale`. |
| Per-item failure | `[i/n] FAIL item_key: error` printed; item skipped; run continues; `latest_error` updated. |
| Auth token expired mid-run | Per-request refresh (5-min skew); single forced refresh + retry on 401/403; run continues. |
| CF down / network | 3 retries w/ backoff per item, then item FAIL; run continues; resumable via `--resume`. |
| Out of credits | CF 500 "insufficient credits" → `OpenRouterInsufficientCreditsError`; `--wait-for-credits` timed-retry loop (management-key polling N/A); resume path intact. |
| Resume | `--resume <run_id>` skips `completed_item_keys` exactly as today; CF calls are stateless so replay is safe. |
| Empty bench / no notes | Existing early-exit behavior unchanged (`Skipping` / exit 1 with message). |
| Scoring version mismatch | WARN to stderr + manifest flag; run proceeds. |

## 12. Auth & permissions

- CF deployed `--no-allow-unauthenticated`; every request must carry a valid OIDC token (identity token, ~1h TTL). Callers use `gcloud auth print-identity-token` (ADC extension later).
- The CF's provider key lives in Secret Manager only (`llm-api-key`); never sent from the caller (request `api_key` field left empty).
- Local dev: `functions-framework` on `http://localhost:8080` with `AUTH_MODE=none`, `LLM_API_KEY` set locally for the CF process only.
- Do not commit `.env`; keep `GCP_IDENTITY_TOKEN` out of shell history / CI logs (inject via secret store).

## 13. Edge cases

1. **Empty `user_prompt` collision:** judge-only mode relaxes prompt requirement (4.1). Guard: `summary_md` + judge ⇒ prompts optional; `summary_md` + no judge + no rubric ⇒ 400 `judge or rubric+target_words required`.
2. **Empty `summary_md` in judge-only request:** treated as generation request → requires prompts (backward compatible).
3. **Non-JSON CF error body:** `_parse_error_response` fallback (already in openrouter_client) reused; message = raw text.
4. **Token decode failure:** treat as expired → refresh once → raise if still failing.
5. **`thinking` models:** CF timeout 3600s; client timeout 600s default; document `--function-timeout 1200` for thinking-heavy variants.
6. **Large chapters:** `source_md` may exceed CF HTTP body limits? 32MB CF gen2 limit — chapters are ≪; still, client warns when `len(source_md) > 500_000` chars.
7. **Concurrent runs sharing token cache:** `AuthTokenProvider` refresh guarded by a module-level lock (single process; subprocess isolation means each optimizer child refreshes independently — acceptable, ≤1 gcloud call per child per hour).
8. **Concurrent permutation-store writes:** unchanged (atomic tmp+rename already); CF mode adds no new writers.
9. **Direct-mode regression:** when `FUNCTION_URL` unset, all paths behave exactly as today (no CF import required at runtime — lazy import in `make_client`).
10. **Judge error degradation:** CF `judge_error` ⇒ `judge_scores` null, sample still scored deterministically (same as local).

## 14. Performance targets

- Per-request overhead: ≤ 300 ms network + CF cold start (~1–3 s, amortized; gen2 keeps warm instances); LLM latency dominates (30–180 s) — total wall-clock ≈ direct mode.
- Token refresh: ≤ 1 gcloud subprocess per child process per ~55 minutes (cache + 300 s skew).
- Batch concurrency: `tools/batch_summarize.py` default 4, recommended 8 (CF scales per-instance concurrency on Cloud Run).
- Resume overhead: O(completed samples) JSONL re-read, unchanged.
- Cost: identical to direct mode (same provider/model/seed); no extra local LLM calls; judge optional as today.

## 15. Acceptance criteria

1. `AUTH_MODE=none FUNCTION_URL=http://localhost:8080 python core/run_candidate.py --bench chapter_fast --profile 30m_gemini-3.1-flash-lite-preview_notthinking --max-samples 2` produces byte-identical `summary_md` and `±0.0001`-matching usage vs direct mode (same seed).
2. Same command against deployed URL (AUTH_MODE=oidc) succeeds; omitting token → 403.
3. `--judge-model openai/gpt-4o-mini` (CF mode) yields `judge_scores` in `samples.jsonl` and `judge_rationale` in trace; values match local-mode judge within tolerance on 10-sample slice.
4. `--resume` after kill mid-run completes without re-running completed items.
5. `python -m autoresearch.agent --candidate <c> --function-url $FUNC_URL --mode hill_climb --max-iter 1` records permutation entries with CF-usage costs.
6. `score_dataset` output in CF-mode manifest equals direct-mode manifest for the same 10-sample slice (±1e-6).
7. `make cf-smoke` passes against the local dev server.

## 16. Rollout checklist

1. Implement CF handler changes (§4); deploy (`gcloud functions deploy summarize ...`); verify with `cloud_function/sample_request.json` + a judge-only curl.
2. Add `core/cf_client.py`; wire `run_candidate.py` (§6); run local smoke + parity slice (AC 1, 6).
3. Auth test against deployed URL (AC 2); fix batch tool auth (§8).
4. Wire `autoresearch/agent.py` flags (§9); dry-run + 1-iteration hill-climb (AC 5).
5. Update `.env.example`, `Makefile`, `README`s, `.gcloudignore`.
6. Full regression on direct mode (FUNCTION_URL unset) — leaderboard + one full chapter_fast run.