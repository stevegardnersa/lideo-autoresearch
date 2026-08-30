# Cloud Function Setup Guide — Step by Step

Complete walkthrough from zero to deployed. Assumes you have a Google account but no GCP project.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Create GCP Project & Enable Billing](#2-create-gcp-project--enable-billing)
3. [Enable Required APIs](#3-enable-required-apis)
4. [Install & Configure gcloud CLI](#4-install--configure-gcloud-cli)
5. [Create the API Key Secret](#5-create-the-api-key-secret)
6. [Grant Service Account Access to Secret](#6-grant-service-account-access-to-secret)
7. [Deploy the Function](#7-deploy-the-function)
8. [Test the Deployed Function](#8-test-the-deployed-function)
9. [Update the Function](#9-update-the-function)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Prerequisites

- **Google account** (Gmail works)
- **API key** for any OpenAI-compatible provider (e.g. [OpenRouter](https://openrouter.ai/keys), [OpenAI](https://platform.openai.com/api-keys), or a self-hosted endpoint)
- **This codebase** committed locally — you need no uncommitted changes (gcloud uploads the whole directory)

---

## 2. Create GCP Project & Enable Billing

### 2a. Create the project

```bash
# Pick a project name (globally unique, lowercase, no spaces)
export PROJECT_ID="chapter-summarizer"

gcloud projects create $PROJECT_ID \
  --name="Chapter Summarizer"

gcloud config set project $PROJECT_ID
```

If that fails because the name is taken, add a suffix:
```bash
export PROJECT_ID="chapter-summarizer-$(whoami)"
gcloud projects create $PROJECT_ID
gcloud config set project $PROJECT_ID
```

**Verify:**
```bash
gcloud config get-value project
# → chapter-summarizer-xxx
```

### 2b. Enable billing

Open the GCP console billing page:

```bash
open "https://console.cloud.google.com/billing/linkedaccount?project=$PROJECT_ID"
```

Or navigate manually:
1. Go to https://console.cloud.google.com/billing
2. Select your project
3. Link a billing account (you need a credit card)
4. Verify billing is active:
```bash
gcloud billing projects describe $PROJECT_ID \
  --format="value(billingEnabled)"
# → True
```

**Why billing is needed:** Cloud Functions, Secret Manager, and Cloud Run all require billing even at free-tier usage.

---

## 3. Enable Required APIs

```bash
gcloud services enable \
  cloudfunctions.googleapis.com \
  cloudbuild.googleapis.com \
  run.googleapis.com \
  secretmanager.googleapis.com \
  logging.googleapis.com \
  --project $PROJECT_ID
```

This takes 30-60 seconds. Verify:
```bash
gcloud services list --enabled --project $PROJECT_ID | grep -E "cloudfunctions|cloudbuild|run|secretmanager|logging"
```

**What each API is for:**
| API | Purpose |
|-----|---------|
| `cloudfunctions` | Runs the function itself (2nd gen = Cloud Run under the hood) |
| `cloudbuild` | Builds the container image from your source |
| `run` | Cloud Run — the underlying runtime for CF 2nd gen |
| `secretmanager` | Stores the API key securely |
| `logging` | Cloud Logging for function logs |

---

## 4. Install & Configure gcloud CLI

### 4a. Check if gcloud is installed

```bash
gcloud --version
```

If not installed:
```bash
# macOS (Homebrew)
brew install --cask google-cloud-sdk

# macOS (manual)
curl -O https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-sdk-484.0.0-darwin-arm.tar.gz
tar xzf google-cloud-sdk-*.tar.gz
./google-cloud-sdk/install.sh

# Linux (apt)
# See: https://cloud.google.com/sdk/docs/install#deb

# Verify
gcloud --version
```

### 4b. Authenticate

```bash
gcloud auth login
```
Opens a browser. Log in with your Google account.

### 4c. Set project

```bash
gcloud config set project $PROJECT_ID
gcloud config get-value project
# → chapter-summarizer-xxx
```

### 4d. Set default region (optional, recommended)

```bash
gcloud config set functions/region us-central1
```

---

## 5. Create the API Key Secret

The secret name can be anything; the deploy command binds it as `LLM_API_KEY` env var. The function resolves the API key in this order:
1. `api_key` field from the request body
2. `LLM_API_KEY` environment variable

```bash
# Paste your key (replace with your actual API key)
printf "sk-or-v1-your-actual-api-key-here" | \
  gcloud secrets create openrouter-api-key \
    --data-file=- \
    --regions=us-central1 \
    --project $PROJECT_ID
```

**Why `--data-file=-`:** Reads the key from stdin instead of showing it in the command line where it would be recorded in shell history.

**Verify:**
```bash
gcloud secrets versions list openrouter-api-key --project $PROJECT_ID
# → NAME: 1, STATE: enabled
```

> **Tip:** You can use any provider's key here — OpenRouter, OpenAI, Anthropic (via API proxy), or a self-hosted endpoint. The function supports any OpenAI-compatible API. The secret name `openrouter-api-key` is just a convention.

---

## 6. Grant Service Account Access to Secret

The Cloud Function runs under a service account. We need to give that account permission to read the secret.

### 6a. Get the project number

```bash
export PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format="value(projectNumber)")
export SA_EMAIL="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
echo "Service account: $SA_EMAIL"
# → 123456789-compute@developer.gserviceaccount.com
```

### 6b. Grant Secret Manager access

```bash
gcloud secrets add-iam-policy-binding openrouter-api-key \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/secretmanager.secretAccessor" \
  --project $PROJECT_ID
```

**Verify:**
```bash
gcloud secrets get-iam-policy openrouter-api-key --project $PROJECT_ID
# → bindings:
# →   - members: serviceAccount:123456789-compute@developer.gserviceaccount.com
# →     role: roles/secretmanager.secretAccessor
```

---

## 7. Deploy the Function

### 7a. Navigate to the project root

```bash
cd /Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool.worktrees/wt1
```

### 7b. Verify the source structure

```bash
ls cloud_function/
# → .gcloudignore  handler.py  main.py  README.md  requirements.txt  sample_request.json
```

### 7c. Deploy

```bash
gcloud functions deploy summarize \
  --gen2 \
  --runtime python312 \
  --trigger-http \
  --entry-point summarize \
  --source . \
  --region us-central1 \
  --project $PROJECT_ID \
  --set-secrets 'LLM_API_KEY=openrouter-api-key:latest' \
  --timeout 3600 \
  --memory 1024Mi \
  --no-allow-unauthenticated
```

**What each flag does:**

| Flag | Value | Purpose |
|------|-------|---------|
| `--gen2` | _(flag)_ | Cloud Functions 2nd gen (Cloud Run backing, longer timeouts, more features) |
| `--runtime` | `python312` | Python version matching our code |
| `--trigger-http` | _(flag)_ | HTTP-triggered (not event/background) |
| `--entry-point` | `summarize` | Must match the function name in `main.py` |
| `--source` | `.` | Uploads the entire project root. `.gcloudignore` excludes unnecessary files |
| `--region` | `us-central1` | Iowa. Closest to west coast. Cheapest. |
| `--set-secrets` | `KEY=secret-name:latest` | Binds the Secret Manager secret as env var `LLM_API_KEY` |
| `--timeout` | `3600` | Maximum 60 minutes (LLM calls can take minutes per request) |
| `--memory` | `1024Mi` | 1 GB RAM. Increase to 2048Mi for thinking/reasoning models |
| `--no-allow-unauthenticated` | _(flag)_ | Require auth. No public internet access without OIDC token |

### 7d. Wait for deployment

Deployment takes 2-5 minutes. You'll see:

```text
Deploying function...
Deploying function (may take a while)...
✓ Deploying function...done.
✓ Function deployed.
✓ Function [summarize] is ready.
```

**Get the function URL:**
```bash
export FUNC_URL=$(gcloud functions describe summarize \
  --gen2 --region us-central1 --project $PROJECT_ID \
  --format="value(serviceConfig.uri)")
echo $FUNC_URL
# → https://summarize-123456789-uc.a.run.app
```

---

## 8. Test the Deployed Function

### 8a. Get an auth token

```bash
gcloud auth print-identity-token
# → eyJhbGciOiJSUzI1NiIs...
```

### 8b. Send a test request

Use the included sample request:

```bash
# Get a fresh token (expires after ~1 hour)
TOKEN=$(gcloud auth print-identity-token)
FUNC_URL=$(gcloud functions describe summarize --gen2 --region us-central1 --project $PROJECT_ID --format="value(serviceConfig.uri)")

curl -X POST $FUNC_URL \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d @cloud_function/sample_request.json | jq .
```

**Expected result — successful summary:**
```json
{
  "success": true,
  "summary": {
    "summary_md": "...generated markdown summary...",
    "estimated_visible_words": 412
  },
  "usage": {
    "prompt_tokens": 1234,
    "completion_tokens": 567,
    "total_tokens": 1801,
    "generation_cost": 0.000123
  }
}
```

**Expected result — auth failure (no token):**
```bash
curl -X POST $FUNC_URL -H "Content-Type: application/json" -d @cloud_function/sample_request.json
# → 403 error (requires authentication)
```

### 8c. Test from Python

```bash
uv run python3 -c "
import json, requests, subprocess

FUNC_URL = '$(gcloud functions describe summarize --gen2 --region us-central1 --project $PROJECT_ID --format="value(serviceConfig.uri)")'
TOKEN = subprocess.check_output(['gcloud', 'auth', 'print-identity-token']).decode().strip()

payload = json.load(open('cloud_function/sample_request.json'))
resp = requests.post(FUNC_URL, json=payload,
                     headers={'Authorization': f'Bearer {TOKEN}'}, timeout=600)
print(f'Status: {resp.status_code}')
print(f'Summary length: {len(resp.json()[\"summary\"][\"summary_md\"])} chars')
print(f'Cost: \${resp.json()[\"usage\"][\"generation_cost\"]:.6f}')
"
```

### 8d. View logs

```bash
gcloud functions logs read summarize \
  --gen2 --region us-central1 --project $PROJECT_ID \
  --limit 20
```

---

## 9. Update the Function

### 9a. After code changes

Whenever you modify `cloud_function/main.py`, `cloud_function/handler.py`, or any imported file (`core/*.py`, `scoring.py`), redeploy:

```bash
gcloud functions deploy summarize \
  --gen2 \
  --runtime python312 \
  --trigger-http \
  --entry-point summarize \
  --source . \
  --region us-central1 \
  --project $PROJECT_ID \
  --set-secrets 'LLM_API_KEY=openrouter-api-key:latest' \
  --timeout 3600 \
  --memory 1024Mi \
  --no-allow-unauthenticated
```

Same command as initial deploy. gcloud detects changes and only rebuilds what's needed (~60 seconds re-deploy vs 2-5 minute initial).

### 9b. Test changes quickly (local dev)

For faster iteration **before** deploying, run locally:

```bash
pip install functions-framework
export LLM_API_KEY="sk-or-v1-..."
functions-framework \
  --target summarize \
  --signature-type http \
  --source cloud_function/main.py \
  --port 8080 \
  --debug

# In another terminal:
curl -X POST http://localhost:8080 \
  -H "Content-Type: application/json" \
  -d @cloud_function/sample_request.json | jq .
```

`--debug` enables hot reload — changes to `cloud_function/` or imported modules take effect on the next request without restarting.

---

## 10. Troubleshooting

### 10a. Deployment fails — "unable to evaluate source"

**Problem:** `--source .` uploads everything, including the `runs/` or `data/` directories which may be huge.
**Fix:** Check `.gcloudignore` excludes large dirs. It already excludes:
```
runs/
artifacts/
data/books/
dashboard/
tools/
bench/
```
If you see "Source size exceeds limit", add more exclusions.

### 10b. Function deploys but returns 500

**Check logs:**
```bash
gcloud functions logs read summarize \
  --gen2 --region us-central1 --project $PROJECT_ID \
  --limit 5
```

**Common causes:**
- **Missing import or syntax error** in a file the function imports. Fix: test locally first.
- **API key expired or invalid.** Check: `printf "test" | gcloud secrets versions access latest --secret=openrouter-api-key`
- **Environment variable not bound.** Check: `--set-secrets` flag was included in deploy command.

### 10c. Function deploys but returns 403 on test

**Problem:** `--no-allow-unauthenticated` blocks anonymous requests, but your token isn't valid.
**Fix:** Ensure you run `gcloud auth login` and the token is fresh:
```bash
gcloud auth print-identity-token | cut -d. -f1 | base64 -d | jq .exp
# Check the expiry timestamp
```

### 10d. `gcloud functions deploy` times out

**Fix:** Increase the deploy timeout or retry:
```bash
gcloud config set builds/timeout 600
```

### 10e. Secret Manager secret doesn't appear

**Verify creation:**
```bash
gcloud secrets list --project $PROJECT_ID
gcloud secrets versions list openrouter-api-key --project $PROJECT_ID
```

**Verify the binding:**
```bash
gcloud secrets get-iam-policy openrouter-api-key --project $PROJECT_ID
```
If no binding for your service account, re-run step 6b.

### 10f. Service account email is wrong

The default compute service account pattern is:
```
<PROJECT_NUMBER>-compute@developer.gserviceaccount.com
```
If you use a custom service account, find it:
```bash
gcloud functions describe summarize --gen2 --region us-central1 --project $PROJECT_ID --format="value(serviceConfig.serviceAccountEmail)"
```

---

## Cleanup

To delete everything (stops billing):

```bash
# Delete the function
gcloud functions delete summarize \
  --gen2 --region us-central1 --project $PROJECT_ID --quiet

# Delete the secret and its versions
gcloud secrets delete openrouter-api-key --project $PROJECT_ID --quiet

# Delete the project (removes all resources, billing stops)
gcloud projects delete $PROJECT_ID --quiet
```

---

## Quick Reference

```bash
# Deploy
gcloud functions deploy summarize --gen2 --runtime python312 \
  --trigger-http --entry-point summarize --source . \
  --region us-central1 \
  --set-secrets 'LLM_API_KEY=openrouter-api-key:latest' \
  --timeout 3600 --memory 1024Mi --no-allow-unauthenticated

# Get URL
gcloud functions describe summarize --gen2 --region us-central1 \
  --format="value(serviceConfig.uri)"

# Test
TOKEN=$(gcloud auth print-identity-token)
curl -X POST $FUNC_URL -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d @cloud_function/sample_request.json

# Logs
gcloud functions logs read summarize --gen2 --region us-central1 --limit 20

# Delete
gcloud functions delete summarize --gen2 --region us-central1 --quiet
```
