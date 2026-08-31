# Cloud Function: Chapter Summarizer

Serverless HTTP endpoint that generates chapter summaries via OpenRouter LLMs, with optional deterministic scoring and LLM-based judging. Deploys to Google Cloud Functions (2nd gen / Cloud Run).

## Architecture

```
HTTP POST /summarize
  │
  ├─ Validate inputs
  ├─ Call OpenRouter (model + system/user prompts + thinking/JSON config)
  ├─ Parse structured JSON response (summary_md + estimated_visible_words)
  ├─ If rubric + target_words provided:
  │   ├─ Run deterministic scoring (length accuracy, coverage, readability, etc.)
  │   └─ If judge=true:
  │       └─ Call judge model → faithfulness / coverage / quality scores
  └─ Return aggregated result
```

Uses stdlib only for LLM calls — no `openai` or `requests` dependency. The only pip dependency is `functions-framework` (the Google Functions dev server).

## Prerequisites

- Python 3.12+
- An [OpenRouter](https://openrouter.ai) account with API credits
- `gcloud` CLI (for deployment only)

## Setup

```bash
# 1. Install the dev server
pip install functions-framework

# 2. Set your OpenRouter key
export LLM_API_KEY="sk-or-v1-..."
```

> **Important for local testing:** This project uses `uv` for dependency management. If you see `PEP 668` errors (system Python refuses pip install), use:
> ```bash
> uv pip install functions-framework
> functions-framework --target summarize ...
> ```

## Local Development

### Start the dev server

From the **project root** (one level above `cloud_function/`):

```bash
functions-framework \
  --target summarize \
  --signature-type http \
  --source cloud_function/main.py \
  --port 8080 \
  --debug
```

The `--source` flag points to the entry-point file. `--debug` enables auto-reload on file changes.

### Send a test request

```bash
curl -X POST http://localhost:8080 \
  -H "Content-Type: application/json" \
  -d @cloud_function/sample_request.json | jq .
```

Or use the convenience wrapper included in the project:

```bash
python cloud_function/local_test.py
```

### Quick smoke test (no API cost)

Set `LLM_API_KEY=sk-placeholder` and call the endpoint. The function will return a clear provider auth error, confirming routing, parsing, and error handling all work without spending credits.

## Deployment

### API Key Setup (one-time)

Before deploying, create the secret or set the env var the function will use.

**Option A — Secret Manager (recommended):**

```bash
# Create the secret
printf "sk-or-v1-..." | gcloud secrets create llm-api-key \
  --data-file=-

# Grant the function's default compute service account access
# (find the service account email after first deploy, or pre-create it)
# gcloud secrets add-iam-policy-binding llm-api-key \
#   --member="serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
#   --role="roles/secretmanager.secretAccessor"
```

This avoids leaking the key in command history, CLI logs, or `gcloud functions describe` output.

**Option B — Plain environment variable (simpler, less secure):**

```bash
export LLM_API_KEY="sk-or-v1-..."
```

### To Google Cloud Functions (2nd gen)

Secret Manager binding:

```bash
# Set these once
export REGION=us-central1
export FUNC_NAME=summarize

# Deploy
gcloud functions deploy $FUNC_NAME \
  --gen2 \
  --runtime python312 \
  --trigger-http \
  --entry-point summarize \
  --source . \
  --region $REGION \
  --set-secrets 'LLM_API_KEY=llm-api-key:latest' \
  --timeout 3600 \
  --memory 1024Mi \
  --no-allow-unauthenticated
```

Plain env var (substitute `--set-secrets` above):

```bash
gcloud functions deploy $FUNC_NAME \
  ...
  --set-env-vars LLM_API_KEY=$LLM_API_KEY \
  ...
```

| Flag | Why |
|------|-----|
| `--gen2` | Cloud Functions 2nd gen (Cloud Run backing) |
| `--runtime python312` | Python version |
| `--entry-point summarize` | Must match the function name in `main.py` |
| `--source .` | Package the entire project root |
| `--set-secrets` | Bind `LLM_API_KEY` from Secret Manager |
| `--timeout 3600` | LLM calls can take minutes; 3600s = max |
| `--memory 1024Mi` | 1 GB is sufficient; increase for thinking models |
| `--no-allow-unauthenticated` | Require auth (recommended) |

The `.gcloudignore` excludes `runs/`, `artifacts/`, `data/`, `tools/`, `bench/`, and `dashboard/` from the upload.

### To Cloud Run (alternative)

```bash
# Build and push
gcloud builds submit --tag gcr.io/$PROJECT/$FUNC_NAME

# Deploy
gcloud run deploy $FUNC_NAME \
  --image gcr.io/$PROJECT/$FUNC_NAME \
  --set-secrets 'LLM_API_KEY=llm-api-key:latest' \
  --timeout 3600 \
  --memory 1024Mi \
  --no-allow-unauthenticated
```

This requires a `Dockerfile` (not included; CF 2nd gen auto-detects Python runtimes).

## API Reference

### Endpoint

`POST /summarize`

### Request Body

```jsonc
{
  // ── Required ──────────────────────────────────────────────
  "source_md": "string",        // Chapter markdown text
  "model": "string",            // Model ID (e.g. "deepseek/deepseek-v4-flash", "gpt-4o")
  "system_prompt": "string",    // Composed system prompt
  "user_prompt": "string",      // Composed user prompt (includes source chapter)

  // ── Optional — Provider ───────────────────────────────────
  "base_url": "",               // OpenAI-compatible API base URL (default: LLM_BASE_URL env, else https://openrouter.ai/api/v1)
  "api_key": "",                // API key (omit to use LLM_API_KEY env var)

  // ── Optional — Model parameters ───────────────────────────
  "thinking": false,            // Enable reasoning tokens (default: false)
  "use_json_schema": true,      // Request structured JSON output (default: true)

  // ── Optional — Scoring ────────────────────────────────────
  "target_words": 400,          // Target word count (triggers deterministic scoring)
  "judge": false,               // Run LLM judge (requires judge_model + rubric)
  "judge_model": "openai/gpt-4o-mini",

  // ── Optional — Rubric (for scoring) ───────────────────────
  "rubric": {
    "headings": ["string"],
    "core_concepts": ["string"],
    "mechanisms_or_explanations": ["string"],
    "critical_qualifiers": ["string"],
    "important_examples": ["string"],
    "key_entities_or_numbers": ["string"],
    "key_terms": ["string"]
  }
}
```

### Response Body

```jsonc
{
  "success": true,

  // ── Summary output ────────────────────────────────────────
  "summary": {
    "summary_md": "string",             // Generated summary markdown
    "estimated_visible_words": 412       // Word count estimate
  },

  // ── Token usage & cost ────────────────────────────────────
  "usage": {
    "prompt_tokens": 1234,
    "completion_tokens": 567,
    "total_tokens": 1801,
    "reasoning_tokens": 0,
    "cached_prompt_tokens": 0,
    "generation_cost": 0.000123,
    "uncached_generation_cost": 0.000123,
    "generation_id": "gen-abc123",
    "provider_name": "DeepSeek",
    "model_id": "deepseek/deepseek-v4-flash"
  },

  // ── Scoring (present only if rubric + target_words provided) ──
  "scoring": {
    "hard_fail": false,
    "hard_fail_reasons": [],
    "deterministic": {
      "visible_words": 412,
      "final_length_error_pct": 3.0,
      "final_length_accuracy": 0.97,
      "heading_coverage": 0.85,
      "concept_phrase_coverage": 0.78,
      "mechanism_phrase_coverage": 0.72,
      "qualifier_phrase_coverage": 0.88,
      "key_term_coverage": 0.82,
      "number_coverage": 0.0,
      "redundancy_score": 0.95,
      "structure_proxy": 0.80,
      "faithfulness_proxy": 0.78,
      "concept_proxy": 0.81
      // ... plus readability, first_pass_accuracy, etc.
    },
    "resolved_faithfulness": 0.78,
    "resolved_concept_coverage": 0.80,
    "resolved_qualifier_preservation": 0.88,
    "resolved_no_fluff": 0.92,
    "resolved_structure_quality": 0.83,
    "quality": 0.82,
    "utility": 0.79
  },

  // ── LLM judge scores (present only if judge=true) ─────────
  "judge_scores": {
    "faithfulness": 0.85,
    "concept_coverage": 0.80,
    "qualifier_preservation": 0.90,
    "no_fluff": 0.88,
    "structure_quality": 0.82,
    "rationale": "The summary preserves all key concepts..."
  },

  // ── Judge error (present if judge=true but judge failed) ──
  "judge_error": "string"
}
```

### Error Responses

| Status | Condition |
|--------|-----------|
| 405 | Non-POST request method |
| 400 | Invalid JSON / missing required fields |
| 500 | Unhandled server error (call OpenRouter failed, etc.) |
| (502) | OpenRouter returns non-200 (host-level error, not at function layer) |

On error:

```json
{ "success": false, "error": "Missing required fields: source_md" }
```

## Environment Variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `LLM_API_KEY` | If no `api_key` in request | API key for the provider |
| `LLM_BASE_URL` | No | Default OpenAI-compatible API base URL (falls back to `https://openrouter.ai/api/v1`) |
| `OPENROUTER_HTTP_REFERER` | No | HTTP Referer header sent to provider |
| `OPENROUTER_APP_TITLE` | No | App title sent to provider for analytics |

The function resolves the API key in this order:
1. `api_key` from request body (highest priority)
2. `LLM_API_KEY` env var

The base URL resolves in this order:
1. `base_url` from request body
2. `LLM_BASE_URL` env var
3. `https://openrouter.ai/api/v1` (last resort)

## Integrating from a Separate Consumer Project

### Python — via `requests`

```python
import json
import requests

FUNCTION_URL = "https://us-central1-YOUR_PROJECT.cloudfunctions.net/summarize"

def generate_summary(
    source_md: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    target_words: int = 0,
    judge: bool = False,
    **kwargs,
) -> dict:
    payload = {
        "source_md": source_md,
        "model": model,
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "target_words": target_words,
        "judge": judge,
        **kwargs,
    }
    resp = requests.post(FUNCTION_URL, json=payload, timeout=600)
    resp.raise_for_status()
    return resp.json()
```

### Python — via `httpx` (async)

```python
import httpx

async def generate_summary_async(payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=600) as client:
        resp = await client.post(FUNCTION_URL, json=payload)
        resp.raise_for_status()
        return resp.json()
```

### Authentication

If deployed with `--no-allow-unauthenticated`, requests must include an OIDC token:

```python
import google.auth
import google.auth.transport.requests

def authenticated_request(payload: dict) -> dict:
    creds, _ = google.auth.default()
    auth_req = google.auth.transport.requests.Request()
    creds.refresh(auth_req)

    resp = requests.post(
        FUNCTION_URL,
        json=payload,
        headers={"Authorization": f"Bearer {creds.token}"},
        timeout=600,
    )
    resp.raise_for_status()
    return resp.json()
```

### curl (with auth token)

```bash
gcloud auth print-identity-token | \
  curl -X POST $FUNCTION_URL \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
    -d @cloud_function/sample_request.json
```

### Expected Result

```python
result = generate_summary(source_md, model, system_prompt, user_prompt, target_words=500)
summary_md = result["summary"]["summary_md"]
cost = result["usage"]["generation_cost"]

if result.get("scoring"):
    quality = result["scoring"]["quality"]
    print(f"Quality: {quality:.2f}, Cost: ${cost:.6f}")

if result.get("judge_scores"):
    faithfulness = result["judge_scores"]["faithfulness"]
    print(f"Judge faithfulness: {faithfulness:.2f}")
```

## Best Practices

### Prompt Composition

Compose prompts **before** calling the function. The function is a pure inference endpoint — it does not compose prompts for you. This keeps the API surface clean and avoids coupling the function to your prompt engineering strategy.

```python
# In your consumer project — compose prompts, then call
system_prompt = compose_system_prompt(style="dense_faithful")
user_prompt = compose_user_prompt(source_md=chapter_text, target_words=500)

result = generate_summary(
    source_md=chapter_text,
    model="deepseek/deepseek-v4-flash",
    system_prompt=system_prompt,
    user_prompt=user_prompt,
    target_words=500,
    judge=True,
)
```

### Rubric Generation

Generate rubrics ahead of time and cache them. The rubric is a static analysis of the source chapter — it doesn't change between summary runs. Build once, reuse across experiments:

```bash
# This project includes a rubric builder:
uv run python tools/build_rubrics.py --book-id your-book
```

### Cost Management

- The `thinking` flag roughly doubles token output (reasoning tokens are billed separately). Use `false` for cost-sensitive bulk runs.
- The `judge` flag adds a second LLM call (default: `gpt-4o-mini`, ~$0.15/M tokens). Omit for pipeline runs where only deterministic scoring is needed.
- Set `use_json_schema=false` only if the model lacks JSON mode or you want free-form output. Structured output reduces parsing errors and is recommended.

### Client-Side Timeout

LLM calls can take 30–180 seconds. Set client timeout to at least 300 seconds (5 minutes). The function timeout is 3600 seconds (1 hour, GCP maximum).

## File Reference

| File | Purpose |
|------|---------|
| `cloud_function/main.py` | CF entry point — validates, dispatches, wraps errors |
| `cloud_function/handler.py` | Core orchestration — LLM call → scoring → judging |
| `cloud_function/requirements.txt` | Single dep: `functions-framework==3.*` |
| `cloud_function/.gcloudignore` | Deployment exclusions |
| `cloud_function/sample_request.json` | Example payload for manual testing |
| `core/openrouter_client.py` | (imported) stdlib OpenRouter client |
| `core/judge.py` | (imported) LLM-based absolute judge |
| `scoring.py` | (imported) Deterministic and resolved scoring |