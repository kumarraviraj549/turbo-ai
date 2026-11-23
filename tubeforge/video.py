"""Video sources: OpenRouter AI video generation and Pixabay/Pexels stock footage."""
import base64
import io
import math
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


def paid_video_estimate(model_count: int, seconds: float, usd_per_clip: float) -> dict:
    """Visible, conservative estimate; actual provider accounting remains in the provider."""
    estimate = round(max(0, int(model_count)) * max(0.0, float(seconds)) * max(0.0, float(usd_per_clip)), 3)
    return {"clip_count": max(0, int(model_count)), "seconds": max(0.0, float(seconds)),
            "usd": estimate, "provider": "OpenRouter/fal/Replicate"}


def can_spend_paid_video(project_settings: dict, estimate_usd: float) -> bool:
    """Hard per-project USD guard. Cached results cost $0 and never require spending."""
    if not project_settings.get("allow_paid_video", False):
        return False
    if estimate_usd is None:
        return False
    try:
        estimate_usd = float(estimate_usd)
    except (TypeError, ValueError):
        return False
    budget = float(project_settings.get("paid_video_budget_usd", 0.0) or 0.0)
    spent = float(project_settings.get("paid_video_spent_usd", 0.0) or 0.0)
    return (math.isfinite(budget) and math.isfinite(spent) and math.isfinite(estimate_usd)
            and budget > 0 and estimate_usd > 0 and spent + estimate_usd <= budget)


def export_flow_pack(scene, image_path: Path, out_path: Path) -> Path:
    """Creates a manual Google Flow import pack; no browser or Google web UI automation."""
    import json
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pack = {
        "provider": "Google Flow / Google Vids",
        "automation": False,
        "first_frame": str(image_path),
        "prompt": " ".join(filter(None, [
            getattr(scene, "video_prompt", ""), getattr(scene, "subject", ""),
            getattr(scene, "action", ""), getattr(scene, "mood", ""),
        ])),
        "shot": getattr(scene, "shot_type", ""),
        "lens": getattr(scene, "lens", ""),
        "camera": getattr(scene, "camera_move", ""),
        "lighting": getattr(scene, "lighting", ""),
        "negative_prompt": getattr(scene, "negative_prompt", ""),
        "duration_seconds": float(getattr(scene, "duration", 5.0)),
        "aspect_ratio": "16:9",
    }
    out_path.write_text(json.dumps(pack, indent=2, ensure_ascii=False), encoding="utf-8")
    return out_path


def import_comfy_workflow(path: Path) -> dict:
    """Accept a ComfyUI-exported API prompt JSON and reject prompts without a mapping."""
    import json
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("prompt"), dict):
        data = data["prompt"]
    if not isinstance(data, dict) or not data:
        raise VideoGenError("ComfyUI workflow must be a non-empty exported API prompt JSON object.")
    if not all(isinstance(key, str) and isinstance(value, dict) for key, value in data.items()):
        raise VideoGenError("Invalid ComfyUI API prompt: node IDs must map to node objects.")
    return data


def comfy_submit(base_url: str, workflow: dict) -> dict:
    """Submit an imported ComfyUI API workflow to the documented /prompt route."""
    from urllib.parse import urlparse
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise VideoGenError("ComfyUI URL is invalid.")
    resp = request("comfyui", "POST", base_url.rstrip("/") + "/prompt", json={"prompt": workflow},
                   timeout=30, retries=2)
    data = resp.json()
    if not data.get("prompt_id"):
        raise VideoGenError("ComfyUI /prompt did not return a prompt_id.")
    return data


def comfy_history(base_url: str, prompt_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", str(prompt_id or "")):
        raise VideoGenError("Invalid ComfyUI prompt_id.")
    resp = request("comfyui", "GET", base_url.rstrip("/") + "/history/" + prompt_id,
                   timeout=30, retries=2)
    return resp.json().get(prompt_id) or {}


def generate_comfy_clip(base_url: str, workflow: dict, out_path: Path,
                        poll_timeout: float = 900,
                        should_cancel: Optional[Callable[[], bool]] = None,
                        log: Optional[Callable[[str], None]] = None) -> Dict:
    """Run a user-exported ComfyUI API workflow and retrieve its first output.

    This deliberately does not rewrite node IDs or invent Wan/LTX fields. The workflow
    must already reference an uploaded image and export one video filename.
    """
    if not base_url:
        raise VideoGenError("ComfyUI URL is not configured")
    submission = comfy_submit(base_url, workflow)
    prompt_id = submission["prompt_id"]
    started = time.time()
    history = {}
    while time.time() - started < poll_timeout:
        if should_cancel and should_cancel():
            raise VideoGenError("cancelled by user")
        history = comfy_history(base_url, prompt_id)
        if history:
            status = history.get("status") or {}
            if status.get("status_str") == "error":
                raise VideoGenError("ComfyUI workflow failed: " + str(status))
            outputs = history.get("outputs") or {}
            if outputs:
                break
        time.sleep(2.0)
    if not history:
        raise VideoGenError(f"ComfyUI job {prompt_id} timed out")
    outputs = history.get("outputs") or {}
    candidates = []
    for node_output in outputs.values():
        if not isinstance(node_output, dict):
            continue
        for key in ("images", "gifs", "videos"):
            for item in node_output.get(key) or []:
                if isinstance(item, dict) and item.get("filename"):
                    suffix = Path(str(item["filename"])).suffix.lower()
                    if key != "images" and suffix in (".mp4", ".mov", ".webm", ".mkv", ".gif"):
                        candidates.append(item)
    if not candidates:
        raise VideoGenError("ComfyUI completed but exposed no downloadable video/image output")
    item = candidates[0]
    from urllib.parse import urlencode
    params = {"filename": item["filename"]}
    if item.get("subfolder"):
        params["subfolder"] = item["subfolder"]
    if item.get("type"):
        params["type"] = item["type"]
    data = request("comfyui", "GET", base_url.rstrip("/") + "/view?" + urlencode(params),
                   timeout=300, retries=2).content
    if len(data) < 100:
        raise VideoGenError("ComfyUI returned an empty output")
    out_path.write_bytes(data)
    if log:
        log(f"🎞️ ComfyUI clip ready: {prompt_id}")
    return {"provider": "comfyui_local", "prompt_id": prompt_id, "filename": item["filename"],
            "usd": 0.0, "cached": False}


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
    id: str
    url: str
    width: int
    height: int
    duration: float
    thumb: str
    user: str
    page: str
    provider: str = "Pixabay"


def search_stock_videos(query: str, portrait: bool = False, min_width: int = 1280, per_page: int = 12) -> List[StockClip]:
    q = re.sub(r"\s+", " ", query).strip()[:100]
    clips = []
    errors = []
    if config.pexels_key():
        params = {"query": q, "orientation": "portrait" if portrait else "landscape",
                  "size": "medium", "per_page": min(80, max(1, per_page))}
        ck = cache.cache_key("pexels_vid", q, params["orientation"], min_width, params["per_page"])
        hits = cache.get_json("pexels", ck, max_age=24 * 3600)
        if hits is None:
            if not cache.reserve_provider_request("pexels", 200, 20000):
                errors.append("Pexels local safety limit reached (200 requests/hour or 20,000/month)")
            else:
                try:
                    resp = request("pexels", "GET", "https://api.pexels.com/v1/videos/search", params=params,
                                   headers={"Authorization": config.pexels_key()}, timeout=30, retries=2)
                    cache.record_usage("pexels")
                    limit, remaining, reset = (resp.headers.get(name) for name in (
                        "X-Ratelimit-Limit", "X-Ratelimit-Remaining", "X-Ratelimit-Reset"))
                    if limit and remaining and reset:
                        cache.record_provider_quota("pexels", int(limit), int(remaining), int(reset))
                    hits = resp.json().get("videos") or []
                    cache.put_json("pexels", ck, hits)
                except ProviderError as exc:
                    errors.append(str(exc)[:160])
        if hits:
            for hit in hits:
                renditions = [item for item in hit.get("video_files", [])
                              if item.get("link") and item.get("file_type") == "video/mp4"
                              and item.get("width") and item.get("height")]
                if not renditions:
                    continue
                big_enough = [item for item in renditions if item["width"] >= min_width]
                best = min(big_enough, key=lambda item: item["width"]) if big_enough else max(
                    renditions, key=lambda item: item["width"])
                if (best["height"] > best["width"]) != portrait:
                    continue
                user = (hit.get("user") or {}).get("name", "unknown")
                clips.append(StockClip(
                    f"pexels-{hit.get('id')}", best["link"], best["width"], best["height"],
                    float(hit.get("duration") or 0), hit.get("image", ""), user, hit.get("url", ""), "Pexels"))

    if config.pixabay_key():
        params = {"key": config.pixabay_key(), "q": q, "video_type": "film", "safesearch": "true",
                  "per_page": per_page, "order": "popular"}
        ck = cache.cache_key("pixabay_vid", q, portrait, per_page)
        hits = cache.get_json("pixabay", ck, max_age=24 * 3600)
        if hits is None:
            try:
                resp = request("pixabay", "GET", "https://pixabay.com/api/videos/", params=params,
                               timeout=30, retries=2)
                cache.record_usage("pixabay")
                hits = resp.json().get("hits") or []
                cache.put_json("pixabay", ck, hits)
            except ProviderError as exc:
                errors.append(str(exc)[:160])
        if hits:
            for hit in hits:
                renditions = [item for item in (hit.get("videos") or {}).values()
                              if isinstance(item, dict) and item.get("url") and item.get("width") and item.get("height")]
                renditions = [item for item in renditions if (item["height"] > item["width"]) == portrait]
                if not renditions:
                    continue
                # Smallest rendition that still meets target width, else the largest available.
                big_enough = [item for item in renditions if item["width"] >= min_width]
                best = min(big_enough, key=lambda item: item["width"]) if big_enough else max(
                    renditions, key=lambda item: item["width"])
                clips.append(StockClip(
                    str(hit.get("id")), best["url"], best["width"], best["height"],
                    float(hit.get("duration") or 0), best.get("thumbnail") or "", hit.get("user", ""),
                    hit.get("pageURL", ""), "Pixabay"))
    if not clips and errors:
        raise VideoGenError("Stock search failed: " + " | ".join(errors[-2:]))
    return clips


def download_stock_clip(clip: StockClip, out_path: Path) -> Path:
    provider_key = clip.provider.lower()
    ck = cache.cache_key(provider_key + "_clip", clip.id, clip.url)
    data = cache.get_bytes("video", ck, ".mp4")
    if not data:
        data = request(f"{provider_key}_cdn", "GET", clip.url, timeout=180, retries=3).content
        if len(data) < 10000:
            raise VideoGenError(f"{clip.provider} returned an empty clip")
        cache.put_bytes("video", ck, ".mp4", data)
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
