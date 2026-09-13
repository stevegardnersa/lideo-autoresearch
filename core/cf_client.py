"""Cloud Function transport for the autoresearch evaluation pipeline.

Provides :class:`CloudFunctionClient`, a stdlib-only (``urllib``) client for the
deployed serverless summariser (GCP Cloud Functions v2). It mirrors the
``OpenRouterClient`` surface used by ``core/run_candidate.py`` so that a run can
route every chapter-summary LLM generation through the Cloud Function instead of
calling the provider directly from the local machine.

The local machine never holds the provider API key in this mode — only an OIDC
identity token for the Function's ``--no-allow-unauthenticated`` layer. Tokens
come from ``gcloud`` (interactive), a static env var (CI), or a service-account
key (``AUTH_MODE=sa_key``, non-interactive), see :class:`AuthTokenProvider`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from typing import Any, Dict, Mapping, Optional, Tuple

from core.openrouter_client import (
    GenerationResult,
    ModelUnavailableError,
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

try:
    from core.reasoning import assert_disabling_reasoning_allowed, register_mandatory_reasoning_models
    from candidate_spec import MANDATORY_REASONING_MODELS as _DEFAULT_MANDATORY_MODELS
    register_mandatory_reasoning_models(_DEFAULT_MANDATORY_MODELS)
except ImportError:  # pragma: no cover - standalone runs without core/candidate_spec
    def assert_disabling_reasoning_allowed(model: Any) -> None:
        return None

ROOT = Path(__file__).resolve().parents[1]

# Default judge source truncation, matching core/run_candidate.py default.
DEFAULT_JUDGE_SOURCE_CHAR_LIMIT = 32000

# Gateway "model unavailable" signals (litellm and compatible proxies).
MODEL_UNAVAILABLE_MARKERS = (
    "no healthy deployments",
    "no fallback model group",
    "no deployments available",
)

# Model-unavailable failures are usually transient gateway health flaps that can
# last 30-120s, so they get their own longer bounded backoff (seconds).
MODEL_UNAVAILABLE_BACKOFF = (15.0, 30.0, 60.0, 120.0)
DEFAULT_MODEL_UNAVAILABLE_RETRIES = len(MODEL_UNAVAILABLE_BACKOFF)

# Google OAuth 2.0 token endpoint (assertion audience for the ID-token grant).
_GOOGLE_OAUTH2_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"


def _is_model_unavailable(message: str) -> bool:
    lowered = str(message or "").lower()
    return any(marker in lowered for marker in MODEL_UNAVAILABLE_MARKERS)


class CloudFunctionAuthError(OpenRouterAPIError):
    """Client-wide Cloud Function auth failure (expired/invalid OIDC token).

    Distinct from per-request failures: a run cannot make progress until the
    operator re-authenticates, so callers should pause rather than mark every
    sample as failed.
    """


def is_auth_error(exc: BaseException) -> bool:
    """True when ``exc`` is a Cloud Function auth failure needing re-auth."""
    return isinstance(exc, CloudFunctionAuthError)


class _UrllibHttpResponse:
    """Adapter exposing the ``google.auth`` transport Response surface."""

    def __init__(self, status: int, headers: Mapping[str, str], data: bytes) -> None:
        self._status = int(status)
        self._headers = {str(k).lower(): v for k, v in dict(headers).items()}
        self._data = data

    @property
    def status(self) -> int:
        return self._status

    @property
    def headers(self) -> Mapping[str, str]:
        return self._headers

    @property
    def data(self) -> bytes:
        return self._data


class _UrllibRequest:
    """Stdlib (urllib) adapter for ``google.auth``'s ``Request`` interface.

    The bundled ``google.auth.transport._http_client`` transport only supports
    http, and ``requests``/``urllib3`` are not dependencies of this repo, so we
    provide an https-capable transport using only the standard library.
    """

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: Optional[bytes] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
        **kwargs: Any,
    ) -> _UrllibHttpResponse:
        request = urllib.request.Request(
            url, data=body, headers=dict(headers or {}), method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or 30) as response:
                return _UrllibHttpResponse(
                    response.status, response.headers, response.read()
                )
        except urllib.error.HTTPError as exc:
            return _UrllibHttpResponse(exc.code, exc.headers, exc.read())


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sign_jwt_openssl(signing_input: bytes, private_key_pem: str) -> str:
    """RS256-sign ``signing_input`` with ``private_key_pem`` via the openssl CLI.

    Returns the base64url-encoded (unpadded) signature. Used only when
    ``google-auth`` is not installed, so ``AUTH_MODE=sa_key`` stays usable on a
    stdlib-only interpreter.
    """
    handle, key_path = tempfile.mkstemp(prefix="cf-sa-key-", suffix=".pem")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as key_file:
            key_file.write(private_key_pem)
        os.chmod(key_path, 0o600)
        proc = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", key_path],
            input=signing_input,
            capture_output=True,
            timeout=30,
        )
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", errors="replace")[:300]
            raise CloudFunctionAuthError(f"openssl JWT signing failed: {detail}")
        return _b64url(proc.stdout)
    except FileNotFoundError as exc:
        raise CloudFunctionAuthError(
            "AUTH_MODE=sa_key needs either the 'google-auth' package "
            "(pip install google-auth) or the openssl CLI."
        ) from exc
    finally:
        try:
            os.unlink(key_path)
        except OSError:
            pass


def _local_scoring_sha() -> str:
    try:
        return hashlib.sha256((ROOT / "scoring.py").read_bytes()).hexdigest()
    except OSError:
        return ""


def _thinking_from_payload(payload: Mapping[str, Any]) -> Optional[bool]:
    """Resolve the effective ``thinking`` signal from a request payload.

    Tri-state: ``True``/``False`` from ``extra_body.thinking.type`` (legacy
    transport), else ``None`` when the payload carries no reasoning signal at
    all. ``None`` must propagate so plain requests stay absent (no
    ``reasoning.enabled``) on the CF path, matching the direct path.
    """
    try:
        thinking_cfg = payload["extra_body"]["thinking"]
        if isinstance(thinking_cfg, dict):
            return str(thinking_cfg.get("type")) == "enabled"
        return None
    except (KeyError, TypeError):
        return None


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
    - ``sa_key``: mint a self-signed OIDC ID token from a service-account
      JSON key (``GCP_SA_KEY_FILE``) targeting ``audience``. Does not shell
      out to ``gcloud`` and works non-interactively (CI, no user creds).
    - ``none``: no token (local ``functions-framework`` dev server).

    Every minted token is cached with a 300s expiry skew decoded from the JWT
    ``exp`` claim, so :meth:`get_token` re-mints automatically as it nears
    expiry.
    """

    def __init__(
        self,
        *,
        auth_mode: str = "oidc",
        env_var: str = "GCP_IDENTITY_TOKEN",
        gcloud_timeout: int = 60,
        sa_key_file: str = "",
        audience: str = "",
    ) -> None:
        self.auth_mode = auth_mode
        self.env_var = env_var
        self.gcloud_timeout = int(gcloud_timeout)
        self.sa_key_file = str(sa_key_file or "").strip()
        self.audience = str(audience or "").strip()
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
                raise CloudFunctionAuthError(
                    f"Auth mode 'env' requires the {self.env_var} environment variable to be set."
                )
            return env_token
        if self.auth_mode == "sa_key":
            if env_token:
                return env_token
            return self._fetch_sa_key_token()
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
            raise CloudFunctionAuthError(
                "gcloud CLI not found. Install the Google Cloud SDK or set "
                f"{self.env_var} and use AUTH_MODE=env."
            )
        except subprocess.TimeoutExpired:
            raise CloudFunctionAuthError("gcloud auth print-identity-token timed out.")
        if proc.returncode != 0:
            raise CloudFunctionAuthError(
                f"gcloud auth print-identity-token failed (exit {proc.returncode}): "
                f"{proc.stderr.strip()[:500]}"
            )
        token = proc.stdout.strip()
        if not token:
            raise CloudFunctionAuthError("gcloud auth print-identity-token returned an empty token.")
        return token

    def _resolve_audience(self) -> str:
        audience = (
            self.audience
            or os.environ.get("CF_IDENTITY_AUDIENCE", "").strip()
            or os.environ.get("FUNCTION_URL", "").strip()
        )
        if not audience:
            raise CloudFunctionAuthError(
                "Auth mode 'sa_key' needs an audience for the ID token: set "
                "CF_IDENTITY_AUDIENCE or FUNCTION_URL (or pass identity_audience)."
            )
        return audience

    def _fetch_sa_key_token(self) -> str:
        if not self.sa_key_file:
            raise CloudFunctionAuthError(
                "Auth mode 'sa_key' requires a service-account key path in "
                "GCP_SA_KEY_FILE."
            )
        key_path = os.path.expanduser(self.sa_key_file)
        if not os.path.isabs(key_path):
            key_path = str(ROOT / key_path)
        if not os.path.isfile(key_path):
            raise CloudFunctionAuthError(
                f"Service-account key not found: {key_path} (GCP_SA_KEY_FILE)."
            )
        audience = self._resolve_audience()
        try:
            from google.oauth2 import service_account
        except ImportError:
            return self._mint_sa_key_token_stdlib(key_path, audience)
        try:
            credentials = service_account.IDTokenCredentials.from_service_account_file(
                key_path, target_audience=audience
            )
            credentials.refresh(_UrllibRequest())
        except CloudFunctionAuthError:
            raise
        except Exception as exc:
            raise CloudFunctionAuthError(
                f"Failed to mint ID token from {key_path} for audience {audience!r}: {exc}"
            ) from exc
        token = str(getattr(credentials, "token", "") or "").strip()
        if not token:
            raise CloudFunctionAuthError(
                f"Service-account key {key_path} produced an empty ID token."
            )
        return token

    def _mint_sa_key_token_stdlib(self, key_path: str, audience: str) -> str:
        """Mint an ID token without google-auth: sign a JWT with openssl.

        Mirrors ``google.oauth2.service_account.IDTokenCredentials``: build an
        RS256 JWT authorization-grant assertion and exchange it for a
        Google-issued ID token at the key's ``token_uri``.
        """
        try:
            with open(key_path, "r", encoding="utf-8") as handle:
                info = json.load(handle)
        except (OSError, ValueError) as exc:
            raise CloudFunctionAuthError(
                f"Could not read service-account key {key_path}: {exc}"
            ) from exc
        email = str(info.get("client_email") or "").strip()
        private_key = str(info.get("private_key") or "").strip()
        token_uri = str(info.get("token_uri") or "").strip() or _GOOGLE_OAUTH2_TOKEN_ENDPOINT
        if not email or not private_key:
            raise CloudFunctionAuthError(
                f"Service-account key {key_path} is missing client_email/private_key."
            )
        now = int(time.time())
        header = {"alg": "RS256", "typ": "JWT"}
        payload = {
            "iat": now,
            "exp": now + 3600,
            "iss": email,
            "aud": token_uri,
            "target_audience": audience,
        }
        signing_input = ".".join(
            (
                _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
                _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
            )
        )
        assertion = f"{signing_input}.{_sign_jwt_openssl(signing_input.encode('utf-8'), private_key)}"
        body = urllib.parse.urlencode(
            {
                "assertion": assertion,
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            token_uri,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise CloudFunctionAuthError(
                f"ID token exchange failed ({exc.code}) for {email}: {detail}"
            ) from exc
        token = str((data or {}).get("id_token") or "").strip()
        if not token:
            raise CloudFunctionAuthError(
                f"ID token exchange for {email} returned no id_token."
            )
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
        model_unavailable_retries: int = DEFAULT_MODEL_UNAVAILABLE_RETRIES,
        api_key: str = "",
        sa_key_file: str = "",
        identity_audience: str = "",
    ) -> None:
        self.function_url = str(function_url).rstrip("/")
        if not self.function_url.startswith("http"):
            raise ValueError(f"function_url must be an http(s) URL, got {function_url!r}")
        self.timeout = int(timeout)
        self.max_retries = int(max_retries)
        self.model_unavailable_retries = max(0, int(model_unavailable_retries))
        self.api_key = str(api_key).strip()

        scheme = urllib.parse.urlparse(self.function_url).scheme.lower()
        resolved = str(auth_mode or "auto").strip().lower()
        if resolved == "auto":
            resolved = "none" if scheme != "https" else "oidc"
        if resolved not in {"oidc", "env", "sa_key", "none"}:
            raise ValueError(f"Invalid auth_mode: {auth_mode!r}")
        if resolved == "none" and scheme == "https":
            print(
                "WARN: Cloud Function called over https with AUTH_MODE=none; "
                "unauthenticated requests will receive 403 unless deployed with --allow-unauthenticated."
            )
        self.auth_mode = resolved
        self._auth = AuthTokenProvider(
            auth_mode=resolved,
            sa_key_file=sa_key_file or os.environ.get("GCP_SA_KEY_FILE", ""),
            audience=identity_audience or self.function_url,
        )
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
        model = str(payload.get("model") or "") if isinstance(payload, Mapping) else ""
        auth_refreshed = False
        model_unavailable_attempts = 0
        last_error: Optional[BaseException] = None
        max_attempts = self.max_retries + 1 + self.model_unavailable_retries
        for attempt in range(max_attempts):
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
                    and self.auth_mode in {"oidc", "sa_key"}
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
                if _is_model_unavailable(message):
                    if model_unavailable_attempts < self.model_unavailable_retries:
                        delay = MODEL_UNAVAILABLE_BACKOFF[
                            min(model_unavailable_attempts, len(MODEL_UNAVAILABLE_BACKOFF) - 1)
                        ]
                        model_unavailable_attempts += 1
                        print(
                            f"WARN: model {model or '<unknown>'} has no healthy deployment; "
                            f"retrying in {delay:.0f}s "
                            f"({model_unavailable_attempts}/{self.model_unavailable_retries})."
                        )
                        time.sleep(delay)
                        continue
                    raise ModelUnavailableError(
                        exc.code,
                        self.function_url,
                        message[:1200],
                        model=model,
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
        prompt_meta: Optional[Mapping[str, Any]] = None,
    ) -> GenerationResult:
        body: Dict[str, Any] = {}
        summary_md = str(summary_md or "").strip()
        reasoning: Any = None
        reasoning_effort: Optional[str] = None
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
            reasoning = payload.get("reasoning") if isinstance(payload, dict) else None
            reasoning_effort = payload.get("reasoning_effort") if isinstance(payload, dict) else None

        body["source_md"] = source_md
        body["judge"] = bool(judge)
        body["score"] = bool(score)
        body["target_words"] = int(target_words)
        if thinking is not None:
            body["thinking"] = bool(thinking)
        if reasoning:
            body["reasoning"] = reasoning
        elif reasoning_effort is not None:
            body["reasoning_effort"] = str(reasoning_effort)

        # Client-side guard: never send a disable-reasoning request for a model
        # whose reasoning is mandatory (it would silently reason at default
        # effort). Mirrors core.reasoning.assert_disabling_reasoning_allowed on
        # the direct path; registered via the generated candidate_spec.
        disable_intent = bool(
            thinking is False
            or (isinstance(reasoning, dict) and (
                reasoning.get("enabled") is False
                or str(reasoning.get("effort") or "").strip().lower() == "none"
            ))
            or (reasoning_effort is not None and str(reasoning_effort).strip().lower() == "none")
        )
        if disable_intent:
            assert_disabling_reasoning_allowed(body.get("model") or "")
        body["use_json_schema"] = bool(True if use_json_schema is None else use_json_schema)
        body["judge_source_char_limit"] = int(judge_source_char_limit)
        if prompt_meta:
            stage = str(prompt_meta.get("stage") or "")
            body["prompt_stage"] = "composer" if stage == "composer" else "chapter"
            if prompt_meta.get("version"):
                body["prompt_version"] = str(prompt_meta.get("version"))
            if prompt_meta.get("fingerprint"):
                body["prompt_fingerprint"] = str(prompt_meta.get("fingerprint"))
            body["allow_prompt_override"] = bool(prompt_meta.get("allow_override"))
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