#!/usr/bin/env python3
"""Read-only diagnostics for local media and runtime engines."""

import json
import os
import platform
import shutil
import subprocess
import sys
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
    depth_tier = "tier_3_ffmpeg_2d_zoom"
    if cpu_3d:
        depth_tier = "tier_2_cpu_onnx_depth_parallax"
    if gpu["nvidia_smi"]:
        depth_tier = "tier_1_native_depthflow_or_gpu"
    report = {
        "ffmpeg": check_ffmpeg("ffmpeg"),
        "ffprobe": check_ffmpeg("ffprobe"),
        "node": check_node(),
        "python": check_python(),
        "gpu": gpu,
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
