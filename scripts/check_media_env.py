#!/usr/bin/env python3
"""Read-only diagnostics for local media and runtime engines."""

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Optional, Tuple


def run(command: list[str], timeout: int = 15) -> Tuple[int, str, str]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return 127, "", str(exc)


def executable(name: str) -> Optional[str]:
    return shutil.which(name)


def version_from_output(output: str) -> str:
    return output.splitlines()[0].strip() if output else ""


def numeric_version(raw: str) -> Optional[Tuple[int, ...]]:
    import re

    match = re.search(r"(\d+(?:\.\d+)+)", raw)
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def check_ffmpeg(name: str) -> Dict:
    path = executable(name)
    if not path:
        return {"available": False, "path": "", "version": "", "detail": f"{name} is not on PATH"}
    code, out, err = run([name, "-version"], 10)
    raw = out or err
    version = version_from_output(raw)
    return {
        "available": code == 0,
        "path": path,
        "version": version,
        "detail": "" if code == 0 else err or "command failed",
    }


def smoke_encoder(codec: str) -> Dict:
    ffmpeg = executable("ffmpeg")
    if not ffmpeg:
        return {"available": False, "detail": "ffmpeg is not on PATH"}
    try:
        with tempfile.TemporaryDirectory(prefix="tubeforge-encoder-") as directory:
            output = Path(directory) / "probe.mp4"
            result = subprocess.run(
                [ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:r=1",
                 "-frames:v", "1", "-an", "-c:v", codec, "-f", "mp4", str(output)],
                capture_output=True, text=True, timeout=20, check=False,
            )
            available = result.returncode == 0 and output.is_file() and output.stat().st_size > 0
            return {"available": available, "detail": "" if available else (result.stderr or "smoke encode failed")[-300:]}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "detail": str(exc)}


def check_hardware_encoders() -> Dict:
    names = ("h264_nvenc", "hevc_nvenc", "h264_qsv", "hevc_qsv", "h264_amf", "hevc_amf")
    return {name: smoke_encoder(name) for name in names}


def check_vulkan_tools() -> Dict:
    tools = {}
    bundled = Path(__file__).resolve().parents[1] / "tools" / "vulkan"
    for key, names in (
        ("rife", ("rife-ncnn-vulkan.exe", "rife-ncnn-vulkan")),
        ("realesrgan", ("realesrgan-ncnn-vulkan.exe", "realesrgan-ncnn-vulkan")),
    ):
        path = next((str(bundled / name) for name in names if (bundled / name).is_file()), "")
        tools[key] = path or next((shutil.which(name) or "" for name in names if shutil.which(name)), "")
    return {
        "available": {name: bool(path) for name, path in tools.items()},
        "paths": tools,
        "fallback": "FFmpeg",
    }


def check_node() -> Dict:
    path = executable("node")
    if not path:
        return {"available": False, "path": "", "version": "", "required": "20+"}
    code, out, err = run(["node", "--version"], 10)
    raw = out or err
    parsed = numeric_version(raw)
    compatible = code == 0 and parsed is not None and parsed[0] >= 20
    return {
        "available": compatible,
        "path": path,
        "version": raw,
        "required": "20+",
        "detail": "" if compatible else "Node.js 20 or newer is required",
    }


def check_python() -> Dict:
    candidates = []
    if shutil.which("python"):
        candidates.append(("python", shutil.which("python")))
    candidates.append(("current", sys.executable))

    for label, path in candidates:
        code, out, err = run([str(path), "--version"], 10)
        raw = out or err
        parsed = numeric_version(raw)
        compatible = code == 0 and parsed is not None and parsed[:2] >= (3, 11)
        if compatible:
            return {
                "available": True,
                "path": str(path),
                "version": raw,
                "required": "3.11+",
                "detail": f"via {label}",
            }

    raw = f"{platform.python_version()}"
    parsed = numeric_version(raw)
    compatible = parsed is not None and parsed[:2] >= (3, 11)
    return {
        "available": compatible,
        "path": sys.executable,
        "version": raw,
        "required": "3.11+",
        "detail": "" if compatible else "Python 3.11 or newer is required",
    }


def windows_gpu_status() -> Dict:
    gpu_adapters = []
    if os.name == "nt":
        ps = (
            "Get-CimInstance Win32_VideoController | "
            "Select-Object Name,AdapterCompatibility,DriverVersion,AdapterRAM | "
            "ConvertTo-Json -Compress"
        )
        code, out, err = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], 20)
        if code == 0 and out:
            try:
                parsed = json.loads(out)
                gpu_adapters = parsed if isinstance(parsed, list) else [parsed]
            except json.JSONDecodeError:
                gpu_adapters = []

    nvidia_smi = executable("nvidia-smi")
    cuda_available = False
    cuda_detail = ""
    if nvidia_smi:
        code, out, err = run([nvidia_smi, "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"], 15)
        cuda_available = code == 0
        cuda_detail = out or err

    opengl_dll = shutil.which("opengl32.dll")
    if not opengl_dll and os.name == "nt":
        candidates = [
            Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "opengl32.dll",
            Path(os.environ.get("WINDIR", r"C:\Windows")) / "SysWOW64" / "opengl32.dll",
        ]
        opengl_dll = str(next((p for p in candidates if p.exists()), ""))

    return {
        "available": bool(gpu_adapters),
        "gpu_adapters": gpu_adapters,
        "nvidia_smi": nvidia_smi or "",
        "cuda_available": cuda_available,
        "cuda_detail": cuda_detail,
        "opengl": {
            "available": bool(opengl_dll),
            "path": opengl_dll or "",
            "detail": "OpenGL system library found" if opengl_dll else "OpenGL system library not found",
        },
        "recommended_acceleration": "cuda/nvenc" if cuda_available else ("d3d11/opengl" if gpu_adapters else "cpu"),
    }


def main() -> int:
    try:
        import cv2
        import onnxruntime
        cpu_3d = True
    except ImportError:
        cv2 = None
        onnxruntime = None
        cpu_3d = False
    gpu = windows_gpu_status()
    try:
        import importlib.util
        native_depthflow = importlib.util.find_spec("depthflow") is not None
    except (ImportError, ValueError):
        native_depthflow = False
    depth_tier = ("tier_1_native_depthflow" if native_depthflow else
                  "tier_2_cpu_onnx_depth_parallax" if cpu_3d else "tier_3_ffmpeg_2d_zoom")
    report = {
        "ffmpeg": check_ffmpeg("ffmpeg"),
        "ffprobe": check_ffmpeg("ffprobe"),
        "video_encoders": {
            "hardware_smoke_tests": check_hardware_encoders(),
            "cpu_fallbacks": {"h264": "libx264", "hevc": "libx265"},
        },
        "node": check_node(),
        "python": check_python(),
        "gpu": gpu,
        "vulkan_postprocessing": check_vulkan_tools(),
        "depth_parallax": {
            "active_tier": depth_tier,
            "tiers": [
                "tier_1_native_depthflow",
                "tier_2_cpu_onnx_depth_parallax",
                "tier_3_ffmpeg_2d_zoom",
            ],
            "cpu_onnx_available": cpu_3d,
            "onnxruntime_version": getattr(onnxruntime, "__version__", "") if onnxruntime else "",
            "opencv_version": getattr(cv2, "__version__", "") if cv2 else "",
        },
        "platform": {"system": platform.system(), "release": platform.release(), "machine": platform.machine()},
    }
    print(json.dumps(report, indent=2))
    required = [report["ffmpeg"]["available"], report["ffprobe"]["available"], report["node"]["available"],
                report["python"]["available"]]
    return 0 if all(required) else 1


if __name__ == "__main__":
    raise SystemExit(main())
