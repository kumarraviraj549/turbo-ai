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
CLOUDFLARE_FREE_NEURONS = 10000


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


def usage_today() -> dict:
    with _usage_lock:
        return _read_usage()
