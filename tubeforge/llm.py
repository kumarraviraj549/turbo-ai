"""LLM router across Groq, OpenRouter, Google Gemini and Cloudflare Workers AI.

Each task type has a preferred provider order. Requests fall through to the next backend on rate limits,
outages or invalid JSON, and backends with fatal errors (bad key, no credits) are skipped for an hour."""
import json
import re
import threading
from typing import Callable, List, Optional, Tuple

from . import cache, config
from .http import ProviderError, breaker_reason, quota_cooldown, request, trip_breaker

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
CF_CHAT_URL = "https://api.cloudflare.com/client/v4/accounts/{acc}/ai/v1/chat/completions"

GROQ_MODELS = ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"]
GEMINI_MODELS = ["gemini-2.5-flash", "gemini-flash-latest", "gemini-2.5-flash-lite", "gemini-flash-lite-latest"]
CF_MODELS = ["@cf/openai/gpt-oss-120b", "@cf/zai-org/glm-5.3-flash", "@cf/meta/llama-3.3-70b-instruct-fp8-fast"]
# Preferred free OpenRouter models for long creative writing (largest first); discovered list is intersected.
OPENROUTER_FREE_PREFERENCE = [
    "baidu/ernie-5.5-flash-preview:free",
    "nvidia/nemotron-3.5-super-vl:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemini-2.0-flash-exp:free",
    "mistralai/mistral-small-3.1-24b-instruct:free",
    "qwen/qwen-2.5-coder-32b-instruct:free",
    "liquid/lfm-3-1.7b:free",
]

TASK_ORDER = {
    "creative": ["openrouter", "groq"],
    "scenes": ["groq", "openrouter"],
    "fast": ["groq", "openrouter"],
}

_openrouter_models_lock = threading.Lock()
_openrouter_models: Optional[List[dict]] = None


class LLMError(RuntimeError):
    pass


def extract_json_text(raw: str) -> str:
    text = (raw or "").strip()
    text = re.sub(r"^<think>.*?</think>", "", text, flags=re.S).strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        return text
    start = min(starts)
    end = max(text.rfind("}"), text.rfind("]"))
    return text[start:end + 1] if end > start else text[start:]


def parse_json(raw: str):
    text = extract_json_text(raw)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        repaired = re.sub(r",\s*([}\]])", r"\1", text)
        repaired = re.sub(r'(["\]}0-9el])\s*\n(\s*["{\[])', r"\1,\n\2", repaired)
        repaired = re.sub(r"}\s*{", "},{", repaired)
        return json.loads(repaired)


def openrouter_catalog() -> List[dict]:
    """Public model list (cached 6h). Used to discover free models and real capabilities."""
    global _openrouter_models
    with _openrouter_models_lock:
        if _openrouter_models is not None:
            return _openrouter_models
        cached = cache.get_json("catalog", "openrouter_models", max_age=6 * 3600)
        if cached:
            _openrouter_models = cached
            return cached
        try:
            resp = request("openrouter", "GET", "https://openrouter.ai/api/v1/models", timeout=30, retries=1)
            models = resp.json().get("data", [])
            slim = [
                {
                    "id": m["id"],
                    "name": m.get("name", m["id"]),
                    "context": m.get("context_length", 0),
                    "output_modalities": (m.get("architecture") or {}).get("output_modalities", ["text"]),
                    "prompt_price": float((m.get("pricing") or {}).get("prompt", 0) or 0),
                    "completion_price": float((m.get("pricing") or {}).get("completion", 0) or 0),
                }
                for m in models
            ]
            cache.put_json("catalog", "openrouter_models", slim)
            _openrouter_models = slim
        except Exception:
            _openrouter_models = []
        return _openrouter_models


def openrouter_text_models(free_only: bool = False) -> List[dict]:
    out = [m for m in openrouter_catalog() if "text" in m["output_modalities"] and "image" not in m["output_modalities"]]
    if free_only:
        out = [m for m in out if m["id"].endswith(":free")]
    return out


def openrouter_free_chain() -> List[str]:
    free_ids = {m["id"] for m in openrouter_text_models(free_only=True)}
    chain = [m for m in OPENROUTER_FREE_PREFERENCE if m in free_ids]
    extras = sorted(
        (m for m in openrouter_text_models(free_only=True) if m["id"] not in chain and m["context"] >= 60000),
        key=lambda m: -m["context"],
    )
    return (chain + [m["id"] for m in extras])[:5] or ["openrouter/free"]


def model_unusable(resp) -> bool:
    """Errors that retrying cannot fix: unknown/decommissioned model, terms not accepted, unsupported params."""
    if resp.status_code == 404:
        return True
    if resp.status_code != 400:
        return False
    text = resp.text.lower()
    return any(s in text for s in ("terms", "decommissioned", "does not exist", "not found", "not a valid model",
                                   "no endpoints found", "model_not_found"))


def _openai_style(provider: str, url: str, key: str, model: str, system: str, user: str,
                  max_tokens: int, temperature: float, json_mode: bool, timeout: float, extra: dict) -> str:
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": temperature,
    }
    body["max_completion_tokens" if provider == "groq" else "max_tokens"] = max_tokens
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    body.update(extra)
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if provider == "openrouter":
        headers["HTTP-Referer"] = "https://localhost/tubeforge-studio"
        headers["X-Title"] = "TubeForge Cinematic Studio"
    resp = request(provider, "POST", url, json=body, headers=headers, timeout=timeout, retries=2, max_wait=25,
                   breaker_key=f"{provider}:{model}", is_fatal=model_unusable)
    data = resp.json()
    choice = (data.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content") or ""
    if not content.strip():
        raise ProviderError(provider, f"{model} returned an empty answer (finish_reason={choice.get('finish_reason')})")
    return content


def _call_groq(model, system, user, max_tokens, temperature, json_mode, timeout):
    extra = {"reasoning_effort": "low"} if "gpt-oss" in model else {}
    if "qwen3" in model:
        extra = {"reasoning_effort": "none"}
    return _openai_style("groq", GROQ_URL, config.groq_key(), model, system, user,
                         max_tokens, temperature, json_mode, timeout, extra)


def _call_openrouter(model, system, user, max_tokens, temperature, json_mode, timeout):
    extra = {"reasoning": {"effort": "low", "exclude": True}}
    return _openai_style("openrouter", OPENROUTER_URL, config.openrouter_key(), model, system, user,
                         max_tokens, temperature, json_mode, timeout, extra)


def _call_cloudflare(model, system, user, max_tokens, temperature, json_mode, timeout):
    url = CF_CHAT_URL.format(acc=config.cloudflare_account())
    extra = {"reasoning_effort": "low"} if "gpt-oss" in model else {}
    out = _openai_style("cloudflare", url, config.cloudflare_token(), model, system, user,
                        max_tokens, temperature, json_mode, timeout, extra)
    # gpt-oss-120b on Workers AI: ~68k neurons per 1M output tokens.
    cache.record_usage("cloudflare_llm", neurons=len(out) / 4 * 0.07 + len(user) / 4 * 0.032)
    return out


def _call_gemini(model, system, user, max_tokens, temperature, json_mode, timeout):
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"parts": [{"text": user}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    resp = request("gemini", "POST", GEMINI_URL.format(model=model), json=body,
                   headers={"x-goog-api-key": config.gemini_key()}, timeout=timeout, retries=1, max_wait=20,
                   breaker_key=f"gemini:{model}")
    parts = ((resp.json().get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts)
    if not text.strip():
        raise ProviderError("gemini", f"{model} returned no text")
    return text


def _backend_models(provider: str, preferred_openrouter: str = "") -> List[str]:
    if provider == "groq":
        return GROQ_MODELS if config.groq_key() else []
    if provider == "openrouter":
        if not config.openrouter_key():
            return []
        models = openrouter_free_chain()
        if preferred_openrouter.endswith(":free"):
            models = [preferred_openrouter] + [m for m in models if m != preferred_openrouter]
        return models
    return []


_CALLERS = {
    "groq": _call_groq,
    "openrouter": _call_openrouter,
    "gemini": _call_gemini,
    "cloudflare": _call_cloudflare,
}


def available_backends() -> List[str]:
    return [p for p in ("groq", "openrouter") if _backend_models(p)]


def complete_json(
    system: str,
    user: str,
    task: str = "fast",
    max_tokens: int = 4000,
    temperature: float = 0.7,
    validate: Optional[Callable] = None,
    log: Optional[Callable[[str], None]] = None,
    start_provider: Optional[str] = None,
    preferred_openrouter: str = "",
    use_cache: bool = True,
    timeout: float = 150,
) -> Tuple[object, str]:
    """Returns (parsed_json, "provider/model"). `validate` may transform the data or raise to reject it."""
    order = list(TASK_ORDER.get(task, TASK_ORDER["fast"]))
    if start_provider in order:
        order.remove(start_provider)
        order.insert(0, start_provider)
    key = cache.cache_key("llm", task, system, user, max_tokens, preferred_openrouter)
    if use_cache:
        hit = cache.get_json("llm", key)
        if hit is not None:
            try:
                data = validate(hit["data"]) if validate else hit["data"]
                return data, hit["model"] + " (cached)"
            except Exception:
                pass
    errors: List[str] = []
    for provider in order:
        for model in _backend_models(provider, preferred_openrouter):
            if breaker_reason(f"{provider}:{model}") or breaker_reason(provider):
                continue
            label = f"{provider}/{model}"
            try:
                raw = _CALLERS[provider](model, system, user, max_tokens, temperature, True, timeout)
                data = parse_json(raw)
                if validate:
                    data = validate(data)
                if use_cache:
                    cache.put_json("llm", key, {"data": data, "model": label})
                return data, label
            except ProviderError as e:
                errors.append(str(e)[:200])
                if e.fatal:
                    # Model-specific problems (terms, plan-gated, removed model) disable that model;
                    # key/credit/quota problems disable the whole provider.
                    msg = str(e).lower()
                    model_scoped = (e.status in (400, 404) or (e.status == 403 and "not available on" in msg)
                                    or (provider == "gemini" and e.status == 429))
                    scope = f"{provider}:{model}" if model_scoped else provider
                    trip_breaker(scope, str(e)[:160], seconds=quota_cooldown(provider, e))
                    if log:
                        log(f"⚠️ {label} unavailable: {str(e)[:140]}")
                    if scope == provider:
                        break
                elif log:
                    log(f"⚠️ {label}: {str(e)[:140]}")
            except Exception as e:
                errors.append(f"{label}: {str(e)[:160]}")
                if log:
                    log(f"⚠️ {label} gave an unusable answer: {str(e)[:120]}")
    raise LLMError("All AI text providers failed. " + " | ".join(errors[-6:]))
