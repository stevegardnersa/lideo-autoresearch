"""Cloud Function transport for the autoresearch evaluation pipeline.

Provides :class:`CloudFunctionClient`, a stdlib-only (``urllib``) client for the
deployed serverless summariser (GCP Cloud Functions v2). It mirrors the
``OpenRouterClient`` surface used by ``core/run_candidate.py`` so that a run can
route every chapter-summary LLM generation through the Cloud Function instead of
calling the provider directly from the local machine.

The local machine never holds the provider API key in this mode — only an OIDC
identity token for the Function's ``--no-allow-unauthenticated`` layer.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from typing import Any, Dict, Mapping, Optional, Tuple

from core.openrouter_client import (
    GenerationResult,
    OpenRouterAPIError,
    OpenRouterHTTPError,
    OpenRouterInsufficientCreditsError,
    UsageRecord,
    _as_float,
    _as_int,
    _extract_message_text,
    _parse_error_response,
)
from scoring import JudgeScores, Rubric, visible_word_count

ROOT = Path(__file__).resolve().parents[1]

# Default judge source truncation, matching core/run_candidate.py default.
DEFAULT_JUDGE_SOURCE_CHAR_LIMIT = 32000


def _local_scoring_sha() -> str:
    try:
        return hashlib.sha256((ROOT / "scoring.py").read_bytes()).hexdigest()
    except OSError:
        return ""


def _thinking_from_payload(payload: Mapping[str, Any]) -> bool:
    try:
        return str(payload["extra_body"]["thinking"]["type"]) == "enabled"
    except (KeyError, TypeError):
        return False


def parse_cf_judge_scores(response: Mapping) -> Optional[Tuple[JudgeScores, str]]:
    """Return ``(scores, rationale)`` parsed from a CF response, or None.

    ``rationale`` is ``resp['judge_scores']['rationale']`` (may be empty).
    """
    judge_raw = (response or {}).get("judge_scores")
    if not isinstance(judge_raw, Mapping) or judge_raw.get("faithfulness") is None:
        return None
    scores = JudgeScores(
        faithfulness=_as_float(judge_raw.get("faithfulness")),
        concept_coverage=_as_float(judge_raw.get("concept_coverage")),
        qualifier_preservation=_as_float(judge_raw.get("qualifier_preservation")),
        no_fluff=_as_float(judge_raw.get("no_fluff")),
        structure_quality=_as_float(judge_raw.get("structure_quality")),
    ).clamped()
    rationale = str(judge_raw.get("rationale", "") or "").strip()
    return scores, rationale


class AuthTokenProvider:
    """Cached OIDC identity-token provider for authenticated CF calls.

    Modes:
    - ``env``: read the token from ``GCP_IDENTITY_TOKEN`` every request.
    - ``oidc``: source order — ``GCP_IDENTITY_TOKEN`` env (CI), else
      ``gcloud auth print-identity-token`` subprocess. Cached with a 300s
      expiry skew decoded from the JWT ``exp`` claim.
    - ``none``: no token (local ``functions-framework`` dev server).
    """

    def __init__(
        self,
        *,
        auth_mode: str = "oidc",
        env_var: str = "GCP_IDENTITY_TOKEN",
        gcloud_timeout: int = 60,
    ) -> None:
        self.auth_mode = auth_mode
        self.env_var = env_var
        self.gcloud_timeout = int(gcloud_timeout)
        self._token = ""
        self._expires_at = 0.0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.auth_mode != "none"

    def get_token(self, *, force_refresh: bool = False) -> str:
        if not self.enabled:
            return ""
        with self._lock:
            if not force_refresh and self._token and time.time() < self._expires_at - 300:
                return self._token
            token = self._fetch_token()
            expires = self._decode_exp(token)
            self._token = token
            self._expires_at = expires if expires > 0 else time.time() + 3600.0
            return token

    def _fetch_token(self) -> str:
        env_token = os.environ.get(self.env_var, "").strip()
        if self.auth_mode == "env":
            if not env_token:
                raise OpenRouterAPIError(
                    f"Auth mode 'env' requires the {self.env_var} environment variable to be set."
                )
            return env_token
        if env_token:
            return env_token
        try:
            proc = subprocess.run(
                ["gcloud", "auth", "print-identity-token"],
                capture_output=True,
                text=True,
                timeout=self.gcloud_timeout,
            )
        except FileNotFoundError:
            raise OpenRouterAPIError(
                "gcloud CLI not found. Install the Google Cloud SDK or set "
                f"{self.env_var} and use AUTH_MODE=env."
            )
        except subprocess.TimeoutExpired:
            raise OpenRouterAPIError("gcloud auth print-identity-token timed out.")
        if proc.returncode != 0:
            raise OpenRouterAPIError(
                f"gcloud auth print-identity-token failed (exit {proc.returncode}): "
                f"{proc.stderr.strip()[:500]}"
            )
        token = proc.stdout.strip()
        if not token:
            raise OpenRouterAPIError("gcloud auth print-identity-token returned an empty token.")
        return token

    @staticmethod
    def _decode_exp(token: str) -> float:
        try:
            payload_b64 = token.split(".")[1]
            padding = "=" * (-len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
            return float(payload.get("exp") or 0.0)
        except Exception:
            return 0.0


class CloudFunctionClient:
    """Minimal ``urllib`` client for the deployed chapter-summarizer Function.

    Implements the subset of the ``OpenRouterClient`` surface that
    ``core/run_candidate.py`` uses, routed through the Cloud Function.
    """

    def __init__(
        self,
        *,
        function_url: str,
        auth_mode: str = "oidc",
        timeout: int = 600,
        max_retries: int = 3,
        api_key: str = "",
    ) -> None:
        self.function_url = str(function_url).rstrip("/")
        if not self.function_url.startswith("http"):
            raise ValueError(f"function_url must be an http(s) URL, got {function_url!r}")
        self.timeout = int(timeout)
        self.max_retries = int(max_retries)
        self.api_key = str(api_key).strip()

        scheme = urllib.parse.urlparse(self.function_url).scheme.lower()
        resolved = str(auth_mode or "auto").strip().lower()
        if resolved == "auto":
            resolved = "none" if scheme != "https" else "oidc"
        if resolved not in {"oidc", "env", "none"}:
            raise ValueError(f"Invalid auth_mode: {auth_mode!r}")
        if resolved == "none" and scheme == "https":
            print(
                "WARN: Cloud Function called over https with AUTH_MODE=none; "
                "unauthenticated requests will receive 403 unless deployed with --allow-unauthenticated."
            )
        self.auth_mode = resolved
        self._auth = AuthTokenProvider(auth_mode=resolved)
        self.last_meta: Dict[str, Any] = {}
        self.scoring_version_mismatch = False

    # ── Auth helpers ────────────────────────────────────────────────────

    def auth_headers(self, *, force_refresh: bool = False) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        elif self.auth_mode != "none":
            headers["Authorization"] = f"Bearer {self._auth.get_token(force_refresh=force_refresh)}"
        return headers

    # ── Unsupported OpenRouterClient surface (CF mode) ──────────────────

    def get_credits(self, *, api_key_override: str = ""):
        raise OpenRouterAPIError("credits endpoint not available in Cloud Function mode")

    def fetch_models(self, *, refresh: bool = False):
        raise OpenRouterAPIError("model catalog not available in Cloud Function mode")

    def supports_parameter(self, model_id: str, parameter: str) -> bool:
        return False

    def estimate_uncached_cost(self, model_id: str, usage: UsageRecord) -> float:
        return float(usage.generation_cost)

    # ── Transport ───────────────────────────────────────────────────────

    def _request_json(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        auth_refreshed = False
        last_error: Optional[BaseException] = None
        for attempt in range(self.max_retries + 1):
            request = urllib.request.Request(
                self.function_url,
                data=body,
                headers={"Content-Type": "application/json", **self.auth_headers()},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    charset = response.headers.get_content_charset() or "utf-8"
                    return json.loads(response.read().decode(charset))
            except urllib.error.HTTPError as exc:
                response_text = exc.read().decode("utf-8", errors="replace")
                message, error_payload = _parse_error_response(response_text)
                if (
                    exc.code in (401, 403)
                    and not self.api_key
                    and self.auth_mode == "oidc"
                    and not auth_refreshed
                ):
                    auth_refreshed = True
                    try:
                        self.auth_headers(force_refresh=True)
                    except OpenRouterAPIError:
                        raise OpenRouterHTTPError(
                            exc.code,
                            self.function_url,
                            message[:1200],
                            response_text=response_text[:4000],
                            error_payload=error_payload,
                        )
                    continue
                if exc.code == 402 or "insufficient credits" in message.lower():
                    raise OpenRouterInsufficientCreditsError(
                        exc.code,
                        self.function_url,
                        message[:1200],
                        response_text=response_text[:4000],
                        error_payload=error_payload,
                    )
                if exc.code == 404:
                    raise OpenRouterHTTPError(
                        404,
                        self.function_url,
                        message[:1200],
                        response_text=response_text[:4000],
                        error_payload=error_payload,
                    )
                if exc.code in {408, 409, 429, 500, 502, 503, 504} and attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                last_error = OpenRouterHTTPError(
                    exc.code,
                    self.function_url,
                    message[:1200],
                    response_text=response_text[:4000],
                    error_payload=error_payload,
                )
                raise last_error
            except urllib.error.URLError as exc:
                last_error = OpenRouterAPIError(f"Network error for {self.function_url}: {exc}")
                if attempt >= self.max_retries:
                    raise last_error
                time.sleep(min(2 ** attempt, 8))
        assert last_error is not None
        raise last_error

    # ── Chat completion (generation / judge-only) ───────────────────────

    def chat_completion(
        self,
        payload: Mapping[str, Any],
        *,
        source_md: str = "",
        judge: bool = False,
        judge_model: str = "",
        judge_source_char_limit: int = DEFAULT_JUDGE_SOURCE_CHAR_LIMIT,
        summary_md: str = "",
        score: bool = True,
        rubric: Optional[Rubric] = None,
        target_words: int = 0,
        thinking: Optional[bool] = None,
        use_json_schema: Optional[bool] = None,
    ) -> GenerationResult:
        body: Dict[str, Any] = {}
        summary_md = str(summary_md or "").strip()
        if summary_md:
            body["summary_md"] = summary_md
            body["model"] = str(payload.get("model") or judge_model or "")
        else:
            model = str(payload.get("model") or "")
            if not model:
                raise OpenRouterAPIError("Cloud Function request is missing 'model'")
            messages = list(payload.get("messages") or [])
            if not messages:
                raise OpenRouterAPIError("Cloud Function request is missing 'messages'")
            system_prompt = _extract_message_text(messages[0].get("content", "")) if messages else ""
            user_prompt = (
                _extract_message_text(messages[1].get("content", "")) if len(messages) > 1 else ""
            )
            body["model"] = model
            body["system_prompt"] = system_prompt
            body["user_prompt"] = user_prompt
            if thinking is None:
                thinking = _thinking_from_payload(payload)
            if use_json_schema is None:
                use_json_schema = bool(payload.get("response_format"))

        body["source_md"] = source_md
        body["judge"] = bool(judge)
        body["score"] = bool(score)
        body["target_words"] = int(target_words)
        body["thinking"] = bool(thinking if thinking is not None else False)
        body["use_json_schema"] = bool(True if use_json_schema is None else use_json_schema)
        body["judge_source_char_limit"] = int(judge_source_char_limit)
        if judge_model:
            body["judge_model"] = str(judge_model)
        if rubric is not None:
            body["rubric"] = asdict(rubric)

        if len(source_md) > 500_000:
            print(
                f"WARN: source_md is {len(source_md)} chars; Cloud Function has a 32MB body limit "
                "but large payloads increase risk of timeouts."
            )

        response = self._request_json(body)
        self._record_meta(response)

        summary_data = response.get("summary") or {}
        if not isinstance(summary_data, Mapping):
            raise OpenRouterAPIError(f"Cloud Function response missing 'summary' block: {response}")
        summary_md_out = str(summary_data.get("summary_md") or "").strip()
        estimated = _as_int(summary_data.get("estimated_visible_words"), 0)
        if not estimated and summary_md_out:
            estimated = visible_word_count(summary_md_out)

        usage = self._parse_usage(response, model_id=str(body.get("model") or ""))
        return GenerationResult(
            summary_md=summary_md_out,
            estimated_visible_words=estimated,
            raw_content=summary_md_out,
            usage=usage,
            raw_response=dict(response),
            model_id=str(body.get("model") or ""),
            parsed_json=dict(summary_data) if summary_data else None,
        )

    def _parse_usage(self, response: Mapping[str, Any], *, model_id: str) -> UsageRecord:
        usage = response.get("usage") or {}
        if not isinstance(usage, Mapping):
            usage = {}
        return UsageRecord(
            prompt_tokens=_as_int(usage.get("prompt_tokens")),
            completion_tokens=_as_int(usage.get("completion_tokens")),
            total_tokens=_as_int(usage.get("total_tokens")),
            reasoning_tokens=_as_int(usage.get("reasoning_tokens")),
            cached_prompt_tokens=_as_int(usage.get("cached_prompt_tokens")),
            generation_cost=_as_float(usage.get("generation_cost")),
            uncached_generation_cost=_as_float(usage.get("uncached_generation_cost")),
            generation_id=str(usage.get("generation_id") or ""),
            provider_name=str(usage.get("provider_name") or ""),
            model_id=str(usage.get("model_id") or model_id),
            raw=dict(usage),
        )

    def _record_meta(self, response: Mapping[str, Any]) -> None:
        meta = response.get("meta")
        if not isinstance(meta, Mapping):
            return
        self.last_meta = dict(meta)
        local = _local_scoring_sha()
        server = str(meta.get("scoring_version") or "")
        if server and local and server != local:
            self.scoring_version_mismatch = True