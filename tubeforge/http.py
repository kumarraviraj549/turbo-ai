"""Resilient HTTP layer shared by every provider: timeouts, retries with backoff, Retry-After handling,
per-provider concurrency + pacing limits, and a circuit breaker for non-transient failures."""
import json
import random
import threading
import time
from typing import Callable, Dict, Optional

import requests

TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529}


class ProviderError(RuntimeError):
    def __init__(self, provider: str, message: str, status: int = 0, fatal: bool = False):
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.status = status
        # Fatal = retrying will not help (bad key, no credits, terms not accepted, unsupported model).
        self.fatal = fatal


class _Limiter:
    def __init__(self, concurrency: int, min_interval: float):
        self.sem = threading.BoundedSemaphore(concurrency)
        self.min_interval = min_interval
        self.lock = threading.Lock()
        self.next_slot = 0.0
        self.blocked_until = 0.0

    def wait_turn(self) -> None:
        with self.lock:
            now = time.time()
            start = max(now, self.next_slot, self.blocked_until)
            self.next_slot = start + self.min_interval
        delay = start - time.time()
        if delay > 0:
            time.sleep(delay)

    def block_for(self, seconds: float) -> None:
        with self.lock:
            self.blocked_until = max(self.blocked_until, time.time() + seconds)


# Concurrency and pacing tuned to each provider's documented free-tier limits.
_LIMITS = {
    "groq": (3, 0.4),
    "openrouter": (4, 0.3),
    "openrouter_video": (3, 1.0),
    "cloudflare": (3, 0.25),
    "pixabay": (4, 0.7),  # 100 requests / 60s
    "pixabay_cdn": (6, 0.0),
    "gemini": (4, 0.2),
    "pollinations": (1, 1.0),
    "edge_tts": (2, 0.5),  # Edge throttles bursts with "No audio was received"
}
_limiters: Dict[str, _Limiter] = {}
_limiters_lock = threading.Lock()

_breaker: Dict[str, dict] = {}
_breaker_lock = threading.Lock()


def limiter(provider: str) -> _Limiter:
    with _limiters_lock:
        if provider not in _limiters:
            conc, interval = _LIMITS.get(provider, (4, 0.0))
            _limiters[provider] = _Limiter(conc, interval)
        return _limiters[provider]


def trip_breaker(key: str, reason: str, seconds: float = 3600) -> None:
    with _breaker_lock:
        _breaker[key] = {"until": time.time() + seconds, "reason": reason}


def breaker_reason(key: str) -> str:
    with _breaker_lock:
        entry = _breaker.get(key)
        if entry and entry["until"] > time.time():
            return entry["reason"]
        _breaker.pop(key, None)
        return ""


def breaker_status() -> Dict[str, str]:
    with _breaker_lock:
        now = time.time()
        return {k: v["reason"] for k, v in _breaker.items() if v["until"] > now}


def reset_breakers() -> None:
    with _breaker_lock:
        _breaker.clear()


def _retry_after_seconds(resp: requests.Response) -> Optional[float]:
    for header in ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        raw = resp.headers.get(header)
        if not raw:
            continue
        try:
            return float(raw)
        except ValueError:
            pass
        # Groq style durations such as "1m26.4s" or "3.07s" or "250ms".
        total, num = 0.0, ""
        i = 0
        while i < len(raw):
            ch = raw[i]
            if ch.isdigit() or ch == ".":
                num += ch
            elif raw[i:i + 2] == "ms":
                total += float(num or 0) / 1000
                num = ""
                i += 1
            elif ch in "hms":
                total += float(num or 0) * {"h": 3600, "m": 60, "s": 1}[ch]
                num = ""
            i += 1
        if total:
            return total
    return None


def error_text(resp: requests.Response) -> str:
    try:
        data = resp.json()
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            msg = err.get("message") or json.dumps(err)
            raw = (err.get("metadata") or {}).get("raw") if isinstance(err.get("metadata"), dict) else None
            return f"{msg} {raw or ''}".strip()[:400]
        if isinstance(err, str):
            return err[:400]
        if isinstance(data, dict) and data.get("errors"):
            return "; ".join(str(e.get("message", e)) for e in data["errors"])[:400]
    except Exception:
        pass
    return resp.text[:400]


_QUOTA_MARKERS = ("daily free allocation", "exceeded your current quota", "quota exceeded",
                  "insufficient credits", "requires more credits", "free-models-per-day")


def quota_exhausted(resp: requests.Response) -> bool:
    """Daily/billing quota errors: retrying within seconds is pointless."""
    if resp.status_code not in (402, 429):
        return False
    text = resp.text.lower()
    return any(m in text for m in _QUOTA_MARKERS)


def quota_cooldown(provider: str, err: "ProviderError") -> float:
    msg = str(err).lower()
    if "daily" in msg or "per-day" in msg or "neurons" in msg:
        return 6 * 3600
    if err.status == 429:
        return 15 * 60
    return 3600


def request(
    provider: str,
    method: str,
    url: str,
    *,
    retries: int = 3,
    timeout: float = 90,
    is_fatal: Optional[Callable[[requests.Response], bool]] = None,
    breaker_key: Optional[str] = None,
    max_wait: float = 65,
    retry_statuses: tuple = (),
    **kwargs,
) -> requests.Response:
    """Performs an HTTP request with retries. Returns a successful response or raises ProviderError."""
    key = breaker_key or provider
    reason = breaker_reason(key)
    if reason:
        raise ProviderError(provider, f"temporarily disabled: {reason}", fatal=True)
    lim = limiter(provider)
    last_err = "unknown error"
    last_status = 0
    for attempt in range(retries + 1):
        lim.wait_turn()
        try:
            with lim.sem:
                resp = requests.request(method, url, timeout=timeout, **kwargs)
        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = f"network error: {type(e).__name__}"
            last_status = 0
            if attempt < retries:
                time.sleep(min(20, 1.5 * 2 ** attempt + random.random()))
                continue
            break
        if resp.ok:
            return resp
        last_status = resp.status_code
        last_err = f"HTTP {resp.status_code}: {error_text(resp)}"
        retryable = resp.status_code in TRANSIENT_STATUS or resp.status_code in retry_statuses
        fatal = resp.status_code not in retry_statuses and (
            resp.status_code in (401, 402, 403) or quota_exhausted(resp) or (is_fatal(resp) if is_fatal else False))
        if fatal:
            raise ProviderError(provider, last_err, resp.status_code, fatal=True)
        if retryable and attempt < retries:
            wait = _retry_after_seconds(resp)
            if wait is None:
                wait = 1.5 * 2 ** attempt + random.random()
            if wait > max_wait:
                break
            if resp.status_code in (429,) + tuple(retry_statuses):
                lim.block_for(wait)
            time.sleep(wait)
            continue
        break
    raise ProviderError(provider, last_err, last_status, fatal=False)


_inflight: Dict[str, threading.Lock] = {}
_inflight_lock = threading.Lock()


def single_flight(key: str) -> threading.Lock:
    """One lock per logical request so identical concurrent requests are only sent once."""
    with _inflight_lock:
        if key not in _inflight:
            _inflight[key] = threading.Lock()
        return _inflight[key]
