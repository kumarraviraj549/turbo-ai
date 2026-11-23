"""Content-addressed disk cache and daily usage accounting (Cloudflare neurons, OpenRouter spend)."""
import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Optional

from .config import CACHE_DIR, OUTPUT_ROOT

_usage_lock = threading.Lock()
USAGE_FILE = OUTPUT_ROOT / "_usage.json"
PROVIDER_QUOTA_FILE = OUTPUT_ROOT / "_provider_quotas.json"
CLOUDFLARE_FREE_NEURONS = 10000
CF_NEURON_WARNING_FRACTION = 0.30


def cloudflare_budget_warning(reservation: float, limit: float = CLOUDFLARE_FREE_NEURONS,
                              fraction: float = CF_NEURON_WARNING_FRACTION) -> dict:
    used = float(usage_today().get("cloudflare_neurons", 0.0))
    return {
        "used": used,
        "reservation": float(reservation),
        "limit": float(limit),
        "crosses_warning": used < limit * fraction <= used + reservation,
        "exceeds_limit": used + reservation > limit,
        "fraction_after": (used + reservation) / limit if limit else 1.0,
    }


def cache_key(*parts) -> str:
    raw = json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def cache_path(namespace: str, key: str, suffix: str) -> Path:
    folder = CACHE_DIR / namespace / key[:2]
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{key}{suffix}"


def get_bytes(namespace: str, key: str, suffix: str) -> Optional[bytes]:
    p = cache_path(namespace, key, suffix)
    if p.exists() and p.stat().st_size > 0:
        return p.read_bytes()
    return None


def put_bytes(namespace: str, key: str, suffix: str, data: bytes) -> Path:
    p = cache_path(namespace, key, suffix)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(p)
    return p


def get_json(namespace: str, key: str, max_age: Optional[float] = None):
    p = cache_path(namespace, key, ".json")
    if not p.exists():
        return None
    if max_age is not None and time.time() - p.stat().st_mtime > max_age:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def put_json(namespace: str, key: str, value) -> None:
    put_bytes(namespace, key, ".json", json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _read_usage() -> dict:
    try:
        data = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if data.get("day") != _today():
        data = {"day": _today(), "cloudflare_neurons": 0.0, "openrouter_usd": 0.0, "calls": {}}
    return data


def record_usage(provider: str, neurons: float = 0.0, usd: float = 0.0) -> None:
    with _usage_lock:
        data = _read_usage()
        data["cloudflare_neurons"] = data.get("cloudflare_neurons", 0.0) + neurons
        data["openrouter_usd"] = data.get("openrouter_usd", 0.0) + usd
        data.setdefault("calls", {})
        data["calls"][provider] = data["calls"].get(provider, 0) + 1
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        USAGE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def reserve_daily_calls(provider: str, limit: int) -> bool:
    """Atomically reserves one provider request against a UTC-day call limit."""
    with _usage_lock:
        data = _read_usage()
        data.setdefault("calls", {})
        calls = data["calls"]
        if int(calls.get(provider, 0)) >= limit:
            return False
        calls[provider] = int(calls.get(provider, 0)) + 1
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        USAGE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return True


def reserve_cloudflare_neurons(neurons: float, limit: float = CLOUDFLARE_FREE_NEURONS) -> bool:
    """Conservatively reserve free-tier image-generation usage before making a provider request."""
    if neurons <= 0:
        return True
    with _usage_lock:
        data = _read_usage()
        used = float(data.get("cloudflare_neurons", 0.0))
        if used + neurons > limit:
            return False
        data["cloudflare_neurons"] = used + neurons
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        USAGE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return True


def release_cloudflare_neurons(neurons: float) -> None:
    """Releases a reservation when a provider request fails before returning a usable image."""
    if neurons <= 0:
        return
    with _usage_lock:
        data = _read_usage()
        data["cloudflare_neurons"] = max(0.0, float(data.get("cloudflare_neurons", 0.0)) - neurons)
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        USAGE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _read_provider_quotas() -> dict:
    try:
        return json.loads(PROVIDER_QUOTA_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_provider_quotas(data: dict) -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = PROVIDER_QUOTA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(PROVIDER_QUOTA_FILE)


def reserve_provider_request(provider: str, hourly_limit: int, monthly_limit: int) -> bool:
    """Conservatively limits a provider's API calls using local UTC-hour and UTC-month windows."""
    now = time.time()
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    month = day[:7]
    with _usage_lock:
        data = _read_provider_quotas()
        entry = data.setdefault(provider, {})
        requests = [float(ts) for ts in entry.get("hourly_timestamps", []) if now - float(ts) < 3600]
        if len(requests) >= hourly_limit:
            return False
        if entry.get("month") != month:
            entry["month"] = month
            entry["month_requests"] = 0
        if int(entry.get("month_requests", 0)) >= monthly_limit:
            return False
        requests.append(now)
        entry["hourly_timestamps"] = requests
        entry["month_requests"] = int(entry.get("month_requests", 0)) + 1
        entry["day"] = day
        _write_provider_quotas(data)
        return True


def record_provider_quota(provider: str, limit: int, remaining: int, reset: int) -> None:
    """Stores provider-reported quota headers when an API response exposes them."""
    with _usage_lock:
        data = _read_provider_quotas()
        entry = data.setdefault(provider, {})
        entry.update(remote_month_limit=int(limit), remote_month_remaining=int(remaining),
                     remote_month_reset=int(reset), remote_updated=int(time.time()))
        _write_provider_quotas(data)


def provider_quota(provider: str) -> dict:
    with _usage_lock:
        entry = _read_provider_quotas().get(provider, {})
        now = time.time()
        return {
            "hourly_used": sum(now - float(ts) < 3600 for ts in entry.get("hourly_timestamps", [])),
            "monthly_used": int(entry.get("month_requests", 0)) if entry.get("month") == time.strftime(
                "%Y-%m", time.gmtime(now)) else 0,
            "remote_month_limit": entry.get("remote_month_limit"),
            "remote_month_remaining": entry.get("remote_month_remaining"),
            "remote_month_reset": entry.get("remote_month_reset"),
        }


def usage_today() -> dict:
    with _usage_lock:
        return _read_usage()
