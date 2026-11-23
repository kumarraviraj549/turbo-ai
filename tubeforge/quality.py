"""Local, provider-free checks for scene-image resolution, blur, duplicates, and character references."""
from pathlib import Path
from typing import Dict, List, Tuple

from PIL import Image, ImageFilter, ImageStat
import cv2
import numpy as np
import subprocess
from pathlib import Path


def _dhash(path: Path) -> int:
    image = Image.open(path).convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixels = list(image.tobytes())
    value = 0
    for y in range(8):
        for x in range(8):
            value = (value << 1) | int(pixels[y * 9 + x] > pixels[y * 9 + x + 1])
    return value


def inspect_project(plan, scene_states: Dict, character_refs: Dict, target_size: Tuple[int, int]) -> Dict:
    """Returns actionable warnings without uploading images or blocking the render."""
    issues: List[str] = []
    checked = []
    hashes = []
    target_w, target_h = target_size
    for index, scene in enumerate(plan.scenes, 1):
        state = scene_states.get(scene.id) or {}
        raw_path = state.get("locked_image") or state.get("image")
        if not raw_path or not Path(raw_path).is_file():
            continue
        path = Path(raw_path)
        try:
            with Image.open(path) as source:
                width, height = source.size
                gray = source.convert("L").resize((256, 144), Image.Resampling.LANCZOS)
                edge_spread = ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).stddev[0]
            digest = _dhash(path)
        except (OSError, ValueError):
            issues.append(f"Scene {index} ({scene.title}): image could not be decoded.")
            continue
        scene_issues = []
        if width < target_w * 0.75 or height < target_h * 0.75:
            scene_issues.append(f"source is {width}x{height}, below the target's 75% threshold")
        if edge_spread < 5.0:
            scene_issues.append("image has very little edge detail; check for blur or a blank frame")
        for other_index, other_title, other_hash in hashes:
            if (digest ^ other_hash).bit_count() <= 4:
                scene_issues.append(f"looks nearly identical to Scene {other_index} ({other_title})")
                break
        hashes.append((index, scene.title or f"Scene {index}", digest))
        checked.append({"scene": index, "title": scene.title, "width": width, "height": height,
                        "edge_detail": round(edge_spread, 1), "issues": scene_issues})
        issues.extend(f"Scene {index} ({scene.title}): {message}." for message in scene_issues)

    appearances = {}
    for scene in plan.scenes:
        for name in scene.characters:
            appearances[name.casefold()] = appearances.get(name.casefold(), 0) + 1
    refs = {str(name).casefold(): path for name, path in character_refs.items() if path and Path(path).is_file()}
    for scene in plan.scenes:
        for name in scene.characters:
            key = name.casefold()
            if appearances.get(key, 0) > 1 and key not in refs:
                issues.append(f"Character {name} appears in multiple scenes without a reference image; "
                              "visual consistency may drift.")
                appearances[key] = 0

    expected_stills = sum(scene.source in ("original_motion", "ai_image") for scene in plan.scenes)
    if len(checked) < expected_stills:
        issues.append(f"Only {len(checked)} of {expected_stills} generated scene stills were available to inspect.")
    if not checked:
        issues.append("No scene stills were available to inspect.")
    return {"status": "review" if issues or not checked else "passed", "checked_scenes": len(checked),
            "scenes": checked, "issues": issues}


def inspect_render(video_path: Path) -> dict:
    """Concrete local QA: decodeability, average black-frame ratio, and per-frame clipping."""
    video_path = Path(video_path)
    if not video_path.is_file():
        return {"status": "fail", "issues": ["Rendered video file is missing."], "checks": {}}
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        return {"status": "fail", "issues": ["Rendered video could not be decoded."], "checks": {}}
    total = 0
    black = 0
    clipped = 0
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        total += 1
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        mean = float(gray.mean())
        if mean < 2.0:
            black += 1
        clipped += int(float(np.mean((gray <= 1) | (gray >= 254))) > 0.02)
    capture.release()
    black_ratio = black / max(1, total)
    issues = []
    if total < 1:
        issues.append("Rendered video contains no decodable frames.")
    if black_ratio > 0.02:
        issues.append(f"{black_ratio:.1%} of frames are nearly black.")
    if clipped:
        issues.append(f"{clipped} frames contain notable shadow/highlight clipping.")
    return {"status": "review" if issues else "passed", "issues": issues,
            "checks": {"frames": total, "black_ratio": round(black_ratio, 4), "clipped_frames": clipped}}


def inspect_subtitle_drift(alignment_words: list, subtitle_chunks: list, duration: float) -> dict:
    """Check that generated subtitle boundaries stay inside aligned media timing."""
    issues = []
    if not alignment_words:
        return {"status": "review", "issues": ["No word alignment available for subtitle drift check."],
                "checks": {}}
    drift = 0.0
    for chunk in subtitle_chunks:
        start = float(getattr(chunk, "start", 0.0))
        end = float(getattr(chunk, "end", 0.0))
        drift = max(drift, max(0.0, -start), max(0.0, end - duration))
    if drift > 0.25:
        issues.append(f"Subtitle timing exceeds media bounds by {drift:.2f}s.")
    return {"status": "review" if issues else "passed", "issues": issues,
            "checks": {"max_boundary_drift": round(drift, 4)}}
