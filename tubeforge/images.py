"""Image generation router: Cloudflare Workers AI (FLUX.2 / Lucid / Phoenix), OpenRouter image models,
Gemini native images, Pollinations and Pixabay stock photos. Every result is a real image from a provider;
if all providers fail the caller gets an exception (never a silent fake)."""
import base64
import hashlib
import io
import math
import random
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from PIL import Image

from . import cache, config
from .http import ProviderError, breaker_reason, quota_cooldown, request, trip_breaker

BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) TubeForgeStudio/2.0"}
CF_BASE = "https://api.cloudflare.com/client/v4/accounts/{acc}/ai/run/"


@dataclass
class ImageEngine:
    key: str
    label: str
    provider: str
    tier: str  # free / premium / paid / stock
    refs: bool = False  # supports reference images (character consistency)
    negative: bool = False  # supports negative prompts natively
    note: str = ""


ENGINES: Dict[str, ImageEngine] = {e.key: e for e in [
    ImageEngine("cf_flux1_schnell", "Cloudflare FLUX.1 Schnell (4 steps)", "cloudflare", "free",
                note="~4.8 neurons per 512px tile + 9.6 per step"),
    ImageEngine("cf_klein4b", "Cloudflare FLUX.2 Klein 4B (fast)", "cloudflare", "free", refs=True,
                note="5.37 neurons per input tile + 26.05 per output tile; supports reference editing"),
    ImageEngine("cf_klein9b", "Cloudflare FLUX.2 Klein 9B (sharper)", "cloudflare", "premium", refs=True,
                note="~1.5k neurons/image"),
    ImageEngine("cf_flux2dev", "Cloudflare FLUX.2 Dev (best detail)", "cloudflare", "premium", refs=True,
                note="~4-5k neurons/image"),
    ImageEngine("cf_lucid", "Cloudflare Leonardo Lucid Origin (native 1080p)", "cloudflare", "premium",
                note="~8k neurons/image"),
    ImageEngine("cf_phoenix", "Cloudflare Leonardo Phoenix (negative prompts)", "cloudflare", "premium",
                negative=True, note="~3-6k neurons/image"),
    ImageEngine("openrouter", "OpenRouter image model (paid credits)", "openrouter", "paid", refs=True,
                note="billed to your OpenRouter credits"),
    ImageEngine("gemini", "Gemini native image", "gemini", "free", refs=True,
                note="requires image quota on the Gemini key"),
    ImageEngine("pollinations_key", "Pollinations (API key)", "pollinations", "free"),
    ImageEngine("pollinations_free", "Pollinations free (slow, 1 image / ~45s)", "pollinations", "free"),
    ImageEngine("pexels", "Pexels stock photo", "pexels", "stock"),
    ImageEngine("pixabay", "Pixabay stock photo (real photo, keyword match)", "pixabay", "stock"),
]}

FREE_ENGINE_KEYS = {"cf_flux1_schnell", "cf_klein4b", "pollinations_key", "pollinations_free", "pexels", "pixabay"}
AUTO_ORDER = ["cf_flux1_schnell", "pollinations_free", "pexels", "pixabay"]
OPENROUTER_DEFAULT_IMAGE_MODEL = "black-forest-labs/flux.2-klein-4b"
GEMINI_IMAGE_MODELS = ["gemini-3.1-flash-image", "gemini-3.1-flash-lite-image"]


class ImageGenError(RuntimeError):
    pass


@dataclass
class ImageResult:
    path: Path
    engine: str
    model: str
    width: int
    height: int
    neurons: float = 0.0
    cached: bool = False
    warnings: List[str] = field(default_factory=list)


def engine_available(key: str) -> Tuple[bool, str]:
    eng = ENGINES[key]
    if eng.provider == "cloudflare":
        if not (config.cloudflare_token() and config.cloudflare_account()):
            return False, "CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID missing"
    elif eng.provider == "openrouter" and not config.openrouter_key():
        return False, "OPENROUTER_API_KEY missing"
    elif eng.provider == "gemini" and not config.gemini_key():
        return False, "GEMINI_API_KEY missing"
    elif key == "pollinations_key" and not config.pollinations_key():
        return False, "POLLINATIONS_API_KEY missing"
    elif key == "pollinations_key" and config.pollinations_key_kind() != "secret":
        return False, "Pollinations image requests require an sk_ secret key; pk_ publishable keys return HTTP 401"
    elif eng.provider == "pixabay" and not config.pixabay_key():
        return False, "PIXABAY_API_KEY missing"
    elif eng.provider == "pexels" and not config.pexels_key():
        return False, "PEXELS_API_KEY missing"
    reason = breaker_reason(eng.provider) or breaker_reason(f"img:{key}")
    if reason:
        return False, reason
    return True, ""


def available_engines() -> List[str]:
    return [k for k in ENGINES if engine_available(k)[0]]


def _tiles(w: int, h: int) -> int:
    return math.ceil(w / 512) * math.ceil(h / 512)


def estimate_neurons(engine: str, w: int, h: int, n_refs: int = 0, steps: int = 0,
                     ref_tiles: Optional[List[int]] = None) -> float:
    if engine == "cf_flux1_schnell":
        # The endpoint has no width/height inputs; observed output is 1024x1024.
        # Keep larger requested targets conservative in case endpoint output sizing changes.
        t = max(_tiles(w, h), _tiles(1024, 1024))
        return 4.8 * t + 9.6 * (steps or 4)
    t = _tiles(w, h)
    mp = w * h / 1_000_000
    if engine == "cf_klein4b":
        reference_tiles = sum(ref_tiles) if ref_tiles is not None else t * n_refs
        return 1364 * mp + 91 * reference_tiles
    if engine == "cf_klein9b":
        return 1364 + 182 * max(0.0, mp - 1) + 91 * mp * n_refs
    if engine == "cf_flux2dev":
        return 37.5 * t * (steps or 25) + 9 * t * n_refs
    if engine == "cf_lucid":
        return 636 * t + 12 * (steps or 25)
    if engine == "cf_phoenix":
        return 530 * t + 10 * (steps or 25)
    return 0.0


def generation_size(target_w: int, target_h: int, engine: str) -> Tuple[int, int]:
    """Native generation size per engine (the renderer upscales/crops to the export resolution)."""
    portrait = target_h > target_w
    square = abs(target_w - target_h) < 8
    if engine == "cf_lucid":
        base = (1920, 1088)
    elif engine == "cf_phoenix":
        base = (1536, 864)
    elif engine in ("pollinations_free",):
        base = (1024, 576)
    else:
        base = (1536, 864)
    if square:
        s = min(base[1] if engine != "pollinations_free" else 768, 1024)
        return s, s
    return (base[1], base[0]) if portrait else base


def aspect_label(w: int, h: int) -> str:
    r = w / max(1, h)
    for label, val in (("16:9", 16 / 9), ("9:16", 9 / 16), ("1:1", 1.0), ("4:3", 4 / 3), ("3:4", 3 / 4),
                       ("21:9", 21 / 9)):
        if abs(r - val) < 0.06:
            return label
    return "16:9" if r > 1 else "9:16"


def _validate_image(data: bytes) -> Image.Image:
    if not data or len(data) < 3000:
        raise ProviderError("image", "provider returned an empty image")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        raise ProviderError("image", "provider returned data that is not an image")
    return img.convert("RGB")


def _ref_bytes(path: Path, max_side: int = 1024) -> bytes:
    img = Image.open(path).convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _ref_tile_count(path: Path, max_side: int = 1024) -> int:
    with Image.open(path) as source:
        width, height = source.size
    scale = min(1.0, max_side / max(width, height))
    return _tiles(max(1, round(width * scale)), max(1, round(height * scale)))


def _cf_flagged(resp) -> bool:
    return resp.status_code == 400 and ("3030" in resp.text or "flagged" in resp.text.lower())


def _cf_fatal(resp) -> bool:
    return resp.status_code in (400, 404) and not _cf_flagged(resp)


def _cf_run(model: str, *, json_body=None, files=None, timeout=150):
    url = CF_BASE.format(acc=config.cloudflare_account()) + model
    headers = {"Authorization": f"Bearer {config.cloudflare_token()}"}
    return request("cloudflare", "POST", url, headers=headers, json=json_body, files=files, timeout=timeout,
                   retries=2, max_wait=30, is_fatal=_cf_fatal, breaker_key="cloudflare")


def _cf_flux2(model: str, prompt: str, w: int, h: int, seed: int, refs: List[Path], steps: int = 0) -> bytes:
    last: Optional[Exception] = None
    for attempt in range(3):
        files = {
            "prompt": (None, prompt[:2048]),
            "width": (None, str(w)),
            "height": (None, str(h)),
            "seed": (None, str((seed + attempt * 7919) % 2 ** 31)),
        }
        if steps:
            files["steps"] = (None, str(steps))
        for i, ref in enumerate(refs[:4]):
            files[f"input_image_{i}"] = (f"ref{i}.jpg", _ref_bytes(ref), "image/jpeg")
        try:
            resp = _cf_run("@cf/black-forest-labs/" + model, files=files)
        except ProviderError as e:
            if e.status == 400 and ("3030" in str(e) or "flagged" in str(e).lower()):
                last = e  # Safety filter false positive: a new seed usually passes.
                continue
            raise
        b64 = (resp.json().get("result") or {}).get("image")
        if not b64:
            raise ProviderError("cloudflare", f"{model} returned no image")
        return base64.b64decode(b64)
    raise ProviderError("cloudflare", f"{model}: output was flagged by the safety filter ({last})")


def _cf_flux1_schnell(prompt: str, w: int, h: int, seed: int, steps: int = 4) -> bytes:
    resp = _cf_run("@cf/black-forest-labs/flux-1-schnell",
                   json_body={"prompt": prompt[:2048], "steps": max(4, min(8, steps))})
    if resp.headers.get("content-type", "").startswith("image/"):
        return resp.content
    b64 = (resp.json().get("result") or {}).get("image")
    if not b64:
        raise ProviderError("cloudflare", "flux-1-schnell returned no image")
    return base64.b64decode(b64)


def _cf_lucid(prompt: str, w: int, h: int, seed: int) -> bytes:
    resp = _cf_run("@cf/leonardo/lucid-origin",
                   json_body={"prompt": prompt[:2048], "width": w, "height": h, "steps": 25, "guidance": 4.5,
                              "seed": seed % 2 ** 31})
    b64 = (resp.json().get("result") or {}).get("image")
    if not b64:
        raise ProviderError("cloudflare", "lucid-origin returned no image")
    return base64.b64decode(b64)


def _cf_phoenix(prompt: str, negative: str, w: int, h: int, seed: int) -> bytes:
    body = {"prompt": prompt[:2048], "width": w, "height": h, "num_steps": 25, "guidance": 3.5,
            "seed": seed % 2 ** 31}
    if negative:
        body["negative_prompt"] = negative[:1000]
    resp = _cf_run("@cf/leonardo/phoenix-1.0", json_body=body)
    if resp.headers.get("content-type", "").startswith("image/"):
        return resp.content
    b64 = (resp.json().get("result") or {}).get("image")
    if not b64:
        raise ProviderError("cloudflare", "phoenix returned no image")
    return base64.b64decode(b64)


def _data_url(path: Path) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(_ref_bytes(path)).decode()


def _openrouter_image(prompt: str, w: int, h: int, seed: int, refs: List[Path], model: str) -> bytes:
    body = {"model": model or OPENROUTER_DEFAULT_IMAGE_MODEL, "prompt": prompt[:3000],
            "aspect_ratio": aspect_label(w, h), "n": 1, "seed": seed % 2 ** 31}
    if refs:
        body["input_references"] = [{"type": "image_url", "image_url": {"url": _data_url(r)}} for r in refs[:4]]
    resp = request("openrouter", "POST", "https://openrouter.ai/api/v1/images", json=body,
                   headers={"Authorization": f"Bearer {config.openrouter_key()}"}, timeout=240, retries=1,
                   max_wait=30, breaker_key="openrouter_images")
    for item in resp.json().get("data") or []:
        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"])
        if item.get("url"):
            return request("openrouter", "GET", item["url"], timeout=120, retries=2).content
    raise ProviderError("openrouter", "image model returned no image")


def _gemini_image(prompt: str, w: int, h: int, refs: List[Path]) -> bytes:
    parts = [{"text": prompt}]
    for r in refs[:3]:
        parts.append({"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(_ref_bytes(r)).decode()}})
    errors = []
    for model in GEMINI_IMAGE_MODELS:
        if breaker_reason(f"gemini:{model}"):
            continue
        try:
            resp = request(
                "gemini", "POST",
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": config.gemini_key()},
                json={"contents": [{"parts": parts}],
                      "generationConfig": {"responseModalities": ["IMAGE"],
                                           "imageConfig": {"aspectRatio": aspect_label(w, h)}}},
                timeout=150, retries=1, max_wait=15, breaker_key=f"gemini:{model}")
        except ProviderError as e:
            errors.append(str(e)[:120])
            if e.fatal or e.status in (429, 404, 400):
                trip_breaker(f"gemini:{model}", str(e)[:160], seconds=quota_cooldown("gemini", e))
            continue
        for cand in resp.json().get("candidates") or []:
            for part in (cand.get("content") or {}).get("parts") or []:
                if "inlineData" in part:
                    return base64.b64decode(part["inlineData"]["data"])
        errors.append(f"{model}: no image in response")
    raise ProviderError("gemini", "no Gemini image model produced an image: " + " | ".join(errors[-3:]), fatal=True)


def extract_original_style(screenshots: List[Path]) -> Dict[str, object]:
    """Derive a copyright-safe original style bible from 1–3 visual references."""
    if not config.gemini_key():
        raise ProviderError("gemini", "GEMINI_API_KEY is required for screenshot style extraction")
    if not 1 <= len(screenshots) <= 3:
        raise ValueError("Upload between one and three screenshots.")
    parts = [{
        "text": (
            "Analyze only broad visual qualities. Do NOT copy or identify characters, logos, compositions, "
            "specific artwork, or distinctive protected elements. Invent an original visual identity and a "
            "new character that is not a clone. Return strict JSON with palette (3-5 hex colors), lighting, "
            "art_medium, framing_rules (array), original_character (name and description), style_prefix, "
            "global_negative_prompt."
        )
    }]
    for path in screenshots:
        image = Image.open(path).convert("RGB")
        image.thumbnail((1024, 1024))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=85)
        parts.append({"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(buffer.getvalue()).decode()}})
    from . import llm

    errors = []
    for model in ("gemini-3.5-flash-lite", "gemini-3.8-flash"):
        try:
            response = request(
                "gemini", "POST",
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": config.gemini_key()},
                json={"contents": [{"parts": parts}],
                      "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 1200}},
                timeout=90, retries=0, breaker_key=f"gemini:{model}",
            )
            text = "".join(
                p.get("text", "")
                for candidate in (response.json().get("candidates") or [])
                for p in (candidate.get("content") or {}).get("parts", [])
            )
            data = llm.parse_json(text)
            palette = data.get("palette") if isinstance(data, dict) else None
            if not isinstance(palette, list) or not 3 <= len(palette) <= 5 \
                    or any(not isinstance(color, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", color)
                           for color in palette):
                raise ValueError("Style extractor returned an invalid hex palette.")
            if not data.get("style_prefix") or not data.get("original_character"):
                raise ValueError("Style extractor omitted the original style or character description.")
            return data
        except Exception as exc:
            errors.append(str(exc)[:160])
    raise ProviderError("gemini", "Could not extract an original style: " + " | ".join(errors))


def _pollinations(prompt: str, w: int, h: int, seed: int, with_key: bool, log) -> bytes:
    if with_key:
        if config.pollinations_key_kind() != "secret":
            raise ProviderError("pollinations", "Image requests require an sk_ secret key; pk_ keys are publishable only",
                                status=401, fatal=True)
        url = "https://gen.pollinations.ai/image/" + urllib.parse.quote(prompt[:1800], safe="")
        params = {"model": "flux", "width": w, "height": h, "seed": seed, "nologo": "true"}
        headers = {"Authorization": f"Bearer {config.pollinations_key()}"}
    else:
        url = "https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt[:900], safe="")
        params = {"width": w, "height": h, "seed": seed, "nologo": "true"}
        headers = BROWSER_HEADERS
    if log and not with_key:
        log("⏳ Pollinations free tier allows about 1 image every 45s from this IP")
    # Pollinations signals its anonymous rate limit with 402 + Retry-After.
    resp = request("pollinations", "GET", url, params=params, headers=headers, timeout=150, retries=2,
                   max_wait=65, retry_statuses=(402,) if not with_key else (),
                   breaker_key="pollinations_key" if with_key else "pollinations")
    return resp.content


def _pixabay_photo(query: str, w: int, h: int, seed: int) -> Tuple[bytes, str]:
    portrait = h > w
    params = {"key": config.pixabay_key(), "q": query[:100], "image_type": "photo", "safesearch": "true",
              "orientation": "vertical" if portrait else "horizontal", "per_page": 20,
              "min_width": min(w, 1280), "order": "popular"}
    ck = cache.cache_key("pixabay_img_search", params["q"], params["orientation"])
    hits = cache.get_json("pixabay", ck, max_age=24 * 3600)
    if hits is None:
        resp = request("pixabay", "GET", "https://pixabay.com/api/", params=params, timeout=30, retries=2)
        hits = resp.json().get("hits") or []
        cache.put_json("pixabay", ck, hits)
    if not hits:
        raise ProviderError("pixabay", f"no stock photo found for '{query}'")
    hit = hits[seed % min(len(hits), 6)]
    url = hit.get("largeImageURL") or hit.get("webformatURL")
    data = request("pixabay_cdn", "GET", url, timeout=60, retries=2).content
    return data, f"Photo by {hit.get('user', 'unknown')} on Pixabay · {hit.get('pageURL', '')}"


def _pexels_request(url: str, params: dict):
    if not cache.reserve_provider_request("pexels", 200, 20000):
        raise ProviderError("pexels", "local safety limit reached (200 requests/hour or 20,000/month)",
                            status=429)
    resp = request("pexels", "GET", url, params=params,
                   headers={"Authorization": config.pexels_key()}, timeout=30, retries=2)
    cache.record_usage("pexels")
    limit = resp.headers.get("X-Ratelimit-Limit")
    remaining = resp.headers.get("X-Ratelimit-Remaining")
    reset = resp.headers.get("X-Ratelimit-Reset")
    if limit and remaining and reset:
        cache.record_provider_quota("pexels", int(limit), int(remaining), int(reset))
    return resp


def _pexels_photo(query: str, w: int, h: int, seed: int) -> Tuple[bytes, str]:
    orientation = "portrait" if h > w else "landscape"
    params = {"query": query[:100], "orientation": orientation, "size": "large", "per_page": 20}
    ck = cache.cache_key("pexels_photo", params["query"], orientation)
    hits = cache.get_json("pexels", ck, max_age=24 * 3600)
    if hits is None:
        resp = _pexels_request("https://api.pexels.com/v1/search", params)
        hits = resp.json().get("photos") or []
        cache.put_json("pexels", ck, hits)
    if not hits:
        raise ProviderError("pexels", f"no stock photo found for '{query}'")
    hit = hits[seed % min(len(hits), 6)]
    source_key = "portrait" if orientation == "portrait" else "landscape"
    url = (hit.get("src") or {}).get(source_key) or (hit.get("src") or {}).get("large")
    if not url:
        raise ProviderError("pexels", f"photo #{hit.get('id')} has no downloadable source")
    data = request("pexels_cdn", "GET", url, timeout=60, retries=2).content
    return data, f"Photo by {hit.get('photographer', 'unknown')} on Pexels · {hit.get('url', '')}"


def _fit(img: Image.Image, w: int, h: int) -> Image.Image:
    """Center-crop to the target aspect ratio and resize with high-quality resampling."""
    src_r = img.width / img.height
    dst_r = w / h
    if abs(src_r - dst_r) > 0.01:
        if src_r > dst_r:
            nw = int(img.height * dst_r)
            x0 = (img.width - nw) // 2
            img = img.crop((x0, 0, x0 + nw, img.height))
        else:
            nh = int(img.width / dst_r)
            y0 = (img.height - nh) // 2
            img = img.crop((0, y0, img.width, y0 + nh))
    if img.size != (w, h):
        img = img.resize((w, h), Image.LANCZOS)
    return img


def generate_image(
    prompt: str,
    out_path: Path,
    width: int,
    height: int,
    seed: Optional[int] = None,
    engine: str = "auto",
    refs: Optional[List[Path]] = None,
    negative: str = "",
    stock_query: str = "",
    openrouter_model: str = "",
    allow_paid: bool = False,
    allow_stock: bool = True,
    log: Optional[Callable[[str], None]] = None,
) -> ImageResult:
    """Generates one image and saves it (fitted to width x height) as JPEG at out_path."""
    if engine != "auto" and engine not in FREE_ENGINE_KEYS:
        raise ImageGenError(f"{engine!r} is not enabled in free-tier-only mode")
    seed = random.randint(1, 2 ** 31 - 1) if seed is None else int(seed)
    refs = [Path(r) for r in (refs or []) if r and Path(r).exists()]
    if engine == "auto":
        order = [e for e in AUTO_ORDER if (allow_stock or ENGINES[e].tier != "stock")]
    else:
        order = [engine] + [e for e in AUTO_ORDER if e != engine and (allow_stock or ENGINES[e].tier != "stock")]
    full_prompt = prompt.strip()
    if negative and not ENGINES.get(engine, ENGINES["cf_klein4b"]).negative:
        # Models without negative prompt support get the exclusions phrased positively.
        full_prompt += f". Avoid: {negative.strip()}"
    warnings: List[str] = []
    for name in order:
        ok, why = engine_available(name)
        if not ok:
            if name == engine:
                warnings.append(f"{ENGINES[name].label} unavailable: {why}")
            continue
        gw, gh = generation_size(width, height, name)
        use_refs = refs if ENGINES[name].refs else []
        ck = cache.cache_key("img", name, openrouter_model if name == "openrouter" else "", full_prompt, negative,
                             gw, gh, seed, [hashlib.sha256(r.read_bytes()).hexdigest() for r in use_refs],
                             stock_query if name in ("pexels", "pixabay") else "")
        model = name
        neurons = 0.0
        reserved_neurons = False
        cached = cache.get_bytes("img", ck, ".jpg")
        try:
            if cached:
                data = cached
            elif name == "cf_flux1_schnell":
                model = "flux-1-schnell"
                neurons = estimate_neurons(name, gw, gh, steps=4)
                warning = cache.cloudflare_budget_warning(neurons)
                if warning["crosses_warning"] and log:
                    log("⚠️ Estimated Cloudflare usage will exceed 30% of the local 10,000-neuron UTC-day budget.")
                if not cache.reserve_cloudflare_neurons(neurons):
                    raise ProviderError("cloudflare", "daily free neuron budget would be exceeded")
                reserved_neurons = True
                data = _cf_flux1_schnell(full_prompt, gw, gh, seed)
            elif name in ("cf_klein4b", "cf_klein9b", "cf_flux2dev"):
                if name != "cf_klein4b":
                    raise ProviderError("cloudflare", f"{name} is not enabled in free-tier-only mode")
                model = {"cf_klein4b": "flux-2-klein-4b", "cf_klein9b": "flux-2-klein-9b",
                         "cf_flux2dev": "flux-2-dev"}[name]
                neurons = estimate_neurons(name, gw, gh, len(use_refs),
                                           ref_tiles=[_ref_tile_count(ref) for ref in use_refs])
                warning = cache.cloudflare_budget_warning(neurons)
                if warning["crosses_warning"] and log:
                    log("⚠️ Estimated Cloudflare usage will exceed 30% of the local 10,000-neuron UTC-day budget.")
                if not cache.reserve_cloudflare_neurons(neurons):
                    raise ProviderError("cloudflare", "daily free neuron budget would be exceeded")
                reserved_neurons = True
                data = _cf_flux2(model, full_prompt, gw, gh, seed, use_refs, steps=25 if name == "cf_flux2dev" else 0)
            elif name == "cf_lucid":
                raise ProviderError("cloudflare", "cf_lucid is not enabled in free-tier-only mode")
            elif name == "cf_phoenix":
                raise ProviderError("cloudflare", "cf_phoenix is not enabled in free-tier-only mode")
            elif name == "openrouter":
                model = openrouter_model or OPENROUTER_DEFAULT_IMAGE_MODEL
                data = _openrouter_image(full_prompt, gw, gh, seed, use_refs, model)
            elif name == "gemini":
                data = _gemini_image(full_prompt, gw, gh, use_refs)
            elif name in ("pollinations_key", "pollinations_free"):
                data = _pollinations(full_prompt, gw, gh, seed, name == "pollinations_key", log)
            elif name == "pixabay":
                data, model = _pixabay_photo(stock_query or prompt[:80], gw, gh, seed)
            elif name == "pexels":
                data, model = _pexels_photo(stock_query or prompt[:80], gw, gh, seed)
            else:
                continue
            img = _validate_image(data)
        except ProviderError as e:
            if reserved_neurons:
                cache.release_cloudflare_neurons(neurons)
            msg = f"{ENGINES[name].label}: {str(e)[:160]}"
            warnings.append(msg)
            if log:
                log(f"⚠️ {msg}")
            if e.fatal:
                scope = ENGINES[name].provider if e.status in (401, 402, 403, 429) else f"img:{name}"
                if name == "openrouter" and e.status == 402:
                    scope = "img:openrouter"
                trip_breaker(scope, str(e)[:160], seconds=quota_cooldown(ENGINES[name].provider, e))
            continue
        except Exception as e:
            if reserved_neurons:
                cache.release_cloudflare_neurons(neurons)
            msg = f"{ENGINES[name].label} failed: {str(e)[:160]}"
            warnings.append(msg)
            if log:
                log(f"⚠️ {msg}")
            continue
        if not cached:
            cache.put_bytes("img", ck, ".jpg", data)
            if neurons:
                cache.record_usage("cloudflare")
            else:
                cache.record_usage(ENGINES[name].provider)
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _fit(img, width, height).save(out_path, "JPEG", quality=94)
        return ImageResult(out_path, name, model, width, height, neurons, bool(cached), warnings)
    raise ImageGenError("No image provider could generate this image. " + " | ".join(warnings[-4:]))
