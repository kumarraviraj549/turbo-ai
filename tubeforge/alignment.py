"""Word-level audio transcription, cache management, and dynamic beat boundaries."""
import hashlib
import json
import math
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

import numpy as np

from . import config
from .http import ProviderError, request

MAX_GROQ_BYTES = 20 * 1024 * 1024
CHUNK_OVERLAP_SECONDS = 1.0
_ALIGN_LOCK = threading.Lock()


class AlignmentError(RuntimeError):
    pass


def _rhubarb_executable() -> Optional[str]:
    found = shutil.which("rhubarb")
    if found:
        return found
    root = Path(__file__).resolve().parents[1] / "tools"
    return next((str(path) for path in (root / "rhubarb.exe", root / "rhubarb")
                 if path.is_file()), None)


def build_mouth_state_timeline(parts: Sequence[tuple[Path, float]], fps: int, cache_path: Path) -> List[dict]:
    """Create frame-based mouth states from voice audio, cached by source content and timing."""
    digest = hashlib.sha256()
    digest.update(f"mouth-v1:{fps}".encode("ascii"))
    for path, offset in parts:
        digest.update(f"{float(offset):.6f}".encode("ascii"))
        digest.update(audio_sha256(Path(path)).encode("ascii"))
    cache_key = digest.hexdigest()
    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("cache_key") == cache_key and isinstance(cached.get("timeline"), list):
                return cached["timeline"]
        except (OSError, ValueError, AttributeError):
            pass

    timeline = []
    rhubarb = _rhubarb_executable()
    for path, offset in parts:
        source = Path(path)
        if not source.is_file():
            raise AlignmentError(f"Voice audio is missing for lip-sync: {source}")
        cues = []
        if rhubarb:
            with tempfile.TemporaryDirectory(prefix="tubeforge-mouth-") as work:
                wav_path = Path(work) / "voice.wav"
                json_path = Path(work) / "mouth.json"
                converted = subprocess.run(
                    ["ffmpeg", "-y", "-v", "error", "-i", str(source), "-ac", "1", "-ar", "16000",
                     "-c:a", "pcm_s16le", str(wav_path)], capture_output=True, text=True, timeout=120,
                )
                if converted.returncode:
                    raise AlignmentError(f"Could not prepare voice audio for Rhubarb: {converted.stderr[-300:]}")
                result = subprocess.run([rhubarb, "-f", "json", "-o", str(json_path), str(wav_path)],
                                        capture_output=True, text=True, timeout=300)
                if result.returncode == 0 and json_path.is_file():
                    payload = json.loads(json_path.read_text(encoding="utf-8"))
                    shape_map = {"X": "closed", "A": "half", "B": "half", "C": "open",
                                 "D": "wide", "E": "wide", "F": "wide"}
                    cues = [{"start": float(item["start"]), "end": float(item["end"]),
                             "mouth": shape_map.get(str(item.get("value", "X")), "closed")}
                            for item in payload.get("mouthCues", [])
                            if float(item.get("end", 0)) > float(item.get("start", 0))]
        if not cues:
            result = subprocess.run(
                ["ffmpeg", "-v", "error", "-i", str(source), "-ac", "1", "-ar", "16000",
                 "-f", "f32le", "-"],
                capture_output=True, timeout=120,
            )
            if result.returncode:
                raise AlignmentError(f"Could not decode voice for lip-sync: {source.name}")
            samples = np.frombuffer(result.stdout, dtype="<f4")
            window = 320
            frames = samples[:len(samples) // window * window].reshape(-1, window)
            if not len(frames):
                continue
            rms = np.sqrt(np.mean(frames * frames, axis=1))
            zcr = np.mean(np.diff(np.signbit(frames).astype(np.int8), axis=1) != 0, axis=1)
            peak = max(float(np.percentile(rms, 95)), 1e-5)
            normalized = rms / peak
            for index, energy in enumerate(normalized):
                if energy < 0.08 or rms[index] < 0.004:
                    mouth = "closed"
                elif energy < 0.28:
                    mouth = "half"
                elif energy < 0.58:
                    mouth = "open"
                else:
                    mouth = "wide" if zcr[index] < 0.24 else "open"
                cues.append({"start": index * 0.02, "end": (index + 1) * 0.02, "mouth": mouth})

        for cue in cues:
            start = max(0, int(round((float(offset) + cue["start"]) * fps)))
            end = max(start, int(round((float(offset) + cue["end"]) * fps)) - 1)
            mouth = cue["mouth"]
            if timeline and timeline[-1]["mouth"] == mouth and start <= timeline[-1]["endFrame"] + 1:
                timeline[-1]["endFrame"] = max(timeline[-1]["endFrame"], end)
            else:
                timeline.append({"startFrame": start, "endFrame": end, "mouth": mouth})

    while len(timeline) > 200:
        shortest = min(range(len(timeline)), key=lambda index:
                       timeline[index]["endFrame"] - timeline[index]["startFrame"])
        neighbor = shortest - 1 if shortest else 1
        start, end = sorted((shortest, neighbor))
        merged = {"startFrame": timeline[start]["startFrame"], "endFrame": timeline[end]["endFrame"],
                  "mouth": timeline[shortest]["mouth"]}
        timeline[start:end + 1] = [merged]
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_suffix(cache_path.suffix + ".tmp")
    temporary.write_text(json.dumps({"cache_key": cache_key, "timeline": timeline}), encoding="utf-8")
    temporary.replace(cache_path)
    return timeline


def audio_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalise_words(words: Sequence[dict]) -> List[dict]:
    result = []
    for item in words:
        try:
            word = str(item.get("word", item.get("text", ""))).strip()
            start, end = float(item["start"]), float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if word and math.isfinite(start) and math.isfinite(end) and end > start >= 0:
            result.append({"word": word, "start": round(start, 4), "end": round(end, 4)})
    return result


def _ffprobe_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise AlignmentError(f"Could not read audio duration for {path.name}") from exc
    if duration <= 0:
        raise AlignmentError(f"Audio has no readable duration: {path.name}")
    return duration


def _groq_one(path: Path, language: Optional[str]) -> List[dict]:
    if not config.groq_key():
        raise AlignmentError("GROQ_API_KEY is not configured")
    data = [
        ("model", "whisper-large-v3-turbo"),
        ("response_format", "verbose_json"),
        ("timestamp_granularities[]", "word"),
    ]
    if language:
        data.append(("language", language))
    response = request(
        "groq",
        "POST",
        "https://api.groq.com/openai/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {config.groq_key()}"},
        data=data,
        files={"file": (path.name, path.read_bytes())},
        timeout=180,
        retries=1,
    )
    try:
        payload = response.json()
    except ValueError as exc:
        raise AlignmentError("Groq transcription returned invalid JSON") from exc
    words = _normalise_words(payload.get("words") or [])
    if not words:
        raise AlignmentError("Groq transcription returned no word timestamps")
    return words


def _groq_transcribe(path: Path, language: Optional[str]) -> List[dict]:
    size = path.stat().st_size
    if size <= MAX_GROQ_BYTES:
        return _groq_one(path, language)

    duration = _ffprobe_duration(path)
    pcm_bytes_per_second = 16_000 * 2
    segment_seconds = max(10.0, (MAX_GROQ_BYTES - 1024 * 1024) / pcm_bytes_per_second)
    segment_seconds = min(segment_seconds, duration)
    result: List[dict] = []
    with tempfile.TemporaryDirectory(prefix="tubeforge-whisper-") as temp_dir:
        start = 0.0
        chunk_index = 0
        while start < duration:
            end = min(duration, start + segment_seconds)
            chunk = Path(temp_dir) / f"chunk_{chunk_index:04d}.wav"
            command = [
                "ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", str(path),
                "-t", f"{end - start:.3f}", "-vn", "-ac", "1", "-ar", "16000",
                "-c:a", "pcm_s16le", str(chunk),
            ]
            proc = subprocess.run(command, capture_output=True, text=True, timeout=300)
            if proc.returncode:
                raise AlignmentError(f"Could not split audio for transcription: {proc.stderr[-300:]}")
            if chunk.stat().st_size > MAX_GROQ_BYTES:
                raise AlignmentError("Transcription chunk exceeds Groq's 20 MB file limit")
            words = _groq_one(chunk, language)
            offset = start
            for word in words:
                if chunk_index and word["start"] < CHUNK_OVERLAP_SECONDS:
                    continue
                result.append({"word": word["word"], "start": round(word["start"] + offset, 4),
                               "end": round(word["end"] + offset, 4)})
            if end >= duration:
                break
            start = end - CHUNK_OVERLAP_SECONDS
            chunk_index += 1
    return result


def _local_transcribe(path: Path, model_size: str = "tiny") -> List[dict]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise AlignmentError(
            "Local transcription needs faster-whisper. Install dependencies from requirements.txt."
        ) from exc
    if model_size not in ("tiny", "base", "small"):
        raise AlignmentError(f"Unsupported local alignment model: {model_size}")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(path), word_timestamps=True, vad_filter=True)
    words = []
    for segment in segments:
        for item in segment.words or []:
            words.append({"word": item.word, "start": item.start, "end": item.end})
    result = _normalise_words(words)
    if not result:
        raise AlignmentError("Local transcription returned no word timestamps")
    return result


def transcribe(path: Path, language: Optional[str] = None,
               log: Optional[Callable[[str], None]] = None, model_size: str = "tiny") -> List[dict]:
    """Transcribes with Groq first and falls back to local faster-whisper on provider failures."""
    path = Path(path)
    if not path.is_file():
        raise AlignmentError(f"Audio file not found: {path}")
    try:
        return _groq_transcribe(path, language)
    except (ProviderError, AlignmentError, OSError, subprocess.SubprocessError) as exc:
        if log:
            log(f"⚠️ Groq word alignment unavailable ({exc}); using local faster-whisper")
        try:
            return _local_transcribe(path, model_size)
        except AlignmentError:
            raise
        except Exception as local_exc:
            raise AlignmentError(f"Local word alignment failed: {local_exc}") from local_exc


def align_project_audio(path: Path, project_dir: Path, language: Optional[str] = None,
                        log: Optional[Callable[[str], None]] = None, force: bool = False,
                        model_size: str = "tiny") -> List[dict]:
    """Writes alignment.json as [{word,start,end}], reused only when the audio SHA-256 matches."""
    path, project_dir = Path(path), Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    digest = audio_sha256(path)
    output = project_dir / "alignment.json"
    hash_file = project_dir / "alignment.sha256"
    with _ALIGN_LOCK:
        if not force and output.is_file() and hash_file.is_file():
            try:
                if hash_file.read_text(encoding="ascii").strip() == digest:
                    cached = json.loads(output.read_text(encoding="utf-8"))
                    if isinstance(cached, list):
                        return _normalise_words(cached)
            except (OSError, ValueError):
                pass
        words = transcribe(path, language, log, model_size)
        tmp = output.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(words, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(output)
        hash_tmp = hash_file.with_suffix(".sha256.tmp")
        hash_tmp.write_text(digest, encoding="ascii")
        hash_tmp.replace(hash_file)
    return words


def dynamic_beats(words: Sequence[dict], hook_seconds: float = 30.0, hook_max: float = 4.5,
                  body_max: float = 7.0, min_seconds: float = 3.0) -> List[dict]:
    """Groups timestamped words into sentence/clause-snapped beats within retention caps."""
    items = _normalise_words(words)
    beats: List[dict] = []
    i = 0
    punctuation = re.compile(r"[.!?;:,।؟]$")
    while i < len(items):
        start = items[i]["start"]
        limit = hook_max if start < hook_seconds else body_max
        j, last_clause = i, None
        while j < len(items) and items[j]["end"] - start <= limit + 1e-6:
            if punctuation.search(items[j]["word"]):
                last_clause = j
            j += 1
        end_index = max(i, j - 1)
        if j < len(items) and last_clause is not None and items[last_clause]["end"] - start >= min_seconds:
            end_index = last_clause
        if j < len(items) and end_index == j - 1:
            remaining_end = items[-1]["end"]
            if remaining_end - items[end_index + 1]["start"] < min_seconds:
                candidate_end = items[-1]["end"]
                if candidate_end - start <= limit + 1e-6:
                    end_index = len(items) - 1
        selected = items[i:end_index + 1]
        beats.append({"start": selected[0]["start"], "end": selected[-1]["end"],
                      "words": selected, "text": " ".join(x["word"] for x in selected)})
        i = end_index + 1
    return beats


def visual_beat_intervals(words: Sequence[dict], duration: float, hook_seconds: float = 30.0,
                          hook_max: float = 4.5, body_max: float = 7.0,
                          min_seconds: float = 3.0) -> List[dict]:
    """Covers the full film with beat intervals, snapping cuts to clauses or word ends."""
    if duration <= 0:
        return []
    items = _normalise_words(words)
    punctuation = re.compile(r"[.!?;:,।؟]$")
    intervals, start = [], 0.0
    while start < duration - 1e-4:
        limit = hook_max if start < hook_seconds else body_max
        cap = min(duration, start + limit)
        candidates = [word for word in items if start + min_seconds <= word["end"] <= cap]
        clauses = [word for word in candidates if punctuation.search(word["word"])]
        if clauses:
            end = clauses[-1]["end"]
        elif candidates:
            end = candidates[-1]["end"]
        else:
            end = cap
        if duration - end < min_seconds and duration - start <= limit:
            end = duration
        if end <= start + 1e-4:
            end = cap
        intervals.append({"start": round(start, 4), "end": round(end, 4),
                          "words": [word for word in items if start <= word["start"] < end]})
        start = end
    return intervals


def persist_distinct_beats(project_data: Dict, visual_beats: Sequence[dict], source_asset_hash: str) -> List[dict]:
    """Persist child beats and enforce a resume-safe visual-difference tuple."""
    preserved = []
    climax_count = 0
    motion_moves = [
        "dolly_in", "orbital_right", "dolly_out", "handheld", "crane_up",
        "close_up", "pan_right", "drone_flyover", "dutch_tilt",
    ]
    for index, beat in enumerate(visual_beats):
        item = dict(beat)
        preset = motion_moves[index % len(motion_moves)]
        previous = preserved[-1]["visualTreatment"]["cameraPreset"] if preserved else ""
        if preset == previous:
            preset = motion_moves[(index + 1) % len(motion_moves)]
        crop = ([0.0, 0.0, 1.0, 1.0] if index == 0 else
                [0.10 + (index % 3) * 0.025, 0.07, 0.80, 0.86])
        treatment = {"motionEngine": "depthflow_3d", "cameraPreset": preset, "cropRect": crop}
        item["childBeat"] = True
        item["beatIndex"] = index
        item["sourceAssetHash"] = source_asset_hash
        item["visualTreatment"] = treatment
        item["duration"] = round(float(item["end"]) - float(item["start"]), 4)
        hook = index < 5 and float(item["start"]) < 30.0
        climax = any(term in item.get("text", "").lower()
                     for term in ("finally", "revealed", "impossible", "turning point", "shocking"))
        item["is_hero_scene"] = hook or (climax and climax_count < 2)
        if climax and not hook and climax_count < 2:
            climax_count += 1
        preserved.append(item)
    project_data["visual_beats"] = preserved
    project_data["beat_mode"] = "zero_extra_quota_smart_variation"
    return preserved


def score_viral_moments(words: Sequence[dict], duration: float, limit: int = 5) -> List[dict]:
    """Rank overlapping 30–55s word-aligned windows using local hook/curiosity signals."""
    items = _normalise_words(words)
    if not items:
        return []
    span = min(55.0, duration)
    if span < 10:
        return []
    candidates = []
    stride = 8.0 if span >= 30 else max(4.0, span / 4.0)
    start = max(0.0, items[0]["start"])
    while start + min(30.0, span) <= min(duration, items[-1]["end"]) + 0.05:
        end = min(start + span, duration, items[-1]["end"])
        selected = [item for item in items if start <= item["start"] < end]
        text = " ".join(item["word"] for item in selected)
        if selected and end - selected[0]["start"] >= min(10.0, span * 0.65):
            lower = text.lower()
            score = 45.0
            score += min(18.0, len(selected) / max(1.0, (end - start)) * 1.8)
            score += min(20.0, sum(1 for term in (
                "secret", "why", "never", "impossible", "revealed", "truth", "but", "because",
                "shocking", "breakthrough", "danger", "mistake",
            ) if term in lower) * 4.0)
            score += min(12.0, sum(1 for item in selected if item["word"].endswith(("?", "!"))) * 2.0)
            title_words = re.findall(r"[A-Za-z0-9']+", text)[:6]
            hook_title = " ".join(title_words[:4]) or "The surprising truth"
            candidates.append({"start": round(selected[0]["start"], 3), "end": round(selected[-1]["end"], 3),
                               "duration": round(selected[-1]["end"] - selected[0]["start"], 3),
                               "title": hook_title, "score": int(max(1, min(100, round(score)))),
                               "text": text})
        start += stride
    candidates.sort(key=lambda item: (-item["score"], item["start"]))
    chosen = []
    for candidate in candidates:
        if all(abs(candidate["start"] - item["start"]) >= 12.0 for item in chosen):
            chosen.append(candidate)
        if len(chosen) >= max(1, min(5, int(limit))):
            break
    return sorted(chosen, key=lambda item: item["start"])
