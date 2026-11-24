"""Per-scene translation, voice synthesis, timing, and dubbed export packs."""
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import audio, cache, jobs, llm, render, tts

LANGUAGES = {
    "English": "en",
    "Hindi": "hi",
    "Spanish": "es",
    "Portuguese": "pt",
    "Tamil": "ta",
    "Telugu": "te",
}
LEAD_IN_SECONDS = 0.35
TAIL_SECONDS = 0.45


def _scene_text(scene) -> str:
    parts = [scene.narration.strip()]
    parts.extend(f"{line.speaker}: {line.line}".strip(": ") for line in scene.dialogue if line.line.strip())
    return "\n".join(part for part in parts if part)


def _word_count(text: str) -> int:
    return len(text.split())


def _target_words(source: str, speech_seconds: float, available_seconds: float) -> int:
    source_words = _word_count(source)
    if source_words <= 0:
        return 0
    ratio = available_seconds / speech_seconds if speech_seconds > 0 else 1.0
    return max(1, round(source_words * max(0.5, min(1.5, ratio))))


def translate_scene(text: str, language: str, target_words: int, log=None) -> str:
    if language not in LANGUAGES:
        raise ValueError(f"Unsupported dub language: {language}")
    if language == "English":
        return text.strip()

    def validate(value):
        if not isinstance(value, dict) or not isinstance(value.get("translation"), str):
            raise ValueError("Translation response must include a translation string.")
        translated = value["translation"].strip()
        if not translated:
            raise ValueError("Translation response was empty.")
        return {"translation": translated}

    data, provider = llm.complete_json(
        "Translate narration for a documentary voice-over. Preserve names, numbers, meaning, and spoken tone. "
        "Return only the requested JSON.",
        json.dumps({
            "target_language": language,
            "target_word_count": target_words,
            "instruction": "Keep close to the target word count so the dub fits the source scene.",
            "text": text,
        }, ensure_ascii=False),
        task="fast", max_tokens=1600, temperature=0.25, validate=validate, log=log,
    )
    if log:
        log(f"🌐 Scene translated to {language} via {provider}.")
    return data["translation"]


def _fit_audio(source: Path, destination: Path, target_duration: float) -> Tuple[Path, float]:
    if target_duration <= 0:
        raise ValueError("Dub scene duration must be positive.")
    measured = tts.probe_duration(source)
    if measured <= 0:
        raise RuntimeError(f"Could not read synthesized dub duration: {source}")
    tempo = max(0.90, min(1.12, measured / target_duration))
    after_tempo = measured / tempo
    if after_tempo > target_duration + 0.08:
        raise RuntimeError(
            f"Dub is {after_tempo:.1f}s for a {target_duration:.1f}s scene even at 1.12× speed. "
            "Shorten the translation or lengthen the scene."
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem + ".part.wav")
    filters = []
    if abs(tempo - 1.0) > 0.005:
        filters.append(f"atempo={tempo:.4f}")
    filters.extend((f"apad=whole_dur={target_duration:.3f}", f"atrim=duration={target_duration:.3f}"))
    result = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(source), "-af", ",".join(filters),
         "-ar", str(audio.SR), "-ac", "2", "-c:a", "pcm_s16le", str(temporary)],
        capture_output=True, text=True, timeout=180, check=False,
    )
    if result.returncode or not temporary.is_file() or temporary.stat().st_size < 100:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Could not fit dub audio to its scene: {result.stderr[-300:]}")
    temporary.replace(destination)
    return destination, tempo


def _voice_engine(language: str, requested: str) -> Tuple[str, str]:
    if requested != "auto":
        engine = requested
    elif language == "English":
        engine = "kokoro"
    elif language == "Hindi":
        engine = "sarvam"
    else:
        engine = "qwen3_tts"
    if engine == "kokoro" and language != "English":
        raise ValueError("Kokoro dub output is English-only; choose an installed local multilingual TTS bridge.")
    if engine == "sarvam" and language not in ("Hindi", "English"):
        raise ValueError("Sarvam dub voices are currently configured for Hindi and English only.")
    if engine == "edge":
        return engine, "en-US-AndrewMultilingualNeural"
    voices = tts.voice_options(language, engine)
    if not voices:
        raise RuntimeError(f"No {engine} voice is configured for {language}.")
    return engine, voices[0][0]


def _voice_synthesis_options(project, engine: str) -> dict:
    if engine != "chatterbox":
        return {}
    reference = project.settings.get("local_voice_reference_path")
    return {
        "voice_reference_path": Path(reference) if reference else None,
        "voice_clone_consent": bool(project.settings.get("local_voice_clone_consent", False)),
    }


def _mix_dub(voice_path: Path, music_path: Path, output: Path, duration: float) -> Path:
    voice = audio.decode(voice_path)
    music = audio.decode(music_path)
    length = max(1, round(duration * audio.SR))
    voice_track = np.zeros((length, 2), dtype=np.float32)
    music_track = np.zeros((length, 2), dtype=np.float32)
    voice_track[:min(length, len(voice))] = voice[:length]
    music_track[:min(length, len(music))] = music[:length]
    music_track = audio.apply_speech_ducking(music_track, voice_track, target_db=-24.0)
    mixed = voice_track + music_track
    output.parent.mkdir(parents=True, exist_ok=True)
    premix = output.with_name(output.stem + ".premix.wav")
    temporary = output.with_name(output.stem + ".part.wav")
    audio._write_pcm_wav(premix, mixed)
    result = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(premix),
         "-af", f"loudnorm=I=-14:TP=-1.5:LRA=11,alimiter=limit=0.84:level=false,atrim=0:{duration:.3f}",
         "-ar", str(audio.SR), "-ac", "2", str(temporary)],
        capture_output=True, text=True, timeout=300, check=False,
    )
    premix.unlink(missing_ok=True)
    if result.returncode or not temporary.is_file() or temporary.stat().st_size < 100:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Could not master dubbed audio: {result.stderr[-300:]}")
    temporary.replace(output)
    return output


def generate_pack(project, language: str, engine: str = "auto", voice: str = "",
                  allow_edge_draft: bool = False, log=None, should_cancel=None) -> Dict[str, str]:
    if language not in LANGUAGES:
        raise ValueError(f"Unsupported dub language: {language}")
    plan = project.plan
    if not plan or not plan.scenes:
        raise ValueError("A saved storyboard is required before generating a dub pack.")
    outputs = project.data.get("outputs") or {}
    music_path = Path(str(outputs.get("music_sfx_stem") or (project.dir / "final" / "music_sfx_stem.wav")))
    if not music_path.is_file():
        raise FileNotFoundError(f"Cached music/SFX stem is missing: {music_path}")
    selected_engine, selected_voice = _voice_engine(language, engine)
    if voice:
        selected_voice = voice
    if selected_engine in ("sarvam", "groq_orpheus") and selected_voice not in {
        item[0] for item in tts.voice_options(language, selected_engine)
    }:
        raise ValueError(f"Voice {selected_voice!r} is not available for {language} with {selected_engine}.")
    if selected_engine == "edge" and not allow_edge_draft:
        raise ValueError("Edge dubbing is draft-only and must be explicitly enabled.")

    scene_durations = [
        max(0.1, float(project.state(scene.id).get("clip_len") or scene.duration or 1.0))
        for scene in plan.scenes
    ]
    transitions = [jobs.scene_transition(project, scene)[1] for scene in plan.scenes[:-1]]
    starts = render.timeline(scene_durations, transitions)
    total = max(tts.probe_duration(music_path), starts[-1] + scene_durations[-1])
    dub_dir = project.dir / "final" / "dubs"
    dub_dir.mkdir(parents=True, exist_ok=True)
    lang_code = LANGUAGES[language]
    voice_track_path = dub_dir / f"voice_{lang_code}.wav"
    voice_track = audio.Track(voice_track_path, total)
    srt_chunks = []
    manifest = []
    generated_count = 0

    for index, scene in enumerate(plan.scenes):
        if should_cancel and should_cancel():
            raise jobs.Cancelled()
        source_text = _scene_text(scene)
        if not source_text:
            continue
        state = project.state(scene.id)
        original_parts = state.get("voice_parts") or []
        original_speech_seconds = sum(float(part.get("duration", 0.0)) for part in original_parts)
        if original_speech_seconds <= 0:
            original_speech_seconds = max(1.0, scene_durations[index] - LEAD_IN_SECONDS - TAIL_SECONDS)
        available = max(0.5, scene_durations[index] - LEAD_IN_SECONDS - TAIL_SECONDS)
        target_words = _target_words(source_text, original_speech_seconds, available)
        translation_key = cache.cache_key("mla_translation_v1", source_text, language, target_words)
        translation_data = cache.get_json("mla_dubbing", translation_key)
        if translation_data and isinstance(translation_data.get("translation"), str):
            translated = translation_data["translation"]
        else:
            translated = translate_scene(source_text, language, target_words, log=log)
            cache.put_json("mla_dubbing", translation_key, {"translation": translated})

        speech_key = cache.cache_key(
            "mla_speech_v1", project.id, scene.id, translated, selected_engine, selected_voice,
            float(project.settings.get("voice_speed", 1.0)), language,
        )
        raw_path = dub_dir / "cache" / f"{speech_key}.wav"
        fitted_path = dub_dir / "cache" / f"{speech_key}_fit.wav"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        if not raw_path.is_file():
            result = tts.synthesize(
                translated, raw_path, engine=selected_engine, voice=selected_voice,
                speed=float(project.settings.get("voice_speed", 1.0)),
                pitch_hz=int(project.settings.get("voice_pitch", 0)),
                language=language, log=log, allow_edge_draft=allow_edge_draft,
                **_voice_synthesis_options(project, selected_engine),
            )
            raw_path = result.path
        target_duration = available
        if not fitted_path.is_file() or tts.probe_duration(fitted_path) <= 0:
            fitted_path, tempo = _fit_audio(raw_path, fitted_path, target_duration)
        else:
            measured = tts.probe_duration(raw_path)
            tempo = max(0.90, min(1.12, measured / target_duration)) if measured > 0 else 1.0
        segment_start = starts[index] + LEAD_IN_SECONDS
        voice_track.add(audio.decode(fitted_path), segment_start, 1.0)
        fitted_duration = min(target_duration, tts.probe_duration(fitted_path))
        words = [
            {"start": segment_start + word.start, "end": segment_start + word.end, "text": word.text}
            for word in tts._estimate_words(translated, fitted_duration)
        ]
        if words:
            srt_chunks.extend(render.chunk_words(words, 7))
        manifest.append({
            "scene_id": scene.id, "language": language, "engine": selected_engine,
            "voice": selected_voice, "translation_sha256": hashlib.sha256(translated.encode("utf-8")).hexdigest(),
            "speech_cache": str(fitted_path), "tempo": tempo,
        })
        generated_count += 1
        if log:
            log(f"🗣️ Dubbed scene {index + 1}/{len(plan.scenes)} in {language} at {tempo:.2f}×.")

    if not generated_count:
        raise ValueError("No narration or dialogue was found in the storyboard.")
    voice_track.write(voice_track_path)
    audio_path = dub_dir / f"audio_{lang_code}.wav"
    subtitle_path = dub_dir / f"subtitles_{lang_code}.srt"
    _mix_dub(voice_track_path, music_path, audio_path, total)
    render.write_srt(srt_chunks, subtitle_path)
    manifest_path = dub_dir / f"manifest_{lang_code}.json"
    manifest_tmp = manifest_path.with_name(manifest_path.name + ".tmp")
    manifest_tmp.write_text(json.dumps({
        "version": 1, "language": language, "engine": selected_engine, "voice": selected_voice,
        "source_music_sha256": hashlib.sha256(music_path.read_bytes()).hexdigest(),
        "scenes": manifest,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest_tmp.replace(manifest_path)
    project.data.setdefault("outputs", {}).setdefault("dubs", {})[language] = {
        "audio": str(audio_path), "srt": str(subtitle_path), "manifest": str(manifest_path),
    }
    project.save()
    return {"audio": str(audio_path), "srt": str(subtitle_path), "manifest": str(manifest_path)}
