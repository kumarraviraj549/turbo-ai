#!/usr/bin/env python3
"""Render one still with DepthFlow, falling back to an overscanned FFmpeg parallax zoom."""

import hashlib
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

from PIL import Image


PRESETS = {"dolly_in", "dolly_out", "orbital_left", "orbital_right", "circle", "vertical_rise"}
INTENSITIES = {"subtle": 0.65, "medium": 1.0, "cinematic": 1.35}
TIER2_MODEL_URL = ("https://huggingface.co/onnx-community/"
                    "depth-anything-v2-small-ONNX/resolve/main/onnx/model.onnx")
TIER2_DATA_URL = ("https://huggingface.co/onnx-community/"
                  "depth-anything-v2-small-ONNX/resolve/main/onnx/model.onnx_data")


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


def _cpu_depth_warp_maps(depth, xx, yy, width: int, height: int, preset: str,
                         t: float, strength: float):
    """Build depth-dependent camera maps used by the CPU 3D parallax renderer."""
    import numpy as np

    oh, ow = xx.shape
    cx, cy = (ow - 1) / 2.0, (oh - 1) / 2.0
    phase = 2.0 * math.pi * t
    zoom = 1.055
    zx = zy = 0.0
    if preset == "dolly_in":
        zoom *= 1.0 + 0.055 * strength * t
    elif preset == "dolly_out":
        zoom = 1.055 + 0.055 * strength * (1.0 - t)
    elif preset == "orbital_left":
        zx = -0.038 * strength * math.sin(math.pi * t)
    elif preset == "orbital_right":
        zx = 0.038 * strength * math.sin(math.pi * t)
    elif preset == "circle":
        zx = 0.038 * strength * math.sin(phase)
        zy = 0.022 * strength * (1.0 - math.cos(phase))
    elif preset == "vertical_rise":
        zy = -0.045 * strength * math.sin(math.pi * t)
    disparity = (depth - 0.5) * (8.0 * strength)
    map_x = cx + (xx - cx) / zoom - disparity * (0.018 * width * strength + zx * ow)
    map_y = cy + (yy - cy) / zoom - disparity * (zy * oh)
    return map_x.astype(np.float32), map_y.astype(np.float32)


def _cache_keys(image_hash: str, config: Dict[str, Any], tier: str = "cpu_onnx") -> tuple[str, str]:
    depth_data = {"image": image_hash, "tier": tier, "estimator": "DepthAnythingV2-small"}
    clip_data = {
        "image": image_hash,
        "tier": tier,
        "preset": config["preset"],
        "duration": round(float(config["duration"]), 3),
        "resolution": [int(config["width"]), int(config["height"])],
        "fps": int(config["fps"]),
        "intensity": config["intensity"],
        "dof": round(float(config["dof"]), 3),
    }
    depth_key = hashlib.sha256(json.dumps(depth_data, sort_keys=True).encode()).hexdigest()
    clip_key = hashlib.sha256(json.dumps(clip_data, sort_keys=True).encode()).hexdigest()
    return depth_key, clip_key


def _depth_map(image_path: Path, depth_path: Path, cache_key: str) -> bool:
    metadata = depth_path.with_suffix(".json")
    if depth_path.is_file() and metadata.is_file():
        try:
            if json.loads(metadata.read_text(encoding="utf-8")).get("cache_key") == cache_key:
                return True
        except (OSError, json.JSONDecodeError):
            pass

    import numpy as np
    from depthflow.estimators.anything import DepthAnythingV2

    with Image.open(image_path) as source:
        rgb = np.asarray(source.convert("RGB"))
    depth = DepthAnythingV2().estimate(rgb)
    depth = np.asarray(depth, dtype=np.float32)
    finite = np.isfinite(depth)
    if not finite.any():
        raise RuntimeError("Depth Anything V2 returned no finite depth values")
    low, high = np.percentile(depth[finite], [2, 98])
    if high <= low:
        raise RuntimeError("Depth Anything V2 returned a flat depth map")
    normalized = np.clip((depth - low) / (high - low), 0.0, 1.0)
    normalized[~finite] = 0.0
    temporary = depth_path.with_name(depth_path.stem + ".part.png")
    Image.fromarray((normalized * 255.0).astype("uint8"), mode="L").save(temporary)
    temporary.replace(depth_path)
    _write_json(metadata, {"cache_key": cache_key})
    return False


def _model_cache() -> Path:
    configured = os.environ.get("TUBEFORGE_DEPTH_CACHE")
    if configured:
        root = Path(configured).expanduser()
    elif os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "TubeForge" / "cache"
    else:
        root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "tubeforge"
    return root / "depth" / "models"


def _download_model_pair() -> Path:
    import urllib.request

    model_dir = _model_cache()
    model_dir.mkdir(parents=True, exist_ok=True)
    model = model_dir / "model.onnx"
    data = model_dir / "model.onnx_data"
    for url, destination in ((TIER2_MODEL_URL, model), (TIER2_DATA_URL, data)):
        if destination.is_file() and destination.stat().st_size > 1024:
            continue
        temporary = destination.with_name(destination.name + ".part")
        request = urllib.request.Request(url, headers={"User-Agent": "TubeForgeStudio/1.0"})
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as target:
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                target.write(block)
        if temporary.stat().st_size <= 1024:
            temporary.unlink(missing_ok=True)
            raise RuntimeError("Depth Anything ONNX download was incomplete")
        temporary.replace(destination)
    return model


def _onnx_depth_map(image_path: Path, depth_path: Path, cache_key: str) -> bool:
    metadata = depth_path.with_suffix(".json")
    if depth_path.is_file() and metadata.is_file():
        try:
            if json.loads(metadata.read_text(encoding="utf-8")).get("cache_key") == cache_key:
                return True
        except (OSError, json.JSONDecodeError):
            pass

    import cv2
    import numpy as np
    import onnxruntime as ort

    model = _download_model_pair()
    session = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])
    input_name = session.get_inputs()[0].name
    rgb = np.asarray(Image.open(image_path).convert("RGB"))
    h, w = rgb.shape[:2]
    max_side = 518
    scale = max_side / max(h, w)
    target_w = max(14, round((w * scale) / 14) * 14)
    target_h = max(14, round((h * scale) / 14) * 14)
    target_w = min(784, target_w)
    target_h = min(784, target_h)
    target_w = max(14, round(target_w / 14) * 14)
    target_h = max(14, round(target_h / 14) * 14)
    resized = cv2.resize(rgb, (target_w, target_h), interpolation=cv2.INTER_LANCZOS4)
    pixels = resized.astype(np.float32) / 255.0
    mean = np.asarray([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.asarray([0.229, 0.224, 0.225], dtype=np.float32)
    pixels = (pixels - mean) / std
    tensor = np.ascontiguousarray(pixels.transpose(2, 0, 1)[None, ...])
    output_name = session.get_outputs()[0].name
    predicted = session.run([output_name], {input_name: tensor})[0]
    depth = np.asarray(predicted).squeeze().astype(np.float32)
    if depth.ndim != 2:
        raise RuntimeError(f"Unexpected Depth Anything output shape: {depth.shape}")
    depth = cv2.resize(depth, (w, h), interpolation=cv2.INTER_CUBIC)
    finite = np.isfinite(depth)
    if not finite.any():
        raise RuntimeError("Depth Anything V2 returned no finite depth values")
    low, high = np.percentile(depth[finite], [2, 98])
    if high <= low:
        raise RuntimeError("Depth Anything V2 returned a flat depth map")
    normalized = np.clip((depth - low) / (high - low), 0.0, 1.0)
    normalized = normalized * 65535.0
    temporary = depth_path.with_name(depth_path.stem + ".part.png")
    cv2.imwrite(str(temporary), normalized.astype(np.uint16))
    temporary.replace(depth_path)
    _write_json(metadata, {"cache_key": cache_key, "tier": "cpu_onnx"})
    return False


def _render_cpu_onnx(image_path: Path, depth_path: Path, output: Path, config: Dict[str, Any]) -> None:
    import cv2
    import numpy as np

    width, height, fps = int(config["width"]), int(config["height"]), int(config["fps"])
    duration = float(config["duration"])
    frames = max(1, round(duration * fps))
    strength = INTENSITIES[config["intensity"]]
    dof = float(config["dof"])
    source = np.asarray(Image.open(image_path).convert("RGB"))
    source = cv2.cvtColor(source, cv2.COLOR_RGB2BGR)
    source = cv2.resize(source, (width, height), interpolation=cv2.INTER_LANCZOS4)
    depth_u16 = cv2.imread(str(depth_path), cv2.IMREAD_UNCHANGED)
    if depth_u16 is None:
        raise RuntimeError("Generated depth map could not be read")
    depth = cv2.resize(depth_u16.astype(np.float32), (width, height), interpolation=cv2.INTER_CUBIC)
    depth = (depth - depth.min()) / (depth.max() - depth.min() + 1e-6)

    overscan = 1.06
    ow, oh = round(width * overscan), round(height * overscan)
    yy, xx = np.mgrid[0:oh, 0:ow].astype(np.float32)
    cx, cy = (ow - 1) / 2.0, (oh - 1) / 2.0

    temporary = output.with_name(output.stem + ".part.mp4")
    temporary.unlink(missing_ok=True)
    ffmpeg = subprocess.Popen(
        ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
         "-s", f"{width}x{height}", "-r", str(fps), "-i", "-",
         "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p", "-r", str(fps), "-movflags", "+faststart", str(temporary)],
        stdin=subprocess.PIPE,
    )
    try:
        for frame in range(frames):
            pad = cv2.resize(source, (ow, oh), interpolation=cv2.INTER_LANCZOS4)
            pad_depth = cv2.resize(depth, (ow, oh), interpolation=cv2.INTER_CUBIC)
            t = _ease(frame / max(1, frames - 1))
            map_x, map_y = _cpu_depth_warp_maps(
                pad_depth, xx, yy, width, height, config["preset"], t, strength,
            )
            warped = cv2.remap(pad, map_x, map_y, interpolation=cv2.INTER_LANCZOS4,
                               borderMode=cv2.BORDER_REPLICATE)

            if dof > 0:
                blur = cv2.GaussianBlur(warped, (0, 0), 1.2 + dof * 2.2)
                mask = np.clip((0.38 - pad_depth) / 0.38, 0.0, 1.0)
                mask = cv2.GaussianBlur(mask, (11, 11), 0)
                warped = (warped * (1.0 - mask[..., None]) + blur * mask[..., None]).astype(np.uint8)

            left = max(0, (ow - width) // 2)
            top = max(0, (oh - height) // 2)
            cropped = warped[top:top + height, left:left + width]
            if cropped.shape[:2] != (height, width):
                cropped = cv2.resize(cropped, (width, height), interpolation=cv2.INTER_LANCZOS4)
            ffmpeg.stdin.write(cropped.astype(np.uint8).tobytes())
    finally:
        if ffmpeg.stdin:
            ffmpeg.stdin.close()
        ffmpeg.wait(timeout=max(120, int(duration * 45)))
    if ffmpeg.returncode or not temporary.is_file() or temporary.stat().st_size == 0:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("CPU ONNX 3D parallax render failed")
    temporary.replace(output)


def _render_depthflow(image_path: Path, depth_path: Path, output: Path, config: Dict[str, Any]) -> None:
    from attrs import define
    from depthflow.scene import DepthScene

    preset = config["preset"]
    strength = INTENSITIES[config["intensity"]]
    dof = float(config["dof"])

    @define
    class MotionScene(DepthScene):
        def update(self) -> None:
            t = _ease(float(self.tau))
            amplitude = 0.13 * strength
            self.state.steady = 0.22
            self.state.focus = 0.35
            self.state.zoom = 1.02
            self.state.sticky = True
            self.state.inpaint.limit = 0.35
            self.state.lens.intensity = min(0.45, dof * 0.45)
            self.state.lens.decay = 0.55
            self.state.lens.quality = 12

            if preset == "dolly_in":
                self.state.height = 0.14 + 0.18 * strength * t
            elif preset == "dolly_out":
                self.state.height = 0.14 + 0.18 * strength * (1.0 - t)
            elif preset == "orbital_left":
                self.state.height = 0.20 * strength
                self.state.offset = (-amplitude * math.sin(math.pi * t), 0.0)
            elif preset == "orbital_right":
                self.state.height = 0.20 * strength
                self.state.offset = (amplitude * math.sin(math.pi * t), 0.0)
            elif preset == "circle":
                self.state.height = 0.20 * strength
                self.state.offset = (amplitude * math.sin(2.0 * math.pi * t),
                                     amplitude * (1.0 - math.cos(2.0 * math.pi * t)) * 0.5)
            else:
                self.state.height = 0.20 * strength
                self.state.offset = (0.0, -amplitude * t)

    scene = MotionScene(backend="headless")
    scene.input(image=str(image_path), depth=str(depth_path))
    scene.main(
        output=str(output),
        time=float(config["duration"]),
        width=int(config["width"]),
        height=int(config["height"]),
        fps=int(config["fps"]),
        quality=100,
        ssaa=1.0,
    )
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError("DepthFlow completed without creating a video")


def _render_ffmpeg_fallback(image_path: Path, output: Path, config: Dict[str, Any]) -> None:
    width, height, fps = int(config["width"]), int(config["height"]), int(config["fps"])
    duration = float(config["duration"])
    frames = max(1, round(duration * fps))
    intensity = INTENSITIES[config["intensity"]]
    zoom_amount = 0.055 * intensity
    if config["preset"] == "dolly_out":
        zoom = f"1+{zoom_amount:.5f}*(1-(0.5-0.5*cos(PI*on/{max(1, frames - 1)})))"
    else:
        zoom = f"1+{zoom_amount:.5f}*(0.5-0.5*cos(PI*on/{max(1, frames - 1)}))"
    phase = f"(2*PI*on/{max(1, frames - 1)})"
    x_motion = "0"
    y_motion = "0"
    if config["preset"] in ("orbital_left", "orbital_right", "circle"):
        direction = -1 if config["preset"] == "orbital_left" else 1
        x_motion = f"(iw-iw/zoom)*0.12*{direction}*sin({phase})"
    if config["preset"] == "vertical_rise":
        y_motion = f"(ih-ih/zoom)*0.12*sin(PI*on/{max(1, frames - 1)})"
    elif config["preset"] == "circle":
        y_motion = f"(ih-ih/zoom)*0.08*(1-cos({phase}))"
    x_expr = f"iw/2-(iw/zoom/2)+{x_motion}"
    y_expr = f"ih/2-(ih/zoom/2)+{y_motion}"
    padded_width = max(width, (width * 114 // 100) // 2 * 2)
    padded_height = max(height, (height * 114 // 100) // 2 * 2)
    filter_graph = (
        f"scale={padded_width}:{padded_height}:force_original_aspect_ratio=increase,"
        f"crop={padded_width}:{padded_height},"
        f"zoompan=z='{zoom}':x='{x_expr}':y='{y_expr}':d=1:s={width}x{height}:fps={fps},"
        f"trim=duration={duration:.3f},setpts=PTS-STARTPTS,format=yuv420p"
    )
    temporary = output.with_name(output.stem + ".part.mp4")
    temporary.unlink(missing_ok=True)
    result = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-loop", "1", "-framerate", str(fps), "-i", str(image_path),
         "-vf", filter_graph, "-frames:v", str(frames), "-an", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temporary)],
        capture_output=True,
        text=True,
        timeout=max(120, int(duration * 45)),
        check=False,
    )
    if result.returncode or not temporary.is_file() or temporary.stat().st_size == 0:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"FFmpeg parallax fallback failed: {(result.stderr or result.stdout)[-500:]}")
    temporary.replace(output)


def run(config: Dict[str, Any]) -> Dict[str, Any]:
    image_path = Path(config["image"])
    depth_path = Path(config["depth"])
    output = Path(config["output"])
    if config["preset"] not in PRESETS:
        raise ValueError(f"Unknown DepthFlow preset: {config['preset']}")
    if config["intensity"] not in INTENSITIES:
        raise ValueError(f"Unknown depth intensity: {config['intensity']}")
    if not image_path.is_file():
        raise FileNotFoundError(f"Scene image not found: {image_path}")
    if float(config["duration"]) <= 0 or int(config["width"]) < 2 or int(config["height"]) < 2 or int(config["fps"]) < 1:
        raise ValueError("Duration, dimensions, and frame rate must be positive")
    output.parent.mkdir(parents=True, exist_ok=True)
    depth_path.parent.mkdir(parents=True, exist_ok=True)
    image_hash = _sha256(image_path)
    depth_key, clip_key = _cache_keys(image_hash, config)
    clip_meta = output.with_suffix(".json")
    depth_meta = depth_path.with_suffix(".json")
    depth_cached = False
    if depth_path.is_file() and depth_meta.is_file():
        try:
            depth_cached = json.loads(depth_meta.read_text(encoding="utf-8")).get("cache_key") == depth_key
        except (OSError, json.JSONDecodeError):
            depth_cached = False
    if output.is_file() and clip_meta.is_file():
        try:
            cached_metadata = json.loads(clip_meta.read_text(encoding="utf-8"))
            if cached_metadata.get("cache_key") == clip_key:
                cached_engine = cached_metadata.get("engine", "depthflow_3d")
                warning = (cached_metadata.get("warning", "") if cached_engine != "depthflow_3d" else "")
                return {"ok": True, "engine": cached_engine, "cached": True, "depth_cached": depth_cached,
                        "warning": warning}
        except (OSError, json.JSONDecodeError):
            pass

    depth_cached = False
    fallback_reason = ""
    temporary = output.with_name(output.stem + ".part.mp4")
    temporary.unlink(missing_ok=True)
    try:
        depth_cached = _depth_map(image_path, depth_path, depth_key)
        _render_depthflow(image_path, depth_path, temporary, config)
        temporary.replace(output)
        engine = "depthflow_3d"
    except Exception as error:
        temporary.unlink(missing_ok=True)
        tier1_reason = f"Tier 1 DepthFlow unavailable ({type(error).__name__}: {error})"
        try:
            depth_cached = _onnx_depth_map(image_path, depth_path, depth_key)
            _render_cpu_onnx(image_path, depth_path, temporary, config)
            temporary.replace(output)
            engine = "cpu_onnx_depth_parallax"
            fallback_reason = tier1_reason
        except Exception as cpu_error:
            temporary.unlink(missing_ok=True)
            fallback_reason = (
                f"{tier1_reason}; Tier 2 CPU ONNX unavailable "
                f"({type(cpu_error).__name__}: {cpu_error})"
            )
            _render_ffmpeg_fallback(image_path, temporary, config)
            temporary.replace(output)
            engine = "ffmpeg_parallax_fallback"
    warning = ""
    if fallback_reason:
        if engine == "cpu_onnx_depth_parallax":
            warning = fallback_reason + "; using CPU ONNX depth-disparity parallax."
        elif engine == "ffmpeg_parallax_fallback":
            warning = fallback_reason + "; using smooth-eased FFmpeg parallax zoom."
    _write_json(clip_meta, {"cache_key": clip_key, "engine": engine, "warning": warning})
    return {"ok": True, "engine": engine, "cached": False, "depth_cached": depth_cached,
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
