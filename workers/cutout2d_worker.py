#!/usr/bin/env python3
"""Separate a still into foreground/background layers and render a simple 2D cutout animation."""

import hashlib
import io
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import numpy as np
from PIL import Image


PRESETS = {"dolly_in", "dolly_out", "orbital_left", "orbital_right", "circle", "vertical_rise"}
INTENSITIES = {"subtle": 0.65, "medium": 1.0, "cinematic": 1.35}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, value: Dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _ease(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return value * value * (3.0 - 2.0 * value)


def _get_segmenter(model: str) -> Callable[[bytes], bytes]:
    from rembg import new_session, remove

    session = new_session(model)
    return lambda contents: remove(contents, session=session, only_mask=False, post_process_mask=True)


def build_layers(
    image_path: Path,
    foreground_path: Path,
    background_path: Path,
    model: str = "birefnet-general-lite",
    segmenter: Optional[Callable[[bytes], bytes]] = None,
) -> str:
    import cv2

    source = Image.open(image_path).convert("RGB")
    source_array = np.asarray(source)
    if segmenter is None:
        segmenter = _get_segmenter(model)
    encoded = segmenter(image_path.read_bytes())
    foreground = Image.open(io.BytesIO(encoded)).convert("RGBA")
    if foreground.size != source.size:
        foreground = foreground.resize(source.size, Image.Resampling.LANCZOS)
    rgba = np.asarray(foreground)
    alpha = rgba[:, :, 3]
    if int(alpha.max()) <= 8 or int(alpha.min()) >= 247:
        raise RuntimeError("Foreground segmentation returned an empty or full-frame mask")

    mask = np.where(alpha > 12, 255, 0).astype(np.uint8)
    radius = max(3, int(round(min(source.size) * 0.008)))
    radius += 1 - radius % 2
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (radius, radius))
    mask = cv2.dilate(mask, kernel, iterations=1)
    bgr = cv2.cvtColor(source_array, cv2.COLOR_RGB2BGR)
    clean_bgr = cv2.inpaint(bgr, mask, 5.0, cv2.INPAINT_TELEA)
    clean_rgb = cv2.cvtColor(clean_bgr, cv2.COLOR_BGR2RGB)

    foreground_path.parent.mkdir(parents=True, exist_ok=True)
    foreground_tmp = foreground_path.with_name(foreground_path.stem + ".part.png")
    background_tmp = background_path.with_name(background_path.stem + ".part.png")
    Image.fromarray(rgba, mode="RGBA").save(foreground_tmp)
    Image.fromarray(clean_rgb, mode="RGB").save(background_tmp)
    foreground_tmp.replace(foreground_path)
    background_tmp.replace(background_path)
    return model


def _fit(image: np.ndarray, width: int, height: int, overscan: float = 1.0) -> np.ndarray:
    import cv2

    source_h, source_w = image.shape[:2]
    scale = max(width * overscan / source_w, height * overscan / source_h)
    resized = cv2.resize(image, (max(width, round(source_w * scale)), max(height, round(source_h * scale))),
                         interpolation=cv2.INTER_LANCZOS4)
    return resized


def _render_layers(
    foreground_path: Path,
    background_path: Path,
    output: Path,
    config: Dict[str, Any],
) -> None:
    import cv2

    width, height, fps = int(config["width"]), int(config["height"]), int(config["fps"])
    duration = float(config["duration"])
    frames = max(1, round(duration * fps))
    intensity = INTENSITIES[str(config.get("intensity", "medium"))]
    background = cv2.imread(str(background_path), cv2.IMREAD_COLOR)
    foreground = cv2.imread(str(foreground_path), cv2.IMREAD_UNCHANGED)
    if background is None or foreground is None or foreground.ndim != 3 or foreground.shape[2] != 4:
        raise RuntimeError("Could not read the generated cutout layers")

    background = _fit(background, width, height, 1.12)
    foreground_h, foreground_w = foreground.shape[:2]
    scale = min(width * 0.92 / foreground_w, height * 0.92 / foreground_h)
    base_fg = cv2.resize(foreground, (max(1, round(foreground_w * scale)), max(1, round(foreground_h * scale))),
                         interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LANCZOS4)
    pad = np.zeros((height, width, 4), dtype=np.uint8)
    left = (width - base_fg.shape[1]) // 2
    top = (height - base_fg.shape[0]) // 2
    pad[top:top + base_fg.shape[0], left:left + base_fg.shape[1]] = base_fg
    shadow_source = cv2.GaussianBlur(pad[:, :, 3], (0, 0), sigmaX=max(2.0, width * 0.006))

    temporary = output.with_name(output.stem + ".part.mp4")
    temporary.unlink(missing_ok=True)
    command = [
        "ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{width}x{height}", "-r", str(fps), "-i", "-", "-frames:v", str(frames),
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temporary),
    ]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        bg_h, bg_w = background.shape[:2]
        center_x, center_y = (bg_w - width) / 2.0, (bg_h - height) / 2.0
        x_room, y_room = (bg_w - width) / 4.0 * intensity, (bg_h - height) / 4.0 * intensity
        for index in range(frames):
            t = index / max(1, frames - 1)
            eased = _ease(t)
            phase = 2.0 * math.pi * t
            x_drift = -x_room * 0.35 * math.sin(phase)
            y_drift = -y_room * 0.25 * math.sin(math.pi * t)
            if config["preset"] == "orbital_left":
                x_drift = -x_room * 0.55 * math.sin(math.pi * t)
            elif config["preset"] == "orbital_right":
                x_drift = x_room * 0.55 * math.sin(math.pi * t)
            elif config["preset"] == "vertical_rise":
                y_drift = -y_room * 0.6 * eased
            elif config["preset"] == "dolly_in":
                x_drift = y_drift = 0.0
            elif config["preset"] == "dolly_out":
                x_drift = y_drift = 0.0
            matrix = np.float32([[1, 0, -center_x + x_drift], [0, 1, -center_y + y_drift]])
            frame = cv2.warpAffine(background, matrix, (width, height), flags=cv2.INTER_LANCZOS4,
                                   borderMode=cv2.BORDER_REPLICATE)

            breathing = 1.0 + 0.012 * intensity * math.sin(2.0 * phase)
            slide_x = width * 0.02 * intensity * (1.0 - eased) if config["preset"] in ("dolly_in", "circle") else 0.0
            fg_matrix = cv2.getRotationMatrix2D((width / 2, height / 2), 0.0, breathing)
            fg_matrix[0, 2] += slide_x
            animated_fg = cv2.warpAffine(pad, fg_matrix, (width, height), flags=cv2.INTER_LINEAR,
                                         borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
            animated_alpha = animated_fg[:, :, 3].astype(np.float32) / 255.0
            shadow = cv2.warpAffine(shadow_source, fg_matrix, (width, height), flags=cv2.INTER_LINEAR,
                                    borderMode=cv2.BORDER_CONSTANT, borderValue=0)
            shadow_matrix = np.float32([[1, 0, width * 0.008], [0, 1, height * 0.012]])
            shadow = cv2.warpAffine(shadow, shadow_matrix, (width, height), flags=cv2.INTER_LINEAR,
                                    borderMode=cv2.BORDER_CONSTANT, borderValue=0)
            shadow_alpha = (shadow.astype(np.float32) / 255.0 * 0.32)[:, :, None]
            frame = (frame.astype(np.float32) * (1.0 - shadow_alpha)).astype(np.uint8)
            foreground_rgb = animated_fg[:, :, :3].astype(np.float32)
            alpha_3d = animated_alpha[:, :, None]
            frame = (foreground_rgb * alpha_3d + frame.astype(np.float32) * (1.0 - alpha_3d)).clip(0, 255)
            process.stdin.write(frame.astype(np.uint8).tobytes())
        process.stdin.close()
        stderr = process.stderr.read().decode("utf-8", errors="replace")
        return_code = process.wait()
    except Exception:
        process.kill()
        process.wait()
        temporary.unlink(missing_ok=True)
        raise
    if return_code or not temporary.is_file() or temporary.stat().st_size == 0:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"FFmpeg cutout render failed: {stderr[-500:]}")
    temporary.replace(output)


def run(config: Dict[str, Any]) -> Dict[str, Any]:
    image_path = Path(config["image"])
    foreground_path = Path(config["foreground"])
    background_path = Path(config["background"])
    output = Path(config["output"])
    if config["preset"] not in PRESETS:
        raise ValueError(f"Unknown cutout motion preset: {config['preset']}")
    if str(config.get("intensity", "medium")) not in INTENSITIES:
        raise ValueError(f"Unknown cutout intensity: {config.get('intensity')}")
    if not image_path.is_file():
        raise FileNotFoundError(f"Scene image not found: {image_path}")
    if float(config["duration"]) <= 0 or int(config["width"]) < 2 or int(config["height"]) < 2 or int(config["fps"]) < 1:
        raise ValueError("Duration, dimensions, and frame rate must be positive")
    output.parent.mkdir(parents=True, exist_ok=True)
    image_hash = _sha256(image_path)
    requested_model = str(config.get("model", "birefnet-general-lite"))
    model = requested_model
    layer_meta = foreground_path.with_suffix(".json")
    cached_layers = False
    if foreground_path.is_file() and background_path.is_file() and layer_meta.is_file():
        try:
            metadata = json.loads(layer_meta.read_text(encoding="utf-8"))
            cached_layers = (metadata.get("image_hash") == image_hash
                             and metadata.get("requested_model") == requested_model)
            if cached_layers:
                model = str(metadata.get("model") or requested_model)
        except (OSError, json.JSONDecodeError):
            cached_layers = False
    warning = ""
    if not cached_layers:
        try:
            model = build_layers(image_path, foreground_path, background_path, model)
        except Exception as first_error:
            if model == "u2net":
                raise
            warning = f"BiRefNet Lite segmentation failed ({type(first_error).__name__}: {first_error}); retried with u2net."
            model = build_layers(image_path, foreground_path, background_path, "u2net")
        _write_json(layer_meta, {"image_hash": image_hash, "requested_model": requested_model, "model": model})

    clip_key = hashlib.sha256(json.dumps({
        "image": image_hash,
        "model": model,
        "preset": config["preset"],
        "duration": round(float(config["duration"]), 3),
        "resolution": [int(config["width"]), int(config["height"])],
        "fps": int(config["fps"]),
        "intensity": config.get("intensity", "medium"),
    }, sort_keys=True).encode()).hexdigest()
    clip_meta = output.with_suffix(".json")
    if output.is_file() and clip_meta.is_file():
        try:
            if json.loads(clip_meta.read_text(encoding="utf-8")).get("cache_key") == clip_key:
                return {"ok": True, "engine": "cutout_2d", "cached": True, "layers_cached": True}
        except (OSError, json.JSONDecodeError):
            pass
    _render_layers(foreground_path, background_path, output, config)
    _write_json(clip_meta, {"cache_key": clip_key, "engine": "cutout_2d"})
    return {"ok": True, "engine": "cutout_2d", "cached": False, "layers_cached": cached_layers,
            "warning": warning}


def main() -> int:
    try:
        config_path = Path(sys.argv[1])
        config = json.loads(config_path.read_text(encoding="utf-8"))
        print(json.dumps(run(config)))
        return 0
    except Exception as error:
        print(json.dumps({"ok": False, "error": f"{type(error).__name__}: {error}"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
