# Plan: Migrate a Next.js App's AI Integration to the Serverless Summariser

Stage: **Ready for hand-off to the consumer-project agent.**
Target consumer stack: **Next.js (App Router), TypeScript, thin REST client, Firebase Hosting backend.**

This plan is the single source of truth for the rework. Everything the other agent needs
to adapt the app's existing LLM integration to the deployed Cloud Function is here:
endpoint contract, auth, a complete reference client, error handling, timeouts,
testing, and deployment.

---

## 1. Overview

The web app today calls the LLM provider (OpenRouter, OpenAI-compatible) directly from its
backend with `model + system_prompt + user_prompt`. A GCP Cloud Functions v2 endpoint
(`summarize`, Python, stdlib-only) now wraps that same provider call **server-side** — the
provider API key lives in the function's Secret Manager and never touches the app.

The function additionally supports:
- **Deterministic scoring** (`rubric` + `target_words`) → `scoring` block in the response
- **LLM judge** (`judge: true`) → `judge_scores` block (5 rubric dimensions + rationale)
- **Judge/scoring-only mode** (`summary_md` present) → re-score/re-judge an existing summary
  with zero generation cost

**Goal:** route the app's summary-generation traffic through the function, keep prompt
composition in the app, wire up generation + scoring + judge, and remove the provider
key from the app's environment.

**Non-goals (explicit scope cuts):**
- No streaming. The function is synchronous request/response (30–180 s typical).
- No browser-side calls. Backend only — the function URL and tokens never reach the client.
- No prompt composition in the function. It is a pure inference endpoint.
- No rubric generation in the function. The app supplies the rubric (see §8).

---

## 2. Source of truth

This plan is generated against the deployed contract in the summariser repo
(`cloud_function/main.py`, `cloud_function/handler.py`, `core/cf_client.py`,
`cloud_function/README.md`). If the contract drifts, regenerate the plan from those files.

**Endpoint:** `POST <FUNCTION_URL>` · `Content-Type: application/json` · no query params.

### 2.1 Request body

| Field | Req | Type | Default | Notes |
|---|---|---|---|---|
| `source_md` | yes | string | — | document/chapter markdown; also the judge input (truncated to `judge_source_char_limit`) |
| `model` | yes | string | — | OpenRouter model id, e.g. `deepseek/deepseek-v4-flash`, `gpt-4o` |
| `system_prompt` | cond | string | — | required unless `summary_md` present |
| `user_prompt` | cond | string | — | required unless `summary_md` present |
| `summary_md` | no | string | `""` | non-empty ⇒ judge/scoring-only mode (prompts optional) |
| `thinking` | no | bool | `false` | reasoning tokens (billed separately, roughly 2× output) |
| `use_json_schema` | no | bool | `true` | structured `{summary_md, estimated_visible_words}` output |
| `target_words` | no | int | `0` | `>0` + `rubric` ⇒ deterministic scoring runs |
| `score` | no | bool | `true` | `false` skips server scoring even if rubric+target_words present |
| `judge` | no | bool | `false` | LLM judge; needs `judge_model` + `rubric` |
| `judge_model` | no | string | `"openai/gpt-4o-mini"` | judge model id |
| `judge_source_char_limit` | no | int | `32000` | judge sees only `source_md[:N]` |
| `rubric` | no | object | — | 7 string-array fields (see below) |

`rubric.*` fields (all optional, arrays of strings):
`headings`, `core_concepts`, `mechanisms_or_explanations`, `critical_qualifiers`,
`important_examples`, `key_entities_or_numbers`, `key_terms`.

Validation rule that matters: `summary_md` present requires `judge=true` **or**
(`rubric` present AND `target_words > 0`); otherwise HTTP 400.

### 2.2 Response (200)

```jsonc
{
  "success": true,
  "summary": { "summary_md": "…", "estimated_visible_words": 412 },
  "usage": {
    "prompt_tokens": 1234,
    "completion_tokens": 567,
    "total_tokens": 1801,
    "reasoning_tokens": 0,
    "cached_prompt_tokens": 0,
    "generation_cost": 0.000123,
    "uncached_generation_cost": 0.000123,
    "generation_id": "gen-abc…",
    "provider_name": "DeepSeek",
    "model_id": "deepseek/deepseek-v4-flash",
    "judge_generation_cost": 0.000041        // 0.0 when no judge ran
  },
  "meta": {
    "scoring_version": "sha256 of scoring.py at deploy",
    "handler_version": "sha256 of handler.py at deploy",
    "deployed_at_utc": "ISO-8601"
  },
  "scoring": {                                // only if rubric + target_words>0 (+ score≠false)
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
      // … read-only; the resolved_* block is what the app consumes
    },
    "resolved_faithfulness": 0.78,
    "resolved_concept_coverage": 0.80,
    "resolved_qualifier_preservation": 0.88,
    "resolved_no_fluff": 0.92,
    "resolved_structure_quality": 0.83,
    "quality": 0.82,
    "utility": 0.79
  },
  "judge_scores": {                           // only if judge=true and judge succeeded
    "faithfulness": 0.85,
    "concept_coverage": 0.80,
    "qualifier_preservation": 0.90,
    "no_fluff": 0.88,
    "structure_quality": 0.82,
    "rationale": "The summary preserves all key concepts…"
  },
  "judge_error": "…"                          // judge=true but judge failed; scoring still present
}
```

### 2.3 Errors

| Status | Meaning | Action |
|---|---|---|
| 400 | missing field / invalid body (`success:false`) | surface to user, fix payload, no retry |
| 401 / 403 | IAM auth rejected | refresh identity token once, retry once |
| 404 | function not deployed / wrong region | no retry; alert ops |
| 408 | request timeout (function side) | retry with backoff |
| 429 | rate limited | retry with backoff |
| 500 | unhandled (incl. provider failure, "insufficient credits") | detect credits string → typed error; else retry |
| 502 / 503 / 504 | transient / host-level | retry with backoff |

Error body (function layer): `{ "success": false, "error": "…" }`.
Cloud Run IAM rejects may return `{ "error": { "code": 403, "message": "…" } }` — the TS
client must tolerate both shapes (see §6.1).

---

## 3. Architecture

```
Browser / UI
   │  (fetch to own backend)
   ▼
Next.js route handler / server action   ← the ONLY place that holds FUNCTION_URL + token
   │  lib/cloud-function.ts
   ▼
POST <FUNCTION_URL>         Authorization: Bearer <OIDC identity token>
   │
   ▼
Cloud Function (Python)     LLM_API_KEY from Secret Manager (server-side only)
   ├─ generate  (unless summary_md given)
   ├─ score     (if rubric + target_words>0)
   └─ judge     (if judge=true)
   │
   ▼
JSON (§2.2) → typed result → UI
```

Rules:
1. **Backend only.** \(FUNCTION_URL\) + bearer tokens never appear in client bundles,
   `NEXT_PUBLIC_*`, or browser code.
2. **Stateless.** Each call is independent; retries and re-queues are safe. There is
   nothing to resume or dedupe server-side.
3. **Prompts are composed in the app** (unchanged from today) and passed as opaque
   strings. The function does not add a data loader or RAG — `source_md` is the full context.

---

## 4. Auth — identity tokens (not access tokens)

Function is deployed `--no-allow-unauthenticated`. Every request needs
`Authorization: Bearer <OIDC identity token>` (the `sub`/`aud` audience is the function
URL). Access tokens from `gcloud auth print-access-token` **will not work** — must be an
identity token.

### 4.1 Decide the runtime (fix this first, it picks the code path)

| App runtime | Token source | Client code |
|---|---|---|
| Firebase App Hosting (Cloud Run) / Cloud Run / Cloud Functions for Firebase | metadata-server ADC | `GoogleAuth().getIdTokenClient(url)` (§5.4) — zero config |
| Vercel / external | env token or SA-key JWT | §4.3 |

**Firebase Hosting + App Hosting is the recommended combination for Next.js.** It runs the
app on Cloud Run (long request ceiling), so it has a metadata server. Confirm the app's
runtime before writing auth code — most of §5.4 depends on it.

### 4.2 IAM (same-project case)

Grant the backend's runtime service account both roles in the **function's project**:

```bash
gcloud projects add-iam-policy-binding <FUNCTION_PROJECT> \
  --member="serviceAccount:<APP_BACKEND_SA>" \
  --role="roles/cloudfunctions.invoker"

# token creator is needed only when the runtime must mint its own identity token
gcloud projects add-iam-policy-binding <APP_PROJECT> \
  --member="serviceAccount:<APP_BACKEND_SA>" \
  --role="roles/iam.serviceAccountTokenCreator"
```

Cross-project: run the invoker grant against `<FUNCTION_PROJECT>` with the app SA as the
member; add iam.gserviceaccounts.getOpenIdToken as needed.

### 4.3 Non-GCP runtime (Vercel)

Option A — static env token: mint an identity token, store in the Vercel secret store,
refresh ~hourly out-of-band:

```ts
// lib/cloud-function-auth.ts
function readEnvToken(): string {
  const t = process.env.GCP_IDENTITY_TOKEN;
  if (!t) throw new CloudFunctionAuthError("GCP_IDENTITY_TOKEN not set");
  return t;
}
```

Option B — service-account key (robust): a SA JSON in secrets (never committed), mint
long-lived process idempotent tokens:

```ts
import { JWT } from "google-auth-library";

const auth = new JWT({
  keyFile: process.env.GCP_SA_KEY_PATH,
  scopes: ["https://www.googleapis.com/auth/cloud-platform"],
});
const client = await auth.getIdTokenClient(FUNCTION_URL);
```

### 4.4 Token lifecycle (all paths)

- Cache the token; decode `exp` from JWT segment 1 (base64url).
- Refresh when `now >= exp - 300s` (300 s skew), or on 401/403 (refresh once, retry once).
- Only mint via gcloud/metadata/http when the cache is cold — at most ~1 mint per hour
  per process.

---

## 5. Full client implementation — `lib/cloud-function.ts`

Drop-in module. Zero runtime deps beyond `google-auth-library` (only for the GCP/SA code
paths; the rest is `fetch`). TypeScript, App Router-safe (server-only import).

### 5.1 Types

```ts
// lib/cloud-function.types.ts
export interface Rubric {
  headings?: string[];
  core_concepts?: string[];
  mechanisms_or_explanations?: string[];
  critical_qualifiers?: string[];
  important_examples?: string[];
  key_entities_or_numbers?: string[];
  key_terms?: string[];
}

export type ThinkingMode = boolean;

export interface GenerateSummaryParams {
  source: string;
  model: string;
  systemPrompt: string;
  userPrompt: string; // typically embeds `source`
  targetWords?: number;    // >0 + rubric ⇒ scoring
  rubric?: Rubric;
  judge?: boolean;
  judgeModel?: string;     // default openai/gpt-4o-mini
  judgeSourceCharLimit?: number; // default 32000
  thinking?: ThinkingMode; // default false
  useJsonSchema?: boolean; // default true
  score?: boolean;         // default true
}

export interface EvaluateSummaryParams {
  source: string;
  model: string;         // trace label only in this mode; no generation happens
  summary: string;       // MANDATORY — switches the function into judge/scoring-only mode
  targetWords?: number;
  rubric?: Rubric;
  judge?: boolean;
  judgeModel?: string;
  judgeSourceCharLimit?: number;
  score?: boolean;
}

export interface UsageInfo {
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
  reasoningTokens: number;
  cachedPromptTokens: number;
  generationCost: number;
  uncachedGenerationCost: number;
  judgeGenerationCost: number;
  generationId: string;
  providerName: string;
  modelId: string;
}

export interface DeployMeta {
  scoringVersion: string;
  handlerVersion: string;
  deployedAtUtc: string;
}

export interface ScoringInfo {
  hardFail: boolean;
  hardFailReasons: string[];
  deterministic: Record<string, number | string | boolean>;
  resolvedFaithfulness: number;
  resolvedConceptCoverage: number;
  resolvedQualifierPreservation: number;
  resolvedNoFluff: number;
  resolvedStructureQuality: number;
  quality: number;
  utility: number;
}

export interface JudgeScores {
  faithfulness: number;
  conceptCoverage: number;
  qualifierPreservation: number;
  noFluff: number;
  structureQuality: number;
  rationale: string;
}

export interface CloudSummaryResult {
  summaryMd: string;
  estimatedVisibleWords: number;
  usage: UsageInfo;
  meta: DeployMeta;
  scoring?: ScoringInfo;
  judgeScores?: JudgeScores;
  judgeError?: string;
  raw: unknown; // full server payload, for debugging/trace
}
```

> Field-name maps used below assume the snake_case response keys; the mapping happens in
> `parseCloudResponse` (§5.5) so the rest of the app sees camelCase only.

### 5.2 Error classes

```ts
// lib/cloud-function-errors.ts
export class CloudFunctionError extends Error {
  constructor(public readonly code: number, message: string) { super(message); }
}

export class CloudFunctionValidationError extends CloudFunctionError {
  constructor(message: string, public readonly details?: string) { super(400, message); }
}

export class CloudFunctionAuthError extends CloudFunctionError {
  constructor(message: string) { super(403, message); }
}

export class CloudFunctionInsufficientCreditsError extends CloudFunctionError {
  constructor(message = "Provider credits exhausted on the server summarizer") { super(402, message); }
}

/** Transient — the caller may retry with backoff. */
export class CloudFunctionTransientError extends CloudFunctionError {
  constructor(code: number, message: string) { super(code, message); }
}

export class CloudFunctionTimeoutError extends CloudFunctionError {
  constructor(message = "Cloud Function request timed out") { super(408, message); }
}

export class CloudFunctionNetworkError extends CloudFunctionError {
  constructor(public readonly cause: unknown) { super(0, `Network error: ${String(cause)}`); }
}
```

### 5.3 Config

```ts
// lib/cloud-function-config.ts
export interface CloudFunctionConfig {
  functionUrl: string;   // from env FUNCTION_URL
  timeoutMs: number;     // from env CF_TIMEOUT (seconds) * 1000; default 600_000
  maxRetries: number;    // default 3
  authMode: "auto" | "oidc" | "env" | "none"; // default auto
}

export function loadCloudFunctionConfig(): CloudFunctionConfig {
  const functionUrl = process.env.FUNCTION_URL ?? process.env.CF_URL ?? "";
  if (!functionUrl) throw new CloudFunctionValidationError("FUNCTION_URL env var not set");
  const rawTimeout = Number(process.env.CF_TIMEOUT ?? "600");
  const timeoutMs = Number.isFinite(rawTimeout) && rawTimeout > 0 ? rawTimeout * 1000 : 600_000;
  return {
    functionUrl,
    timeoutMs,
    maxRetries: 3,
    authMode: (process.env.AUTH_MODE as CloudFunctionConfig["authMode"] ?? "auto"),
  };
}
```

### 5.4 Token provider

```ts
// lib/cloud-function-auth.ts
import type { IdTokenClient } from "google-auth-library";
import { CloudFunctionAuthError } from "./cloud-function-errors";

export type AuthMode = "auto" | "oidc" | "env" | "none";

interface AuthOpts {
  mode: AuthMode;
  functionUrl: string;
  /** Static token for `env` mode (GCP_IDENTITY_TOKEN). */
  staticToken?: string;
  /** True when running where the metadata server exists (App Hosting/Cloud Run). */
  metadataServerAvailable?: boolean;
}

/** Minimal JWT exp decode — base64url segment 1, JSON `exp`. */
export function decodeJwtExp(token: string): number {
  try {
    const b64 = token.split(".")[1];
    const padded = b64 + "=".repeat((4 - (b64.length % 4)) % 4);
    const json = JSON.parse(Buffer.from(padded, "base64url").toString("utf8"));
    const exp = Number(json.exp);
    return Number.isFinite(exp) ? exp : 0;
  } catch { return 0; }
}

const EXPIRY_SKEW_S = 300;

export class TokenProvider {
  private cached: string | null = null;
  private expiresAt = 0;
  private idTokenClient?: IdTokenClient;

  constructor(private readonly opts: AuthOpts) {}

  get enabled(): boolean {
    if (this.opts.mode === "none") return false;
    if (this.opts.mode === "env") return Boolean(this.opts.staticToken);
    return true;
  }

  async getToken(force = false): Promise<string> {
    if (!this.enabled) return "";
    const now = (Date.now() / 1000);
    if (!force && this.cached && this.expiresAt - EXPIRY_SKEW_S > now) {
      return this.cached;
    }
    const token = await this.fetch();
    this.expiresAt = decodeJwtExp(token) || now + 3600;
    this.cached = token;
    return token;
  }

  private async fetch(): Promise<string> {
    if (this.opts.mode === "env") {
      const t = this.opts.staticToken;
      if (!t) throw new CloudFunctionAuthError("env auth requires a static identity token");
      return t;
    }
    const useMetadata = this.opts.metadataServerAvailable !== false;
    if (useMetadata) {
      // Metadata-server ADC path (App Hosting / Cloud Run / Cloud Functions).
      // getIdTokenClient(url) mints an OIDC identity token (audience = the function URL)
      // using the runtime's default credentials — zero config on GCP-managed runtimes.
      const { GoogleAuth } = await import("google-auth-library");
      this.idTokenClient ??= await new GoogleAuth().getIdTokenClient(this.opts.functionUrl);
      const headers = await this.idTokenClient.getRequestHeaders();
      const auth = headers["Authorization"];
      if (!auth) throw new CloudFunctionAuthError(
        "getIdTokenClient returned no Authorization header",
      );
      return auth.replace(/^Bearer\s+/i, "");
    }
    throw new CloudFunctionAuthError(
      "No metadata server and authMode is not 'env'; set GCP_IDENTITY_TOKEN or run on GCP",
    );
  }
}
```

> Note for the implementer: **this is the only line that varies across
> `google-auth-library` majors.** v7–v8 `getIdTokenClient(url)` returns an `IdTokenClient`
> whose `getRequestHeaders()` itself refreshes the token and returns the `Authorization`
> header — the snippet above works for both. If the installed major's types differ, keep the
> *contract*: call `getIdTokenClient(FUNCTION_URL)`, obtain an `Authorization` header, strip
> the `Bearer ` prefix. The error/retry handling (§6) is library-independent.
>
> `getRequestHeaders()` mints on every cold call; the client's internal token cache makes
> subsequent calls cheap. The TokenProvider wrapper keeps the 300s expiry skew on top of it.

### 5.5 Core client + retry/timeout

```ts
// lib/cloud-function.ts
import { loadCloudFunctionConfig, CloudFunctionConfig } from "./cloud-function-config";
import { TokenProvider } from "./cloud-function-auth";
import {
  CloudFunctionError, CloudFunctionAuthError, CloudFunctionInsufficientCreditsError,
  CloudFunctionTransientError, CloudFunctionTimeoutError, CloudFunctionNetworkError,
  CloudFunctionValidationError,
} from "./cloud-function-errors";
import type {
  CloudSummaryResult, Rubric, GenerateSummaryParams, EvaluateSummaryParams,
} from "./cloud-function.types";

const CF_MAX_BODY_BYTES = 32 * 1024 * 1024;
const RETRYABLE = new Set([408, 429, 500, 502, 503, 504]);

export class CloudFunctionClient {
  private readonly cfg: CloudFunctionConfig;
  private readonly auth: TokenProvider;

  constructor(cfg?: Partial<CloudFunctionConfig>) {
    this.cfg = { ...loadCloudFunctionConfig(), ...cfg };
    this.auth = new TokenProvider({
      mode: this.cfg.authMode,
      functionUrl: this.cfg.functionUrl,
      staticToken: process.env.GCP_IDENTITY_TOKEN,
    });
  }

  // ── Public API ────────────────────────────────────────────────────────

  async generateSummary(p: GenerateSummaryParams): Promise<CloudSummaryResult> {
    return this.invoke({ ...p, judgeSourceCharLimit: p.judgeSourceCharLimit ?? 32_000 });
  }

  async evaluateSummary(p: EvaluateSummaryParams): Promise<CloudSummaryResult> {
    return this.invoke({ ...p, judgeSourceCharLimit: p.judgeSourceCharLimit ?? 32_000 });
  }

  // ── Payload assembly (snake_case wire format) ─────────────────────────

  private buildPayload(
    p: GenerateSummaryParams | EvaluateSummaryParams,
  ): Record<string, unknown> {
    const wire: Record<string, unknown> = {
      source_md: p.source,
      model: p.model,
      thinking: p.thinking ?? false,
      use_json_schema: p.useJsonSchema ?? true,
      target_words: p.targetWords ?? 0,
      score: p.score ?? true,
      judge: p.judge ?? false,
      judge_model: p.judgeModel ?? "openai/gpt-4o-mini",
      judge_source_char_limit: p.judgeSourceCharLimit ?? 32_000,
    };
    if (p.rubric) wire.rubric = this.toWireRubric(p.rubric);
    if ("summary" in p && p.summary) {
      wire.summary_md = p.summary;          // judge/scoring-only mode → prompts optional
    } else {
      wire.system_prompt = p.systemPrompt;
      wire.user_prompt = p.userPrompt;
    }
    return wire;
  }

  private toWireRubric(r: Rubric): Record<string, string[]> {
    return {
      headings: r.headings ?? [],
      core_concepts: r.core_concepts ?? [],
      mechanisms_or_explanations: r.mechanisms_or_explanations ?? [],
      critical_qualifiers: r.critical_qualifiers ?? [],
      important_examples: r.important_examples ?? [],
      key_entities_or_numbers: r.key_entities_or_numbers ?? [],
      key_terms: r.key_terms ?? [],
    };
  }

  // ── Transport: POST + auth + retry + timeout ──────────────────────────

  private async invoke(p: GenerateSummaryParams | EvaluateSummaryParams): Promise<CloudSummaryResult> {
    if (p.source.length > 500_000) {
      console.warn("[cloud-function] source_md > 500k chars; timeout/body risk increases");
    }
    const body = JSON.stringify(this.buildPayload(p));

    let authRefreshAttempt = 0;
    let lastError: Error | null = null;

    for (let attempt = 0; attempt <= this.cfg.maxRetries; attempt++) {
      try {
        const headers: Record<string, string> = { "Content-Type": "application/json" };
        const token = await this.auth.getToken(authRefreshAttempt > 0);
        if (token) headers.Authorization = `Bearer ${token}`;

        const res = await fetch(this.cfg.functionUrl, {
          method: "POST",
          headers,
          body,
          // NOTE: keep the signal created per attempt; AbortController is single-fire.
          signal: AbortSignal.timeout(this.cfg.timeoutMs),
        });

        if (res.status === 401 || res.status === 403) {
          if (authRefreshAttempt === 0) { authRefreshAttempt++; continue; } // refresh + retry once
          throw CloudFunctionAuthError.fromStatus(res.status, await this.readError(res));
        }

        const raw = (await res.json()) as Record<string, unknown>;
        if (!res.ok) {
          throw this.mapError(res.status, raw);
        }
        if (raw.success !== true) {
          throw new CloudFunctionValidationError(
            String(raw.error ?? "server returned success:false"),
          );
        }
        return this.parseCloudResponse(raw);
      } catch (err) {
        if (err instanceof CloudFunctionTimeoutError) throw err;           // do not retry past app timeout
        if (err instanceof CloudFunctionTransientError && attempt < this.cfg.maxRetries) {
          await sleep(Math.min(2 ** attempt, 8) * 1000);
          lastError = err;
          continue;
        }
        if (err instanceof CloudFunctionNetworkError && attempt < this.cfg.maxRetries) {
          await sleep(Math.min(2 ** attempt, 8) * 1000);
          lastError = err;
          continue;
        }
        throw err;
      }
    }
    throw lastError ?? new CloudFunctionError(0, "Cloud Function call failed");
  }

  private mapError(status: number, raw: unknown): CloudFunctionError {
    const message = extractError(raw);
    if (status === 402 || /insufficient credits/i.test(message)) {
      return new CloudFunctionInsufficientCreditsError();
    }
    if (RETRYABLE.has(status) && status !== 402) return new CloudFunctionTransientError(status, message);
    if (status === 404) return new CloudFunctionError(404, `Function not found: ${message}`);
    if (status >= 400 && status < 500) return new CloudFunctionValidationError(message);
    return new CloudFunctionTransientError(status, message);
  }

  private async readError(res: Response): Promise<string> {
    try { const j = (await res.json()) as Record<string, unknown>;
      return extractError(j); } catch { return await res.text(); }
  }

  private parseCloudResponse(raw: Record<string, unknown>): CloudSummaryResult {
    const summary = (raw.summary ?? {}) as Record<string, unknown>;
    const usage = (raw.usage ?? {}) as Record<string, unknown>;
    const meta = (raw.meta ?? {}) as Record<string, unknown>;
    const scoring = raw.scoring as Record<string, unknown> | undefined;
    const judge = raw.judge_scores as Record<string, unknown> | undefined;

    return {
      summaryMd: String(summary.summary_md ?? ""),
      estimatedVisibleWords: Number(summary.estimated_visible_words ?? 0),
      usage: {
        promptTokens: num(usage.prompt_tokens), completionTokens: num(usage.completion_tokens),
        totalTokens: num(usage.total_tokens), reasoningTokens: num(usage.reasoning_tokens),
        cachedPromptTokens: num(usage.cached_prompt_tokens),
        generationCost: num(usage.generation_cost), uncachedGenerationCost: num(usage.uncached_generation_cost),
        judgeGenerationCost: num(usage.judge_generation_cost), generationId: str(usage.generation_id),
        providerName: str(usage.provider_name), modelId: str(usage.model_id),
      },
      meta: {
        scoringVersion: str(meta.scoring_version),
        handlerVersion: str(meta.handler_version),
        deployedAtUtc: str(meta.deployed_at_utc),
      },
      scoring: scoring ? {
        hardFail: Boolean(scoring.hard_fail),
        hardFailReasons: Array.isArray(scoring.hard_fail_reasons) ? scoring.hard_fail_reasons.map(String) : [],
        deterministic: (scoring.deterministic ?? {}) as Record<string, number | string | boolean>,
        resolvedFaithfulness: num(scoring.resolved_faithfulness),
        resolvedConceptCoverage: num(scoring.resolved_concept_coverage),
        resolvedQualifierPreservation: num(scoring.resolved_qualifier_preservation),
        resolvedNoFluff: num(scoring.resolved_no_fluff),
        resolvedStructureQuality: num(scoring.resolved_structure_quality),
        quality: num(scoring.quality), utility: num(scoring.utility),
      } : undefined,
      judgeScores: judge ? {
        faithfulness: num(judge.faithfulness), conceptCoverage: num(judge.concept_coverage),
        qualifierPreservation: num(judge.qualifier_preservation), noFluff: num(judge.no_fluff),
        structureQuality: num(judge.structure_quality), rationale: str(judge.rationale),
      } : undefined,
      judgeError: raw.judge_error ? String(raw.judge_error) : undefined,
      raw,
    };
  }
}

// server-only singleton, re-used across requests
import "server-only";
export const cloudFunction = new CloudFunctionClient();

// ── helpers ─────────────────────────────────────────────────────────────
function extractError(raw: unknown): string {
  if (typeof raw === "string") return raw;
  if (raw && typeof raw === "object") {
    const r = raw as Record<string, unknown>;
    if (typeof r.error === "string") return r.error;
    const inner = r.error as Record<string, unknown> | undefined;   // {error:{message}}
    if (inner && typeof inner.message === "string") return inner.message;
    if (typeof r.message === "string") return r.message;
  }
  return "Unknown Cloud Function error";
}
function num(v: unknown): number { const n = Number(v); return Number.isFinite(n) ? n : 0; }
function str(v: unknown): string { return v == null ? "" : String(v); }
function sleep(ms: number): Promise<void> { return new Promise((r) => setTimeout(r, ms)); }
```

### 5.6 Wire into the UI layer (App Router)

```ts
// app/api/summarize/route.ts
import { NextResponse } from "next/server";
import { cloudFunction, CloudFunctionInsufficientCreditsError } from "@/lib/cloud-function";

export async function POST(req: Request) {
  const { source, model, systemPrompt, userPrompt, targetWords, rubric, judge } = await req.json();

  try {
    const result = await cloudFunction.generateSummary({
      source, model, systemPrompt, userPrompt,
      targetWords, rubric: rubric ?? undefined, judge: judge ?? false,
    });
    return NextResponse.json(result);
  } catch (err) {
    if (err instanceof CloudFunctionInsufficientCreditsError) {
      return NextResponse.json({ error: "Summarizer credits exhausted — try again later" }, { status: 503 });
    }
    return NextResponse.json({ error: err instanceof Error ? err.message : "summarizer failure" }, { status: 502 });
  }
}
```

> Client `fetch` to `/api/summarize` can use a long timeout; the route handler itself runs
> on the server runtime (see §7 for runtime ceilings).

---

## 6. Error handling contract

1. **Typed errors** — the client converts every failure into one of the `CloudFunctionError`
   subclasses (§5.2). Route handlers catch by type, never by message sniffing.
2. **Retry matrix** — `CloudFunctionTransientError` (408/429/500/502/503/504) and
   `CloudFunctionNetworkError` (fetch `TypeError`, DNS, TCP reset): up to 3 attempts,
   backoff `min(2^attempt, 8)` s. **Never retry** 400/404, validation, or auth-after-refresh.
3. **Auth** — 401/403 triggers one forced token refresh + one retry; then
   `CloudFunctionAuthError`.
4. **Credits** — any body containing `insufficient credits` (or HTTP 402) → typed
   `CloudFunctionInsufficientCreditsError`; surface a friendly 503 "try later" instead of a
   500 stack.
5. **`success:false` with HTTP 200** — the function always uses non-2xx for failures, but
   the client guards `success !== true` defensively and raises `CloudFunctionValidationError`.
6. **Judge degradation** — `judgeError` is a *successful* outcome; the summary + `scoring`
   remain usable. Do not treat `judgeError` as a request failure.
7. **Logging** — catch-all wrapper in the route handler logs `message`, `code`, `model`,
   and `result.usage` when available; never logs request bodies (may contain full documents)
   or the auth header.

---

## 7. Timeout handling — read before wiring UI

Generations take **30–180 s**; thinking models can go longer. Two ceilings interact:

| Layer | Limit | Where it bites |
|---|---|---|
| Cloud Function | 3600 s | function-side; set at deploy |
| App request | **runtime-dependent** | **the critical constraint** |
| Client | `CF_TIMEOUT` (default 600 s) | `AbortSignal.timeout` — must be < runtime ceiling |

Runtime ceilings (confirm yours):
- Firebase App Hosting / Cloud Run: configurable, up to 3600 s ✔
- Cloud Functions for Firebase: 540 s ✔ (fits 30–180 s, tight for thinking models + slow providers)
- **Vercel: Hobby 60 s / Pro 300–900 s — 60 s can kill a normal generation** ✗

Mitigations if the ceiling is tight:
1. Prefer `thinking: false` + fast models (`deepseek/deepseek-v4-flash`) for interactive calls.
2. Raise the plan/function duration at the host — not `CF_TIMEOUT`.
3. Or queue the job server-side, poll via `evaluateSummary`/status, and show a "processing"
   state — moving the request out of the HTTP window entirely. (Largest change; defer to v2.)

UI guidance when the request is synchronous:
- Show progress state immediately; disable the button for the duration (prevent double-submit —
  each call costs money).
- Surface `CloudFunctionTimeoutError` distinctly: "This is taking longer than expected — try
  again or a faster model."
- Do **not** set client `fetch` timeouts shorter than `CF_TIMEOUT`.

---

## 8. Adapting the existing integration

1. **Find the seam.** Locate where the backend today builds the provider request
   (`model` + `system` + `user` + `temperature` + `max_tokens`). Keep prompt composition
   and model config exactly as-is. Swap only the transport: `cloudFunction.generateSummary(...)`.
2. **The rubric is the genuinely new dependency.** Scoring + judge both require a `rubric`
   (7 static lists per document). It is cheap to reuse from the summariser repo
   (`tools/build_rubrics.py` — same JSON shape), or the app can build its own rubric once
   per document with a single cheap LLM call and **cache it** (database/blob; keyed by
   document id + version). Never regenerate per summary run.
   - content without a rubric → omit `rubric`/`targetWords`; the request proceeds as
     generation-only — graceful degradation for uncached content.
3. **`targetWords`** must be `> 0` for the `scoring` block to appear. Pass the app's
   configured target length.
4. **`judge: true`** for LLM quality scores; read `judgeScores.rationale` for the
   explainability surface. Judge sees only `source_md[:32000]` — if the app's documents
   routinely exceed 32k chars, either raise `judgeSourceCharLimit` or note scores reflect
   only the first 32k chars.
5. **Model picker** stays client-side; any OpenRouter model id is valid. The function does
   not host a model catalog (the app's existing model list is authoritative).
6. **Cost surfacing** — `usage.generationCost` + `usage.judgeGenerationCost` give per-call
   USD. Surface in the UI's admin page if present.

---

## 9. Config & env (app side)

```
# .env.local / deploy env / secret store
FUNCTION_URL=https://us-central1-<FUNCTION_PROJECT>.cloudfunctions.net/summarize
CF_TIMEOUT=600                 # seconds; keep < host runtime ceiling
AUTH_MODE=auto                 # auto | oidc | env | none
# auth choose one by runtime:
#  GCP-managed:                 nothing extra (metadata server)
#  static token (Vercel):       GCP_IDENTITY_TOKEN=<minted identity token, ~1h TTL>
#  service-account (Vercel):    GCP_SA_KEY_PATH=/run/secrets/cloud-fn-sa.json
```

Never send `api_key` or `base_url` in the request — the function's Secret Manager key is
the only credential for the provider.

---

## 10. Testing plan

1. **Contract tests, offline (app CI)** — a tiny mock server implementing §2.1/§2.2
   (30 lines, `node:http`). Unit-test the client: request body shape (camelCase→snake_case),
   response mapping, `success:false`, 401/403 refresh flow, 429/500 backoff, timeout abort.
   Use vitest `vi.useFakeTimers()` for backoff assertions.
2. **Free function smoke (no LLM spend)** — against the running function:
   - scoring-only: `summary_md` + `rubric` + `targetWords=50` → expect 200 with `scoring`,
     `usage` zeros, no `summary` generation. Free.
   - `mean judge-only` mode returns 400 w/o rubric+target_words or judge — validates the
     guard.
3. **Staging E2E** — one production-shaped document; assert `summaryMd` non-empty,
   `scoring.quality` and `judgeScores.*` present, `usage.costs` sane, `meta` present.
4. **Auth matrix** — no token → 401/403; stale token → auto-refresh → success
   (verify on the real deployed runtime, metadata-ADC path).
5. **Rollback drill** — with the feature flag ON, verify old path is one env flip away (§11).

Fixtures can be copied from the summariser repo: `cloud_function/sample_request.json`
(request) and the README's example (response).

---

## 11. Deployment steps

### 11.1 Function side (summariser repo — confirm, don't build)

The function must already be deployed with the `meta` block (deployed after commit
`564d301`). Verify:

```bash
FUNCTION_URL=$(
  gcloud functions describe summarize --region us-central1 --gen2 --format='value(url)'
)
curl -sf "$FUNCTION_URL" -X POST -H "Content-Type: application/json" \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  -d '{"source_md":"x","model":"m","summary_md":"s","judge":true,"rubric":{"headings":["a"]},"target_words":10}' \
  | jq '.meta'   # expect scoring_version/handler_version/deployed_at_utc
```

If `meta` is absent → redeploy the function (see summariser repo `cloud_function/README.md`),
then re-verify. Capture `meta.scoring_version` into the app's runtime config for change
detection.

### 11.2 IAM

```bash
# same project: grant the backend's runtime SA the invoker role (run against FUNCTION_PROJECT)
gcloud projects add-iam-policy-binding <FUNCTION_PROJECT> \
  --member="serviceAccount:<APP_BACKEND_SA>" --role="roles/cloudfunctions.invoker"

gcloud projects add-iam-policy-binding <APP_PROJECT> \
  --member="serviceAccount:<APP_BACKEND_SA>" --role="roles/iam.serviceAccountTokenCreator"
```

Find the SA: App Hosting → your project → compute default SA; Firebase App Hosting uses
`<PROJECT_NUMBER>[-compute]@developer.gserviceaccount.com` or a project-owned SA. Cloud Run
shows it under IAM → Service Accounts / Cloud Run service.

### 11.3 App deploy (Firebase)

1. Add env vars to the backend runtime config (§9): `FUNCTION_URL`, `CF_TIMEOUT`,
   `AUTH_MODE`. Secrets (`GCP_IDENTITY_TOKEN`, `GCP_SA_KEY_PATH`) via the secret store —
   never plain env for tokens/keys.
2. Deploy the Next.js backend per your App Hosting/functions-for-Firebase workflow:
   ```bash
   firebase deploy --only hosting:backend          # hosting layer
   # plus the app's backend runtime (App Hosting / Cloud Run / Cloud Functions)
   ```
   (Exact targets depend on the confirmed hosting shape — §4.1.)
3. Post-deploy smoke: call `/api/summarize` in staging with a tiny document → expect 200 +
   `summary.summary_md`.

### 11.4 Rollout (flag-gated)

1. **Feature flag** `use-cloud-function` (default **off**): `CF_ON=true` routes to the
   function; `CF_ON` absent/false keeps the existing direct-provider transport. The two
   transports share the same prompts/results interface — the switch is one env var.
2. **Shadow (optional, one doc):** call both paths, log `summary_md` equality + cost diff.
   Terminate both provider calls before cutover.
3. **Cutover:** set `CF_ON=true` in staging, verify, then prod. Run one end-to-end flow.
4. **Rollback:** flip `CF_ON=false` (provider key re-attached to app env) — instant.

### 11.5 Cleanup (post-cutover, ≥ 1 week)

- Remove the direct-provider transport + the provider key from the app's deploy config.
- Delete `GCP_IDENTITY_TOKEN`/SA-key from app secrets only after the metadata-ADC path is
  confirmed on the deployed runtime.
- At that point the app holds no provider credentials at all.

---

## 12. Acceptance checklist (for the consumer agent)

- [ ] `FUNCTION_URL` verified deployed with `meta`; `meta.scoring_version` captured.
- [ ] Backend holds **no** provider key; all summary traffic → function.
- [ ] `generateSummary` returns `summaryMd` + usage; prompt composition is byte-identical
      with the pre-migration path for the same inputs.
- [ ] `rubric` + `targetWords>0` → `scoring.quality`/`utility` surfaced; judge → `judgeScores`
      + `rationale`; `judgeError` degrades to scoring-only without failing the request.
- [ ] Auth proven on the deployed runtime (metadata-ADC or `env`); stale-token auto-refresh
      tested; 401/403 post-refresh surfaces typed `CloudFunctionAuthError`.
- [ ] Retry matrix + backoff unit-tested (fake timers); 4xx not retried; timeout aborts and
      does not retry.
- [ ] UI shows progress; double-submit prevented; `CloudFunctionTimeoutError` handled.
- [ ] Offline contract tests + staging E2E green; `CF_ON=false` fallback intact; rollback
      rehearsed.
- [ ] `usage.generationCost` + `judgeGenerationCost` logged per request.

## 13. Confirmations the web-app team owns

1. Hosting shape (App Hosting vs Hosting+Cloud Functions vs Vercel) → fixes §4 auth path and
   §7 runtime ceiling.
2. Function endpoint URL/region + confirmed deployed ≥ `564d301` (`meta` present).
3. Backend service-account email → IAM grants in §11.2.
4. Rubric producer (summariser repo `tools/build_rubrics.py` vs in-app builder) + cache store.
5. Whether judge truncation at 32k chars is acceptable for the app's document sizes.