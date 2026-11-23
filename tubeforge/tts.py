"""Narration and dialogue voice synthesis with word timings for subtitles.
Kokoro is the offline English production default; hosted providers are optional, and Edge is draft preview only."""
import asyncio
import base64
import hashlib
import io
import os
import random
import re
import subprocess
import sys
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from . import cache, config
from .http import ProviderError, breaker_reason, limiter, request, trip_breaker

# name -> (edge voice id, description)
EDGE_VOICES: Dict[str, List[Tuple[str, str]]] = {
    "English": [
        ("en-US-AndrewMultilingualNeural", "Andrew - warm cinematic narrator (M)"),
        ("en-US-ChristopherNeural", "Christopher - deep documentary (M)"),
        ("en-GB-RyanNeural", "Ryan - British epic narrator (M)"),
        ("en-US-BrianMultilingualNeural", "Brian - calm storyteller (M)"),
        ("en-US-GuyNeural", "Guy - energetic host (M)"),
        ("en-US-EricNeural", "Eric - gritty, rugged (M)"),
        ("en-US-AvaMultilingualNeural", "Ava - expressive, emotional (F)"),
        ("en-US-EmmaMultilingualNeural", "Emma - clear, modern (F)"),
        ("en-US-AriaNeural", "Aria - professional (F)"),
        ("en-GB-SoniaNeural", "Sonia - British narrator (F)"),
        ("en-US-JennyNeural", "Jenny - friendly (F)"),
        ("en-AU-NatashaNeural", "Natasha - Australian (F)"),
        ("en-IN-PrabhatNeural", "Prabhat - Indian English (M)"),
        ("en-IN-NeerjaNeural", "Neerja - Indian English (F)"),
        ("en-US-AnaNeural", "Ana - child voice (F)"),
    ],
    "Hindi": [("hi-IN-MadhurNeural", "Madhur (M)"), ("hi-IN-SwaraNeural", "Swara (F)")],
    "Spanish": [("es-MX-JorgeNeural", "Jorge - Mexico (M)"), ("es-ES-AlvaroNeural", "Alvaro - Spain (M)"),
                ("es-ES-ElviraNeural", "Elvira - Spain (F)"), ("es-MX-DaliaNeural", "Dalia - Mexico (F)")],
    "French": [("fr-FR-HenriNeural", "Henri (M)"), ("fr-FR-RemyMultilingualNeural", "Remy (M)"),
               ("fr-FR-DeniseNeural", "Denise (F)"), ("fr-FR-VivienneMultilingualNeural", "Vivienne (F)")],
    "German": [("de-DE-ConradNeural", "Conrad (M)"), ("de-DE-FlorianMultilingualNeural", "Florian (M)"),
               ("de-DE-KatjaNeural", "Katja (F)"), ("de-DE-SeraphinaMultilingualNeural", "Seraphina (F)")],
    "Portuguese": [("pt-BR-AntonioNeural", "Antonio (M)"), ("pt-BR-FranciscaNeural", "Francisca (F)")],
    "Italian": [("it-IT-DiegoNeural", "Diego (M)"), ("it-IT-ElsaNeural", "Elsa (F)")],
    "Japanese": [("ja-JP-KeitaNeural", "Keita (M)"), ("ja-JP-NanamiNeural", "Nanami (F)")],
    "Korean": [("ko-KR-InJoonNeural", "InJoon (M)"), ("ko-KR-SunHiNeural", "SunHi (F)")],
    "Chinese": [("zh-CN-YunxiNeural", "Yunxi (M)"), ("zh-CN-XiaoxiaoNeural", "Xiaoxiao (F)")],
    "Arabic": [("ar-SA-HamedNeural", "Hamed (M)"), ("ar-SA-ZariyahNeural", "Zariyah (F)")],
    "Russian": [("ru-RU-DmitryNeural", "Dmitry (M)"), ("ru-RU-SvetlanaNeural", "Svetlana (F)")],
    "Turkish": [("tr-TR-AhmetNeural", "Ahmet (M)"), ("tr-TR-EmelNeural", "Emel (F)")],
    "Bengali": [("bn-IN-BashkarNeural", "Bashkar (M)"), ("bn-IN-TanishaaNeural", "Tanishaa (F)")],
    "Tamil": [("ta-IN-ValluvarNeural", "Valluvar (M)"), ("ta-IN-PallaviNeural", "Pallavi (F)")],
    "Indonesian": [("id-ID-ArdiNeural", "Ardi (M)"), ("id-ID-GadisNeural", "Gadis (F)")],
}
AURA2_SPEAKERS = ["orion", "zeus", "apollo", "arcas", "orpheus", "atlas", "luna", "asteria", "athena", "hera",
                  "aurora", "thalia"]
ORPHEUS_VOICES = ["troy", "austin", "daniel", "autumn", "diana", "hannah"]
EMOTION_TO_ORPHEUS = {"epic": "[dramatic]", "dark": "[whisper]", "sad": "[sad]", "happy": "[cheerful]",
                      "tense": "[serious]", "inspiring": "[dramatic]"}

TTS_ENGINES = {
    "kokoro": "Kokoro-82M (offline, Apache-2.0 model, production)",
    "edge": "Edge TTS (Fast Draft Preview)",
    "cf_aura2": "Cloudflare Deepgram Aura-2 (premium, ~2.7k neurons per 1k chars)",
    "groq_orpheus": "Groq Orpheus (free-tier English, rate-limited; Kokoro fallback)",
    "sarvam": "Sarvam Bulbul v4 Flash (Hindi / Hinglish, API key required)",
}
KOKORO_VOICES = [
    ("af_bella", "Bella (American English, F)"),
    ("am_adam", "Adam (American English, M)"),
    ("bf_emma", "Emma (British English, F)"),
    ("bm_george", "George (British English, M)"),
]
SARVAM_VOICES = {
    "English": [("shubh_enhi_ads", "Shubh (English/Hindi)"),
                ("shubh_en_narration", "Shubh (English narration)")],
    "Hindi": [("shubh_hi_stories_mixed", "Shubh (Hindi stories, mixed)"),
              ("simran_hi_narration", "Simran (Hindi narration)"),
              ("shubh_enhi_ads", "Shubh (English/Hindi)")],
}
_KOKORO_MODEL_URL = ("https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
                     "model-files-v1.1/kokoro-v1.0.onnx")
_KOKORO_VOICES_URL = ("https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
                      "model-files-v1.1/voices-v1.0.bin")
_KOKORO_INIT_LOCK = threading.Lock()
_KOKORO_RUN_LOCK = threading.Lock()
_KOKORO_INSTANCE = None


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class TTSResult:
    path: Path
    duration: float
    engine: str
    voice: str
    words: List[Word] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


class TTSError(RuntimeError):
    pass


@dataclass
class SpeechChunk:
    text: str
    paragraph_start: bool


def voice_options(language: str, engine: str = "edge") -> List[Tuple[str, str]]:
    if engine == "kokoro":
        return KOKORO_VOICES if language == "English" else []
    if engine == "groq_orpheus":
        return [(v, v.title()) for v in ORPHEUS_VOICES]
    if engine == "sarvam":
        return SARVAM_VOICES.get(language, SARVAM_VOICES["English"])
    return EDGE_VOICES.get(language) or EDGE_VOICES["English"]


def split_text_chunks(text: str, max_chars: int) -> List[SpeechChunk]:
    """Splits at sentence and word boundaries while preserving paragraph starts."""
    chunks: List[SpeechChunk] = []
    paragraphs = re.split(r"\n\s*\n", (text or "").strip())
    for paragraph_index, paragraph in enumerate(paragraphs):
        paragraph = re.sub(r"\s+", " ", paragraph).strip()
        if not paragraph:
            continue
        sentences = re.split(r"(?<=[.!?।])\s+", paragraph)
        parts: List[str] = []
        current = ""
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) > max_chars:
                pieces = []
                long_words = sentence.split()
                for word in long_words:
                    if len(word) > max_chars:
                        pieces.extend(word[i:i + max_chars] for i in range(0, len(word), max_chars))
                    else:
                        pieces.append(word)
            else:
                pieces = [sentence]
            for piece in pieces:
                candidate = f"{current} {piece}".strip()
                if current and len(candidate) > max_chars:
                    parts.append(current)
                    current = piece
                else:
                    current = candidate
        if current:
            parts.append(current)
        for index, part in enumerate(parts):
            chunks.append(SpeechChunk(part, paragraph_index > 0 and index == 0))
    return chunks


def probe_duration(path: Path) -> float:
    res = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                          "default=noprint_wrappers=1:nokey=1", str(path)], capture_output=True, text=True)
    try:
        return float(res.stdout.strip())
    except ValueError:
        return 0.0


def _pct(v: float) -> str:
    return f"{int(round(v)):+d}%"


def _run_async(coro):
    """Runs a coroutine from any thread (Streamlit and worker threads have no running loop)."""
    loop = asyncio.new_event_loop()

    def handle_exception(loop, context):
        error = context.get("exception")
        if (sys.platform == "win32" and isinstance(error, ConnectionResetError)
                and getattr(error, "winerror", None) == 10054
                and context.get("message") ==
                "Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)"):
            return
        loop.default_exception_handler(context)

    loop.set_exception_handler(handle_exception)
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()


async def _edge_stream(text: str, voice: str, rate: str, pitch: str, volume: str):
    import edge_tts

    audio = bytearray()
    words: List[Word] = []
    comm = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch, volume=volume,
                                boundary="WordBoundary", receive_timeout=90)
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            audio.extend(chunk["data"])
        elif chunk["type"] == "WordBoundary":
            s = chunk["offset"] / 1e7
            words.append(Word(s, s + chunk["duration"] / 1e7, chunk["text"]))
    return bytes(audio), words


EDGE_TIMEOUT = 45.0


def _edge(text: str, voice: str, speed: float, pitch_hz: int, volume: int) -> Tuple[bytes, List[Word]]:
    rate = _pct((speed - 1.0) * 100)
    pitch = f"{int(pitch_hz):+d}Hz"
    vol = _pct(volume)
    last: Optional[Exception] = None
    lim = limiter("edge_tts")
    for attempt in range(6):
        try:
            with lim.sem:
                lim.wait_turn()
                # Hard timeout: a stalled Edge websocket must never hang the whole render job.
                audio, words = _run_async(asyncio.wait_for(_edge_stream(text, voice, rate, pitch, vol),
                                                           timeout=EDGE_TIMEOUT + len(text) / 20.0))
            if len(audio) > 1000:
                return audio, words
            last = TTSError("edge-tts returned no audio")
        except asyncio.TimeoutError:
            last = TTSError("Edge voice service timed out")
        except Exception as e:
            last = e
        time.sleep(min(20.0, 2.0 * (2 ** attempt)) * random.uniform(0.6, 1.2))
    raise TTSError(f"Edge voice synthesis failed: {str(last)[:160]}")


def _aura2(text: str, speaker: str) -> bytes:
    if not (config.cloudflare_token() and config.cloudflare_account()):
        raise TTSError("Cloudflare keys missing")
    url = (f"https://api.cloudflare.com/client/v4/accounts/{config.cloudflare_account()}"
           f"/ai/run/@cf/deepgram/aura-2-en")
    resp = request("cloudflare", "POST", url, headers={"Authorization": f"Bearer {config.cloudflare_token()}"},
                   json={"text": text[:1900], "speaker": speaker, "encoding": "mp3"}, timeout=120, retries=2,
                   breaker_key="cloudflare")
    if resp.headers.get("content-type", "").startswith("application/json"):
        raise TTSError(f"Aura-2 returned no audio: {resp.text[:120]}")
    cache.record_usage("cloudflare", neurons=2727 * len(text) / 1000.0)
    return resp.content


def _orpheus(text: str, voice: str, emotion: str) -> bytes:
    if not config.groq_key():
        raise TTSError("GROQ_API_KEY missing")
    if not cache.reserve_daily_calls("groq_tts", 100):
        raise ProviderError("groq_tts", "daily free TTS request limit reached", status=429)
    if voice not in ORPHEUS_VOICES:
        raise TTSError(f"Unsupported Orpheus voice: {voice}")
    tag = EMOTION_TO_ORPHEUS.get(emotion, "")
    resp = request("groq_tts", "POST", "https://api.groq.com/openai/v1/audio/speech",
                   headers={"Authorization": f"Bearer {config.groq_key()}"},
                   json={"model": "canopylabs/orpheus-v1-english", "voice": voice,
                         "input": (f"{tag} " if tag else "") + text, "response_format": "wav"},
                   timeout=120, retries=0,
                   is_fatal=lambda r: r.status_code == 400 and ("terms" in r.text or "model" in r.text))
    return resp.content


def _sarvam(text: str, voice: str, language: str, speed: float) -> bytes:
    if not config.sarvam_key():
        raise TTSError("SARVAM_API_KEY missing")
    valid_speakers = {v for values in SARVAM_VOICES.values() for v, _ in values}
    if voice not in valid_speakers:
        raise TTSError(f"Unsupported Sarvam v4 speaker: {voice}")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()[:2500]
    language_code = "hi-IN" if language == "Hindi" else "en-IN"
    resp = request(
        "sarvam",
        "POST",
        "https://api.sarvam.ai/text-to-speech",
        headers={"api-subscription-key": config.sarvam_key()},
        json={
            "text": text,
            "target_language_code": language_code,
            "speaker": voice,
            "pace": max(0.5, min(2.0, float(speed))),
            "model": "bulbul:v4-flash",
            "speech_sample_rate": 24000,
            "output_audio_codec": "wav",
        },
        timeout=120,
        retries=0,
    )
    try:
        audio = (resp.json().get("audios") or [])[0]
        return base64.b64decode(audio, validate=True)
    except (ValueError, TypeError, IndexError) as exc:
        raise TTSError("Sarvam returned an invalid audio response") from exc


def _kokoro_cache_dir() -> Path:
    configured = os.environ.get("TUBEFORGE_TTS_CACHE")
    if configured:
        root = Path(configured).expanduser()
    elif os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "TubeForge" / "cache"
    else:
        root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "tubeforge"
    return root / "kokoro"


def _download_model_file(url: str, destination: Path) -> Path:
    if destination.exists() and destination.stat().st_size:
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        from urllib.request import Request, urlopen

        request_ = Request(url, headers={"User-Agent": "TubeForgeStudio/1.0"})
        with urlopen(request_, timeout=60) as response, temporary.open("wb") as target:
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                target.write(block)
        if not temporary.exists() or temporary.stat().st_size < 1024:
            raise TTSError(f"Downloaded Kokoro model file is incomplete: {destination.name}")
        temporary.replace(destination)
        return destination
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        if isinstance(exc, TTSError):
            raise
        raise TTSError(f"Could not download Kokoro model file {destination.name}: {exc}") from exc


def _kokoro():
    global _KOKORO_INSTANCE
    if _KOKORO_INSTANCE is not None:
        return _KOKORO_INSTANCE
    with _KOKORO_INIT_LOCK:
        if _KOKORO_INSTANCE is not None:
            return _KOKORO_INSTANCE
        try:
            from kokoro_onnx import Kokoro
        except ImportError as exc:
            raise TTSError("Kokoro is not installed; install the project requirements to use local production TTS") from exc
        model_dir = _kokoro_cache_dir() / "models"
        model = _download_model_file(_KOKORO_MODEL_URL, model_dir / "kokoro-v1.0.onnx")
        voices = _download_model_file(_KOKORO_VOICES_URL, model_dir / "voices-v1.0.bin")
        try:
            _KOKORO_INSTANCE = Kokoro(str(model), str(voices))
        except Exception as exc:
            raise TTSError(f"Could not initialize Kokoro ONNX: {exc}") from exc
    return _KOKORO_INSTANCE


def _write_wav_bytes(samples: np.ndarray, sample_rate: int) -> bytes:
    samples = np.asarray(samples, dtype=np.float32).reshape(-1)
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm.tobytes())
    return buf.getvalue()


def _read_wav_bytes(data: bytes) -> Tuple[np.ndarray, int]:
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            channels, width, rate, frames = wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()
            raw = wav.readframes(frames)
    except (wave.Error, EOFError) as exc:
        raise TTSError("Audio provider returned an invalid WAV file") from exc
    if width == 2:
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        samples = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise TTSError(f"Unsupported WAV sample width: {width}")
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if rate != 24000:
        samples = _resample(samples, rate, 24000)
        rate = 24000
    return samples, rate


def _decode_audio_bytes(data: bytes) -> Tuple[np.ndarray, int]:
    res = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "f32le", "-ac", "1", "-ar", "24000", "pipe:1"],
        input=data,
        capture_output=True,
    )
    if res.returncode:
        raise TTSError(f"Could not decode provider audio: {res.stderr.decode(errors='replace')[-240:]}")
    return np.frombuffer(res.stdout, dtype="<f4").copy(), 24000


def _resample(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate:
        return np.asarray(samples, dtype=np.float32)
    return np.interp(
        np.arange(int(len(samples) * target_rate / source_rate), dtype=np.float64) * source_rate / target_rate,
        np.arange(len(samples), dtype=np.float64),
        np.asarray(samples, dtype=np.float32),
    ).astype(np.float32)


def _stitch_chunks(chunks: List[Tuple[SpeechChunk, np.ndarray]]) -> Tuple[np.ndarray, List[Word]]:
    output = np.zeros(0, dtype=np.float32)
    words: List[Word] = []
    crossfade = int(0.03 * 24000)
    nonempty = [(chunk, np.asarray(samples, dtype=np.float32).reshape(-1))
                for chunk, samples in chunks if len(samples)]
    for chunk, samples in nonempty:
        if not len(output):
            fade = min(crossfade, len(samples))
            samples = samples.copy()
            if fade:
                samples[:fade] *= np.linspace(0.0, 1.0, fade, dtype=np.float32)
            start = 0.0
            output = samples
        elif chunk.paragraph_start:
            fade = min(crossfade, len(output), len(samples))
            if fade:
                output[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)
                samples = samples.copy()
                samples[:fade] *= np.linspace(0.0, 1.0, fade, dtype=np.float32)
            pause = int(0.45 * 24000)
            output = np.concatenate((output, np.zeros(pause, dtype=np.float32), samples))
            start = (len(output) - len(samples)) / 24000
        else:
            overlap = min(crossfade, len(output), len(samples))
            if overlap:
                left = np.asarray(output[-overlap:], dtype=np.float32)
                right = np.asarray(samples[:overlap], dtype=np.float32)
                zero_range = np.where((left[:-1] <= 0.0) & (left[1:] >= 0.0))[0]
                if len(zero_range):
                    overlap = min(overlap, max(1, int(zero_range[-1]) + 1))
                cosine = (1.0 - np.cos(np.linspace(0.0, np.pi, overlap, dtype=np.float32))) * 0.5
                fade_out = 1.0 - cosine
                fade_in = cosine
                blend = output[-overlap:] * fade_out + samples[:overlap] * fade_in
                output = np.concatenate((output[:-overlap], blend, samples[overlap:]))
            else:
                output = np.concatenate((output, samples))
            start = (len(output) - len(samples)) / 24000
        duration = len(samples) / 24000
        words.extend(Word(w.start + start, w.end + start, w.text) for w in _estimate_words(chunk.text, duration))
    if len(output) and nonempty:
        fade = min(crossfade, len(output))
        output[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)
    return output, words


def _kokoro_generate_chunk(text: str, voice: str, speed: float) -> np.ndarray:
    try:
        with _KOKORO_RUN_LOCK:
            language = "en-gb" if voice.startswith(("bf_", "bm_")) else "en-us"
            samples, sample_rate = _kokoro().create(text, voice=voice, speed=speed, lang=language)
    except TTSError:
        raise
    except Exception as exc:
        raise TTSError(f"Kokoro synthesis failed: {exc}") from exc
    return _resample(np.asarray(samples, dtype=np.float32), int(sample_rate), 24000)


def _synthesize_kokoro(text: str, voice: str, speed: float, log=None) -> Tuple[np.ndarray, List[Word]]:
    chunks = split_text_chunks(text, 220)
    audio_chunks = []
    chunk_dir = _kokoro_cache_dir() / "chunks"
    for index, chunk in enumerate(chunks, 1):
        key = hashlib.sha256(f"{chunk.text}{voice}{float(speed)}".encode("utf-8")).hexdigest()
        path = chunk_dir / f"{key}.wav"
        if path.exists() and path.stat().st_size:
            samples, _ = _read_wav_bytes(path.read_bytes())
        else:
            if log:
                log(f"Offline Kokoro voice chunk {index}/{len(chunks)}")
            samples = _kokoro_generate_chunk(chunk.text, voice, speed)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".wav.tmp")
            temporary.write_bytes(_write_wav_bytes(samples, 24000))
            temporary.replace(path)
        audio_chunks.append((chunk, samples))
    return _stitch_chunks(audio_chunks)


def _synthesize_remote_chunks(
    text: str,
    engine: str,
    voice: str,
    speed: float,
    language: str,
    emotion: str,
    log=None,
) -> Tuple[np.ndarray, List[Word]]:
    max_chars = 2500 if engine == "sarvam" else 195
    chunks = split_text_chunks(text, max_chars)
    audio_chunks = []
    chunk_dir = _kokoro_cache_dir() / "provider_chunks"
    for index, chunk in enumerate(chunks, 1):
        key = cache.cache_key(engine, chunk.text, voice, speed, language, emotion)
        path = chunk_dir / f"{key}.wav"
        if path.exists() and path.stat().st_size:
            samples, _ = _read_wav_bytes(path.read_bytes())
        else:
            if log:
                log(f"Generating {engine} voice chunk {index}/{len(chunks)}")
            if engine == "groq_orpheus":
                raw = _orpheus(chunk.text, voice, emotion)
            else:
                raw = _sarvam(chunk.text, voice, language, speed)
            samples, _ = _decode_audio_bytes(raw)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".wav.tmp")
            temporary.write_bytes(_write_wav_bytes(samples, 24000))
            temporary.replace(path)
        audio_chunks.append((chunk, samples))
    return _stitch_chunks(audio_chunks)


def _estimate_words(text: str, duration: float) -> List[Word]:
    """Proportional word timing for engines without boundaries (weighted by word length)."""
    tokens = text.split()
    if not tokens or duration <= 0:
        return []
    weights = [len(t) + 2 for t in tokens]
    total = float(sum(weights))
    t = 0.0
    out = []
    for tok, w in zip(tokens, weights):
        d = duration * w / total
        out.append(Word(t, t + d, tok))
        t += d
    return out


def _to_mp3(src: Path, dst: Path) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-ar", "44100", "-ac", "2", "-b:a", "192k",
                    str(dst)], check=True)


def synthesize(
    text: str,
    out_path: Path,
    engine: str = "edge",
    voice: str = "en-US-AndrewMultilingualNeural",
    speed: float = 1.0,
    pitch_hz: int = 0,
    volume: int = 0,
    emotion: str = "",
    log: Optional[Callable[[str], None]] = None,
    language: str = "English",
) -> TTSResult:
    """Synthesizes speech with word timings; production engines preserve chunk-level resume caches."""
    text = re.sub(r"[ \t]+", " ", (text or "").strip())
    text = re.sub(r"\n{3,}", "\n\n", text)
    if not text:
        raise TTSError("empty narration")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    warnings: List[str] = []

    if engine in ("kokoro", "sarvam", "groq_orpheus"):
        if engine == "kokoro" and language != "English":
            raise TTSError("Kokoro production voices currently support English; choose Sarvam or Edge TTS for this language")
        voice_choices = dict(voice_options(language="English" if engine != "sarvam" else "Hindi", engine=engine))
        if engine == "sarvam":
            voice_choices = {v: label for lang in SARVAM_VOICES.values() for v, label in lang}
        selected_voice = voice if voice in voice_choices else next(iter(voice_choices), voice)
        try:
            if engine == "kokoro":
                samples, words = _synthesize_kokoro(text, selected_voice, speed, log)
                used_engine = "kokoro"
            elif engine == "sarvam":
                samples, words = _synthesize_remote_chunks(text, engine, selected_voice, speed, language, emotion, log)
                used_engine = "sarvam"
            else:
                try:
                    samples, words = _synthesize_remote_chunks(
                        text, engine, selected_voice, speed, language, emotion, log
                    )
                    used_engine = "groq_orpheus"
                except ProviderError as exc:
                    warning = (
                        f"Groq Orpheus request failed (HTTP {exc.status or 'network'}); "
                        f"continuing locally with Kokoro: {exc}"
                    )
                    warnings.append(warning)
                    if log:
                        log(warning)
                    samples, words = _synthesize_kokoro(text, "af_bella", speed, log)
                    selected_voice = "af_bella"
                    used_engine = "kokoro"
            result_path = out_path.with_suffix(".wav")
            temporary = result_path.with_suffix(".tmp.wav")
            temporary.write_bytes(_write_wav_bytes(samples, 24000))
            temporary.replace(result_path)
            duration = len(samples) / 24000.0
            return TTSResult(result_path, duration, used_engine, selected_voice, words, warnings)
        except (ProviderError, TTSError):
            raise

    order = [engine] + (["edge"] if engine == "cf_aura2" else [])
    for eng in order:
        v = voice if eng == engine else "en-US-AndrewMultilingualNeural" if not voice.count("-") >= 2 else voice
        ck = cache.cache_key("tts", eng, v, text, round(speed, 2), pitch_hz, volume, emotion)
        meta = cache.get_json("tts", ck)
        data = cache.get_bytes("tts", ck, ".mp3")
        try:
            if data and meta:
                words = [Word(**w) for w in meta.get("words", [])]
            elif eng == "edge":
                data, words = _edge(text, v, speed, pitch_hz, volume)
            elif eng == "cf_aura2":
                if breaker_reason("cloudflare"):
                    raise TTSError(breaker_reason("cloudflare"))
                data, words = _aura2(text, v if v in AURA2_SPEAKERS else "orion"), []
            else:
                raise TTSError(f"unknown TTS engine {eng}")
        except (ProviderError, TTSError) as e:
            msg = str(e)
            if isinstance(e, ProviderError) and e.fatal:
                if "terms" in msg:
                    msg = "Groq Orpheus needs its model terms accepted at console.groq.com (admin)."
                trip_breaker("groq_tts" if eng == "groq_orpheus" else "cloudflare", msg[:160], 3600)
            warnings.append(f"{TTS_ENGINES.get(eng, eng)}: {msg[:160]}")
            if log:
                log(f"⚠️ Voice engine {eng} failed, falling back: {msg[:120]}")
            continue
        raw = out_path.with_suffix(".raw")
        raw.write_bytes(data)
        try:
            if eng == "edge" or (data and meta):
                raw.replace(out_path)
            else:
                _to_mp3(raw, out_path)
                raw.unlink(missing_ok=True)
                # Speed adjustment for engines without a native rate control.
                if abs(speed - 1.0) > 0.02:
                    tmp = out_path.with_suffix(".speed.mp3")
                    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(out_path), "-filter:a",
                                    f"atempo={max(0.5, min(2.0, speed)):.3f}", str(tmp)], check=True)
                    tmp.replace(out_path)
        finally:
            if raw.exists():
                raw.unlink()
        dur = probe_duration(out_path)
        if dur <= 0.2:
            warnings.append(f"{eng}: produced unreadable audio")
            continue
        if not words:
            words = _estimate_words(text, dur)
        if not (data and meta):
            cache.put_bytes("tts", ck, ".mp3", out_path.read_bytes())
            cache.put_json("tts", ck, {"words": [w.__dict__ for w in words]})
        return TTSResult(out_path, dur, eng, v, words, warnings)
    raise TTSError("All voice engines failed: " + " | ".join(warnings))


def sample(voice: str, language: str = "English", engine: str = "edge", speed: float = 1.0) -> bytes:
    samples = {"English": "In a world of light and shadow, one story is about to begin.",
               "Hindi": "रोशनी और छाया की इस दुनिया में, एक कहानी शुरू होने वाली है।",
               "Spanish": "En un mundo de luz y sombra, una historia está a punto de comenzar."}
    out = config.CACHE_DIR / "tts_samples" / f"{engine}_{voice}_{speed}.mp3"
    if not out.exists():
        result = synthesize(samples.get(language, samples["English"]), out, engine, voice, speed, language=language)
        if result.path.suffix.lower() != ".mp3":
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(result.path), "-ar", "44100", "-ac", "2",
                            "-b:a", "192k", str(out)], check=True)
    return out.read_bytes()
