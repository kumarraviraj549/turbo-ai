"""Opt-in adapters for local REST media services."""
import base64
import ipaddress
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlparse

import requests

from . import config

SERVICE_ENV = {
    "acestep": "ACESTEP_URL",
    "chatterbox": "CHATTERBOX_URL",
    "qwen3_tts": "QWEN3_TTS_URL",
    "dia": "DIA_TTS_URL",
    "mmaudio": "MMAUDIO_URL",
}


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def service_url(service: str) -> str:
    env_name = SERVICE_ENV.get(service)
    if not env_name:
        raise ValueError(f"Unknown local AI service: {service}")
    raw = config.secret(env_name)
    if not raw:
        raise RuntimeError(f"{env_name} is not configured.")
    parsed = urlparse(raw)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or not _is_loopback(parsed.hostname)):
        raise ValueError(f"{env_name} must be a loopback URL such as http://127.0.0.1:8000.")
    return raw.rstrip("/")


def _audio_payload(response: requests.Response) -> bytes:
    content_type = response.headers.get("content-type", "").lower()
    if "json" not in content_type:
        return response.content
    try:
        data = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Local AI service returned invalid JSON.") from exc
    encoded = data.get("audio_base64") or data.get("audio")
    if isinstance(encoded, str):
        try:
            return base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise RuntimeError("Local AI service returned invalid base64 audio.") from exc
    audio_url = data.get("audio_url")
    if isinstance(audio_url, str):
        parsed = urlparse(audio_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or not _is_loopback(parsed.hostname):
            raise RuntimeError("Local AI service returned a non-loopback audio URL.")
        fetched = requests.get(audio_url, timeout=120, allow_redirects=False)
        if not fetched.ok:
            raise RuntimeError(f"Could not retrieve local AI audio (HTTP {fetched.status_code}).")
        return fetched.content
    raise RuntimeError("Local AI service response did not contain audio.")


def generate_audio(service: str, payload: Dict[str, Any], out_path: Path, timeout: float = 600) -> Path:
    """POSTs to a loopback-only /generate endpoint and atomically stores returned audio."""
    base = service_url(service)
    response = requests.post(
        base + "/generate", json=payload, timeout=timeout, allow_redirects=False,
    )
    if not response.ok:
        raise RuntimeError(f"{service} local service returned HTTP {response.status_code}.")
    data = _audio_payload(response)
    if len(data) < 100 or len(data) > 250 * 1024 * 1024:
        raise RuntimeError(f"{service} returned an empty or oversized audio response.")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = out_path.with_name(out_path.stem + ".part" + out_path.suffix)
    temporary.write_bytes(data)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(temporary)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    try:
        duration = float(probe.stdout.strip())
    except ValueError as exc:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"{service} returned unreadable audio: {probe.stderr[-300:]}") from exc
    if duration <= 0:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"{service} returned audio with no duration.")
    temporary.replace(out_path)
    return out_path


def normalize_audio(source: Path, destination: Path) -> Path:
    """Converts a local adapter response to the project's standard PCM WAV format."""
    source, destination = Path(source), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem + ".part.wav")
    result = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(source), "-vn", "-ar", "24000", "-ac", "1",
         "-c:a", "pcm_s16le", str(temporary)],
        capture_output=True, text=True, timeout=180, check=False,
    )
    if result.returncode or not temporary.is_file() or temporary.stat().st_size < 100:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Could not normalize local AI audio: {result.stderr[-300:]}")
    temporary.replace(destination)
    return destination
