"""Lightweight retention and pacing checks for the storyboard timeline."""
from __future__ import annotations

from typing import Any, Dict, List, Sequence


def analyze(scenes: Sequence[Any], alignment_words: Sequence[Dict[str, Any]],
            scene_state: Dict[str, Dict[str, Any]], fps: int = 30) -> Dict[str, Any]:
    findings: List[Dict[str, Any]] = []
    starts: List[float] = []
    cursor = 0.0
    for scene in scenes:
        starts.append(cursor)
        cursor += max(0.1, float(getattr(scene, "duration", 0.0)))

    for left, right in zip(alignment_words, alignment_words[1:]):
        gap = float(right.get("start", 0.0)) - float(left.get("end", 0.0))
        if gap >= 0.8:
            findings.append({
                "kind": "dead_air", "start": float(left.get("end", 0.0)),
                "end": float(right.get("start", 0.0)), "severity": min(1.0, gap / 2.0),
                "message": f"{gap:.1f}s gap between spoken words",
                "recommendation": "Trim the gap or add a deliberate visual beat before the next line.",
            })

    hook_cuts = sum(1 for start in starts[1:] if start <= 30.0)
    if cursor >= 15.0 and hook_cuts < 2:
        findings.append({
            "kind": "hook_density", "start": 0.0, "end": min(30.0, cursor),
            "severity": 0.65, "message": f"Only {hook_cuts} scene cut(s) in the first 30 seconds",
            "recommendation": "Add an earlier visual change or a stronger opening shot.",
        })

    for index, scene in enumerate(scenes):
        start = starts[index]
        end = start + max(0.1, float(getattr(scene, "duration", 0.0)))
        text = str(getattr(scene, "narration", "") or "")
        count = len(text.split())
        wpm = count / max(0.1, end - start) * 60.0
        if count >= 5 and wpm < 115.0:
            findings.append({
                "kind": "slow_pacing", "start": start, "end": end,
                "severity": min(1.0, (115.0 - wpm) / 115.0),
                "message": f"Scene {index + 1} narration pace is about {wpm:.0f} words/min",
                "recommendation": "Tighten the narration or add purposeful visual movement.",
            })
        clip = scene_state.get(str(getattr(scene, "id", "")), {}).get("clip")
        if clip:
            findings.extend(_low_motion_findings(clip, start, end, index, fps))

    for index, word in enumerate(alignment_words):
        if index % 4:
            continue
        following = alignment_words[index:index + 4]
        if len(following) < 3:
            continue
        text = " ".join(str(item.get("word", "")) for item in following)
        start = float(following[0].get("start", 0.0))
        end = float(following[-1].get("end", start))
        cps = len(text) / max(0.1, end - start)
        if cps > 22.0:
            findings.append({
                "kind": "caption_speed", "start": start, "end": end,
                "severity": min(1.0, cps / 40.0),
                "message": f"Caption reading speed is {cps:.0f} characters/second",
                "recommendation": "Use fewer words per caption or hold the caption longer.",
            })

    points = []
    buckets = 60
    for bucket in range(buckets):
        at = cursor * bucket / max(1, buckets - 1)
        active = [item["severity"] for item in findings if item["start"] <= at <= item["end"]]
        points.append({"time": round(at, 2), "risk": round(max(active, default=0.0), 3)})
    return {"duration": cursor, "findings": findings, "timeline": points}


def _low_motion_findings(clip: str, scene_start: float, scene_end: float, scene_index: int,
                         fps: int) -> List[Dict[str, Any]]:
    try:
        import cv2
    except ImportError:
        return []
    capture = cv2.VideoCapture(clip)
    if not capture.isOpened():
        return []
    frames: List[Any] = []
    frame_count = max(1, int(capture.get(cv2.CAP_PROP_FRAME_COUNT)))
    stride = max(1, int(fps * 0.5))
    for index in range(0, frame_count, stride):
        capture.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, frame = capture.read()
        if ok:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            frames.append(cv2.resize(gray, (64, 36), interpolation=cv2.INTER_AREA))
    capture.release()
    if len(frames) < 3:
        return []
    differences = [
        float(cv2.absdiff(left, right).mean()) / 255.0
        for left, right in zip(frames, frames[1:])
    ]
    findings = []
    first = None
    for index, difference in enumerate(differences + [1.0]):
        if difference < 0.012:
            if first is None:
                first = index
        elif first is not None:
            if index - first >= 15:
                start = scene_start + first * 0.5
                end = min(scene_end, scene_start + index * 0.5)
                findings.append({
                    "kind": "low_motion", "start": start, "end": end,
                    "severity": 0.55, "message": f"Scene {scene_index + 1} has a low-motion plateau",
                    "recommendation": "Change the shot, add a camera move, or introduce a detail cut.",
                })
            first = None
    return findings
