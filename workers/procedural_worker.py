"""Scene-at-a-time Remotion rendering with strict props validation and resume caching."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "remotion-engine"
ENTRY = "src/index.tsx"
COMPOSITION = "ProceduralScene"


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run_cli(arguments, timeout: float) -> None:
    node = shutil.which("node")
    remotion_cli = ENGINE / "node_modules" / "@remotion" / "cli" / "remotion-cli.js"
    if not node:
        raise RuntimeError("Node.js 20+ is required for the procedural scene engine.")
    version = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=5, check=True)
    match = re.match(r"v?(\d+)", version.stdout.strip())
    if not match or int(match.group(1)) < 20:
        raise RuntimeError(f"Node.js 20+ is required for the procedural scene engine; found {version.stdout.strip()!r}.")
    if not remotion_cli.is_file():
        raise RuntimeError("Remotion dependencies are not installed; run npm install in remotion-engine.")
    def execute(gl_renderer: str) -> tuple[int, str]:
        gl_arguments = [*arguments, "--gl", gl_renderer]
        process = subprocess.Popen(
            [node, str(remotion_cli), *gl_arguments], cwd=str(ENGINE), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        )
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                output, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                output, _ = process.communicate()
            raise RuntimeError(f"Remotion exceeded the {timeout:.0f}s timeout: {output[-1800:]}")
        return process.returncode, output or ""

    returncode, output = execute("angle")
    if returncode:
        software_returncode, software_output = execute("swangle")
        if software_returncode:
            raise RuntimeError(
                f"Remotion exited with code {software_returncode}: {(software_output or output)[-2400:]}"
            )


def run(config: Dict[str, Any]) -> Dict[str, Any]:
    mode = str(config.get("mode", "render"))
    if mode not in ("render", "still"):
        raise ValueError(f"Unsupported procedural worker mode: {mode}")
    props = config.get("props")
    if not isinstance(props, dict):
        raise ValueError("Procedural props must be a JSON object.")

    scene_dir = Path(config["scene_dir"])
    scene_dir.mkdir(parents=True, exist_ok=True)
    scene_number = max(1, int(config.get("scene_index", 1)))
    props_path = scene_dir / f"scene_{scene_number:02d}_props.json"
    _write_json(props_path, props)
    public_dir = str(config.get("public_dir", "")).strip()
    public_args = ["--public-dir", public_dir] if public_dir and Path(public_dir).is_dir() else []

    if mode == "still":
        output = Path(config["output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        frame = max(0, int(config.get("frame", 0)))
        _run_cli(["still", ENTRY, COMPOSITION, str(output), "--props", str(props_path), *public_args,
                  "--frame", str(frame), "--log", "error"], float(config.get("timeout", 600)))
        if not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError("Remotion completed without creating a preview image.")
        return {"ok": True, "cached": False, "output": str(output), "props": str(props_path)}

    output = Path(config["output"])
    if output.name != f"scene_{scene_number:02d}_proc.mp4":
        raise ValueError("Procedural scene output must use the scene_XX_proc.mp4 cache filename.")
    image = Path(config["image"])
    if not image.is_file():
        raise FileNotFoundError(f"Procedural source image is missing: {image}")
    signature = {
        "image_sha256": _sha256_file(image),
        "props": props,
        "duration": float(config["duration"]),
        "width": int(config["width"]),
        "height": int(config["height"]),
        "fps": int(config["fps"]),
    }
    key = hashlib.sha256(json.dumps(signature, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    sidecar = output.with_suffix(".json")
    if output.is_file() and output.stat().st_size and sidecar.is_file():
        try:
            cached = json.loads(sidecar.read_text(encoding="utf-8"))
            if cached.get("cache_key") == key:
                return {"ok": True, "cached": True, "output": str(output), "cache_key": key}
        except (OSError, json.JSONDecodeError):
            pass

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f"{output.stem}.{os.getpid()}.{time.monotonic_ns()}.rendering.mp4")
    temporary.unlink(missing_ok=True)
    timeout = float(config.get("timeout", max(1800, float(config["duration"]) * 120)))
    _run_cli(
        ["render", ENTRY, COMPOSITION, str(temporary), "--props", str(props_path),
         *public_args, "--log", "error", "--concurrency", "2"],
        timeout,
    )
    if not temporary.is_file() or temporary.stat().st_size == 0:
        raise RuntimeError("Remotion completed without creating the scene video.")
    temporary.replace(output)
    _write_json(sidecar, {"cache_key": key, **signature})
    return {"ok": True, "cached": False, "output": str(output), "cache_key": key}


def main() -> None:
    try:
        config_path = Path(sys.argv[1])
        result = run(json.loads(config_path.read_text(encoding="utf-8")))
        print(json.dumps(result, ensure_ascii=False))
    except Exception as error:  # noqa: BLE001 - surfaced to the renderer through stdout and exit status
        print(json.dumps({"ok": False, "error": f"{type(error).__name__}: {error}"}), flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
