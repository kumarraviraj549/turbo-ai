"""OpenTimelineIO JSON export with a dependency-free schema-compatible fallback."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


def _time(value: float, rate: float) -> Dict[str, Any]:
    return {"OTIO_SCHEMA": "RationalTime.1", "value": round(float(value) * rate, 6), "rate": rate}


def _range(start: float, duration: float, rate: float) -> Dict[str, Any]:
    return {
        "OTIO_SCHEMA": "TimeRange.1",
        "start_time": _time(start, rate),
        "duration": _time(duration, rate),
    }


def _relative(path: Optional[str | Path], output_dir: Path) -> Optional[str]:
    if not path:
        return None
    resolved = Path(path).resolve()
    return Path(os.path.relpath(resolved, output_dir.resolve())).as_posix()


def _external_clip(name: str, path: Optional[str | Path], start: float, duration: float, rate: float,
                   output_dir: Path, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    target = _relative(path, output_dir)
    if target is None:
        media_reference = {
            "OTIO_SCHEMA": "MissingReference.1",
            "name": name,
            "available_range": None,
            "available_image_bounds": None,
            "metadata": {},
        }
    else:
        media_reference = {
            "OTIO_SCHEMA": "ExternalReference.1",
            "target_url": target,
            "available_range": _range(0.0, duration, rate),
            "available_image_bounds": None,
            "metadata": {},
        }
    return {
        "OTIO_SCHEMA": "Clip.2",
        "name": name,
        "source_range": _range(0.0, duration, rate),
        "effects": [],
        "markers": [],
        "enabled": True,
        "media_references": {"DEFAULT_MEDIA": media_reference},
        "active_media_reference_key": "DEFAULT_MEDIA",
        "metadata": metadata or {},
    }


def _track(name: str, kind: str, children: Optional[List[Dict[str, Any]]] = None,
           markers: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    return {
        "OTIO_SCHEMA": "Track.1",
        "name": name,
        "kind": kind,
        "children": children or [],
        "source_range": None,
        "effects": [],
        "markers": markers or [],
        "enabled": True,
        "metadata": {},
    }


def _marker(name: str, at: float, rate: float, comment: str = "") -> Dict[str, Any]:
    return {
        "OTIO_SCHEMA": "Marker.2",
        "name": name,
        "color": "YELLOW",
        "marked_range": _range(at, 0.0, rate),
        "comment": comment,
        "metadata": {},
    }


def build_timeline(
    *,
    name: str,
    output_dir: Path,
    scenes: Sequence[Dict[str, Any]],
    durations: Sequence[float],
    transitions: Sequence[Sequence[Any]],
    fps: float,
    narration: Optional[str | Path] = None,
    music_sfx: Optional[str | Path] = None,
    overlays: Optional[Sequence[Dict[str, Any]]] = None,
    beat_times: Optional[Iterable[float]] = None,
) -> Dict[str, Any]:
    output_dir = Path(output_dir)
    video_children: List[Dict[str, Any]] = []
    overlay_children: List[Dict[str, Any]] = []
    chapters: List[Dict[str, Any]] = []
    cursor = 0.0
    for index, scene in enumerate(scenes):
        duration = max(0.0, float(durations[index]))
        clip_path = scene.get("clip")
        chapter = str(scene.get("chapter") or "")
        if chapter and (index == 0 or chapter != str(scenes[index - 1].get("chapter") or "")):
            chapters.append(_marker(chapter, cursor, fps, "Chapter"))
        video_children.append(_external_clip(
            str(scene.get("title") or f"Scene {index + 1}"), clip_path,
            0.0, duration, fps, output_dir,
            {"tubeForge": {"scene_id": str(scene.get("id") or ""), "chapter": chapter}},
        ))
        overlay = (overlays or [])[index] if index < len(overlays or []) else {}
        if overlay.get("text"):
            overlay_children.append(_external_clip(
                f"Overlay — {scene.get('title') or index + 1}", None,
                0.0, duration, fps, output_dir,
                {"tubeForge": {"text": str(overlay["text"]),
                               "kind": str(overlay.get("kind") or "Lower Third"),
                               "scene_id": str(scene.get("id") or "")}},
            ))
        cursor += duration
        if index < len(scenes) - 1 and index < len(transitions):
            transition = transitions[index]
            transition_name = str(transition[0] if transition else "Dissolve")
            transition_duration = max(0.0, float(transition[1] if len(transition) > 1 else 0.0))
            if transition_duration:
                half = transition_duration / 2.0
                video_children.append({
                    "OTIO_SCHEMA": "Transition.1",
                    "name": transition_name,
                    "transition_type": "SMPTE_Dissolve",
                    "in_offset": _time(half, fps),
                    "out_offset": _time(half, fps),
                    "metadata": {"tubeForge": {"transition": transition_name}},
                })
                cursor -= transition_duration

    video_markers = chapters + [
        _marker("Beat", float(at), fps, "Detected music beat")
        for at in (beat_times or []) if 0.0 <= float(at) <= cursor
    ]
    audio_children: List[Dict[str, Any]] = []
    if narration:
        audio_children.append(_external_clip("Narration stem", narration, 0.0, cursor, fps, output_dir))
    music_children: List[Dict[str, Any]] = []
    if music_sfx:
        music_children.append(_external_clip("Music + SFX stem", music_sfx, 0.0, cursor, fps, output_dir))
    tracks = [
        _track("Video Track 1", "Video", video_children, video_markers),
        _track("Video Track 2 · Overlays & graphics", "Video", overlay_children),
        _track("Audio Track 1 · Narration", "Audio", audio_children),
        _track("Audio Track 2 · Music + SFX", "Audio", music_children),
    ]
    return {
        "OTIO_SCHEMA": "Timeline.1",
        "name": name,
        "global_start_time": _time(0.0, fps),
        "tracks": {
            "OTIO_SCHEMA": "Stack.1",
            "name": "Tracks",
            "children": tracks,
            "source_range": None,
            "effects": [],
            "markers": [],
            "enabled": True,
            "metadata": {},
        },
        "metadata": {"tubeForge": {"fps": fps}},
    }


def write_timeline(path: Path, **kwargs: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = build_timeline(output_dir=path.parent, **kwargs)
    serialized = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        import opentimelineio as otio
    except ImportError:
        pass
    else:
        otio.adapters.read_from_string(serialized, adapter_name="otio_json")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(serialized + "\n", encoding="utf-8")
    temporary.replace(path)
    return path
