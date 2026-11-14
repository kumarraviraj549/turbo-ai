"""Narration / dialogue voice synthesis with word timings for subtitles.
Engines: Microsoft Edge neural voices (free, 40+ languages), Cloudflare Aura-2 (premium, neurons),
Groq Orpheus (expressive, requires accepting model terms in the Groq console)."""
import asyncio
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

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
    "edge": "Microsoft neural voices (free, multilingual)",
    "cf_aura2": "Cloudflare Deepgram Aura-2 (premium, ~2.7k neurons per 1k chars)",
    "groq_orpheus": "Groq Orpheus expressive voices (English, needs terms accepted)",
}


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


def voice_options(language: str) -> List[Tuple[str, str]]:
    return EDGE_VOICES.get(language) or EDGE_VOICES["English"]


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
    tag = EMOTION_TO_ORPHEUS.get(emotion, "")
    resp = request("groq", "POST", "https://api.groq.com/openai/v1/audio/speech",
                   headers={"Authorization": f"Bearer {config.groq_key()}"},
                   json={"model": "canopylabs/orpheus-v1-english", "voice": voice,
                         "input": (f"{tag} " if tag else "") + text[:1200], "response_format": "wav"},
                   timeout=120, retries=2, breaker_key="groq_tts",
                   is_fatal=lambda r: r.status_code == 400 and ("terms" in r.text or "model" in r.text))
    return resp.content


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
) -> TTSResult:
    """Synthesizes text to an MP3 at out_path with word timings. Falls back to Edge voices if needed."""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if not text:
        raise TTSError("empty narration")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    warnings: List[str] = []
    order = [engine] + (["edge"] if engine != "edge" else [])
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
            elif eng == "groq_orpheus":
                if breaker_reason("groq_tts"):
                    raise TTSError(breaker_reason("groq_tts"))
                data, words = _orpheus(text, v if v in ORPHEUS_VOICES else "troy", emotion), []
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
        synthesize(samples.get(language, samples["English"]), out, engine, voice, speed)
    return out.read_bytes()
