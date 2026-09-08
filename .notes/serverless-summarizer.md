# Serverless Chapter Summarizer — Architecture & Operations

Two components:

- **`cloud_function/`** — deployable HTTP endpoint. Validates, calls any OpenAI-compatible API (OpenRouter, OpenAI, etc.), scores, judges. One summary per request.
- **`tools/batch_summarize.py`** — consumer-side batch worker. Reads bench JSONL, fans out to CF (or direct API), collects samples+traces, runs `score_dataset`, writes run artifacts.

Both share the same prompt rendering, rubric parsing, and scoring pipeline via imports from `candidate_spec.py` and `scoring.py`.

> **First time deploying?** See `.notes/setup-cloud-function.md` for the complete walkthrough from zero to deployed.

---

## Component Map

```
[bench JSONL] → tools/batch_summarize.py ──HTTP──→ cloud_function/main.py (CF entry)
                                                        │
                                                    cloud_function/handler.py
                                                        │
                                                    core/openrouter_client.py
                                                        │
                                                    core/judge.py (optional)
                                                        │
                                                    scoring.py
                                                        │
                                                Response JSON ──→ samples.jsonl
                                                                  state.json
                                                                  run-id.json (scored)
```

| File | Role |
|------|------|
| `cloud_function/main.py` | `@functions_framework.http` entry. Validates JSON body, checks required fields, wraps errors in JSON. |
| `cloud_function/handler.py` | `run_summarize()` — builds LLM payload, calls client, parses structured JSON response, optionally scores + judges, returns flat dict. |
| `cloud_function/requirements.txt` | `functions-framework==3.*` — only pip dep. Everything else is stdlib. |
| `cloud_function/README.md` | Full API ref, deployment commands, env vars, integration examples. |
| `tools/batch_summarize.py` | Parallel batch worker. Two modes: `--function-url` (async httpx) or direct API (sync). |
| `cloud_function/sample_request.json` | Example POST body for manual testing. |

---

## Cloud Function (`cloud_function/`)

### Request

```
POST /summarize
Content-Type: application/json

{
  "source_md": "..." ,            // required — chapter markdown
  "model": "deepseek/...",        // required — model ID
  "system_prompt": "...",         // required — composed system prompt
  "user_prompt": "...",           // required — composed user prompt
  "base_url": "",                 // optional — API base URL (default: https://openrouter.ai/api/v1)
  "api_key": "",                  // optional — API key (omit to use LLM_API_KEY env var)
  "thinking": false,              // optional — enable reasoning tokens
  "use_json_schema": true,        // optional — structured JSON output
  "target_words": 400,            // optional — triggers deterministic scoring
  "judge": false,                 // optional — run LLM judge
  "judge_model": "openai/gpt-4o-mini",  // optional — judge model ID
  "rubric": { ... }               // optional — rubric fields for scoring
}
```

### Response

```json
{
  "success": true,
  "summary": { "summary_md": "...", "estimated_visible_words": 412 },
  "usage": { "prompt_tokens": 1234, "generation_cost": 0.000123 },
  "scoring": { ... },    // present if rubric + target_words provided
  "judge_scores": { ... } // present if judge=true
}
```

### Flow

1. `run_summarize()` in `handler.py` — builds payload via `_build_openrouter_payload()` (stdlib — no `requests`/`openai`)
2. `_build_client()` — `OpenRouterClient.from_env()` (reads API key from request body, else `LLM_API_KEY` env; base URL from request, else `LLM_BASE_URL` env, else OpenRouter default; 600s timeout, 3 retries)
3. Parse structured JSON response (handles `use_json_schema=true/false`)
4. If rubric + target_words present → create `SummarySample`, call `score_sample()`, optionally `judge_summary_absolute()`
5. Return aggregated dict

### Development

```bash
pip install functions-framework

export LLM_API_KEY="sk-or-v1-..."

functions-framework \
  --target summarize \
  --signature-type http \
  --source cloud_function/main.py \
  --port 8080 \
  --debug

curl -X POST http://localhost:8080 \
  -H "Content-Type: application/json" \
  -d @cloud_function/sample_request.json
```

### Deployment

```bash
gcloud functions deploy summarize \
  --gen2 \
  --runtime python312 \
  --trigger-http \
  --entry-point summarize \
  --source . \
  --region us-central1 \
  --set-secrets 'LLM_API_KEY=llm-api-key:latest' \
  --timeout 3600 \
  --memory 1024Mi \
  --no-allow-unauthenticated
```

---

## Batch Worker (`tools/batch_summarize.py`)

Parallel batch worker that consumes a bench JSONL and fans out to the CF (or direct OpenAI-compatible API).

### Modes

| Mode | Flags | HTTP Library | When |
|------|-------|-------------|------|
| **Cloud Function** | `--function-url <url>` | `httpx` (async) | Production. Authenticated, scalable, no local API key needed. |
| **Direct API** | (omit `--function-url`) `--base-url <url> --api-key-env ENV` | `OpenAIClient` (sync) | Testing/dev. Requires API key via `--api-key`, `--api-key-env`, or direct env var. |
| **Direct API (OpenRouter)** | `--base-url https://openrouter.ai/api/v1 --api-key-env LLM_API_KEY` | `OpenAIClient` (sync) | Testing against OpenRouter. Default if no flags provided. |
| **Dry run / mock** | `LLM_API_KEY=sk-placeholder` | `_mock_gen()` | Integration testing. No API cost. |

### Flow

1. Load bench JSONL (`bench/<name>.jsonl`)
2. For each item: `load_book_data()` → render prompts via `candidate_spec.py` system/user renderers → allocate target words from budget allocator
3. Send to CF (or API) with semaphore-based concurrency
4. Parse response into `SummarySample` + trace dict
5. Append to `samples.jsonl` + update `state.json` (resumable)
6. After all items: `score_dataset()` → write run artifact JSON

### State Management

Uses same `run_id` + `state.json` / `samples.jsonl` convention as the main runner.

| File | Purpose |
|------|---------|
| `runs/batch/<run_id>.state.json` | Progress tracker. `completed_count`, `completed_item_keys`, `status`. |
| `runs/batch/<run_id>.samples.jsonl` | Append-only sample records. One JSON object per line. |
| `runs/batch/<run_id>.json` | Final artifact. Summary block + sample list + traces. |

**Resume:** pass `--resume <run_id>` to skip completed items. Reads `completed_item_keys` from `state.json`, reads existing `samples.jsonl`.

### Target Word Allocation

Per-chapter target words are derived from the budget allocator in `candidate_spec.py`:

```python
wpm = spec.budget_allocator.words_per_minute
multiplier = spec.budget_allocator.chapter_stage_multiplier_30m  # or _60m
total_stage_budget = minutes * wpm * multiplier
est_book_words = max(source_words, total_stage_budget * 3)
target_words = clamp(100, int(total_budget * source_words / est_book_words),
                     int(source_words * max_ratio))
```

### Usage

```bash
# Cloud Function — 8 concurrent, with judging
uv run python tools/batch_summarize.py \
  --bench chapter_fast \
  --profile 30m_deepseek-v4-flash_notthinking \
  --function-url https://us-central1-PROJECT.cloudfunctions.net/summarize \
  --concurrency 8 \
  --judge

# OpenAI-compatible provider (e.g. OpenAI directly)
uv run python tools/batch_summarize.py \
  --bench chapter_fast \
  --profile 30m_deepseek-v4-flash_notthinking \
  --base-url https://api.openai.com/v1 \
  --api-key-env OPENAI_API_KEY

# Direct OpenRouter — 2 samples only
uv run python tools/batch_summarize.py \
  --bench bench/test-2.jsonl \
  --profile 30m_deepseek-v4-flash_notthinking \
  --max-samples 2

# Resume after partial completion
uv run python tools/batch_summarize.py \
  --bench chapter_fast \
  --profile 30m_deepseek-v4-flash_thinking \
  --function-url $FUNC_URL \
  --resume batch_chapter_fast_30m_deepseek-v4-flash_thinking_20260616T120000Z
```

---

## Prompt Pipeline

Prompts are composed **before** entering the function — the CF is a pure inference+scoring endpoint.

```python
# Consumer project
from candidate_spec import get_candidate, render_chapter_system, render_chapter_user

spec = get_candidate("30m_deepseek-v4-flash_notthinking")
system = render_chapter_system(spec)
user = render_chapter_user(spec, source_md=chapter, target_words=500,
                           book_title=..., chapter_title=..., toc=..., metadata=...)

# Then call CF with pre-composed prompts
result = requests.post(FUNCTION_URL, json={
    "source_md": chapter,
    "model": spec.chapter_stage.model,
    "system_prompt": system,
    "user_prompt": user,
    "target_words": 500,
    "judge": True,
})
```

Convention: `use_json_schema=true` prepends `"Respond using JSON format exactly matching the provided schema.\n\n"` to the system prompt (both in CF handler and batch tool).

---

## Scoring & Judging

### Deterministic scoring (always runs if rubric + target_words provided)

`scoring.py` metrics computed server-side in the CF:
- Length accuracy (`final_length_accuracy`)
- Heading coverage from rubric
- Concept/mechanism/qualifier phrase coverage (fuzzy match)
- Key term coverage
- Number coverage
- Redundancy score
- Structure proxy + faithfulness proxy
- Readability metrics

Resolved into five dimensions + `quality` + `utility`.

### LLM judging (optional, `judge=true`)

Second LLM call from the CF (default: `gpt-4o-mini`):
- `faithfulness`, `concept_coverage`, `qualifier_preservation`, `no_fluff`, `structure_quality`
- `rationale` — free-text explanation

Returns in `judge_scores` key. If judge call fails, returns in `judge_error` key.

### Data source for scoring

Rubrics are generated offline by `tools/build_rubrics.py` and stored at `artifacts/rubrics/<book_id>/<chapter_id>.json`. The CF accepts them inline in the request body. The batch tool loads them from disk and passes them in the CF payload.

---

## Cost & Performance

| Component | Model (default) | ~Tokens | ~Cost |
|-----------|----------------|---------|-------|
| Chapter summary | `deepseek-v4-flash` | 0.5-2K input / 0.3-1K output | ~$0.0002 |
| Judge (optional) | `gpt-4o-mini` | 2-4K input / 0.1K output | ~$0.0005 |
| **Total (with judge)** | | | **~$0.0007/sample** |

- `thinking=true` roughly doubles output tokens (reasoning tokens billed separately)
- 30-book corpus × 10 chapters × 3 profile candidates = 900 samples ≈ $0.60 (no judge)
- 8 concurrent requests: CF latency ~30-120s per request (dominated by LLM time)

---

## Production Considerations

### Authentication

Deploy with `--no-allow-unauthenticated`. Clients use OIDC tokens:

```python
import google.auth
creds, _ = google.auth.default()
auth_req = google.auth.transport.requests.Request()
creds.refresh(auth_req)

requests.post(FUNCTION_URL, json=payload,
              headers={"Authorization": f"Bearer {creds.token}"})
```

### Timeouts

- CF timeout: 3600s (GCP max). Set via `--timeout 3600`.
- Client timeout: ≥300s. LLM calls take 30-180s.
- Batch tool: `--function-timeout 600` (default). Adjust per model — thinking models need more.

### Secrets

For the deployed CF, the API key comes from Secret Manager: `LLM_API_KEY` → Secret Manager → bound via `--set-secrets`. Base URL defaults to `LLM_BASE_URL` env when set.

For batch tool direct API mode, use `--api-key <key>` or `--api-key-env <env_var>`.

Never hardcode keys in source or command history.

```bash
printf "sk-or-v1-..." | gcloud secrets create llm-api-key \
  --data-file=-
```

### Monitoring

- CF logs: `gcloud functions logs read summarize --gen2`
- Errors surface in both CF response (`error` key) and stderr logs
- Batch tool prints per-item failures with `[index/total] FAIL item_key: error`
- `state.json` records `latest_error` and `status` (`running` → `finished`)
