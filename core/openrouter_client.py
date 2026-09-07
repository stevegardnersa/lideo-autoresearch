from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple


class OpenRouterAPIError(RuntimeError):
    """Raised when the OpenRouter API returns a terminal error."""


class OpenRouterHTTPError(OpenRouterAPIError):
    """Structured HTTP error returned by the OpenRouter API."""

    def __init__(
        self,
        status_code: int,
        path: str,
        message: str,
        *,
        response_text: str = "",
        error_payload: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.status_code = int(status_code)
        self.path = str(path)
        self.response_text = str(response_text)
        self.error_payload = dict(error_payload) if isinstance(error_payload, Mapping) else None
        super().__init__(f"OpenRouter HTTP {self.status_code} for {self.path}: {message}")


class OpenRouterInsufficientCreditsError(OpenRouterHTTPError):
    """Raised when OpenRouter returns HTTP 402 insufficient credits."""


@dataclass(frozen=True)
class ModelPricingTier:
    prompt: float = 0.0
    completion: float = 0.0
    request: float = 0.0
    input_cache_read: float = 0.0
    min_context: int = 0


@dataclass(frozen=True)
class ModelInfo:
    model_id: str
    context_length: int = 0
    pricing: Tuple[ModelPricingTier, ...] = ()
    supported_parameters: Tuple[str, ...] = ()
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UsageRecord:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0
    cached_prompt_tokens: int = 0
    generation_cost: float = 0.0
    uncached_generation_cost: float = 0.0
    generation_id: str = ""
    provider_name: str = ""
    model_id: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GenerationResult:
    summary_md: str = ""
    estimated_visible_words: int = 0
    raw_content: str = ""
    usage: UsageRecord = field(default_factory=UsageRecord)
    raw_response: Dict[str, Any] = field(default_factory=dict)
    model_id: str = ""
    parsed_json: Optional[Dict[str, Any]] = None
    finish_reason: Optional[str] = None
    json_recovery: str = "exact"

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"


@dataclass(frozen=True)
class CreditsRecord:
    total_credits: float = 0.0
    total_usage: float = 0.0
    remaining_credits: float = 0.0
    raw: Dict[str, Any] = field(default_factory=dict)


UsageBreakdown = UsageRecord
ChatResult = GenerationResult


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default



def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default



def _strip_markdown_light(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"(^|\n)\s{0,3}#{1,6}\s+", r"\1", text)
    text = re.sub(r"(^|\n)\s{0,3}[-*+]\s+", r"\1", text)
    text = re.sub(r"(^|\n)\s{0,3}\d+[.)]\s+", r"\1", text)
    text = text.replace("**", "").replace("__", "").replace("*", "").replace("_", "")
    text = re.sub(r"<[^>]+>", " ", text)
    return text



def _visible_word_count(text: str) -> int:
    stripped = _strip_markdown_light(text)
    return len(re.findall(r"\b\w+\b", stripped, flags=re.UNICODE))



def _extract_message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content") or ""
                if text:
                    parts.append(str(text))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return str(content or "")



def _unwrap_fenced_json(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].startswith("```"):
        return "\n".join(lines[1:-1]).strip()
    return stripped



def _try_parse_json(text: str) -> Optional[Dict[str, Any]]:
    candidate = _unwrap_fenced_json(text)
    if not candidate:
        return None
    if candidate.startswith("json\n"):
        candidate = candidate.split("\n", 1)[1].strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _escape_ctrl_in_strings(text: str) -> str:
    """Escape raw control chars (\\n, \\t, \\r) that sit INSIDE JSON strings.

    Models occasionally emit literal newlines/tabs inside string values. The
    JSON spec forbids that; json.loads raises 'Invalid control character'.
    This rewrites only the control chars that are inside a string, leaving the
    structural whitespace outside strings untouched."""
    out: list[str] = []
    in_string = False
    escaped = False
    escape_map = {"\n": "\\n", "\t": "\\t", "\r": "\\r"}
    for ch in text:
        if in_string:
            if escaped:
                out.append(ch)
                escaped = False
                continue
            if ch == "\\":
                out.append(ch)
                escaped = True
                continue
            if ch == '"':
                out.append(ch)
                in_string = False
                continue
            if ch in escape_map:
                out.append(escape_map[ch])
                continue
            out.append(ch)
            continue
        if ch == '"':
            out.append(ch)
            in_string = True
            continue
        out.append(ch)
    return "".join(out)


def _first_json_object(text: str) -> str:
    """Cut text at the end of the first balanced JSON object.

    Handles strings (including escaped quotes), so braces inside string
    values do not count toward depth."""
    depth = 0
    in_string = False
    escaped = False
    for index, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            depth += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                return text[: index + 1]
    return text


def _decode_json_string(raw: str) -> str:
    """Decode the inner content of a JSON string (text between the quotes).

    Tolerates an unterminated trailing escape sequence (truncated output),
    which json.loads rejects with 'Invalid \\escape'."""
    out: list[str] = []
    index = 0
    while index < len(raw):
        ch = raw[index]
        if ch == "\\" and index + 1 < len(raw):
            nxt = raw[index + 1]
            simple = {
                "n": "\n",
                "t": "\t",
                "r": "\r",
                "b": "\b",
                "f": "\f",
                '"': '"',
                "\\": "\\",
                "/": "/",
            }
            if nxt in simple:
                out.append(simple[nxt])
                index += 2
                continue
            if nxt == "u" and index + 5 <= len(raw):
                try:
                    out.append(chr(int(raw[index + 2 : index + 6], 16)))
                    index += 6
                    continue
                except ValueError:
                    pass
            out.append(nxt)
            index += 2
            continue
        out.append(ch)
        index += 1
    return "".join(out)


def _salvage_truncated_json(text: str) -> Optional[Dict[str, Any]]:
    """Recover a summary_md from truncated JSON output (finish_reason == length).

    The model ran out of output budget mid-payload: the closing of the
    summary_md string (or the whole payload tail) is missing. Takes everything
    from the start of the summary_md value to end-of-text, decodes it, and
    drops an incomplete trailing token."""
    key_start = text.find('"summary_md"')
    while key_start != -1:
        colon = text.find(":", key_start)
        if colon == -1:
            return None
        quote = text.find('"', colon + 1)
        if quote == -1:
            return None
        raw = text[quote + 1 :]
        end = None
        escaped = False
        for index, ch in enumerate(raw):
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == '"':
                end = index
                break
        value = _decode_json_string(raw if end is None else raw[:end])
        if end is None:
            value = re.sub(r"\s+\S*$", "", value.rstrip())
        value = value.strip()
        if value:
            return {"summary_md": value}
        key_start = text.find('"summary_md"', colon + 1)
    return None


def _recover_json_payload(text: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """Parse model JSON, trying successively more aggressive repairs.

    Returns (payload, mode) where mode describes how the payload was obtained:
      'exact'        - parsed cleanly first try
      'exact_parsed' - provider's structured 'parsed' message (used upstream)
      'escaped'      - raw control chars inside strings escaped
      'truncate_obj'  - trailing garbage after first balanced JSON object
      'escaped_truncate_obj' - both of the above
      'salvaged'      - truncated JSON: summary_md recovered to end-of-text
      None            - unrecoverable; text is not (repair-wise) JSON
    """
    candidate = _unwrap_fenced_json(text)
    if not candidate:
        return None, "None"
    if candidate.startswith("json\n"):
        candidate = candidate.split("\n", 1)[1].strip()

    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed, "exact"
    except json.JSONDecodeError:
        pass

    escaped = _escape_ctrl_in_strings(candidate)
    modes = (
        ("escaped", escaped),
        ("truncate_obj", _first_json_object(candidate)),
        ("escaped_truncate_obj", _first_json_object(escaped)),
    )
    for mode, cand in modes:
        try:
            parsed = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed, mode

    salvaged = _salvage_truncated_json(candidate)
    if isinstance(salvaged, dict):
        return salvaged, "salvaged"
    return None, "None"


def _extract_parsed_payload(
    response: Mapping[str, Any], content_text: str
) -> Tuple[Optional[Dict[str, Any]], str]:
    choices = response.get("choices") or []
    if not choices:
        return None, "None"
    message = choices[0].get("message") or {}
    parsed = message.get("parsed")
    if isinstance(parsed, dict):
        return dict(parsed), "exact_parsed"
    return _recover_json_payload(content_text)



def _parse_error_response(response_text: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    raw_text = str(response_text or "")
    message = raw_text.strip()
    payload: Optional[Dict[str, Any]] = None
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        payload = parsed
        error_block = parsed.get("error")
        if isinstance(error_block, Mapping):
            message = str(error_block.get("message") or message or "")
    return message or "OpenRouter API error", payload


class OpenRouterClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        http_referer: str = "",
        x_title: str = "",
        timeout: int = 90,
        max_retries: int = 5,
        pricing_snapshot: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> None:
        if not api_key:
            raise ValueError("OpenRouter API key is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.http_referer = http_referer.strip()
        self.x_title = x_title.strip()
        self.timeout = timeout
        self.max_retries = max_retries
        self.pricing_snapshot = {
            str(key): dict(value)
            for key, value in (pricing_snapshot or {}).items()
            if isinstance(value, Mapping)
        }
        self._model_cache: Dict[str, ModelInfo] = {}

    @classmethod
    def from_env(
        cls,
        *,
        api_key_env: str = "OPENROUTER_API_KEY",
        pricing_snapshot_path: str | os.PathLike[str] | None = None,
        referer: str = "",
        title: str = "",
        base_url: str = "https://openrouter.ai/api/v1",
        timeout: int = 90,
        max_retries: int = 5,
    ) -> "OpenRouterClient":
        api_key = os.getenv(api_key_env, "")
        if not api_key:
            raise ValueError(f"Environment variable {api_key_env} is required")

        snapshot: Dict[str, Mapping[str, Any]] = {}
        if pricing_snapshot_path:
            snapshot_path = Path(pricing_snapshot_path)
            if snapshot_path.exists():
                loaded = json.loads(snapshot_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    snapshot = {
                        str(key): value
                        for key, value in loaded.items()
                        if isinstance(value, Mapping)
                    }

        return cls(
            api_key=api_key,
            base_url=base_url,
            http_referer=referer or os.getenv("OPENROUTER_HTTP_REFERER", ""),
            x_title=title or os.getenv("OPENROUTER_APP_TITLE", ""),
            timeout=timeout,
            max_retries=max_retries,
            pricing_snapshot=snapshot,
        )

    def _headers(self, *, api_key_override: str = "") -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {api_key_override or self.api_key}",
            "Content-Type": "application/json",
        }
        if self.http_referer:
            headers["HTTP-Referer"] = self.http_referer
        if self.x_title:
            headers["X-Title"] = self.x_title
        return headers

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Mapping[str, Any]] = None,
        query: Optional[Mapping[str, Any]] = None,
        api_key_override: str = "",
    ) -> Dict[str, Any]:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers=self._headers(api_key_override=api_key_override),
            method=method.upper(),
        )

        last_error: Optional[BaseException] = None
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    charset = response.headers.get_content_charset() or "utf-8"
                    return json.loads(response.read().decode(charset))
            except urllib.error.HTTPError as exc:
                response_text = exc.read().decode("utf-8", errors="replace")
                message, error_payload = _parse_error_response(response_text)
                if exc.code == 402:
                    raise OpenRouterInsufficientCreditsError(
                        exc.code,
                        path,
                        message[:1200],
                        response_text=response_text[:4000],
                        error_payload=error_payload,
                    )
                last_error = OpenRouterHTTPError(
                    exc.code,
                    path,
                    message[:1200],
                    response_text=response_text[:4000],
                    error_payload=error_payload,
                )
                if exc.code not in {408, 409, 429, 500, 502, 503, 504} or attempt >= self.max_retries:
                    body_snippet = json.dumps(payload)[:2000] if payload else "no body"
                    print(f"OpenRouter HTTP {exc.code} for {path}")
                    print(f"  request body (first 2000 chars): {body_snippet}")
                    print(f"  response message: {message[:500]}")
                    print(f"  response keys: {list(error_payload.keys()) if isinstance(error_payload, dict) else 'N/A'}")
                    raise last_error
                if exc.code == 429:
                    retry_after = exc.headers.get("Retry-After")
                    if retry_after:
                        try:
                            wait = float(retry_after)
                        except ValueError:
                            wait = min(2 ** attempt, 60)
                    else:
                        wait = min(2 ** attempt * 2, 120)
                    print(f"[llm] {path} HTTP {exc.code} attempt {attempt + 1}/{self.max_retries + 1} — retrying in {wait:.0f}s", flush=True)
                    time.sleep(wait)
                    continue
                wait = min(2 ** attempt, 8)
                print(f"[llm] {path} HTTP {exc.code} attempt {attempt + 1}/{self.max_retries + 1} — retrying in {wait:.0f}s", flush=True)
                time.sleep(wait)
            except urllib.error.URLError as exc:
                last_error = OpenRouterAPIError(f"Network error for {path}: {exc}")
                if attempt >= self.max_retries:
                    raise last_error
                wait = min(2 ** attempt, 8)
                print(f"[llm] {path} timed out/network error after {self.timeout}s — attempt {attempt + 1}/{self.max_retries + 1}, retrying in {wait:.0f}s", flush=True)
                time.sleep(wait)
        assert last_error is not None
        raise last_error

    def get_credits(self, *, api_key_override: str = "") -> CreditsRecord:
        response = self._request_json("GET", "/credits", api_key_override=api_key_override)
        if not isinstance(response, Mapping):
            raise OpenRouterAPIError(f"Unexpected /credits response: {response}")
        data = response.get("data") or {}
        if not isinstance(data, Mapping):
            data = {}
        total_credits = _as_float(data.get("total_credits"))
        total_usage = _as_float(data.get("total_usage"))
        remaining_credits = float(total_credits - total_usage)
        return CreditsRecord(
            total_credits=total_credits,
            total_usage=total_usage,
            remaining_credits=remaining_credits,
            raw=dict(response),
        )

    def _snapshot_cost(self, model_id: str, usage: UsageRecord) -> Optional[float]:
        if not model_id:
            return None
        record = self.pricing_snapshot.get(model_id)
        if not record:
            return None

        def _to_tier(raw: Mapping[str, Any]) -> Tuple[float, float, float, float, int]:
            return (
                _as_float(raw.get("input_cost_per_million")) / 1_000_000.0,
                _as_float(raw.get("output_cost_per_million")) / 1_000_000.0,
                _as_float(raw.get("cached_input_cost_per_million")) / 1_000_000.0,
                _as_float(raw.get("request_cost")),
                _as_int(raw.get("min_context")),
            )

        tiers_raw = record.get("pricing_tiers")
        tiers = [_to_tier(t) for t in tiers_raw if isinstance(t, Mapping)] if isinstance(tiers_raw, list) else []
        if not tiers:
            tiers = [_to_tier(record)]
        if not tiers:
            return None

        prompt_price, completion_price, cached_price, request_cost, _ = tiers[0]
        for input_price, output_price, cache_price, req, min_context in tiers:
            if usage.prompt_tokens >= min_context:
                prompt_price, completion_price, cached_price, request_cost = (
                    input_price,
                    output_price,
                    cache_price,
                    req,
                )
        cached = max(0, min(usage.cached_prompt_tokens, usage.prompt_tokens))
        uncached = usage.prompt_tokens - cached
        return float(
            (uncached * prompt_price)
            + (cached * cached_price)
            + (usage.completion_tokens * completion_price)
            + request_cost
        )

    def fetch_models(self, *, refresh: bool = False) -> Dict[str, ModelInfo]:
        if self._model_cache and not refresh:
            return dict(self._model_cache)

        response = self._request_json("GET", "/models")
        items = response.get("data") if isinstance(response, dict) else response
        if not isinstance(items, list):
            raise OpenRouterAPIError(f"Unexpected /models response: {response}")

        catalog: Dict[str, ModelInfo] = {}
        for item in items:
            if not isinstance(item, Mapping) or not item.get("id"):
                continue
            pricing_source = item.get("pricing")
            if isinstance(pricing_source, list):
                tiers_source = pricing_source
            elif isinstance(pricing_source, Mapping):
                tiers_source = [pricing_source]
            else:
                tiers_source = []
            tiers = tuple(
                ModelPricingTier(
                    prompt=_as_float(tier.get("prompt")),
                    completion=_as_float(tier.get("completion")),
                    request=_as_float(tier.get("request")),
                    input_cache_read=_as_float(tier.get("input_cache_read")),
                    min_context=_as_int(tier.get("min_context")),
                )
                for tier in tiers_source
                if isinstance(tier, Mapping)
            )
            catalog[str(item["id"])] = ModelInfo(
                model_id=str(item["id"]),
                context_length=_as_int(item.get("context_length") or (item.get("top_provider") or {}).get("context_length")),
                pricing=tiers,
                supported_parameters=tuple(str(param) for param in (item.get("supported_parameters") or []) if param),
                raw=dict(item),
            )

        for model_id, record in self.pricing_snapshot.items():
            if model_id in catalog:
                continue
            tier = ModelPricingTier(
                prompt=_as_float(record.get("input_cost_per_million")) / 1_000_000.0,
                completion=_as_float(record.get("output_cost_per_million")) / 1_000_000.0,
                input_cache_read=_as_float(record.get("cached_input_cost_per_million")) / 1_000_000.0,
                request=0.0,
                min_context=0,
            )
            catalog[model_id] = ModelInfo(model_id=model_id, pricing=(tier,), raw={"pricing_snapshot": dict(record)})

        self._model_cache = catalog
        return dict(self._model_cache)

    def supports_parameter(self, model_id: str, parameter: str) -> bool:
        try:
            info = self.fetch_models().get(model_id)
        except OpenRouterAPIError:
            return False
        return False if info is None else parameter in info.supported_parameters

    def estimate_uncached_cost(self, model_id: str, usage: UsageRecord) -> float:
        if not model_id:
            return usage.generation_cost

        snapshot_cost = self._snapshot_cost(model_id, usage)
        if snapshot_cost is not None:
            return snapshot_cost

        try:
            info = self.fetch_models().get(model_id)
        except OpenRouterAPIError:
            return usage.generation_cost
        if info is None or not info.pricing:
            return usage.generation_cost

        tier = sorted(info.pricing, key=lambda item: item.min_context)[0]
        for candidate in sorted(info.pricing, key=lambda item: item.min_context):
            if usage.prompt_tokens >= candidate.min_context:
                tier = candidate
        return float((usage.prompt_tokens * tier.prompt) + (usage.completion_tokens * tier.completion) + tier.request)

    def _parse_usage(self, response: Mapping[str, Any], *, model_id: str) -> UsageRecord:
        usage = response.get("usage") or {}
        prompt_details = usage.get("prompt_tokens_details") or {}
        cached_tokens = (
            prompt_details.get("cached_tokens")
            or usage.get("cached_tokens")
            or response.get("native_tokens_cached")
            or 0
        )
        generation_cost = _as_float(usage.get("cost") or response.get("total_cost"))
        record = UsageRecord(
            prompt_tokens=_as_int(usage.get("prompt_tokens") or response.get("tokens_prompt")),
            completion_tokens=_as_int(usage.get("completion_tokens") or response.get("tokens_completion")),
            total_tokens=_as_int(usage.get("total_tokens")),
            reasoning_tokens=_as_int(usage.get("reasoning_tokens") or usage.get("output_tokens_reasoning")),
            cached_prompt_tokens=_as_int(cached_tokens),
            generation_cost=generation_cost,
            uncached_generation_cost=0.0,
            generation_id=str(response.get("id") or ""),
            provider_name=str(response.get("provider") or response.get("provider_name") or ""),
            model_id=model_id,
            raw=dict(usage) if isinstance(usage, Mapping) else {},
        )
        uncached = self.estimate_uncached_cost(model_id, record)
        return UsageRecord(
            prompt_tokens=record.prompt_tokens,
            completion_tokens=record.completion_tokens,
            total_tokens=record.total_tokens,
            reasoning_tokens=record.reasoning_tokens,
            cached_prompt_tokens=record.cached_prompt_tokens,
            generation_cost=record.generation_cost,
            uncached_generation_cost=uncached,
            generation_id=record.generation_id,
            provider_name=record.provider_name,
            model_id=record.model_id,
            raw=record.raw,
        )

    def chat_completion(self, payload: Mapping[str, Any]) -> GenerationResult:
        response = self._request_json("POST", "/chat/completions", payload=payload)
        choices = response.get("choices") or []
        if not choices:
            raise OpenRouterAPIError(f"Missing choices in response: {response}")

        message = choices[0].get("message") or {}
        content_text = _extract_message_text(message.get("content", ""))
        parsed_json, json_recovery = _extract_parsed_payload(response, content_text)
        finish_reason = choices[0].get("finish_reason") or ""
        model_id = str(response.get("model") or payload.get("model") or "")

        if isinstance(parsed_json, dict):
            raw_content = json.dumps(parsed_json, ensure_ascii=False)
            summary_md = str(parsed_json.get("summary_md") or "").strip()
            estimated_visible_words = _as_int(parsed_json.get("estimated_visible_words"), _visible_word_count(summary_md))
        else:
            raw_content = content_text
            summary_md = content_text.strip()
            estimated_visible_words = _visible_word_count(summary_md)

        usage = self._parse_usage(response, model_id=model_id)
        return GenerationResult(
            summary_md=summary_md,
            estimated_visible_words=estimated_visible_words,
            raw_content=raw_content,
            usage=usage,
            raw_response=dict(response),
            model_id=model_id,
            parsed_json=parsed_json,
            finish_reason=finish_reason or None,
            json_recovery=json_recovery,
        )
