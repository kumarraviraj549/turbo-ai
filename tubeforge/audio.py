"""Audio engine: procedurally composed film score (mood-based chords, bass, arpeggios, percussion), synthesized
ambience beds and sound effects, timeline track building, and the final mix (voice-ducked music, ambience,
SFX, EBU R128 loudness normalisation). Everything is generated locally - no stock music API exists for the
configured providers, so the score is honestly labelled as a generated score."""
import math
import subprocess
import wave
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .config import CACHE_DIR

SR = 44100
AUDIO_CACHE = CACHE_DIR / "audio"
SCORE_VERSION = 2

NOTE = {"C": 0, "C#": 1, "D": 2, "D#": 3, "E": 4, "F": 5, "F#": 6, "G": 7, "G#": 8, "A": 9, "A#": 10, "B": 11}

# root, bpm, progression of (semitones from root, quality), layers
MOODS: Dict[str, Dict] = {
    "epic": {"root": "D", "bpm": 88, "prog": [(0, "m"), (-4, "M"), (3, "M"), (-2, "M")], "arp": False,
             "perc": "taiko", "bright": 0.8},
    "dark": {"root": "C", "bpm": 66, "prog": [(0, "m"), (0, "m"), (-4, "M"), (-5, "m")], "arp": False,
             "perc": "heartbeat", "bright": 0.35},
    "uplifting": {"root": "G", "bpm": 100, "prog": [(0, "M"), (7, "M"), (9, "m"), (5, "M")], "arp": True,
                  "perc": "pop", "bright": 0.9},
    "mysterious": {"root": "A", "bpm": 76, "prog": [(0, "m"), (5, "m"), (-4, "M"), (-5, "sus")], "arp": True,
                   "perc": "ticks", "bright": 0.55},
    "emotional": {"root": "F", "bpm": 64, "prog": [(0, "M"), (9, "m"), (5, "M"), (7, "M")], "arp": True,
                  "perc": "none", "bright": 0.6},
    "tense": {"root": "E", "bpm": 118, "prog": [(0, "m"), (1, "M"), (0, "m"), (-2, "M")], "arp": True,
              "perc": "pulse", "bright": 0.5},
    "ambient": {"root": "E", "bpm": 60, "prog": [(0, "sus"), (-3, "M"), (2, "sus"), (-5, "M")], "arp": False,
                "perc": "none", "bright": 0.5},
}
CUE_LEVELS = {"calm": (0.75, 0.0), "build": (0.9, 0.55), "climax": (1.0, 1.0), "tension": (0.85, 0.7),
              "resolve": (0.8, 0.2), "silence": (0.0, 0.0), "": (0.85, 0.45)}


def _midi_hz(semi_from_a4: float) -> float:
    return 440.0 * 2 ** (semi_from_a4 / 12.0)


def _chord(root_semi: int, quality: str) -> List[int]:
    iv = {"M": [0, 4, 7, 12], "m": [0, 3, 7, 12], "sus": [0, 5, 7, 14]}.get(quality, [0, 4, 7, 12])
    return [root_semi + i for i in iv]


def _ambient_variation(seed: int) -> Tuple[str, int, List[Tuple[int, str]], float]:
    """Returns a calm, repeatable key, progression, tempo, and tone for an ambient score."""
    rng = np.random.default_rng(seed)
    roots = list(NOTE)
    progressions = [
        [(0, "sus"), (9, "m"), (5, "M"), (7, "sus")],
        [(0, "M"), (5, "M"), (9, "m"), (7, "sus")],
        [(0, "m"), (5, "M"), (3, "M"), (-2, "sus")],
        [(0, "sus"), (-5, "M"), (2, "m"), (-3, "M")],
        [(0, "M"), (2, "m"), (7, "M"), (5, "M")],
    ]
    root = roots[int(rng.integers(len(roots)))]
    progression = progressions[int(rng.integers(len(progressions)))]
    bpm = int(rng.integers(52, 70))
    brightness = float(rng.uniform(0.38, 0.62))
    return root, bpm, progression, brightness


def _mood_variation(mood: str, seed: int) -> Dict:
    """Create a stable but distinct key, tempo, and chord order for each film's mood."""
    arrangement = dict(MOODS[mood])
    rng = np.random.default_rng(seed)
    arrangement["transpose"] = int(rng.choice([-3, -2, 0, 2, 4, 5]))
    arrangement["bpm"] = max(40, arrangement["bpm"] + int(rng.integers(-4, 5)))
    progression = list(arrangement["prog"])
    if len(progression) > 1:
        offset = int(rng.integers(0, len(progression)))
        arrangement["prog"] = progression[offset:] + progression[:offset]
    arrangement["bright"] = float(np.clip(arrangement["bright"] + rng.uniform(-0.06, 0.06), 0.25, 0.95))
    return arrangement


def _spectral(x: np.ndarray, lo: float = 0.0, hi: float = 22050.0, tilt: float = 0.0) -> np.ndarray:
    """Zero-phase FFT band filter with optional spectral tilt (dB/octave) - circular, so loops stay seamless."""
    n = x.shape[0]
    X = np.fft.rfft(x, axis=0)
    f = np.fft.rfftfreq(n, 1.0 / SR)
    g = np.ones_like(f)
    if lo > 0:
        g *= 1.0 / np.sqrt(1.0 + (lo / np.maximum(f, 1e-3)) ** 4)
    if hi < SR / 2:
        g *= 1.0 / np.sqrt(1.0 + (f / hi) ** 4)
    if tilt:
        g *= (np.maximum(f, 20.0) / 1000.0) ** (tilt / 6.02)
    if x.ndim == 2:
        g = g[:, None]
    return np.fft.irfft(X * g, n=n, axis=0).astype(np.float32)


def _reverb(x: np.ndarray, seconds: float, mix: float, seed: int = 7) -> np.ndarray:
    """Circular convolution with a decaying stereo noise tail (seamless for loops)."""
    n = x.shape[0]
    rng = np.random.default_rng(seed)
    L = min(n, int(seconds * SR))
    t = np.arange(L) / SR
    env = np.exp(-6.9 * t / seconds)
    ir = np.zeros((n, 2), dtype=np.float32)
    ir[:L, 0] = rng.standard_normal(L) * env
    ir[:L, 1] = rng.standard_normal(L) * env
    ir[:L] = _spectral(ir[:L], 120, 7000)
    ir /= np.sqrt((ir ** 2).sum(axis=0, keepdims=True)) + 1e-9
    wet = np.fft.irfft(np.fft.rfft(x, axis=0) * np.fft.rfft(ir, axis=0), n=n, axis=0)
    return ((1 - mix) * x + mix * wet).astype(np.float32)


def _place(buf: np.ndarray, sig: np.ndarray, start: int) -> None:
    """Adds sig into buf at start, wrapping around the end (circular loop buffer)."""
    n = buf.shape[0]
    start %= n
    end = start + sig.shape[0]
    if end <= n:
        buf[start:end] += sig
    else:
        k = n - start
        buf[start:] += sig[:k]
        rest = sig[k:]
        while rest.shape[0]:
            m = min(n, rest.shape[0])
            buf[:m] += rest[:m]
            rest = rest[m:]


def _tone(freq: float, dur: float, harmonics: Sequence[float], attack: float, release: float, detune: float = 0.0,
          decay: float = 0.0) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    out = np.zeros((n, 2), dtype=np.float32)
    for ch, dt in enumerate((-detune, detune)):
        f = freq * (1 + dt)
        s = np.zeros(n, dtype=np.float64)
        for h, amp in enumerate(harmonics, 1):
            if f * h > 16000:
                break
            s += amp * np.sin(2 * np.pi * f * h * t + ch * 0.7 * h)
        out[:, ch] = s
    env = np.ones(n)
    a = max(1, int(attack * SR))
    r = max(1, int(release * SR))
    env[:a] = np.linspace(0, 1, a) ** 2
    env[-r:] *= np.linspace(1, 0, r) ** 1.5
    if decay:
        env *= np.exp(-t / decay)
    return (out * env[:, None]).astype(np.float32)


def _norm(x: np.ndarray, peak_db: float = -1.0) -> np.ndarray:
    p = float(np.max(np.abs(x))) or 1.0
    return (x * (10 ** (peak_db / 20) / p)).astype(np.float32)


def _write_wav(path: Path, x: np.ndarray) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
    tmp = path.with_suffix(".tmp.wav")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    tmp.replace(path)
    return path


def compose_score(mood: str, seed: int = 1) -> Tuple[Path, Path, float]:
    """Composes a seamless loop for the mood. Returns (harmony_stem.wav, percussion_stem.wav, loop_seconds)."""
    mood = mood if mood in MOODS else "epic"
    if mood == "ambient":
        m = dict(MOODS[mood])
        root, bpm, progression, brightness = _ambient_variation(seed)
        m.update(root=root, bpm=bpm, prog=progression, bright=brightness)
        m["transpose"] = 0
    else:
        m = _mood_variation(mood, seed)
    harm_p = AUDIO_CACHE / f"score_v{SCORE_VERSION}_{mood}_{seed}_harm.wav"
    perc_p = AUDIO_CACHE / f"score_v{SCORE_VERSION}_{mood}_{seed}_perc.wav"
    beat = 60.0 / m["bpm"]
    bar = beat * 4
    chord_len = bar * 2
    total = chord_len * len(m["prog"])
    if harm_p.exists() and perc_p.exists():
        return harm_p, perc_p, total
    n = int(round(total * SR))
    rng = np.random.default_rng(seed)
    harm = np.zeros((n, 2), dtype=np.float32)
    perc = np.zeros((n, 2), dtype=np.float32)
    root = NOTE[m["root"]] - 9 - 12 + int(m.get("transpose", 0))  # semitones from A4, octave 3
    bright = m["bright"]
    pad_h = [1.0 * bright ** 0.2] + [bright ** (h * 0.5) / (h + 1) for h in range(1, 8)]
    for ci, (off, q) in enumerate(m["prog"]):
        start = int(ci * chord_len * SR)
        notes = _chord(root + off, q)
        for k, semi in enumerate(notes):
            tone = _tone(_midi_hz(semi), chord_len + 1.6, pad_h, 1.1, 1.6, detune=0.0025 + 0.001 * k)
            _place(harm, tone * (0.16 if k < 3 else 0.09), start)
        bass = _tone(_midi_hz(root + off - 12), chord_len + 0.6, [1.0, 0.35, 0.12], 0.05, 0.5)
        _place(harm, bass * 0.32, start)
        if m["arp"]:
            seq = notes + [notes[1] + 12, notes[2] + 12]
            steps = int(chord_len / (beat / 2))
            for s in range(steps):
                semi = seq[(s * 3 + ci) % len(seq)] + 12
                pl = _tone(_midi_hz(semi), beat * 1.6, [1.0, 0.25, 0.1, 0.05], 0.005, 0.3, decay=beat * 0.6)
                pan = 0.6 + 0.4 * math.sin(s * 0.9)
                pl[:, 0] *= pan
                pl[:, 1] *= 1.6 - pan
                _place(harm, pl * 0.085, int(start + s * beat / 2 * SR))
    harm = _spectral(harm, 35, 9000 + 5000 * bright)
    harm = _reverb(harm, 3.2, 0.38, seed)

    def kick(f0=110.0, f1=42.0, dur=0.6, amp=1.0):
        t = np.arange(int(dur * SR)) / SR
        f = f1 + (f0 - f1) * np.exp(-t * 18)
        ph = 2 * np.pi * np.cumsum(f) / SR
        s = np.sin(ph) * np.exp(-t * 6.5) * amp
        return np.stack([s, s], 1).astype(np.float32)

    def noise_hit(dur=0.05, lo=4000, amp=0.3, decay=0.02):
        k = int(dur * SR)
        s = rng.standard_normal((k, 2)).astype(np.float32)
        s = _spectral(s, lo, 16000) * np.exp(-np.arange(k) / SR / decay)[:, None]
        return s * amp

    beats = int(round(total / beat))
    style = m["perc"]
    for b in range(beats):
        at = int(b * beat * SR)
        if style == "taiko":
            if b % 4 in (0, 2):
                _place(perc, kick(90, 38, 1.0, 1.0), at)
                _place(perc, noise_hit(0.25, 150, 0.25, 0.08), at)
            if b % 8 == 7:
                for s in range(4):
                    _place(perc, kick(140, 60, 0.3, 0.45), int(at + s * beat / 4 * SR))
            _place(perc, noise_hit(0.04, 6000, 0.08, 0.015), int(at + beat / 2 * SR))
        elif style == "heartbeat":
            if b % 2 == 0:
                _place(perc, kick(70, 35, 0.5, 0.9), at)
                _place(perc, kick(65, 35, 0.4, 0.6), int(at + 0.28 * SR))
        elif style == "pop":
            if b % 2 == 0:
                _place(perc, kick(120, 45, 0.45, 0.85), at)
            else:
                _place(perc, noise_hit(0.18, 1500, 0.35, 0.06), at)
            for s in range(2):
                _place(perc, noise_hit(0.05, 7000, 0.07, 0.02), int(at + (s + 0.5) * beat / 2 * SR))
        elif style == "ticks":
            if b % 4 == 0:
                _place(perc, kick(80, 40, 0.8, 0.6), at)
            _place(perc, noise_hit(0.03, 8000, 0.06, 0.01), int(at + beat * 0.75 * SR))
        elif style == "pulse":
            for s in range(4):
                semi = root - 12
                pl = _tone(_midi_hz(semi), beat / 4 * 0.9, [1, 0.5, 0.3, 0.2], 0.003, 0.03, decay=0.08)
                _place(perc, pl * 0.35, int(at + s * beat / 4 * SR))
            if b % 4 == 0:
                _place(perc, kick(100, 40, 0.6, 0.8), at)
    if style != "none":
        perc = _reverb(perc, 1.6, 0.18, seed + 1)
    _write_wav(harm_p, _norm(harm, -3.0))
    _write_wav(perc_p, _norm(perc, -3.0) if np.any(perc) else perc)
    return harm_p, perc_p, total


def ambience_loop(kind: str, seconds: float = 24.0, seed: int = 3) -> Optional[Path]:
    if kind in ("", "none", None):
        return None
    path = AUDIO_CACHE / f"amb_{kind}_{seed}.wav"
    if path.exists():
        return path
    n = int(seconds * SR)
    rng = np.random.default_rng(seed)
    t = np.arange(n) / SR
    white = rng.standard_normal((n, 2)).astype(np.float32)

    def lfo(freqs, depth=0.5):
        # integer cycles per loop keep it seamless
        s = np.zeros(n)
        for f in freqs:
            cyc = max(1, round(f * seconds))
            s += np.sin(2 * np.pi * cyc * t / seconds + rng.random() * 6.28)
        return (1 - depth + depth * (0.5 + 0.5 * s / len(freqs)))[:, None]

    def clicks(rate, lo, amp, dur=0.012):
        out = np.zeros((n, 2), dtype=np.float32)
        for _ in range(int(rate * seconds)):
            k = int(dur * SR)
            c = rng.standard_normal((k, 2)).astype(np.float32) * np.exp(-np.arange(k) / (k / 4))[:, None]
            _place(out, c * amp * rng.random(), int(rng.random() * n))
        return _spectral(out, lo, 12000)

    if kind == "rain":
        x = _spectral(white, 500, 9000, tilt=-1.5) * 0.5 + clicks(60, 2500, 0.6)
    elif kind == "storm":
        x = _spectral(white, 400, 8000, tilt=-1.5) * 0.55 + clicks(40, 2000, 0.5)
        rumble = np.zeros((n, 2), dtype=np.float32)
        for _ in range(2):
            k = int(3.5 * SR)
            r = rng.standard_normal((k, 2)).astype(np.float32) * np.exp(-np.arange(k) / SR / 1.1)[:, None]
            _place(rumble, r, int(rng.random() * n))
        x += _spectral(rumble, 25, 180) * 3.0
    elif kind == "wind":
        x = _spectral(white, 60, 700, tilt=-3) * lfo([0.07, 0.13, 0.21], 0.8)
    elif kind == "ocean":
        x = _spectral(white, 80, 2500, tilt=-2.5) * lfo([1 / 8.0], 0.85) ** 1.5
    elif kind == "city":
        x = _spectral(white, 40, 1400, tilt=-2.5) * 0.8 * lfo([0.05, 0.11], 0.3)
        x += (np.sin(2 * np.pi * 60 * t) * 0.03)[:, None]
    elif kind == "fire":
        x = _spectral(white, 40, 500, tilt=-3) * 0.6 * lfo([0.3, 0.7], 0.4) + clicks(25, 1500, 0.9, 0.006)
    elif kind == "forest":
        x = _spectral(white, 100, 900, tilt=-3) * 0.4 * lfo([0.08, 0.15], 0.7)
        for _ in range(int(seconds * 0.6)):
            d = 0.06 + rng.random() * 0.1
            k = int(d * SR)
            tt = np.arange(k) / SR
            f0 = 2500 + rng.random() * 2500
            ch = np.sin(2 * np.pi * np.cumsum(f0 + 1200 * np.sin(tt * 40)) / SR) * np.sin(np.pi * tt / d)
            chirp = np.stack([ch, ch * rng.random()], 1).astype(np.float32) * 0.15
            for rep in range(1 + int(rng.random() * 3)):
                _place(x, chirp, int(rng.random() * n) + rep * int(0.15 * SR))
    elif kind == "space":
        drone = (np.sin(2 * np.pi * 55 * t)
                 + 0.6 * np.sin(2 * np.pi * 55.5 * t) + 0.3 * np.sin(2 * np.pi * 82.5 * t))
        x = np.stack([drone, np.roll(drone, 900)], 1).astype(np.float32) * 0.25
        x += _spectral(white, 2000, 9000) * 0.05 * lfo([0.1], 0.9)
    elif kind == "crowd":
        x = np.zeros((n, 2), dtype=np.float32)
        for f in (400, 700, 1100, 1700, 2400):
            x += _spectral(white, f * 0.8, f * 1.25) * lfo([0.3 + rng.random(), 0.9 + rng.random()], 0.7)
    else:
        return None
    x = _reverb(x.astype(np.float32), 1.2, 0.2, seed)
    _write_wav(path, _norm(x, -6.0))
    return path


def sfx(kind: str, seed: int = 5) -> Optional[Path]:
    if kind in ("", "none", None):
        return None
    path = AUDIO_CACHE / f"sfx_{kind}_{seed}.wav"
    if path.exists():
        return path
    rng = np.random.default_rng(seed)
    if kind == "whoosh":
        d = 1.0
        n = int(d * SR)
        t = np.arange(n) / SR
        w = rng.standard_normal((n, 2)).astype(np.float32)
        low, high = _spectral(w, 150, 900), _spectral(w, 1500, 7000)
        env = np.sin(np.pi * t / d) ** 2
        mixk = np.sin(np.pi * t / d)[:, None]
        x = (low * (1 - mixk) + high * mixk) * env[:, None]
        x[:, 0] *= np.linspace(1.3, 0.5, n)
        x[:, 1] *= np.linspace(0.5, 1.3, n)
    elif kind in ("impact", "boom"):
        d = 2.5 if kind == "boom" else 1.6
        n = int(d * SR)
        t = np.arange(n) / SR
        f = 35 + (90 if kind == "impact" else 60) * np.exp(-t * 8)
        s = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * (2.2 if kind == "boom" else 3.5))
        burst = rng.standard_normal((n, 2)).astype(np.float32) * np.exp(-t * 18)[:, None]
        x = np.stack([s, s], 1).astype(np.float32) + _spectral(burst, 60, 3000) * 0.6
        x = _reverb(x, 2.5, 0.3, seed)
    elif kind == "riser":
        d = 2.5
        n = int(d * SR)
        t = np.arange(n) / SR
        w = _spectral(rng.standard_normal((n, 2)).astype(np.float32), 1200, 9000)
        sweep = np.sin(2 * np.pi * np.cumsum(200 + 1000 * (t / d) ** 2) / SR)
        x = (w * 0.6 + np.stack([sweep, sweep], 1) * 0.3) * ((t / d) ** 2.2)[:, None]
    elif kind == "shimmer":
        d = 2.2
        x = np.zeros((int(d * SR), 2), dtype=np.float32)
        for i, semi in enumerate([12, 16, 19, 24, 28, 31]):
            _place(x, _tone(_midi_hz(semi), 1.6, [1, 0.2], 0.005, 0.5, 0.002, decay=0.5) * 0.25, int(i * 0.08 * SR))
        x = _reverb(x, 2.0, 0.45, seed)
    elif kind == "heartbeat":
        d = 1.2
        n = int(d * SR)
        x = np.zeros((n, 2), dtype=np.float32)
        for at, amp in ((0.0, 1.0), (0.3, 0.7)):
            k = int(0.3 * SR)
            tt = np.arange(k) / SR
            s = np.sin(2 * np.pi * np.cumsum(40 + 50 * np.exp(-tt * 30)) / SR) * np.exp(-tt * 14) * amp
            _place(x, np.stack([s, s], 1).astype(np.float32), int(at * SR))
    else:
        return None
    _write_wav(path, _norm(x.astype(np.float32), -2.0))
    return path


def decode(path: Path) -> np.ndarray:
    """Decodes any audio/video file to float32 stereo at SR."""
    res = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "2", "-ar", str(SR), "-"],
                         capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(f"cannot decode audio {path.name}: {res.stderr.decode()[-200:]}")
    return np.frombuffer(res.stdout, dtype=np.float32).reshape(-1, 2)


def _write_pcm_wav(path: Path, data: np.ndarray) -> None:
    stereo = np.asarray(data, dtype=np.float32).reshape(-1, 2)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(SR)
        pcm = (np.clip(stereo, -1.0, 1.0) * 32767.0).astype("<i2")
        output.writeframes(pcm.tobytes())


def _rms_db(data: np.ndarray, eps: float = 1e-9) -> float:
    rms = float(np.sqrt(np.mean(np.asarray(data, dtype=np.float32) ** 2) + eps))
    return 20.0 * math.log10(max(rms, eps))


def apply_speech_ducking(music: np.ndarray, voice: np.ndarray, target_db: float = -24.0,
                        attack_seconds: float = 0.150, release_seconds: float = 0.400) -> np.ndarray:
    """Apply deterministic RMS-envelope ducking, calibrated in dB relative to narration."""
    music = np.asarray(music, dtype=np.float32)
    voice = np.asarray(voice, dtype=np.float32)
    length = min(music.shape[0], voice.shape[0])
    if length <= 0:
        return music
    music, voice = music[:length], voice[:length]

    window = max(1, int(0.050 * SR))
    mono_voice = voice.mean(axis=1)
    mono_music = music.mean(axis=1)
    padded_voice = np.pad(mono_voice, (window // 2, window // 2), mode="edge")
    voice_rms = np.sqrt(np.convolve(padded_voice * padded_voice,
                                     np.ones(window, dtype=np.float32) / window, mode="same")[:length])
    padded_music = np.pad(mono_music, (window // 2, window // 2), mode="edge")
    music_rms = np.sqrt(np.convolve(padded_music * padded_music,
                                     np.ones(window, dtype=np.float32) / window, mode="same")[:length])
    rms = voice_rms
    active_floor = max(float(np.percentile(rms, 20)), 1e-5)
    active = rms > max(active_floor * 2.5, 0.003)

    target_db = float(np.clip(target_db, -26.0, -22.0))
    ratio = 10.0 ** (float(np.clip(target_db, -26.0, -22.0)) / 20.0)
    desired = np.ones(length, dtype=np.float32)
    active_indices = np.flatnonzero(active)
    if active_indices.size:
        desired[active_indices] = np.clip(
            (voice_rms[active_indices] * ratio) / np.maximum(music_rms[active_indices], 1e-9),
            0.01, 1.0,
        )

    attack = max(1, int(attack_seconds * SR))
    release = max(1, int(release_seconds * SR))
    gains = np.ones(length, dtype=np.float32)
    current = 1.0
    for index in range(length):
        target = desired[index]
        if target < current:
            alpha = 1.0 / attack
        else:
            alpha = 1.0 / release
        current += (target - current) * alpha
        gains[index] = current

    pause_boost = np.ones(length, dtype=np.float32)
    pause_frames = int(0.600 * SR)
    i = 0
    while i < length:
        if active[i]:
            i += 1
            continue
        start = i
        while i < length and not active[i]:
            i += 1
        if i - start >= pause_frames:
            pause_boost[start:i] = 10.0 ** (6.0 / 20.0)
    return (music * gains[:, None] * pause_boost[:, None]).astype(np.float32)


class Track:
    """Disk-backed stereo timeline (keeps RAM flat for long 50-scene films)."""

    def __init__(self, path: Path, seconds: float):
        self.path = path
        self.n = int(math.ceil(seconds * SR)) + SR
        path.parent.mkdir(parents=True, exist_ok=True)
        self.buf = np.memmap(str(path.with_suffix(".f32")), dtype=np.float32, mode="w+", shape=(self.n, 2))

    def add(self, sig: np.ndarray, at: float, gain: float = 1.0, fade_in: float = 0.0, fade_out: float = 0.0) -> None:
        s = int(at * SR)
        if s >= self.n or sig.shape[0] == 0:
            return
        sig = sig[: self.n - s].astype(np.float32) * gain
        if fade_in > 0 or fade_out > 0:
            sig = sig.copy()
            fi, fo = min(sig.shape[0], int(fade_in * SR)), min(sig.shape[0], int(fade_out * SR))
            if fi:
                sig[:fi] *= np.linspace(0, 1, fi)[:, None]
            if fo:
                sig[-fo:] *= np.linspace(1, 0, fo)[:, None]
        self.buf[s: s + sig.shape[0]] += sig

    def add_loop(self, loop: np.ndarray, start: float, end: float, gain: float, fade: float = 1.0,
                 offset: float = 0.0) -> None:
        dur = max(0.0, end - start)
        if dur <= 0 or loop.shape[0] == 0:
            return
        n = int(dur * SR)
        off = int(offset * SR) % loop.shape[0]
        reps = (off + n) // loop.shape[0] + 1
        sig = np.tile(loop, (reps, 1))[off: off + n]
        self.add(sig, start, gain, fade, fade)

    def write(self, out: Path) -> Path:
        self.buf.flush()
        tmp = out.with_suffix(".tmp.wav")
        with wave.open(str(tmp), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(SR)
            step = SR * 30
            for i in range(0, self.n, step):
                w.writeframes((np.clip(self.buf[i: i + step], -1, 1) * 32767).astype("<i2").tobytes())
        tmp.replace(out)
        del self.buf
        try:
            self.path.with_suffix(".f32").unlink()
        except OSError:
            pass
        return out


def build_music(out: Path, total: float, mood: str, cues: List[Tuple[float, float, str]], seed: int = 1,
                user_music: Optional[Path] = None) -> Path:
    """Music bed: user track (looped) or generated score with per-scene intensity cues."""
    tr = Track(out, total)
    if user_music:
        loop = decode(user_music)
        tr.add_loop(loop, 0, total, 1.0, fade=2.0)
        return tr.write(out)
    harm_p, perc_p, _ = compose_score(mood, seed)
    harm, perc = decode(harm_p), decode(perc_p)
    for st, en, cue in cues:
        hg, pg = CUE_LEVELS.get(cue, CUE_LEVELS[""])
        if hg:
            tr.add_loop(harm, st, en + 0.8, hg, fade=0.8, offset=st)
        if pg and np.any(perc):
            tr.add_loop(perc, st, en + 0.8, pg * 0.9, fade=0.8, offset=st)
    return tr.write(out)


def mix_final(voice: Optional[Path], music: Optional[Path], fx: Optional[Path], out: Path, total: float,
              voice_vol: float = 1.0, music_vol: float = 0.35, fx_vol: float = 0.5, duck: float = 0.6,
              loudness: float = -14.0, duck_db: Optional[float] = None) -> Path:
    """Mix stems with deterministic speech-envelope ducking and EBU R128 mastering."""
    voice_audio = decode(voice) * float(voice_vol) if voice else None
    music_audio = decode(music) * float(music_vol) if music else None
    fx_audio = decode(fx) * float(fx_vol) if fx else None
    length = max(1, int(total * SR))

    if voice_audio is not None:
        tmp_voice = out.with_suffix(".voice-clean.wav")
        _write_pcm_wav(tmp_voice, voice_audio)
        cleaned = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(tmp_voice),
             "-af", "highpass=f=80,acompressor=threshold=0.125:ratio=2.5:attack=20:release=250:makeup=1",
             "-ar", str(SR), "-ac", "2", str(out.with_suffix(".voice.wav"))],
            capture_output=True,
            check=False,
        )
        tmp_voice.unlink(missing_ok=True)
        if cleaned.returncode:
            raise RuntimeError(f"voice cleanup failed: {cleaned.stderr[-400:]}")
        voice_clean_path = out.with_suffix(".voice.wav")
        voice_audio = decode(voice_clean_path)

    if music_audio is not None and voice_audio is not None:
        target_db = -24.0 if duck_db is None else float(duck_db)
        if duck_db is None:
            target_db = float(np.clip(-22.0 - 4.0 * float(duck), -26.0, -22.0))
        music_audio = apply_speech_ducking(music_audio, voice_audio, target_db=target_db)

    stems = [stem for stem in (voice_audio, music_audio, fx_audio) if stem is not None]
    if not stems:
        raise RuntimeError("nothing to mix")
    mixed = np.zeros((length, 2), dtype=np.float32)
    for stem in stems:
        n = min(length, stem.shape[0])
        mixed[:n] += stem[:n].astype(np.float32)
    premix = out.with_suffix(".premix.wav")
    _write_pcm_wav(premix, mixed)
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(premix),
           "-af", f"loudnorm=I={loudness}:TP=-1.5:LRA=11,alimiter=limit=0.95,atrim=0:{total:.3f}",
           "-ar", str(SR), "-ac", "2", str(out)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"audio mix failed: {res.stderr[-400:]}")
    return out
