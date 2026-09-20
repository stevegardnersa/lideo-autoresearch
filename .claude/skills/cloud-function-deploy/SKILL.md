---
name: cloud-function-deploy
description: >-
  Verify, deploy, and roll back the serverless chapter-summariser Cloud Function
  (GCP Cloud Functions v2). Runs Tier 0/1 local checks, optionally a real-cost
  Tier 2 generation, then deploys via gcloud, smoke-tests the deployed function
  with OIDC auth, and rolls back on failure. Use when asked to "deploy the cloud
  function", "ship the summariser function", "verify the function", or after any
  change to cloud_function/, core/*.py, or scoring.py.
---

# Cloud Function deploy

Operate the serverless summariser: `cloud_function/main.py` + `handler.py` behind
GCP Cloud Functions v2 (`summarize`, gen2, OIDC-authed, secret-bound `LLM_API_KEY`).

## When to use

Run the full pipeline before every deploy. Run Tier 0/1 alone for a fast local
signal while iterating on `cloud_function/` or `core/` code.

## Tier 0 — contract smoke (fast, no cost)

Boots the local `functions-framework` dev server and exercises the request
contract without calling any provider.

```bash
make cf-smoke PYTHON=.venv/bin/python
```

Verifies: scoring-only mode returns 200 with zero generation cost; validator
rejects `summary_md` without judge/rubric; validator rejects generation missing
prompts.

## Tier 1 — client + passthrough smoke (no cost)

Boots the dev server plus an in-process OpenRouter stub, then drives the real
`CloudFunctionClient` end-to-end:

```bash
make cf-client-smoke PYTHON=.venv/bin/python
```

Verifies 7 checks:
1. scoring-only round trip (zero cost, `scoring` block, `meta.scoring_version` surfaced)
2. generation round trip through the stub; the Function's `LLM_API_KEY` reaches the provider hop
3. `reasoning_effort` maps to native `reasoning.effort` (no top-level scalar, no `extra_body`)
4. `reasoning` dict forwarded verbatim (no `extra_body`)
5. legacy `thinking=false` maps to native `reasoning.enabled=false`
6. `reasoning_effort="none"` maps to native `reasoning.enabled=false`
7. legacy `thinking=true` maps to native `reasoning.enabled=true`

The handler emits OpenRouter-native reasoning controls only; the litellm
`extra_body` wrapper is never produced (OpenRouter ignores it, silently leaving
reasoning at provider defaults).

Any failure here = bug in `cf_client.py` or `handler.py`. Fix before Tier 2/deploy.

## Tier 2 — real generation (cost, optional)

One real generation through the local dev server to sanity-check cost + a live
summary. Requires a working provider.

0. **Provider health gate first.** The function hangs ~600 s/attempt against a dead
   upstream, so verify the provider from here BEFORE booting anything. With the key
   in `$KEY` (prompt the user; never log it) and `$MODEL` from
   `cloud_function/sample_request.json`:
   ```python
   import json, os, urllib.request, urllib.error, time
   key = os.environ["KEY"]; t = time.time()
   body = json.dumps({"model": "deepseek/deepseek-v4-flash",
                      "messages": [{"role": "user", "content": "Say pong"}],
                      "max_tokens": 10}).encode()
   req = urllib.request.Request(
       "https://openrouter.ai/api/v1/chat/completions", data=body,
       headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
       method="POST")
   try:
       with urllib.request.urlopen(req, timeout=60) as r:
           print("provider OK", time.time() - t)
   except urllib.error.HTTPError as e:
       print("provider HTTP", e.code, e.read()[:200])
   except Exception as e:
       print("provider", type(e).__name__, e)
   ```
   Also confirm the model slug exists:
   `urllib.request.urlopen("https://openrouter.ai/api/v1/models")` and check the id
   is in `data[].id`. A 401 "User not found" = revoked key; a timeout = proxy down.
   Do NOT proceed to deploy on either.
1. Get a key. Prefer the root `.env` `OPENROUTER_API_KEY` (app's real provider);
   otherwise prompt the user to supply one (do not log it).
2. Boot the dev server on a free port:
   ```bash
   LLM_API_KEY=sk-or-v1-... .venv/bin/python -m functions_framework \
     --target summarize --signature-type http \
     --source cloud_function/main.py --port 8081
   ```
3. Post `cloud_function/sample_request.json` with `Authorization` omitted:
   ```bash
   curl -s -X POST http://localhost:8081 \
     -H "Content-Type: application/json" \
     -d @cloud_function/sample_request.json | jq '{success, summary, usage}'
   ```
4. Assert `success == true`, non-empty `summary.summary_md` (note: nested — the
   top level is `{success, summary:{summary_md, estimated_visible_words}, usage, scoring, meta}`),
   `summary.estimated_visible_words > 0`, and a sensible `usage.generation_cost`.
   Check `reasoning_effort` passthrough by adding `"reasoning_effort": "high"` to the
   body and confirming the summary still returns.

Report cost to the user before proceeding to deploy.

## Deploy

Prereqs: `gcloud` authed, `PROJECT_ID` known, and a Secret Manager secret holding a
provider key that the function's service account can read. Two supported configs:

| Config | Secret | `LLM_BASE_URL` env | sample model |
|--------|--------|--------------------|--------------|
| Direct OpenRouter (default) | `openrouter-api-key` (sk-or-v1-…) | `https://openrouter.ai/api/v1` | `deepseek/deepseek-v4-flash` |
| litellm proxy (current live config) | `llm-api-key` (litellm virtual key `sk-…`) | `https://litellm.graceflow.com` | `openrouter/deepseek/deepseek-v4-flash` |

Create the secret from a working key (only if it does not exist):
```bash
echo -n "$KEY" | gcloud secrets create openrouter-api-key --project "$PROJECT_ID" \
  --data-file=- && echo "$FUNC_SA" | xargs -I{} gcloud secrets add-iam-policy-binding \
  openrouter-api-key --project "$PROJECT_ID" --member="serviceAccount:{}" \
  --role=roles/secretmanager.secretAccessor
```
Update the secret (existing deployments / key rotation):
```bash
echo -n "$KEY" | gcloud secrets versions add openrouter-api-key --project "$PROJECT_ID" --data-file=-
```
See `.notes/setup-cloud-function.md` steps 6b/10e for IAM binding details.

Deploy (idempotent; gcloud rebuilds only changed files) — default = direct
OpenRouter:

```bash
gcloud functions deploy summarize \
  --gen2 \
  --runtime python312 \
  --trigger-http \
  --entry-point summarize \
  --source . \
  --region us-central1 \
  --project "$PROJECT_ID" \
  --set-secrets 'LLM_API_KEY=openrouter-api-key:latest' \
  --set-env-vars 'LLM_BASE_URL=https://openrouter.ai/api/v1' \
  --timeout 3600 \
  --memory 1024Mi \
  --no-allow-unauthenticated
```

For the litellm proxy config, replace the two flags with
`--set-secrets 'LLM_API_KEY=llm-api-key:latest'` and
`--set-env-vars 'LLM_BASE_URL=https://litellm.graceflow.com'`.

Notes:
- `--source .` uploads the project root; `.gcloudignore` must exclude `runs/`,
  `artifacts/`, `data/books/`, `dashboard/`, `tools/`, `bench/`, `.venv/`, plus
  the agent session dirs (`.lcm/`, `.opencode/`, `.sessions/`, `.notes/`), `tests/`,
  `secrets/`, scratch files (`results.tsv`, `*.md`). A loose root `.gcloudignore`
  can push the upload into the hundreds of MB and stall the deploy.
- Entry point is the root `main.py`, which is authoritative nowhere: it merely
  re-exports `cloud_function.main.summarize`. All logic lives in `cloud_function/`.
  The deploy contract (root shim → `@functions_framework.http summarize` →
  `cloud_function/handler.py`, `.gcloudignore` exclusions) is drift-guarded by
  `tests/test_deploy_entrypoint.py`, wired into `make test`.
- Bump `--memory 2048Mi` if deploying for thinking/reasoning models.
- Initial deploy 2-5 min; re-deploy ~60 s.

## Post-deploy smoke (OIDC)

1. Get the URL + a fresh identity token:
   ```bash
   FUNC_URL=$(gcloud functions describe summarize --gen2 --region us-central1 \
     --project "$PROJECT_ID" --format="value(serviceConfig.uri)")
   TOKEN=$(gcloud auth print-identity-token)
   ```
2. Scoring-only probe (no provider cost) through the real deployment:
   ```bash
   curl -s -o /tmp/cf_probe.json -w "%{http_code}\n" -X POST "$FUNC_URL" \
     -H "Content-Type: application/json" \
     -H "Authorization: Bearer $TOKEN" \
     -d '{"source_md":"probe","model":"test/x","summary_md":"hi","target_words":1,"score":true,"rubric":{"core_concepts":["feedback"]}}'
   ```
   Expect `200`, `success: true`, `generation_cost == 0.0`.
3. Assert `meta.scoring_version` in the response equals the local
   `sha256(scoring.py)` (the client enforces this as
   `CloudFunctionClient.scoring_version_mismatch`). A mismatch means the deployed
   scoring logic is stale — redeploy.
4. Optional real generation through the deployed function:
   ```bash
   curl -s -X POST "$FUNC_URL" -H "Content-Type: application/json" \
     -H "Authorization: Bearer $TOKEN" \
     -d @cloud_function/sample_request.json | jq '{success, summary, usage}'
   ```
5. Check logs if anything is off:
   ```bash
   gcloud functions logs read summarize --gen2 --region us-central1 --project "$PROJECT_ID" --limit 20
   ```

## Rollback

The function has no versioning — rollback = redeploy the previous source.

1. If `git` has the prior state, checkout `cloud_function/`, `core/`, `scoring.py`
   to the last known-good ref, then redeploy with the same deploy command.
2. No prior ref → there is no rollback target. Do not deploy; surface this to the
   user and stop.

## Failure triage

| Symptom | Check |
|---------|-------|
| deploy fails "unable to evaluate source" | `.gcloudignore` too permissive; trim large dirs |
| 500 after deploy | logs first; missing import, bad key, or `--set-secrets` missing |
| 403 on test | stale token; `gcloud auth login` then re-run `print-identity-token` |
| deploy times out | `gcloud config set builds/timeout 600` |
| secret not visible | `gcloud secrets versions list llm-api-key`; check IAM binding |
| scoring_version mismatch | deployed `scoring.py` stale; redeploy |
| generation hangs 10+ min, no request log | upstream provider dead. Run the Tier 2 provider health gate; fix provider/key first |
| provider 401 "Malformed API Key" | key type wrong for the endpoint. litellm rejects sk-or-v1…; OpenRouter rejects litellm `sk-…`. Match the key to `LLM_BASE_URL` (see table in Deploy) |
| 404 / invalid model from provider | model slug wrong for the base URL; use the table in Deploy |

## After deploy

- Report the function URL + scoring_version hash.
- If the user asks to update docs, point `document-release` at the new
  `handler_version` / `deployed_at_utc` in the smoke output.