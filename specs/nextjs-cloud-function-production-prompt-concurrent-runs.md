# Spec: Next.js Consumer of the `summarize` Cloud Function — Production Prompt + Concurrent Runs

**Consumer:** a separate Next.js (App Router) application.
**Provider:** the GCP Cloud Function v2 `summarize` in this repo (`cloud_function/`).
**Scope:** server-side integration contract only. No UI is specified here (no `design_ref/` exists); anything visual is out of scope. The spec is what the Next.js project cannot infer from the CF source: request/response semantics, production-prompt resolution, auth, bounded-concurrency orchestration, retries, partial failure, cost/time guards, and state flows.

**Contract snapshot** (pin this in the consumer repo): CF bundle registry `registry_sha = 986960edf2d0d37b3513bd7c54526586bad6bec63d42416c18f77d7e110bc713`, `slots.chapter.general.production = chapter-p16` (label `base: readable`, fingerprint `f3b9ba81d23cd37c`). Source of truth: `cloud_function/handler.py`, `cloud_function/main.py`, `cloud_function/prompts/registry.json`, `core/cf_client.py`.

---

## 1. Goals

1. Let the Next.js app summarise chapters through the CF **without holding the OpenRouter key** on the browser or client.
2. Use the CF's **production system prompt** by default, resolved at request time from the CF's bundled registry — the app never hardcodes prompt text and never needs redeploying when production is promoted.
3. Run **many chapter summaries concurrently** with a bounded, abortable, retrying orchestration layer suitable for Next.js server runtimes.
4. Produce deterministic, auditable results: every response records which prompt version/fingerprint actually served it, plus token/cost/usage.
5. Fail loudly and in a typed way (never silently fall back to a stale prompt or an unauthenticated call).

## 2. Non-Goals

- Authoring, editing, promoting, or syncing prompt versions (that is the Python repo's `tools/prompts_api.py` / `tools/sync_prompts.py` and Settings > Prompts UI).
- Re-implementing deterministic scoring or the LLM judge in Next.js. The CF returns `scoring` / `judge_scores`; treat them as read-only data.
- Browser-side direct calls to the CF (token exposure; CORS; key handling). All CF traffic goes through Next.js server code.
- Streaming token-by-token generation. The CF returns a complete JSON body; only *batch progress* may be streamed (see §9).

## 3. Architecture

```
Browser
  │  same-origin fetch (Next.js auth/session)
  ▼
Next.js server (Node runtime)
  ├─ route handler(s): /api/summarize, /api/summarize/batch, /api/summarize/jobs/:id
  ├─ lib/cf/oidc.ts          → google-auth-library ID-token client
  ├─ lib/cf/client.ts        → summarizeChapter()  (single CF call, typed)
  └─ lib/cf/pool.ts          → summarizeChapters() (bounded concurrency, retries)
  │  HTTPS POST + Authorization: Bearer <OIDC id token>
  ▼
GCP Cloud Function v2 "summarize"  (--no-allow-unauthenticated)
  ├─ resolves system prompt (production default or prompt_version)
  ├─ calls OpenRouter, optional scoring/judge
  └─ returns JSON
```

**Runtime:** use the **Node.js runtime** for route handlers (`export const runtime = 'nodejs'`). The Edge runtime lacks `google-auth-library` and long-running request support.

**Trust boundary:** `FUNCTION_URL`, credentials, `CF_CONCURRENCY`, timeouts, and retry settings are server-only env vars. Never prefix them `NEXT_PUBLIC_`. The browser sends only user content (chapter text, model choice, target words); the server validates and composes the CF request. Never accept `api_key`, `system_prompt_override`, or `allow_prompt_override` from the browser.

## 4. Endpoint & transport

- **URL:** the deployed function's root URL, e.g. `https://<region>-<project>.cloudfunctions.net/summarize`. POST to the URL exactly as returned by deploy; the Python smoke server posts to `/`.
- **Method:** `POST` only. `GET`/etc → `405 {"success": false, "error": "Method GET not allowed"}`.
- **Headers:** `Content-Type: application/json`; `Authorization: Bearer <OIDC ID token>` (unless the deployment is intentionally unauthenticated for local dev).
- **Body:** JSON object; UTF-8. Practical ceiling 32 MB (GCP request limit). Keep chapter payloads well under (recommend ≤ 500 000 chars per `source_md`; cf. `core/cf_client.py` warning).
- **Response:** JSON object, HTTP 200 on success. Error shape is always `{ "success": false, "error": string, ... }`.

## 5. Authentication & permissions

The function is deployed `--no-allow-unauthenticated`; every call needs a Google-issued OIDC **ID token** whose audience is the function URL.

### 5.1 Next.js on GCP (Cloud Run / App Hosting / GCE)

Use Application Default Credentials via the attached service account. Grant it:

```
gcloud functions add-invoker-policy-binding summarize \
  --region REGION --project PROJECT \
  --member "serviceAccount:<nextjs-runtime-sa>@PROJECT.iam.gserviceaccount.com"
```

(Invoker can also be granted on the underlying Cloud Run service with `roles/run.invoker`.)

### 5.2 Next.js off GCP (Vercel, containers, etc.)

Create a dedicated invoker service account and store its JSON key as a server secret (`GOOGLE_APPLICATION_CREDENTIALS` or `GCP_SA_KEY_FILE`):

```
gcloud iam service-accounts create nextjs-cf-invoker --project PROJECT
gcloud functions add-invoker-policy-binding summarize \
  --region REGION --project PROJECT \
  --member "serviceAccount:nextjs-cf-invoker@PROJECT.iam.gserviceaccount.com"
gcloud iam service-accounts keys create ./cf-invoker-key.json \
  --iam-account nextjs-cf-invoker@PROJECT.iam.gserviceaccount.com
```

Key must be mounted/read only server-side. Rotate on a schedule.

### 5.3 Token minting in Node

Use `google-auth-library`'s `IdTokenClient`; it caches the token and refreshes near expiry, and attaches `Authorization` to `request()`:

```ts
import { GoogleAuth } from 'google-auth-library';

let cached: { url: string; client: Awaited<ReturnType<GoogleAuth['getIdTokenClient']>> } | null = null;

export async function cfAuthClient(functionUrl: string) {
  if (cached?.url === functionUrl) return cached.client;
  const auth = new GoogleAuth({ scopes: [] }); // ADC or GOOGLE_APPLICATION_CREDENTIALS
  const client = await auth.getIdTokenClient(functionUrl); // audience = function URL
  cached = { url: functionUrl, client };
  return client;
}
```

- Audience **must** be the function URL (`CF_IDENTITY_AUDIENCE` default in the Python client). A mismatched audience → `401/403`.
- On `401/403`, force a fresh token once and retry the single request; do not loop. If it fails again, surface an auth error and pause the batch (the whole run is blocked until credentials are fixed — match the Python client's "do not mark every sample failed" behavior).
- Cache the client per process; do not mint a token per request.

### 5.4 Local development

Run the Python dev server with a placeholder key and no auth:

```
functions-framework --target summarize --signature-type http \
  --source cloud_function/main.py --port 8080
```

Set `FUNCTION_URL=http://localhost:8080` and skip the `Authorization` header when the URL is not `https`. Emit a startup warning if `https` is paired with no credentials.

## 6. Using the production prompt (primary path)

The CF owns the production system prompt. The Next.js app's job is to **omit** the fields that would override it.

### 6.1 Default request (production prompt)

Send `model`, `source_md`, and `user_prompt`. Do **not** send `system_prompt`, `system_prompt_override`, `prompt_version`, or `allow_prompt_override`.

`handler._resolve_prompt` then falls through to:

```
slots["chapter"]["general"]["production"]   // currently "chapter-p16"
```

and returns `meta.prompt_source = "production_default"`, `meta.prompt_version`, `meta.prompt_fingerprint`, `meta.prompt_registry_sha`.

**The `user_prompt` is always required and always client-composed** (it embeds the chapter and per-request numbers like `target_words` / sentence budget). The production system prompt does not remove this obligation. Compose it in the Next.js server; the CF is a pure inference endpoint and never composes prompts.

> If the consumer previously sent its own `system_prompt`, that path is a **request override** (`meta.prompt_source = "request"`). Remove it to actually get production.

### 6.2 Pinning a specific version

To freeze a version regardless of what production points at, send `prompt_version` (and optionally `prompt_fingerprint`):

```json
{ "model": "deepseek/deepseek-v4-flash", "source_md": "...", "user_prompt": "...",
  "prompt_stage": "chapter", "prompt_version": "chapter-p16",
  "prompt_fingerprint": "f3b9ba81d23cd37c", "target_words": 400 }
```

- The CF resolves the text from **its own bundled registry**, not the version id you might have locally.
- If the id is unknown to the deployed CF and override is not allowed → **HTTP 422** `{success:false, error:"unknown_prompt_version", allowed:[...ids]}`. Treat as a permanent configuration error; do not retry; log `allowed` to make fixing the pin trivial.
- A client `prompt_fingerprint` mismatch is **non-fatal**: the CF proceeds with its registry text and sets `meta.prompt_warning = "fingerprint_mismatch"`. Log it.

### 6.3 Experimental inline override (do not expose to browser)

New prompts can be tested on the live CF without redeploy by sending `system_prompt_override` + `allow_prompt_override: true`. This is an operator/admin capability only. If the Next.js app must support it, gate it behind a server-side allowlist/env flag, never a user field, because it lets a caller run arbitrary text as the system prompt on your billed account.

### 6.4 Production prompt drift

Since production is promoted on the Python side (`Set production` → `tools/sync_prompts.py` → deploy), the CF's `registry_sha`/`prompt_version` can change without any Next.js deploy. Therefore:

- Never cache rendered prompt text in the Next.js app.
- Log `meta.prompt_version` + `meta.prompt_fingerprint` + `meta.prompt_registry_sha` with every result.
- Optionally surface them in an internal admin view so drift is visible.

### 6.5 Other stages

`prompt_stage` selects `chapter` (default) or `composer`. Anything else → `400 invalid_prompt_stage`. `slots.composer.general.production` is currently `composer-p1`. For chapter summarisation, leave `prompt_stage` unset or `"chapter"`.

## 7. Request contract

| Field | Type | Required | Notes |
|---|---|---|---|
| `source_md` | string | yes | Chapter markdown. Non-empty. |
| `model` | string | yes | OpenRouter model id, e.g. `deepseek/deepseek-v4-flash`. |
| `user_prompt` | string | yes* | *Client-composed; required unless `summary_md` mode. |
| `system_prompt` | string | no | Inline system prompt. **Sets `prompt_source="request"`, bypassing production.** |
| `prompt_stage` | `"chapter"\|"composer"` | no | Default `chapter`. |
| `prompt_version` | string | no | Registry id to resolve server-side. |
| `prompt_fingerprint` | string | no | Drift check only (`sha256[:16]`). |
| `system_prompt_override` | string | no | Honored only with `allow_prompt_override=true`. |
| `allow_prompt_override` | bool | no | Default `false`. |
| `target_words` | int | no | >0 + `rubric` + `score:true` triggers deterministic scoring. |
| `score` | bool | no | Default `true`. Set `false` to skip server scoring. |
| `rubric` | object | no | See §7.1. Required for scoring and the judge. |
| `judge` | bool | no | Default `false`. Adds a second LLM call. |
| `judge_model` | string | no | Default `openai/gpt-4o-mini`. |
| `judge_source_char_limit` | int | no | Default `32000`. Truncates source fed to judge. |
| `summary_md` | string | no | Non-empty → skip generation; judge/scoring-only mode. |
| `base_url` | string | no | Provider base; omit to use CF env. |
| `api_key` | string | no | **Do not send from Next.js.** CF uses its own secret. |
| `thinking` | bool | no | Legacy reasoning toggle. Omit → provider default. |
| `reasoning` | object | no | OpenRouter-native, e.g. `{"effort":"high"}`. |
| `reasoning_effort` | string | no | Scalar tier; `"none"` disables. |
| `use_json_schema` | bool | no | Default `true`; recommended. |

### 7.1 Rubric shape

```json
{
  "headings": [], "core_concepts": [], "mechanisms_or_explanations": [],
  "critical_qualifiers": [], "important_examples": [],
  "key_entities_or_numbers": [], "key_terms": []
}
```

All values arrays of strings. Build/cache rubrics ahead of time; they do not change between runs of the same source.

### 7.2 Reasoning rules

Exactly one reasoning signal is effective. Precedence: `reasoning` → `reasoning_effort` → `thinking`; none → no `reasoning` field (provider default). For models in `candidate_spec.MANDATORY_REASONING_MODELS` (e.g. `stepfun/step-3.7-flash`, `stepfun/step-3.5-flash`), a disable request (`"none"` / `enabled:false`) is rejected client-side/should be rejected before sending. The Next.js layer should validate this if it exposes reasoning controls.

### 7.3 `summary_md` (judge/scoring-only) mode

When `summary_md` is non-empty, the server skips generation. Validation `main.summarize` enforces at least one of:
- `judge:true`, or
- `rubric` **and** `target_words > 0` (with `score:true`).

Otherwise → `400 judge or rubric+target_words required when summary_md is provided`. In this mode `system_prompt`/`user_prompt` are optional.

## 8. Concurrent runs

Two independent concurrency layers must both be configured:

1. **Next.js → CF fan-out** (this app): how many CF requests are in flight at once. Bounded by the app.
2. **CF/Cloud Run autoscaling** (server): how many requests the Function accepts in parallel before queueing. Controlled at deploy time (`--concurrency`, `--max-instances`).

### 8.1 CF request is stateless and independent

Each `POST` is a complete, independent generation. There is **no server-side idempotency key** and no shared state between requests. Therefore:

- Running N requests concurrently is safe; results are independent.
- A retried request generates and bills a **new** OpenRouter call. Retries are safe for correctness but cost money. Never retry a request that returned HTTP 200.
- Deduplicate identical work units (same `source_md` hash + `model` + `prompt_version` + `target_words` + rubric) **before** dispatch.

### 8.2 Bounded pool (Next.js side)

Implement `summarizeChapters(items, options)` with a semaphore-style pool (or `p-limit`):

```ts
const results = await pool(
  chapters,
  options.concurrency,          // default from CF_CONCURRENCY, fallback 4
  (chapter, signal) => summarizeChapter({ ...chapter, signal }, options),
);
```

- Default concurrency **4**; allow `1..16`. Values above ~16 rarely help: OpenRouter provider rate limits, per-account credit throughput, and CF instance caps dominate. Make it env-configurable (`CF_CONCURRENCY`), not a UI field.
- Every in-flight call shares a per-batch `AbortController`. Caller cancellation aborts all queued and in-flight work; an aborted item must not be retried.
- Concurrency is per-Next.js-instance. With multiple serverless instances the effective fan-out is `instances × CF_CONCURRENCY`; size CF capacity accordingly.

### 8.3 Retry policy

| Class | Examples | Action |
|---|---|---|
| Transport / timeout | `URLError`, `AbortError` (non-cancel), socket hangup | Retry with backoff. |
| Transient HTTP | 408, 429, 500, 502, 503, 504 | Retry with exponential backoff + full jitter, max 3 attempts. |
| Model unavailable | gateway message "no healthy deployments" / "no fallback model group" | Retry on a longer bounded backoff (15/30/60/120 s). |
| Auth | 401/403 | Force-refresh OIDC token once; if still failing, abort batch (do not burn through all items). |
| Permanent | 400, 404, 405, 422 | **No retry.** Surface immediately. 422 aborts the batch (config error). |
| Billing | 402 / "insufficient credits" | Abort the entire batch; surface a typed `insufficient_credits` error; stop dispatching. |

Backoff base: `min(2**attempt, 8)s` with jitter, matching `core/cf_client.py`. Cap total attempts at 4. Do not retry `AbortError` caused by caller cancellation.

### 8.4 Timeouts

- CF deployment timeout can be up to `3600s`; a single generation may take 30–180 s, and thinking models 5–10 min.
- Set the Next.js HTTP client timeout to **≥ 600 s** (`CF_TIMEOUT_MS`, default `600000`). A shorter timeout causes duplicate billed generations via retries.
- Serverless platform function-duration limits (e.g. Vercel `maxDuration`) are usually shorter than a large batch. See §9 for the job pattern; do not attempt a 60-chapter synchronous fan-out on a 300 s platform limit.

### 8.5 Adaptive backpressure & cost guards

- On repeated `429`s, halve the effective concurrency for the remainder of the batch (floor 1) and recover slowly. Never exceed the configured max.
- Track `usage.generation_cost` per item. If `maxBatchCostUsd` is set and exceeded, stop dispatching queued items and return a `budget_exceeded` batch status.
- Track wall-clock; if `maxBatchDurationMs` is exceeded, stop dispatching and mark remaining items `skipped`.
- Record `meta.scoring_version` vs the consumer's expectation; a mismatch is a warning, not an abort (mirror the Python client). Optionally flag in the result.

### 8.6 Result ordering & partial failure

- Accept a caller-supplied `id` per item; key results by `id`. Never rely on response array order.
- Return a per-item envelope and never throw away successes because one item failed:

```ts
type BatchItemResult<T> =
  | { ok: true; id: string; data: T; attempts: number; latencyMs: number }
  | { ok: false; id: string; error: CfError; attempts: number; latencyMs: number };

interface BatchResult<T> {
  status: 'complete' | 'partial' | 'aborted' | 'budget_exceeded';
  items: BatchItemResult<T>[];
  totals: { requested: number; succeeded: number; failed: number; skipped: number;
            generationCostUsd: number; totalTokens: number; wallMs: number };
}
```

- `partial` = some items succeeded, some failed permanently. `aborted` = auth/402/batch cancellation. `budget_exceeded` = guard tripped.

### 8.7 CF capacity (server-side, verify & document)

The function must accept whatever fan-out the app produces. Before enabling high concurrency, confirm the deployed values (via `gcloud functions describe` / Cloud Run console) for:

- **Per-instance request concurrency** (`--concurrency`). Gen2 defaults are low/serial unless explicitly raised; if it is `1`, each in-flight request consumes a whole instance.
- **Max instances** (`--max-instances`): caps parallel throughput and cost.
- **Min instances** (`--min-instances`): avoids cold-start latency under burst.

Deploy guidance for concurrent batch use:

```
gcloud functions deploy summarize --gen2 --runtime python312 --trigger-http \
  --entry-point summarize --source . --region REGION \
  --set-secrets 'LLM_API_KEY=llm-api-key:latest' \
  --timeout 3600 --memory 1024Mi \
  --concurrency 8 --max-instances 20 \
  --no-allow-unauthenticated
```

Sizing rule: `app_concurrency × expected_concurrent_batches ≤ max-instances × per-instance-concurrency` minus headroom. If the app fans out faster than the function scales, requests queue and may hit platform/gateway timeouts. Have the Next.js pool back off (§8.5) rather than unbounded-retry against a saturated function.

## 9. Next.js route patterns

### 9.1 Single summary

`POST /api/summarize` → validates body, calls `summarizeChapter`, returns `{ ok, summary, usage, meta, scoring?, judge_scores? }`.

- `export const runtime = 'nodejs'`.
- `export const maxDuration = <platform max>`; for a single chapter this is usually enough.
- Do not expose a raw pass-through of the CF URL; validate and re-shape.

### 9.2 Synchronous batch (small N)

`POST /api/summarize/batch` (body: `{ items: [...] }`) runs the bounded pool and returns `BatchResult`. Use only when `N` is small enough that `N / concurrency × p95_latency` fits the platform duration limit.

### 9.3 Durable job (large N)

For a full book (dozens–hundreds of chapters):

1. `POST /api/summarize/jobs` → validates, persists a job record `{id, status:'queued', items:[{id,status}], totals}`, returns `202 {jobId}`.
2. Worker (queue consumer, Cloud Task, or `waitUntil`/background function) runs the bounded pool, updating per-item status. An in-memory map is **not** durable across serverless invocations; use a database/Redis/KV or a real queue.
3. `GET /api/summarize/jobs/:id` → status + partial results (polling).
4. `GET /api/summarize/jobs/:id/events` → SSE progress stream (optional): emit `item` (started/succeeded/failed) and `done` events.
5. `POST /api/summarize/jobs/:id/cancel` → abort controller; mark in-flight/queued as cancelled; completed results are kept.

Persist enough per item to resume: `id`, input hash, status, attempts, result or error, `meta.prompt_version`, `meta.prompt_fingerprint`, usage/cost. Because the CF is stateless, resuming means only re-dispatching non-succeeded items (accepting that they bill again).

## 10. Response contract

Success (HTTP 200):

```jsonc
{
  "success": true,
  "summary": { "summary_md": "string", "estimated_visible_words": 412 },
  "usage": {
    "prompt_tokens": 1234, "completion_tokens": 567, "total_tokens": 1801,
    "reasoning_tokens": 0, "cached_prompt_tokens": 0,
    "generation_cost": 0.000123, "uncached_generation_cost": 0.000123,
    "generation_id": "gen-abc123", "provider_name": "DeepSeek",
    "model_id": "deepseek/deepseek-v4-flash",
    "judge_generation_cost": 0.0
  },
  "meta": {
    "scoring_version": "<sha256 of scoring.py at deploy>",
    "handler_version": "<sha256 of handler.py at deploy>",
    "deployed_at_utc": "ISO-8601",
    "prompt_registry_sha": "986960ed…",
    "prompt_version": "chapter-p16",
    "prompt_fingerprint": "f3b9ba81d23cd37c",
    "prompt_source": "production_default",   // registry | inline_override | request | production_default
    "prompt_warning": ""                      // e.g. fingerprint_mismatch
  },
  "scoring": { /* present only when rubric + target_words + score:true */
    "hard_fail": false, "hard_fail_reasons": [],
    "deterministic": { "visible_words": 412, "final_length_error_pct": 3.0,
                       "final_length_accuracy": 0.97, "heading_coverage": 0.85,
                       "concept_phrase_coverage": 0.78, "mechanism_phrase_coverage": 0.72,
                       "qualifier_phrase_coverage": 0.88, "key_term_coverage": 0.82,
                       "number_coverage": 0.0, "redundancy_score": 0.95,
                       "structure_proxy": 0.80, "faithfulness_proxy": 0.78,
                       "concept_proxy": 0.81 /* + readability, first_pass_accuracy, … */ },
    "resolved_faithfulness": 0.78, "resolved_concept_coverage": 0.80,
    "resolved_qualifier_preservation": 0.88, "resolved_no_fluff": 0.92,
    "resolved_structure_quality": 0.83, "quality": 0.82, "utility": 0.79
  },
  "judge_scores": { /* present only when judge:true and it succeeded */
    "faithfulness": 0.85, "concept_coverage": 0.80,
    "qualifier_preservation": 0.90, "no_fluff": 0.88,
    "structure_quality": 0.82, "rationale": "…"
  },
  "judge_error": "string"  // present when judge:true but the judge failed
}
```

- `deterministic` key set is broader than the sample above; type it as `Record<string, number|boolean|string[]>` or a permissive `unknown` and narrow only the keys you display.
- A `judge_error` does **not** fail the request: generation succeeded. Surface it as a degraded result.
- Missing `scoring` means inputs were insufficient (no rubric or `target_words<=0` or `score:false`), not an error.

## 11. Error taxonomy

| HTTP | `error` | Meaning | Next.js handling |
|---|---|---|---|
| 400 | `Invalid JSON body` / `Request body must be a JSON object` | Malformed | Permanent; fix request. |
| 400 | `Missing required fields: …` | Validation | Permanent; surface field names. |
| 400 | `judge or rubric+target_words required when summary_md is provided` | Bad summary_md mode | Permanent. |
| 400 | `invalid_prompt_stage` | Bad stage | Permanent. |
| 405 | `Method … not allowed` | Wrong verb | Bug; fix. |
| 422 | `unknown_prompt_version` (+ `allowed`) | CF registry lacks the id | Permanent; abort batch; log `allowed`. |
| 422 | `missing_system_prompt` | No prompt available | Permanent. |
| 401/403 | — | Auth/audience/permission | Refresh token once; else abort batch. |
| 402 | insufficient credits (wrapped) | Provider billing | Abort batch; typed error; alert. |
| 404 | — | Wrong URL / not deployed | Permanent; fix config. |
| 429 | — | Provider/gateway rate limit | Backoff + adaptive concurrency. |
| 500 | `Internal error: …` | Unhandled server error | Retry; capture `generation_id` if present. |
| 502/503/504 | — | Gateway/instance | Retry with backoff. |

Map all of the above into a discriminated `CfError` union: `{ kind: 'validation'|'auth'|'permanent'|'insufficient_credits'|'model_unavailable'|'rate_limit'|'transient'|'timeout'|'cancelled', status?, message, allowed?, raw? }`.

## 12. Data model (TypeScript)

```ts
type PromptStage = 'chapter' | 'composer';
type PromptSource = 'production_default' | 'registry' | 'inline_override' | 'request';

interface SummarizeRequest {
  source_md: string;
  model: string;
  user_prompt: string;
  // production prompt: omit system_prompt/override/version
  prompt_stage?: PromptStage;
  prompt_version?: string;
  prompt_fingerprint?: string;
  target_words?: number;
  score?: boolean;
  rubric?: Rubric;
  judge?: boolean;
  judge_model?: string;
  judge_source_char_limit?: number;
  summary_md?: string;
  reasoning?: Record<string, unknown>;
  reasoning_effort?: string;
  thinking?: boolean;
  use_json_schema?: boolean;
}

interface SummaryMeta {
  scoring_version: string; handler_version: string; deployed_at_utc: string;
  prompt_registry_sha: string; prompt_version: string;
  prompt_fingerprint: string; prompt_source: PromptSource; prompt_warning: string;
}

interface SummaryUsage {
  prompt_tokens: number; completion_tokens: number; total_tokens: number;
  reasoning_tokens: number; cached_prompt_tokens: number;
  generation_cost: number; uncached_generation_cost: number;
  generation_id: string; provider_name: string; model_id: string;
  judge_generation_cost?: number;
}

interface SummarizeResponse {
  success: true;
  summary: { summary_md: string; estimated_visible_words: number };
  usage: SummaryUsage;
  meta: SummaryMeta;
  scoring?: ScoringBlock;
  judge_scores?: JudgeScores;
  judge_error?: string;
}
```

## 13. Validation rules (reject before dispatch)

- `source_md`: non-empty string; enforce a configurable max length (recommend 500 000 chars) before building JSON.
- `model`: non-empty; if the app offers a model list, allowlist it server-side.
- `user_prompt`: required unless `summary_md` mode; non-empty.
- `target_words`: integer ≥ 0 (0 = no numeric target).
- Scoring requires `rubric` non-null **and** `target_words > 0` **and** `score !== false`.
- `judge:true` requires `rubric`; `judge_model` defaults when absent.
- `summary_md` mode requires `judge:true` or (`rubric` + `target_words>0`).
- `prompt_stage` ∈ `{chapter, composer}`.
- At most one of `reasoning` / `reasoning_effort` / `thinking` is treated as authoritative (precedence §7.2); reject disabling reasoning for mandatory-reasoning models.
- Reject any browser-supplied `api_key`, `base_url`, `system_prompt_override`, `allow_prompt_override` unless an explicit server-side admin path allows it.

## 14. State flows

Per item:

```
queued → dispatching → (awaiting) → succeeded
                       ↘ retrying (n/3) → succeeded | failed_permanent
                       ↘ cancelled
```

Batch:

```
idle → validating → running → (partial) → complete
                     ↘ aborted          (auth / insufficient credits / cancel)
                     ↘ budget_exceeded  (cost or duration guard)
```

- **Loading:** dispatch at bounded concurrency; expose per-item status; never block the whole batch on one slow item beyond the timeout.
- **Empty:** zero items → immediately `complete` with empty `items`; do not call the CF.
- **Error:** surface typed errors; permanent items fail without retry; transient retried; batch continues after a single item failure.
- **Offline (server can't reach CF):** `URLError`/DNS failure → transient retry, then `failed`. Do not spin; confirm batch status reflects network outage distinctly from provider errors.
- **Cancelled:** abort in-flight; queued items → `cancelled`; no retries.

## 15. Observability

Log per CF call: correlation/batch id, item id, `model`, `target_words`, `meta.prompt_version`, `meta.prompt_fingerprint`, `meta.prompt_registry_sha`, `meta.prompt_source`, `meta.prompt_warning`, `usage.generation_id`, `usage.provider_name`, tokens, `generation_cost`, latency, attempts, outcome, error kind.

Metrics: request count, success rate, p50/p95 latency, retries by class, 429 rate, total tokens/cost, batch wall time, concurrency in use. Never log full `source_md`/`summary_md` at info level (could be licensed book text); hash or truncate.

## 16. Performance targets

| Target | Value |
|---|---|
| Single chapter, p50 | ≤ 25 s (non-thinking) |
| Single chapter, p95 | ≤ 90 s; thinking models ≤ 10 min (client timeout 600 s) |
| Batch overhead (non-CF time) | < 5% of wall time |
| Fan-out | configurable 1–16, default 4 |
| Retry amplification | ≤ 1.5× billed generations by default |
| Cold path | first request after idle may add instance cold-start latency; use `--min-instances` if unacceptable |

## 17. Accessibility (only if a progress UI is built)

No UI is specified by this doc, but if the Next.js app renders batch progress:

- Per-item status updates announced via `aria-live="polite"`; batch completion via `aria-live="assertive"` or focus management.
- Status conveyed by text/icon, not color alone.
- Retry/cancel controls keyboard reachable with visible focus; `aria-busy` on the batch region.
- Respect `prefers-reduced-motion` for progress animation.
- Error messages readable by screen readers (association with the affected item).

## 18. Config surface (server-only)

```env
# Required
FUNCTION_URL=https://<region>-<project>.cloudfunctions.net/summarize

# Auth (choose per deployment shape)
GOOGLE_APPLICATION_CREDENTIALS=secrets/cf-invoker-key.json   # off-GCP
# On GCP, ADC via attached service account is preferred (no key file).

# Orchestration
CF_CONCURRENCY=4          # in-flight CF requests per process; 1..16
CF_TIMEOUT_MS=600000      # per-request client timeout; must exceed generation latency
CF_MAX_ATTEMPTS=4         # total attempts including the first
CF_MAX_BATCH_COST_USD=    # optional; empty = unbounded
CF_MAX_BATCH_DURATION_MS= # optional; empty = unbounded

# Optional
CF_DEFAULT_MODEL=deepseek/deepseek-v4-flash
CF_JUDGE_MODEL=openai/gpt-4o-mini
CF_JUDGE_SOURCE_CHAR_LIMIT=32000
```

## 19. Tests

- **Unit (mocked fetch):** request shaping; production path omits `system_prompt`/`prompt_version`; pinning sets `prompt_version` + fingerprint; validation rejects bad input; `summary_md` mode rule.
- **Concurrency:** pool never exceeds `CF_CONCURRENCY`; cancellation aborts all; results keyed by `id`; partial failure preserves successes.
- **Retry:** each error class maps to the correct decision (retry vs abort); backoff bounded; no retry after 422/402/400/404/405 or cancellation; auth refresh once.
- **Error parsing:** the full `{success:false,error}` taxonomy → `CfError` kinds.
- **Contract smoke:** against local `functions-framework` with `LLM_API_KEY=sk-placeholder` (no cost) — verify routing, 200 error-body parsing, and that a bogus key yields a clean provider auth error rather than a crash.
- **Staging smoke:** one authenticated production-prompt request; assert `meta.prompt_source === "production_default"` and a non-empty `meta.prompt_version`/`prompt_fingerprint`/`prompt_registry_sha`.
- **Concurrency integration (staging):** run K chapters with `CF_CONCURRENCY=K`, assert all succeed and CF does not return 429/503 at the chosen `--max-instances`.

## 20. Acceptance criteria

1. A single chapter call with no `system_prompt` returns a summary and `meta.prompt_source = "production_default"`, with `meta.prompt_version`/`prompt_fingerprint`/`prompt_registry_sha` populated.
2. Promoting a prompt in the Python registry + sync + redeploy changes the served prompt with **no Next.js deploy**; the Next.js app reports the new `prompt_version` in `meta`.
3. Sending `prompt_version` unknown to the CF returns 422 with `allowed`, and the app treats it as a permanent error (no retry).
4. A batch of N chapters runs at bounded concurrency, returns per-item successes/failures keyed by id, and exposes aggregate cost/tokens/wall time.
5. Cancellation aborts in-flight and queued work without retries.
6. 402/insufficient-credits and repeated auth failures abort the batch with a typed error rather than failing every item individually.
7. The OpenRouter key and OIDC credentials never appear in browser bundles, network responses, or logs.
8. `CF_TIMEOUT_MS` ≥ 600 s by default, so no duplicate billed generation is caused by client timeout.

## 21. Out of scope

Prompt authoring/promotion/sync tooling; registry UI; scoring/judge math; model selection policy; UI/visual design; direct browser→CF calls; multi-tenant quota/billing per user (though `maxBatchCostUsd` is the hook for it).

## 22. Open questions

- Deployment shape of the Next.js app (GCP-attached SA vs off-GCP key) determines the auth path; both are supported above.
- Whether the app needs the durable job pattern (§9.3) or synchronous batch suffices depends on the target book size and hosting platform's function-duration limit.
- Whether experimental inline overrides are needed in the app at all; if yes, require a server-side admin gate.
