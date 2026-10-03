"""
Unified LLM client for ApplyPilot.

Auto-detects provider from environment:
  GEMINI_API_KEY  -> Google Gemini (default: gemini-3.6-flash)
  OPENAI_API_KEY  -> OpenAI (default: gpt-4o-mini)
  LLM_URL         -> Local llama.cpp / Ollama compatible endpoint

LLM_MODEL env var overrides the model name for any provider.
"""

import logging
import os
import threading
import time

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

def _detect_provider() -> tuple[str, str, str]:
    """Return (base_url, model, api_key) based on environment variables.

    Reads env at call time (not module import time) so that load_env() called
    in _bootstrap() is always visible here.
    """
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    local_url = os.environ.get("LLM_URL", "")
    model_override = os.environ.get("LLM_MODEL", "")

    if local_url:
        return (
            local_url.rstrip("/"),
            model_override or "local-model",
            os.environ.get("LLM_API_KEY", ""),
        )

    if gemini_key:
        return (
            "https://generativelanguage.googleapis.com/v1beta/openai",
            model_override or "gemini-3.8-flash",
            gemini_key,
        )

    if openai_key:
        return (
            "https://api.openai.com/v1",
            model_override or "gpt-4o-mini",
            openai_key,
        )

    raise RuntimeError(
        "No LLM provider configured. "
        "Set GEMINI_API_KEY, OPENAI_API_KEY, or LLM_URL in your environment."
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_MAX_RETRIES = 5
_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "75"))  # seconds

# Base wait on first 429/503 (doubles each retry, caps at 60s).
# Gemini free tier is 15 RPM = 4s minimum between requests; 10s gives headroom.
_RATE_LIMIT_BASE_WAIT = 10

# Proactive pacing: never fire requests faster than this. Hammering a free-tier
# endpoint turns one 429 into a storm that never recovers, so we stay under the
# limit instead of only reacting after the fact. Override with LLM_MIN_INTERVAL.
# Default 4.5s ~= 13 RPM, safely under Gemini's free 15 RPM.
_MIN_REQUEST_INTERVAL = float(os.environ.get("LLM_MIN_INTERVAL", "4.5"))
_throttle_lock = threading.Lock()
_last_request_at = 0.0


def _throttle() -> None:
    """Block so consecutive requests respect the minimum interval."""
    global _last_request_at
    if _MIN_REQUEST_INTERVAL <= 0:
        return
    with _throttle_lock:
        now = time.monotonic()
        wait = _last_request_at + _MIN_REQUEST_INTERVAL - now
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


_GEMINI_COMPAT_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
_GEMINI_NATIVE_BASE = "https://generativelanguage.googleapis.com/v1beta"


class LLMClient:
    """Thin LLM client supporting OpenAI-compatible and native Gemini endpoints.

    Supports automatic model fallback across candidate models (configured via
    comma-separated LLM_MODEL or LLM_FALLBACK_MODELS).
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        fallback_models: list[str] | None = None,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self._client = httpx.Client(timeout=_TIMEOUT)
        self._use_native_gemini: bool = False
        self._is_gemini: bool = base_url.startswith(_GEMINI_COMPAT_BASE)

        # Parse primary model and extra fallbacks from model string
        models = [m.strip() for m in str(model).split(",") if m.strip()]
        self.model = models[0] if models else "local-model"
        extra_from_model = models[1:] if len(models) > 1 else []

        env_fallback_str = os.environ.get("LLM_FALLBACK_MODELS", "")
        env_fallbacks = [m.strip() for m in env_fallback_str.split(",") if m.strip()]

        combined = extra_from_model + (fallback_models or []) + env_fallbacks
        self.fallback_models: list[str] = [m for m in dict.fromkeys(combined) if m != self.model]

    def _resolve_endpoint(self, model_name: str) -> tuple[str, str, str, bool]:
        """Resolve (base_url, model, api_key, is_gemini) for a candidate model.

        Supports cross-provider routing:
          - 'gemini:*' or 'gemini-*' with GEMINI_API_KEY -> Google Gemini API
          - 'openai:*' or 'gpt-*' with OPENAI_API_KEY -> OpenAI API
          - default -> self.base_url and self.api_key
        """
        gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
        openai_key = os.environ.get("OPENAI_API_KEY", "").strip()

        if model_name.startswith("gemini:"):
            target_model = model_name.removeprefix("gemini:")
            return (_GEMINI_COMPAT_BASE, target_model, gemini_key or self.api_key, True)

        if model_name.startswith("openai:"):
            target_model = model_name.removeprefix("openai:")
            return ("https://api.openai.com/v1", target_model, openai_key or self.api_key, False)

        if (model_name.startswith("gemini-") or "gemini" in model_name.lower()) and gemini_key and not self._is_gemini:
            return (_GEMINI_COMPAT_BASE, model_name, gemini_key, True)

        if (model_name.startswith("gpt-") or "o1-" in model_name or "o3-" in model_name) and openai_key and not self.base_url.startswith("https://api.openai.com"):
            return ("https://api.openai.com/v1", model_name, openai_key, False)

        return (self.base_url, model_name, self.api_key, self._is_gemini)

    # -- Native Gemini API --------------------------------------------------

    def _chat_native_gemini(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        model_name: str | None = None,
        api_key: str | None = None,
    ) -> str:
        """Call the native Gemini generateContent API."""
        target_model = model_name or self.model
        target_key = api_key if api_key is not None else self.api_key
        contents: list[dict] = []
        system_parts: list[dict] = []

        for msg in messages:
            role = msg["role"]
            text = msg.get("content", "")
            if role == "system":
                system_parts.append({"text": text})
            elif role == "user":
                contents.append({"role": "user", "parts": [{"text": text}]})
            elif role == "assistant":
                contents.append({"role": "model", "parts": [{"text": text}]})

        payload: dict = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}

        url = f"{_GEMINI_NATIVE_BASE}/models/{target_model}:generateContent"
        resp = self._client.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            params={"key": target_key},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]

    # -- OpenAI-compat API --------------------------------------------------

    def _chat_compat(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        model_name: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        is_gemini: bool = False,
    ) -> str:
        """Call the OpenAI-compatible endpoint."""
        target_model = model_name or self.model
        target_url = (base_url or self.base_url).rstrip("/")
        target_key = api_key if api_key is not None else self.api_key
        target_is_gemini = is_gemini or target_url.startswith(_GEMINI_COMPAT_BASE)

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if target_key:
            headers["Authorization"] = f"Bearer {target_key}"

        payload = {
            "model": target_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if target_is_gemini:
            effort = os.environ.get("LLM_REASONING_EFFORT", "").strip()
            if effort:
                payload["reasoning_effort"] = effort

        resp = self._client.post(
            f"{target_url}/chat/completions",
            json=payload,
            headers=headers,
        )

        if resp.status_code == 403 and target_is_gemini:
            raise _GeminiCompatForbidden(resp)

        return self._handle_compat_response(resp)

    @staticmethod
    def _handle_compat_response(resp: httpx.Response) -> str:
        resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]
        msg = choice.get("message", {})
        content = msg.get("content")
        # Handle reasoning models where final output or thoughts may be in reasoning_content
        if not content:
            content = msg.get("reasoning_content")
        if not content:
            reason = choice.get("finish_reason", "unknown")
            raise RuntimeError(f"LLM returned no content (finish_reason={reason})")
        return content

    # -- public API ---------------------------------------------------------

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        """Send a chat completion request with automatic model and provider fallback."""
        candidates = [self.model] + [m for m in self.fallback_models if m != self.model]
        gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if gemini_key and not self._is_gemini:
            if not any("gemini" in m.lower() for m in candidates):
                candidates.append("gemini-3.8-flash")

        last_error = None

        for model_idx, current_model in enumerate(candidates):
            has_fallback = model_idx < len(candidates) - 1
            next_model = candidates[model_idx + 1] if has_fallback else None

            cand_base_url, cand_model, cand_api_key, cand_is_gemini = self._resolve_endpoint(current_model)

            # Qwen3 optimization: prepend /no_think to skip chain-of-thought
            req_messages = messages
            if "qwen" in cand_model.lower() and req_messages:
                first = req_messages[0]
                if first.get("role") == "user" and not first["content"].startswith("/no_think"):
                    req_messages = [{"role": first["role"], "content": f"/no_think\n{first['content']}"}] + req_messages[1:]

            for attempt in range(_MAX_RETRIES):
                _throttle()
                try:
                    if self._use_native_gemini and cand_is_gemini:
                        return self._chat_native_gemini(
                            req_messages, temperature, max_tokens,
                            model_name=cand_model, api_key=cand_api_key,
                        )
                    return self._chat_compat(
                        req_messages, temperature, max_tokens,
                        model_name=cand_model, base_url=cand_base_url,
                        api_key=cand_api_key, is_gemini=cand_is_gemini,
                    )

                except _GeminiCompatForbidden:
                    log.warning(
                        "Gemini compat endpoint returned 403 for model '%s'. "
                        "Switching to native generateContent API.",
                        cand_model,
                    )
                    self._use_native_gemini = True
                    try:
                        return self._chat_native_gemini(
                            req_messages, temperature, max_tokens,
                            model_name=cand_model, api_key=cand_api_key,
                        )
                    except httpx.HTTPStatusError as native_exc:
                        last_error = native_exc
                        if has_fallback:
                            log.warning("Model '%s' failed on native Gemini (%s). Trying fallback '%s'...", current_model, native_exc, next_model)
                            break
                        raise RuntimeError(
                            f"Both Gemini endpoints failed for '{current_model}': {native_exc.response.status_code}"
                        ) from native_exc

                except httpx.HTTPStatusError as exc:
                    resp = exc.response
                    last_error = exc

                    # Check for quota exhaustion message
                    err_msg = ""
                    try:
                        err_msg = resp.json().get("error", {}).get("message", "")
                    except Exception:
                        err_msg = resp.text[:120]

                    is_quota_exhausted = (
                        resp.status_code == 429
                        and any(kw in err_msg.lower() for kw in ("quota", "exhausted", "limit reached", "balance"))
                    )

                    # If quota exhausted or model missing or persistent server error, immediately try fallback
                    if has_fallback and (is_quota_exhausted or resp.status_code in (400, 404, 500, 502, 503)):
                        log.warning(
                            "Model '%s' failed (HTTP %d: %s). Immediately falling back to '%s'...",
                            current_model, resp.status_code, err_msg or resp.reason_phrase, next_model
                        )
                        break

                    # Transient rate limit retry
                    if resp.status_code in (429, 503) and attempt < _MAX_RETRIES - 1:
                        retry_after = (
                            resp.headers.get("Retry-After")
                            or resp.headers.get("X-RateLimit-Reset-Requests")
                        )
                        if retry_after:
                            try:
                                wait = float(retry_after)
                            except (ValueError, TypeError):
                                wait = _RATE_LIMIT_BASE_WAIT * (2 ** attempt)
                        else:
                            wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)

                        log.warning(
                            "Model '%s' rate limited (HTTP %s). Waiting %ds before retry %d/%d.",
                            current_model, resp.status_code, wait, attempt + 1, _MAX_RETRIES,
                        )
                        time.sleep(wait)
                        continue

                    if has_fallback:
                        log.warning(
                            "Model '%s' exhausted retries (HTTP %d: %s). Falling back to '%s'...",
                            current_model, resp.status_code, err_msg or resp.reason_phrase, next_model
                        )
                        break

                    raise

                except httpx.TimeoutException as exc:
                    last_error = exc
                    if has_fallback:
                        log.warning(
                            "Model '%s' timed out. Immediately falling back to '%s'...",
                            current_model, next_model
                        )
                        break

                    if attempt < _MAX_RETRIES - 1:
                        wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)
                        log.warning(
                            "Model '%s' request timed out, retrying in %ds (attempt %d/%d)",
                            current_model, wait, attempt + 1, _MAX_RETRIES,
                        )
                        time.sleep(wait)
                        continue

                    raise

                except Exception as exc:
                    last_error = exc
                    if has_fallback:
                        log.warning(
                            "Model '%s' encountered error (%s). Falling back to '%s'...",
                            current_model, exc, next_model
                        )
                        break
                    raise

        if last_error:
            raise RuntimeError(f"All configured LLM models failed ({', '.join(candidates)}). Last error: {last_error}")
        raise RuntimeError("LLM request failed after all models and retries")

        if last_error:
            raise RuntimeError(f"All configured LLM models failed ({', '.join(candidates)}). Last error: {last_error}")
        raise RuntimeError("LLM request failed after all models and retries")

    def ask(self, prompt: str, **kwargs) -> str:
        """Convenience: single user prompt -> assistant response."""
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def close(self) -> None:
        self._client.close()


class _GeminiCompatForbidden(Exception):
    """Sentinel: Gemini OpenAI-compat returned 403. Switch to native API."""
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"Gemini compat 403: {response.text[:200]}")


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: LLMClient | None = None


def get_client() -> LLMClient:
    """Return (or create) the module-level LLMClient singleton."""
    global _instance
    if _instance is None:
        base_url, model, api_key = _detect_provider()
        log.info("LLM provider: %s  model: %s", base_url, model)
        _instance = LLMClient(base_url, model, api_key)
    return _instance
