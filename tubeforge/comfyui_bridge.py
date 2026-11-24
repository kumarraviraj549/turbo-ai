"""Named, user-workflow-backed ComfyUI profiles and scene image/lip-sync actions."""
import copy
import hashlib
import ipaddress
import json
import mimetypes
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import urlencode, urlparse

from PIL import Image

from . import http, video

PRESETS = {
    "wan22_video": {"label": "Wan 2.2 video", "kind": "video"},
    "kandinsky60_video": {"label": "Kandinsky 6.0 video", "kind": "video"},
    "ltx25_video": {"label": "LTX-2.5 video", "kind": "video"},
    "flux_kontext_edit": {"label": "FLUX Kontext surgical edit", "kind": "image_edit"},
    "qwen_image_edit": {"label": "Qwen-Image-Edit surgical edit", "kind": "image_edit"},
    "musetalk_lipsync": {"label": "MuseTalk hero lip-sync", "kind": "lipsync"},
    "latentsync16_lipsync": {"label": "LatentSync 1.6 hero lip-sync", "kind": "lipsync"},
}


def content_key(*parts) -> str:
    digest = hashlib.sha256()
    for part in parts:
        if isinstance(part, (str, Path)) and Path(part).is_file():
            path = Path(part)
            digest.update(path.name.encode("utf-8"))
            with path.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
        else:
            digest.update(str(part).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def validate_endpoint(base_url: str, allow_remote: bool = False) -> str:
    parsed = urlparse(str(base_url or ""))
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("ComfyUI endpoint must be a valid HTTP(S) URL without embedded credentials.")
    hostname = (parsed.hostname or "").lower()
    try:
        is_loopback = hostname == "localhost" or ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        is_loopback = hostname == "localhost"
    if not is_loopback and not allow_remote:
        raise ValueError("Remote ComfyUI requires the explicit Enable remote ComfyUI setting.")
    return base_url.rstrip("/")


def load_profile(preset: str, workflow_path: Path) -> dict:
    if preset not in PRESETS:
        raise ValueError(f"Unknown ComfyUI preset: {preset}")
    return video.import_comfy_workflow(Path(workflow_path))


def prepare_video_workflow(preset: str, workflow_path: Path, prompt: str) -> dict:
    if PRESETS.get(preset, {}).get("kind") != "video":
        raise ValueError("Select a video preset for ComfyUI video generation.")
    return _replace_prompt(load_profile(preset, workflow_path), prompt.strip())


def _replace_prompt(workflow: dict, prompt: str) -> dict:
    updated = copy.deepcopy(workflow)
    candidates = []
    for node_id, node in updated.items():
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict):
            continue
        class_type = str(node.get("class_type", "")).casefold()
        if "text" in inputs and any(marker in class_type for marker in ("textencode", "prompt", "wan", "qwen")):
            if "negative" not in class_type:
                candidates.append((node_id, inputs))
    if not candidates:
        raise ValueError("ComfyUI workflow has no recognizable positive text input to patch.")
    for _, inputs in candidates:
        inputs["text"] = prompt
    return updated


def _upload_input(base_url: str, path: Path) -> str:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"ComfyUI input media is missing: {path}")
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    with path.open("rb") as media_file:
        response = http.request(
            "comfyui", "POST", base_url + "/upload/image",
            files={"image": (path.name, media_file, mime)},
            data={"type": "input", "overwrite": "true"},
            timeout=180, retries=1,
        )
    try:
        name = response.json().get("name")
    except (ValueError, AttributeError) as exc:
        raise RuntimeError("ComfyUI upload did not return a JSON filename.") from exc
    if not isinstance(name, str) or not name:
        raise RuntimeError("ComfyUI upload did not return an input filename.")
    return name


def _patch_media(workflow: dict, media_name: str, media_kind: str) -> dict:
    updated = copy.deepcopy(workflow)
    allowed = {
        "image": ("loadimage", "loadimagemask", "image"),
        "video": ("loadvideo", "videopath", "video"),
        "audio": ("loadaudio", "audiopath", "audio"),
    }[media_kind]
    changed = False
    for node in updated.values():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        class_type = str(node.get("class_type", "")).casefold()
        inputs = node["inputs"]
        keys = [key for key in inputs if any(token in str(key).casefold() for token in allowed[1:])]
        if any(token in class_type for token in allowed[:1]) and not keys:
            keys = ["image" if media_kind == "image" else "video" if media_kind == "video" else "audio"]
        for key in keys:
            if isinstance(inputs.get(key), (str, type(None))):
                inputs[key] = media_name
                changed = True
    if not changed:
        raise ValueError(f"ComfyUI workflow has no recognized {media_kind} loader input.")
    return updated


def _first_output(base_url: str, workflow: dict, media_keys: Tuple[str, ...],
                  timeout: float = 900, should_cancel: Optional[Callable[[], bool]] = None) -> Tuple[dict, dict]:
    submission = video.comfy_submit(base_url, workflow)
    prompt_id = submission["prompt_id"]
    started = time.monotonic()
    history = {}
    while time.monotonic() - started < timeout:
        if should_cancel and should_cancel():
            raise RuntimeError("ComfyUI generation cancelled.")
        history = video.comfy_history(base_url, prompt_id)
        if history:
            status = history.get("status") or {}
            if status.get("status_str") == "error":
                raise RuntimeError("ComfyUI workflow failed.")
            outputs = history.get("outputs") or {}
            candidates = [
                item for node in outputs.values() if isinstance(node, dict)
                for key in media_keys for item in (node.get(key) or [])
                if isinstance(item, dict) and item.get("filename")
            ]
            if candidates:
                return candidates[0], {"prompt_id": prompt_id, "history": history}
        time.sleep(2.0)
    raise TimeoutError(f"ComfyUI {media_keys[0]} generation timed out.")


def _download_output(base_url: str, item: dict) -> bytes:
    query = {"filename": item["filename"]}
    if item.get("subfolder"):
        query["subfolder"] = item["subfolder"]
    if item.get("type"):
        query["type"] = item["type"]
    return http.request(
        "comfyui", "GET", base_url + "/view?" + urlencode(query),
        timeout=180, retries=1,
    ).content


def generate_surgical_edit(base_url: str, preset: str, workflow_path: Path, source_image: Path,
                           prompt: str, out_path: Path, should_cancel=None,
                           allow_remote: bool = False) -> Path:
    if PRESETS.get(preset, {}).get("kind") != "image_edit":
        raise ValueError("Select a FLUX Kontext or Qwen-Image-Edit profile for surgical image edits.")
    out_path = Path(out_path)
    if out_path.is_file() and out_path.stat().st_size:
        return out_path
    base_url = validate_endpoint(base_url, allow_remote)
    workflow = _replace_prompt(load_profile(preset, workflow_path), prompt.strip())
    workflow = _patch_media(workflow, _upload_input(base_url, source_image), "image")
    item, _ = _first_output(base_url, workflow, ("images",), should_cancel=should_cancel)
    data = _download_output(base_url, item)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = out_path.with_name(out_path.stem + ".part" + out_path.suffix)
    temporary.write_bytes(data)
    try:
        with Image.open(temporary) as image:
            image.verify()
    except (OSError, ValueError) as exc:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("ComfyUI surgical edit returned an invalid image.") from exc
    temporary.replace(out_path)
    return out_path


def generate_lipsync(base_url: str, preset: str, workflow_path: Path, source_video: Path,
                     narration: Path, out_path: Path, should_cancel=None,
                     allow_remote: bool = False) -> Path:
    if PRESETS.get(preset, {}).get("kind") != "lipsync":
        raise ValueError("Select the MuseTalk or LatentSync profile for hero lip-sync.")
    out_path = Path(out_path)
    if out_path.is_file() and out_path.stat().st_size and out_path.suffix.lower() == ".mp4":
        return out_path
    base_url = validate_endpoint(base_url, allow_remote)
    workflow = load_profile(preset, workflow_path)
    video_name = _upload_input(base_url, source_video)
    audio_name = _upload_input(base_url, narration)
    workflow = _patch_media(workflow, video_name, "video")
    workflow = _patch_media(workflow, audio_name, "audio")
    item, _ = _first_output(base_url, workflow, ("videos", "gifs"), should_cancel=should_cancel)
    data = _download_output(base_url, item)
    if len(data) < 10_000:
        raise RuntimeError("ComfyUI lip-sync output is empty or incomplete.")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = out_path.with_name(out_path.stem + ".part.mp4")
    source_path = out_path.with_name(out_path.stem + ".source" + Path(str(item["filename"])).suffix)
    source_path.write_bytes(data)
    completed = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(source_path), "-c:v", "libx264",
         "-c:a", "aac", "-movflags", "+faststart", str(temporary)],
        capture_output=True, text=True, timeout=600, check=False,
    )
    source_path.unlink(missing_ok=True)
    if completed.returncode or not temporary.is_file() or temporary.stat().st_size < 10_000:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Could not normalize ComfyUI lip-sync output: {completed.stderr[-300:]}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.unlink(missing_ok=True)
    temporary.replace(out_path)
    return out_path


def preset_labels(kind: Optional[str] = None) -> Dict[str, str]:
    return {key: value["label"] for key, value in PRESETS.items()
            if kind is None or value["kind"] == kind}
