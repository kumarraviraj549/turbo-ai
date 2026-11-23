#!/usr/bin/env python3
"""Minimal, explicit live-key smoke checks. Secrets are never included in output."""
import argparse
import json
import re
import sys
import tempfile
import time
import wave
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Dict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tubeforge import alignment, cache, config, images, llm, tts, youtube_upload
from tubeforge.http import ProviderError, request


class FallbackCheckError(ProviderError):
    def __init__(self, status: int):
        super().__init__("groq_tts", "Orpheus failed but the offline fallback produced valid audio.", status=status)
        self.fallback_succeeded = True


@contextmanager
def _single_attempt(module):
    """Prevent the shared retry layer from spending more than one live request per check."""
    original = module.request

    def once(*args, **kwargs):
        kwargs["retries"] = 0
        kwargs["max_wait"] = 0
        return original(*args, **kwargs)

    module.request = once
    try:
        yield
    finally:
        module.request = original


def _result(name: str, func: Callable[[], str], configured: bool = True) -> Dict:
    if not configured:
        return {"provider": name, "status": "SKIPPED", "http_status": None, "latency_ms": 0,
                "message": "No key configured."}
    started = time.perf_counter()
    try:
        detail = func() or "Live request succeeded."
        return {"provider": name, "status": "PASS", "http_status": 200,
                "latency_ms": round((time.perf_counter() - started) * 1000), "message": detail[:240]}
    except ProviderError as exc:
        status = "RATE_LIMITED" if exc.status == 429 else "FAIL"
        messages = {
            400: "Request rejected; verify the key, model/endpoint access, and required request fields.",
            401: "Authentication failed; verify the key is active and copied without whitespace.",
            402: "Provider requires available credits or an eligible plan for this endpoint.",
            404: "Endpoint or model was not found; check the provider's current API catalog.",
            429: "Rate limited; retry after the provider's reset window.",
        }
        message = messages.get(exc.status, "Provider request failed; check connectivity and provider status.")
        if exc.status == 400 and exc.provider == "groq_tts":
            message = "Orpheus rejected the request; verify model access/terms, voice, and WAV settings."
        elif exc.status == 400 and exc.provider == "cloudflare":
            message = "Cloudflare rejected the image request; verify account/model access and current input schema."
        elif exc.status == 400 and exc.provider == "pixabay":
            message = "Pixabay rejected the search; verify the API key, query parameters, and account status."
        if getattr(exc, "fallback_succeeded", False):
            message += " Offline Kokoro fallback still generated a valid WAV."
        return {"provider": name, "status": status, "http_status": exc.status or None,
                "latency_ms": round((time.perf_counter() - started) * 1000), "message": message}
    except Exception as exc:
        return {"provider": name, "status": "FAIL", "http_status": None,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "message": f"{type(exc).__name__}; check provider settings and local dependencies."}


def _gemini() -> str:
    with _single_attempt(llm):
        raw = llm._call_gemini("gemini-3.5-flash-lite", "Return JSON only.", 'Return {"ok":true}',
                               30, 0.0, True, 30)
    if llm.parse_json(raw).get("ok") is not True:
        raise ValueError("Gemini structured JSON validation failed.")
    return "Structured JSON parsed successfully."


def _groq_llm() -> str:
    with _single_attempt(llm):
        raw = llm._call_groq("openai/gpt-oss-120b", "Return a short response.", "Reply with OK.",
                             30, 0.0, False, 30)
    if not raw.strip():
        raise ValueError("Groq returned an empty completion.")
    return "Completion received."


def _groq_whisper() -> str:
    with tempfile.TemporaryDirectory(prefix="tubeforge-live-whisper-") as directory:
        path = Path(directory) / "probe.wav"
        tts.synthesize("This is a short word timestamp check.", path, engine="kokoro",
                       voice="af_bella", speed=1.0, log=None)
        try:
            with _single_attempt(alignment):
                words = alignment._groq_one(path, "en")
        except Exception as exc:
            if isinstance(exc, ProviderError):
                raise
            raise ValueError("Whisper did not return valid word timestamps.") from exc
        if not isinstance(words, list):
            raise ValueError("Whisper word timestamps were invalid.")
    return "Word-level timestamp response parsed."


def _groq_orpheus() -> str:
    with tempfile.TemporaryDirectory(prefix="tubeforge-live-orpheus-") as directory:
        path = Path(directory) / "probe.wav"
        with _single_attempt(tts):
            result = tts.synthesize("This is a short live voice check.", path, engine="groq_orpheus",
                                    voice="troy", speed=1.0, log=None)
        with wave.open(str(result.path), "rb") as source:
            if source.getnframes() <= 0 or source.getframerate() <= 0:
                raise ValueError("Orpheus returned an empty WAV.")
        if result.warnings:
            match = re.search(r"HTTP (\d+)", result.warnings[0])
            raise FallbackCheckError(int(match.group(1)) if match else 0)
        return "Valid WAV response received."


def _cloudflare() -> str:
    neurons = images.estimate_neurons("cf_flux1_schnell", 512, 512, steps=4)
    if not cache.reserve_cloudflare_neurons(neurons):
        raise ProviderError("cloudflare", "daily free neuron budget guard blocked the live check", status=429)
    try:
        with _single_attempt(images):
            data = images._cf_flux1_schnell("simple blue geometric circle on a plain light background", 512, 512, 1, 4)
        image = images._validate_image(data)
        # The reservation already adds neurons to the UTC-day usage total.
        cache.record_usage("cloudflare")
        return f"Valid image bytes received ({image.size[0]}x{image.size[1]})."
    except Exception:
        cache.release_cloudflare_neurons(neurons)
        raise


def _pexels() -> str:
    response = request("pexels", "GET", "https://api.pexels.com/v1/videos/search",
                       params={"query": "ocean", "orientation": "landscape", "per_page": 1},
                       headers={"Authorization": config.pexels_key()}, timeout=30, retries=0)
    videos = response.json().get("videos") or []
    files = [item for hit in videos for item in hit.get("video_files", [])
             if item.get("link") and item.get("file_type") == "video/mp4"]
    if not files:
        raise ValueError("No playable Pexels MP4 was returned.")
    clip_url = files[0]["link"]
    if not clip_url.lower().split("?")[0].endswith(".mp4"):
        raise ValueError("Pexels did not return an MP4 URL.")
    return "Playable landscape MP4 URL returned."


def _pixabay() -> str:
    response = request("pixabay", "GET", "https://pixabay.com/api/videos/",
                       params={"key": config.pixabay_key(), "q": "ocean", "video_type": "film",
                               "safesearch": "true", "per_page": 1},
                       timeout=30, retries=0)
    hits = response.json().get("hits") or []
    files = [item for hit in hits for item in (hit.get("videos") or {}).values()
             if isinstance(item, dict) and item.get("url")]
    if not files:
        raise ValueError("No playable Pixabay MP4 was returned.")
    clip_url = files[0]["url"]
    if not clip_url.lower().split("?")[0].endswith(".mp4"):
        raise ValueError("Pixabay did not return an MP4 URL.")
    return "Playable landscape MP4 URL returned."


def _sarvam() -> str:
    raw = tts._sarvam("आपका order confirm हो गया है।", "shubh_enhi_ads", "Hindi", 1.0)
    samples, _ = tts._read_wav_bytes(raw)
    if samples.size == 0:
        raise ValueError("Sarvam returned empty audio.")
    return "Valid audio response received."


def _pollinations() -> str:
    if config.pollinations_key_kind() != "secret":
        raise ProviderError("pollinations", "A secret sk_ key is required; pk_ is not accepted for image calls",
                            status=401)
    with _single_attempt(images):
        raw = images._pollinations("simple blue circle", 512, 512, 1, True, None)
    image = images._validate_image(raw)
    return f"Valid image bytes received ({image.size[0]}x{image.size[1]})."


def _youtube_oauth() -> str:
    client_path = Path(config.youtube_client_secret_path())
    token_path = config.CACHE_DIR / "youtube" / "readonly_token.json"
    title = youtube_upload.channel_title(client_path, token_path, allow_browser=False)
    return f"Read-only OAuth verified for channel: {title}."


def run_live_checks() -> Dict:
    results = [
        _result("Gemini", _gemini, bool(config.gemini_key())),
        _result("Groq LLM", _groq_llm, bool(config.groq_key())),
        _result("Groq Whisper", _groq_whisper, bool(config.groq_key())),
        _result("Groq Orpheus", _groq_orpheus, bool(config.groq_key())),
        _result("Cloudflare Workers AI", _cloudflare,
                bool(config.cloudflare_token() and config.cloudflare_account())),
        _result("Pexels video", _pexels, bool(config.pexels_key())),
        _result("Pixabay video", _pixabay, bool(config.pixabay_key())),
        _result("Sarvam", _sarvam, bool(config.sarvam_key())),
        _result("Pollinations", _pollinations, bool(config.pollinations_key())),
        _result("YouTube OAuth", _youtube_oauth, Path(config.youtube_client_secret_path()).is_file()),
    ]
    return {"results": results, "summary": {
        status: sum(row["status"] == status for row in results)
        for status in ("PASS", "FAIL", "RATE_LIMITED", "SKIPPED")
    }}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print machine-readable results.")
    args = parser.parse_args()
    report = run_live_checks()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if report["summary"]["FAIL"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
