"""Safe template-only Manim CE worker; never evaluates model- or user-supplied Python."""
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict

TEMPLATES = {"compounding_curve", "equation_transform", "balance_sheet_flow"}

_SCENE_SOURCE = r'''
import json
from pathlib import Path
from manim import *

cfg = json.loads(Path(__CONFIG_PATH__).read_text(encoding="utf-8"))

class SafeMathScene(Scene):
    def construct(self):
        template = cfg["template"]
        title = Text(cfg["title"], font_size=38, color=TEAL)
        title.to_edge(UP)
        self.play(FadeIn(title), run_time=0.6)
        if template == "compounding_curve":
            axes = Axes(
                x_range=[0, 10, 2], y_range=[0, cfg["y_max"], cfg["y_step"]],
                x_length=9, y_length=4.5, tips=False,
                axis_config={"include_numbers": True, "font_size": 22},
            ).shift(DOWN * 0.3)
            rate = cfg["annual_rate"]
            principal = cfg["principal"]
            curve = axes.plot(lambda x: min(cfg["y_max"] * 0.95, principal * (1 + rate) ** x),
                              x_range=[0, 10], color=YELLOW, stroke_width=5)
            self.play(Create(axes), run_time=0.6)
            self.play(Create(curve), run_time=max(1.0, cfg["duration"] - 1.8))
            self.wait(0.5)
        elif template == "equation_transform":
            first = Text(cfg["equation"], font_size=48, color=WHITE)
            second = Text(cfg["result"], font_size=48, color=YELLOW)
            self.play(Write(first), run_time=1.0)
            self.wait(0.5)
            self.play(TransformMatchingShapes(first.copy(), second), run_time=max(1.0, cfg["duration"] - 2.0))
            self.wait(0.5)
        elif template == "balance_sheet_flow":
            labels = ("ASSETS", "LIABILITIES", "EQUITY")
            colors = (BLUE, RED, GREEN)
            cards = VGroup()
            for label, color in zip(labels, colors):
                box = RoundedRectangle(width=2.7, height=1.25, corner_radius=0.12,
                                       color=color, fill_color=color, fill_opacity=0.18)
                text = Text(label, font_size=24, color=WHITE)
                cards.add(VGroup(box, text.move_to(box.get_center())))
            cards.arrange(RIGHT, buff=0.45).shift(DOWN * 0.2)
            arrows = VGroup(
                Arrow(cards[0].get_right(), cards[1].get_left(), color=TEAL),
                Arrow(cards[1].get_right(), cards[2].get_left(), color=TEAL),
            )
            self.play(FadeIn(cards), run_time=0.8)
            self.play(Create(arrows), run_time=max(1.0, cfg["duration"] - 2.0))
            self.wait(0.4)
        else:
            raise ValueError("Unsupported safe Manim template.")
'''


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_config(config: Dict) -> Dict:
    if not isinstance(config, dict) or config.get("template") not in TEMPLATES:
        raise ValueError(f"Manim template must be one of {sorted(TEMPLATES)}.")
    normalized = {
        "template": str(config["template"]),
        "title": str(config.get("title", "Math in Motion"))[:100],
        "duration": max(2.0, min(30.0, float(config.get("duration", 8.0)))),
        "width": max(320, min(3840, int(config.get("width", 1920)))),
        "height": max(320, min(2160, int(config.get("height", 1080)))),
        "fps": max(15, min(60, int(config.get("fps", 30)))),
        "principal": max(0.1, min(100000.0, float(config.get("principal", 100.0)))),
        "annual_rate": max(0.0, min(1.0, float(config.get("annual_rate", 0.08)))),
        "y_max": max(10.0, min(1000000.0, float(config.get("y_max", 250.0)))),
        "y_step": max(1.0, min(100000.0, float(config.get("y_step", 50.0)))),
        "equation": str(config.get("equation", "A = B + C"))[:100],
        "result": str(config.get("result", "A - C = B"))[:100],
    }
    return normalized


def run(config: Dict) -> Dict:
    cfg = validate_config(config)
    output = Path(config["output"]).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    signature = hashlib.sha256(
        json.dumps(cfg, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    sidecar = output.with_suffix(".json")
    if output.is_file() and sidecar.is_file():
        try:
            if json.loads(sidecar.read_text(encoding="utf-8")).get("cache_key") == signature:
                return {"ok": True, "cached": True, "output": str(output), "cache_key": signature}
        except (OSError, ValueError):
            pass
    executable = shutil.which("manim")
    if not executable:
        try:
            import manim  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("Manim CE is unavailable; install requirements-pro-optional.txt.") from exc
        executable = sys.executable
        prefix = [executable, "-m", "manim"]
    else:
        prefix = [executable]
    with tempfile.TemporaryDirectory(prefix="tf-manim-", dir=output.parent) as temp:
        work = Path(temp)
        config_path = work / "config.json"
        config_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        source = work / "safe_math_scene.py"
        source.write_text(
            _SCENE_SOURCE.replace("__CONFIG_PATH__", json.dumps(str(config_path))),
            encoding="utf-8",
        )
        command = [
            *prefix, "-ql", "-r", f"{cfg['width']},{cfg['height']}", "--fps", str(cfg["fps"]),
            "--media_dir", str(work / "media"), str(source), "SafeMathScene",
        ]
        result = subprocess.run(command, cwd=work, capture_output=True, text=True,
                                timeout=max(300, cfg["duration"] * 90), check=False)
        candidates = sorted((work / "media").rglob("*.mp4")) if (work / "media").exists() else []
        if result.returncode or not candidates:
            raise RuntimeError(f"Manim render failed: {(result.stderr or result.stdout)[-1200:]}")
        temporary = output.with_name(output.stem + ".part.mp4")
        normalized = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(candidates[0]), "-vf",
             f"scale={cfg['width']}:{cfg['height']}:force_original_aspect_ratio=decrease,"
             f"pad={cfg['width']}:{cfg['height']}:(ow-iw)/2:(oh-ih)/2,fps={cfg['fps']},"
             f"trim=duration={cfg['duration']:.3f},setpts=PTS-STARTPTS",
             "-t", f"{cfg['duration']:.3f}", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
             str(temporary)],
            capture_output=True, text=True, timeout=max(300, cfg["duration"] * 60), check=False,
        )
        if normalized.returncode or not temporary.is_file() or temporary.stat().st_size < 10_000:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"Could not normalize Manim render: {normalized.stderr[-800:]}")
        temporary.replace(output)
    sidecar.write_text(json.dumps({"cache_key": signature, "config": cfg}, indent=2), encoding="utf-8")
    return {"ok": True, "cached": False, "output": str(output), "cache_key": signature}


def main() -> None:
    try:
        result = run(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
        print(json.dumps(result, ensure_ascii=False))
    except Exception as error:  # noqa: BLE001 - surfaced to the render job
        print(json.dumps({"ok": False, "error": f"{type(error).__name__}: {error}"}), flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
