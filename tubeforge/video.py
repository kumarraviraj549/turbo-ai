"""Video sources: OpenRouter AI video generation (async submit/poll/download, paid credits) and Pixabay
stock footage (free). Both return real video files; nothing is simulated."""
import base64
import io
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from PIL import Image

from . import cache, config
from .http import ProviderError, breaker_reason, request, trip_breaker

OR_VIDEOS = "https://openrouter.ai/api/v1/videos"
PREFERRED_VIDEO_MODELS = [
    "bytedance/seedance-2.0-fast", "kwaivgi/kling-v3.0-std", "alibaba/wan-2.7", "google/veo-3.1-lite",
    "minimax/hailuo-2.3", "google/veo-3.1-fast", "x-ai/grok-imagine-video", "runway/gen-4.5",
]
_RES_PIXELS = {"360p": (640, 360), "480p": (854, 480), "720p": (1280, 720), "768p": (1366, 768),
               "1080p": (1920, 1080), "2K": (2560, 1440), "4K": (3840, 2160)}


class VideoGenError(RuntimeError):
    pass


def _auth() -> dict:
    return {"Authorization": f"Bearer {config.openrouter_key()}"}


def video_models(force: bool = False) -> List[dict]:
    """OpenRouter video model catalogue (public endpoint, cached 6h)."""
    ck = cache.cache_key("or_video_models")
    if not force:
        hit = cache.get_json("openrouter", ck, max_age=6 * 3600)
        if hit:
            return hit
    try:
        resp = request("openrouter", "GET", OR_VIDEOS + "/models", timeout=30, retries=2)
        data = resp.json().get("data") or []
    except ProviderError:
        return cache.get_json("openrouter", ck) or []
    models = [m for m in data if m.get("supported_durations") and m.get("supported_resolutions")]
    cache.put_json("openrouter", ck, models)
    return models


def model_info(model_id: str) -> Optional[dict]:
    return next((m for m in video_models() if m.get("id") == model_id), None)


def _nearest(values: List[int], want: float) -> int:
    return min(values, key=lambda v: (abs(v - want), -v))


def pick_params(model: dict, want_seconds: float, want_res: str, aspect: str) -> Dict:
    durations = sorted(int(d) for d in model.get("supported_durations") or [5])
    longer = [d for d in durations if d >= want_seconds]
    duration = longer[0] if longer else durations[-1]
    resolutions = model.get("supported_resolutions") or ["720p"]
    order = ["360p", "480p", "720p", "768p", "1080p", "2K", "4K"]
    want_idx = order.index(want_res) if want_res in order else 2
    res = min(resolutions, key=lambda r: (abs(order.index(r) - want_idx) if r in order else 9,
                                          -(order.index(r) if r in order else 0)))
    aspects = model.get("supported_aspect_ratios") or ["16:9"]
    ar = aspect if aspect in aspects else aspects[0]
    return {"duration": duration, "resolution": res, "aspect_ratio": ar}


def estimate_cost(model: dict, duration: float, resolution: str, audio: bool = False) -> Optional[float]:
    """Best-effort USD estimate from OpenRouter pricing SKUs. Returns None when pricing is not per-second."""
    skus = model.get("pricing_skus") or {}
    if not skus:
        return None
    res_key = resolution.lower()

    def pick(prefixes: List[str]) -> Optional[Tuple[str, float]]:
        cands = []
        for k, v in skus.items():
            if not any(k.startswith(p) for p in prefixes):
                continue
            try:
                val = float(v)
            except (TypeError, ValueError):
                continue
            score = 0
            if res_key in k.lower():
                score += 4
            elif re.search(r"_(\d+p|2k|4k)$", k.lower()):
                score -= 4
            if ("without_audio" in k) == (not audio):
                score += 2
            if "image_to_video" in k:
                score += 1
            cands.append((score, k, val))
        if not cands:
            return None
        cands.sort(reverse=True)
        return cands[0][1], cands[0][2]

    hit = pick(["duration_seconds", "image_to_video_duration_seconds", "text_to_video_duration_seconds"])
    if hit:
        return round(hit[1] * duration, 3)
    hit = pick(["cents_per_video_output_second", "cents_per_second_output"])
    if hit:
        return round(hit[1] * duration / 100.0, 3)
    hit = pick(["video_tokens"])
    if hit:
        w, h = _RES_PIXELS.get(resolution, (1280, 720))
        tokens = w * h * 24 * duration / 1024.0
        return round(hit[1] * tokens, 3)
    return None


def compatible_models(want_seconds: float, want_res: str, aspect: str, image_to_video: bool = True) -> List[dict]:
    out = []
    for m in video_models():
        if aspect not in (m.get("supported_aspect_ratios") or []):
            continue
        if image_to_video and "first_frame" not in (m.get("supported_frame_images") or []):
            continue
        p = pick_params(m, want_seconds, want_res, aspect)
        out.append({"id": m["id"], "name": m.get("name", m["id"]), **p,
                    "usd": estimate_cost(m, p["duration"], p["resolution"])})
    rank = {mid: i for i, mid in enumerate(PREFERRED_VIDEO_MODELS)}
    out.sort(key=lambda x: (rank.get(x["id"], 99), x["usd"] if x["usd"] is not None else 99))
    return out


def openrouter_credits() -> Optional[dict]:
    """Returns {'limit_remaining', 'is_free_tier', 'usage'} for the configured key, or None."""
    if not config.openrouter_key():
        return None
    ck = cache.cache_key("or_key_info", config.openrouter_key()[-6:])
    hit = cache.get_json("openrouter", ck, max_age=300)
    if hit:
        return hit
    try:
        resp = request("openrouter", "GET", "https://openrouter.ai/api/v1/key", headers=_auth(), timeout=20, retries=1)
        data = resp.json().get("data") or {}
    except ProviderError:
        return None
    info = {"limit_remaining": data.get("limit_remaining"), "is_free_tier": data.get("is_free_tier"),
            "usage": data.get("usage"), "limit": data.get("limit")}
    cache.put_json("openrouter", ck, info)
    return info


def _first_frame_url(image_path: Path) -> str:
    img = Image.open(image_path).convert("RGB")
    img.thumbnail((1920, 1920))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def generate_ai_clip(
    prompt: str,
    out_path: Path,
    model_id: str,
    want_seconds: float,
    want_res: str,
    aspect: str,
    first_frame: Optional[Path] = None,
    seed: Optional[int] = None,
    log: Optional[Callable[[str], None]] = None,
    poll_timeout: float = 900,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> Dict:
    """Submits an OpenRouter video job, polls until done and downloads the MP4."""
    if not config.openrouter_key():
        raise VideoGenError("OPENROUTER_API_KEY missing")
    reason = breaker_reason("openrouter_video")
    if reason:
        raise VideoGenError(f"OpenRouter video disabled: {reason}")
    model = model_info(model_id)
    if not model:
        raise VideoGenError(f"Unknown OpenRouter video model '{model_id}'")
    params = pick_params(model, want_seconds, want_res, aspect)
    ck = cache.cache_key("orvid", model_id, prompt, params, seed,
                         cache.cache_key(first_frame.read_bytes()) if first_frame else "")
    hit = cache.get_bytes("video", ck, ".mp4")
    if hit:
        out_path.write_bytes(hit)
        return {"model": model_id, **params, "usd": 0.0, "cached": True}
    body = {"model": model_id, "prompt": prompt[:2500], **params, "generate_audio": False}
    if seed is not None and model.get("seed"):
        body["seed"] = int(seed) % 2 ** 31
    if first_frame and "first_frame" in (model.get("supported_frame_images") or []):
        body["frame_images"] = [{"type": "image_url", "image_url": {"url": _first_frame_url(first_frame)},
                                 "frame_type": "first_frame"}]
    try:
        resp = request("openrouter_video", "POST", OR_VIDEOS, json=body, headers=_auth(), timeout=120, retries=1,
                       max_wait=30, breaker_key="openrouter_video")
    except ProviderError as e:
        if e.status == 402 or "credit" in str(e).lower():
            trip_breaker("openrouter_video", "OpenRouter account has no credits for paid video models", 3600)
            raise VideoGenError("OpenRouter needs credits for AI video (HTTP 402). Add credits at "
                                "openrouter.ai/settings/credits or use the free cinematic engine.")
        raise VideoGenError(str(e))
    job = resp.json()
    job_id = job.get("id")
    poll_url = job.get("polling_url") or f"{OR_VIDEOS}/{job_id}"
    if log:
        log(f"🎞️ OpenRouter job {job_id} submitted ({model_id}, {params['duration']}s {params['resolution']})")
    start = time.time()
    status = job.get("status", "pending")
    delay = 4.0
    while status not in ("completed", "failed", "cancelled", "expired"):
        if should_cancel and should_cancel():
            raise VideoGenError("cancelled by user")
        if time.time() - start > poll_timeout:
            raise VideoGenError(f"OpenRouter video job {job_id} timed out after {int(poll_timeout)}s")
        time.sleep(delay)
        delay = min(15.0, delay * 1.3)
        job = request("openrouter_video", "GET", poll_url, headers=_auth(), timeout=30, retries=3).json()
        status = job.get("status", status)
    if status != "completed":
        raise VideoGenError(f"OpenRouter video job {status}: {job.get('error') or ''}")
    urls = job.get("unsigned_urls") or [u.get("url") for u in job.get("outputs", []) if isinstance(u, dict)]
    urls = [u for u in urls if u]
    if not urls:
        raise VideoGenError("OpenRouter video job completed without a download URL")
    data = request("openrouter_video", "GET", urls[0], headers=_auth(), timeout=300, retries=3).content
    if len(data) < 10000:
        raise VideoGenError("OpenRouter returned an empty video file")
    out_path.write_bytes(data)
    cache.put_bytes("video", ck, ".mp4", data)
    usd = (job.get("usage") or {}).get("cost")
    if usd is None:
        usd = estimate_cost(model, params["duration"], params["resolution"]) or 0.0
    cache.record_usage("openrouter_video", usd=float(usd))
    return {"model": model_id, **params, "usd": float(usd), "cached": False, "job_id": job_id}


@dataclass
class StockClip:
    id: int
    url: str
    width: int
    height: int
    duration: float
    thumb: str
    user: str
    page: str


def search_stock_videos(query: str, portrait: bool = False, min_width: int = 1280, per_page: int = 12) -> List[StockClip]:
    if not config.pixabay_key():
        return []
    q = re.sub(r"\s+", " ", query).strip()[:100]
    params = {"key": config.pixabay_key(), "q": q, "video_type": "film", "safesearch": "true", "per_page": per_page,
              "order": "popular"}
    ck = cache.cache_key("pixabay_vid", q, per_page)
    hits = cache.get_json("pixabay", ck, max_age=24 * 3600)
    if hits is None:
        resp = request("pixabay", "GET", "https://pixabay.com/api/videos/", params=params, timeout=30, retries=2)
        hits = resp.json().get("hits") or []
        cache.put_json("pixabay", ck, hits)
    clips = []
    for h in hits:
        vids = h.get("videos") or {}
        renditions = [v for v in vids.values() if isinstance(v, dict) and v.get("url") and v.get("width")]
        if not renditions:
            continue
        # Smallest rendition that still meets the target width, else the largest available.
        big_enough = [v for v in renditions if v["width"] >= min_width]
        best = min(big_enough, key=lambda v: v["width"]) if big_enough else max(renditions, key=lambda v: v["width"])
        clips.append(StockClip(h.get("id"), best["url"], best.get("width", 0), best.get("height", 0),
                               float(h.get("duration") or 0), best.get("thumbnail") or "", h.get("user", ""),
                               h.get("pageURL", "")))
    return clips


def download_stock_clip(clip: StockClip, out_path: Path) -> Path:
    ck = cache.cache_key("pixabay_clip", clip.id, clip.url)
    data = cache.get_bytes("video", ck, ".mp4")
    if not data:
        data = request("pixabay_cdn", "GET", clip.url, timeout=180, retries=3).content
        if len(data) < 10000:
            raise VideoGenError("Pixabay returned an empty clip")
        cache.put_bytes("video", ck, ".mp4", data)
        cache.record_usage("pixabay")
    out_path.write_bytes(data)
    return out_path


def fetch_stock_clip(query: str, out_path: Path, portrait: bool, min_seconds: float, min_width: int,
                     variant: int = 0) -> Optional[StockClip]:
    clips = search_stock_videos(query, portrait, min_width)
    if not clips:
        words = query.split()
        if len(words) > 2:
            clips = search_stock_videos(" ".join(words[:2]), portrait, min_width)
    if not clips:
        return None
    good = [c for c in clips if c.duration >= min(min_seconds, 8)] or clips
    clip = good[variant % len(good)]
    download_stock_clip(clip, out_path)
    return clip
