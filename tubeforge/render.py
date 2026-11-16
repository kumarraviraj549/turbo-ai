"""Cinematic renderer.

Stage 1 (Python, per frame): virtual camera (sub-pixel homography: dolly/pan/tilt/orbit/crane/drone/handheld)
over a still image or video footage, plus procedural atmosphere (fog, rain, snow, embers, dust, light leaks).
Stage 2 (FFmpeg filter graph): colour grade, bloom, depth of field, chromatic aberration, vignette, film grain,
letterbox, text overlays and word-timed captions. Scenes are joined with xfade transitions."""
import math
import random
import re
import subprocess
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from . import cinema

LATIN_FONTS = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
]
SERIF_FONTS = [
    "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
    "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSerif-Bold.ttf",
]
UNICODE_FONTS = ["/Library/Fonts/Arial Unicode.ttf", "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"]
DEVANAGARI_FONTS = ["/System/Library/Fonts/Kohinoor.ttc", "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf"]
CJK_FONTS = ["/System/Library/Fonts/Hiragino Sans GB.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"]
_font_cache: Dict[tuple, ImageFont.FreeTypeFont] = {}


class RenderError(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


def load_font(size: int, text: str = "", serif: bool = False) -> ImageFont.FreeTypeFont:
    if any("\u0900" <= ch <= "\u097f" for ch in text):
        cands = UNICODE_FONTS + DEVANAGARI_FONTS + LATIN_FONTS
    elif any(ord(ch) >= 0x3000 for ch in text):
        cands = UNICODE_FONTS + CJK_FONTS + LATIN_FONTS
    elif any(ord(ch) > 0x024F for ch in text):
        cands = UNICODE_FONTS + DEVANAGARI_FONTS + CJK_FONTS + LATIN_FONTS
    else:
        cands = (SERIF_FONTS if serif else []) + LATIN_FONTS
    for c in cands:
        key = (c, size)
        if key in _font_cache:
            return _font_cache[key]
        if Path(c).exists():
            try:
                f = ImageFont.truetype(c, size)
                _font_cache[key] = f
                return f
            except Exception:
                continue
    return ImageFont.load_default()


# ----------------------------------------------------------------------------------------------- camera
def ease(kind: str, t: float) -> float:
    t = min(1.0, max(0.0, t))
    if kind == "in":
        return t * t * t
    if kind == "out":
        return 1 - (1 - t) ** 3
    if kind == "inout":
        return 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2
    return t


def _pair(move: dict, key: str, default: float) -> Tuple[float, float]:
    v = move.get(key)
    if v is None:
        return default, default
    if isinstance(v, (int, float)):
        return float(v), float(v)
    return float(v[0]), float(v[1])


def camera_quad(move: dict, t: float, time_s: float, W: int, H: int, intensity: float, seed: int,
                reverse: bool = False) -> List[Tuple[float, float]]:
    """Source-image quad (tl, tr, br, bl) that the output frame shows at normalized time t."""
    e = ease(move.get("ease", "inout"), 1 - t if reverse else t)
    k = max(0.0, intensity)
    z0, z1 = _pair(move, "z", 1.0)
    z = 1 + (z0 + (z1 - z0) * e - 1) * k
    x = sum(_pair(move, "x", 0.0)[i] * (1 - e if i == 0 else e) for i in (0, 1)) * min(1.0, k)
    y = sum(_pair(move, "y", 0.0)[i] * (1 - e if i == 0 else e) for i in (0, 1)) * min(1.0, k)
    r0, r1 = _pair(move, "rot", 0.0)
    rot = (r0 + (r1 - r0) * e) * k
    yw0, yw1 = _pair(move, "yaw", 0.0)
    yaw = (yw0 + (yw1 - yw0) * e) * k
    p0, p1 = _pair(move, "pitch", 0.0)
    pitch = (p0 + (p1 - p0) * e) * k
    shake = float(move.get("shake", 0.0)) * k
    rng = random.Random(seed)
    ph = [rng.random() * 6.283 for _ in range(6)]
    if shake:
        z = max(z, 1.0 + 0.06 * shake)
    z = max(1.0, z)
    w, h = W / z, H / z
    mx, my = (W - w) / 2, (H - h) / 2
    cx = W / 2 + x * mx * 0.94
    cy = H / 2 + y * my * 0.94
    if shake:
        amp = 0.0045 * W * shake
        ts = time_s
        cx += amp * (0.6 * math.sin(6.283 * 0.47 * ts + ph[0]) + 0.3 * math.sin(6.283 * 1.13 * ts + ph[1])
                     + 0.1 * math.sin(6.283 * 2.9 * ts + ph[2]))
        cy += amp * (0.6 * math.sin(6.283 * 0.39 * ts + ph[3]) + 0.3 * math.sin(6.283 * 1.31 * ts + ph[4])
                     + 0.1 * math.sin(6.283 * 3.3 * ts + ph[5]))
        rot += 0.35 * shake * math.sin(6.283 * 0.27 * ts + ph[2])
    hw, hh = w / 2, h / 2
    left_s = 1 - max(yaw, 0.0) * 2
    right_s = 1 - max(-yaw, 0.0) * 2
    top_s = 1 - max(pitch, 0.0) * 2
    bot_s = 1 - max(-pitch, 0.0) * 2
    pts = [(-hw * top_s, -hh * left_s), (hw * top_s, -hh * right_s), (hw * bot_s, hh * right_s),
           (-hw * bot_s, hh * left_s)]
    a = math.radians(rot)
    ca, sa = math.cos(a), math.sin(a)
    pts = [(px * ca - py * sa + cx, px * sa + py * ca + cy) for px, py in pts]
    # Keep the quad inside the source: shift, then shrink about its centre if still out of bounds.
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    dx = (-min(xs) if min(xs) < 0 else 0) + (W - max(xs) if max(xs) > W else 0)
    dy = (-min(ys) if min(ys) < 0 else 0) + (H - max(ys) if max(ys) > H else 0)
    pts = [(px + dx, py + dy) for px, py in pts]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    if min(xs) < 0 or max(xs) > W or min(ys) < 0 or max(ys) > H:
        qcx, qcy = sum(xs) / 4, sum(ys) / 4
        s = min(1.0, min(qcx, W - qcx) / max(1e-6, max(abs(px - qcx) for px in xs)),
                min(qcy, H - qcy) / max(1e-6, max(abs(py - qcy) for py in ys)))
        pts = [(qcx + (px - qcx) * s, qcy + (py - qcy) * s) for px, py in pts]
    return pts


def perspective_coeffs(out_w: int, out_h: int, quad: Sequence[Tuple[float, float]]) -> List[float]:
    src = [(0, 0), (out_w, 0), (out_w, out_h), (0, out_h)]
    A, B = [], []
    for (x, y), (u, v) in zip(src, quad):
        A.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        A.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        B.extend([u, v])
    return np.linalg.solve(np.array(A, dtype=np.float64), np.array(B, dtype=np.float64)).tolist()


def cover_resize(img: Image.Image, w: int, h: int) -> Image.Image:
    r = max(w / img.width, h / img.height)
    nw, nh = max(w, int(math.ceil(img.width * r))), max(h, int(math.ceil(img.height * r)))
    img = img.resize((nw, nh), Image.LANCZOS)
    x0, y0 = (nw - w) // 2, (nh - h) // 2
    return img.crop((x0, y0, x0 + w, y0 + h))


# ------------------------------------------------------------------------------------------ atmosphere
class Atmosphere:
    """Procedural particle / haze layer drawn at half resolution and upscaled (soft, cheap)."""

    def __init__(self, kind: str, w: int, h: int, fps: int, seed: int, intensity: float = 1.0):
        self.kind = kind if kind in cinema.ATMOSPHERES else "none"
        self.W, self.H = w, h
        self.w, self.h = max(2, w // 2), max(2, h // 2)
        self.fps = fps
        self.k = max(0.0, min(2.0, intensity))
        self.rng = np.random.default_rng(seed)
        n = {"rain": 420, "snow": 260, "embers": 110, "dust": 150, "fireflies": 55}.get(self.kind, 0)
        n = int(n * max(0.25, self.k))
        self.p = self.rng.random((n, 2)) * [self.w, self.h] if n else None
        self.size = self.rng.random(n) if n else None
        self.phase = self.rng.random(n) * 6.283 if n else None
        if self.kind == "fog":
            noise = (self.rng.random((max(4, self.h // 40), max(8, self.w // 20))) * 255).astype(np.uint8)
            tex = Image.fromarray(noise, "L").resize((self.w * 2, self.h), Image.BICUBIC)
            tex = tex.filter(ImageFilter.GaussianBlur(radius=max(6, self.h // 18)))
            grad = Image.linear_gradient("L").resize((self.w * 2, self.h))  # denser near the ground
            self.fog = ImageChops.multiply(tex, grad.point(lambda v: 90 + v * 0.65))
        if self.kind == "light_leaks":
            blob = Image.radial_gradient("L").resize((self.w, self.w)).point(lambda v: max(0, 255 - v) ** 2 // 255)
            self.blob = blob

    @property
    def additive(self) -> bool:
        return self.kind in ("embers", "dust", "light_leaks", "fireflies")

    def layer(self, frame_idx: int) -> Optional[Image.Image]:
        if self.kind == "none":
            return None
        t = frame_idx / float(self.fps)
        k = self.k
        w, h = self.w, self.h
        if self.kind == "fog":
            off = int((t * w * 0.035) % w)
            crop = self.fog.crop((off, 0, off + w, h))
            alpha = crop.point(lambda v: int(min(255, v * 0.55 * k)))
            lay = Image.new("RGBA", (w, h), (205, 210, 220, 0))
            lay.putalpha(alpha)
            return lay.resize((self.W, self.H), Image.BILINEAR)
        img = Image.new("RGB" if self.additive else "RGBA", (w, h), (0, 0, 0) if self.additive else (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        p, s, ph = self.p, self.size, self.phase
        dt = 1.0 / self.fps
        if self.kind == "rain":
            p[:, 1] += (h * 1.9 + s * h * 0.8) * dt
            p[:, 0] += h * 0.18 * dt
            for (x, y), sz in zip(p, s):
                ln = 6 + sz * 14
                d.line([(x, y), (x - ln * 0.12, y - ln)], fill=(200, 215, 235, int(70 + 80 * sz)), width=1)
        elif self.kind == "snow":
            p[:, 1] += (h * 0.08 + s * h * 0.07) * dt
            p[:, 0] += np.sin(t * 1.3 + ph) * 0.6
            for (x, y), sz in zip(p, s):
                r = 0.6 + sz * 2.2
                d.ellipse([x - r, y - r, x + r, y + r], fill=(245, 248, 255, int(150 + 90 * sz)))
        elif self.kind == "embers":
            p[:, 1] -= (h * 0.10 + s * h * 0.16) * dt
            p[:, 0] += np.sin(t * 2.1 + ph) * 0.9
            for (x, y), sz, f in zip(p, s, ph):
                flick = 0.55 + 0.45 * math.sin(t * 9 + f)
                r = 0.7 + sz * 2.0
                c = (int(255 * flick), int((110 + 80 * sz) * flick), int(30 * flick))
                d.ellipse([x - r * 2.2, y - r * 2.2, x + r * 2.2, y + r * 2.2], fill=(c[0] // 4, c[1] // 4, c[2] // 4))
                d.ellipse([x - r, y - r, x + r, y + r], fill=c)
        elif self.kind == "dust":
            p[:, 0] += (np.sin(t * 0.4 + ph) * 0.25 + 0.12)
            p[:, 1] += np.cos(t * 0.33 + ph) * 0.18 - 0.05
            for (x, y), sz, f in zip(p, s, ph):
                a = int((25 + 70 * sz) * (0.6 + 0.4 * math.sin(t * 1.7 + f)))
                r = 0.5 + sz * 1.6
                d.ellipse([x - r, y - r, x + r, y + r], fill=(a, int(a * 0.92), int(a * 0.8)))
        elif self.kind == "fireflies":
            p[:, 0] += np.sin(t * 0.8 + ph * 3) * 0.7
            p[:, 1] += np.cos(t * 0.6 + ph * 2) * 0.5
            for (x, y), sz, f in zip(p, s, ph):
                g = max(0.0, math.sin(t * 2.2 + f * 3))
                r = 1.2 + sz * 2.5
                c = (int(150 * g), int(230 * g), int(90 * g))
                d.ellipse([x - r * 3, y - r * 3, x + r * 3, y + r * 3], fill=(c[0] // 5, c[1] // 5, c[2] // 5))
                d.ellipse([x - r, y - r, x + r, y + r], fill=c)
        elif self.kind == "light_leaks":
            for i, (col, speed) in enumerate((((255, 120, 40), 0.07), ((255, 60, 120), 0.045))):
                pos = (t * speed + i * 0.5) % 1.6 - 0.3
                bx = int(pos * w - w / 2)
                by = int(h * (0.15 + 0.5 * i) - w / 2 + math.sin(t * 0.5 + i) * h * 0.1)
                a = 0.32 * k * (0.7 + 0.3 * math.sin(t * 0.9 + i * 2))
                tint = Image.new("RGB", self.blob.size, tuple(int(c * a) for c in col))
                patch = ImageChops.multiply(tint, self.blob.convert("RGB"))
                img.paste(ImageChops.add(img.crop((bx, by, bx + self.w, by + self.w)), patch), (bx, by))
        if self.p is not None:
            p[:, 0] %= w
            p[:, 1] %= h
        return img.resize((self.W, self.H), Image.BILINEAR)

    def apply(self, frame: Image.Image, frame_idx: int) -> Image.Image:
        lay = self.layer(frame_idx)
        if lay is None:
            return frame
        if self.additive:
            if self.k != 1.0 and self.kind != "light_leaks":
                lay = lay.point(lambda v: int(min(255, v * self.k)))
            return ImageChops.screen(frame, lay)
        base = frame.convert("RGBA")
        return Image.alpha_composite(base, lay).convert("RGB")


# ------------------------------------------------------------------------------------------- motion fx
def _depth(u: float, v: float) -> float:
    """Depth proxy (1 = near subject, 0 = far background): subjects sit near the centre / lower-middle."""
    return math.exp(-(((u - 0.5) ** 2) / 0.07 + ((v - 0.56) ** 2) / 0.11))


def _soft_sprite(w: int, h: int, color: Tuple[int, int, int], sharp_x: float, sharp_y: float) -> np.ndarray:
    xs = np.linspace(-1, 1, max(2, w), dtype=np.float32)
    ys = np.linspace(-1, 1, max(2, h), dtype=np.float32)
    g = np.exp(-(np.abs(xs)[None, :] * sharp_x) - (ys[:, None] ** 2) * sharp_y)
    return g[:, :, None] * np.array(color, dtype=np.float32)[None, None, :]


def _add_sprite(canvas: np.ndarray, spr: np.ndarray, cx: float, cy: float, gain: float) -> None:
    H, W = canvas.shape[:2]
    h, w = spr.shape[:2]
    x0, y0 = int(cx - w / 2), int(cy - h / 2)
    ax0, ay0, ax1, ay1 = max(0, x0), max(0, y0), min(W, x0 + w), min(H, y0 + h)
    if ax1 <= ax0 or ay1 <= ay0:
        return
    canvas[ay0:ay1, ax0:ax1] += spr[ay0 - y0:ay1 - y0, ax0 - x0:ax1 - x0] * gain


class MotionFX:
    """Per-frame animation effects: breathing zoom, 2.5D parallax (depth-weighted mesh warp, no layer ghosting),
    anamorphic lens-flare sweep and film gate weave."""

    def __init__(self, effect: str, W: int, H: int, fps: int, duration: float, seed: int, gate_weave: bool = False):
        self.effect, self.W, self.H, self.fps = effect or "None", W, H, fps
        self.dur = max(0.1, duration)
        self.weave = gate_weave
        rng = random.Random(seed * 7919 + 13)
        self.ph = [rng.random() * 6.283 for _ in range(4)]
        self.dir = 1 if rng.random() < 0.5 else -1
        self.grid = (16, 9) if W >= H else (9, 16)
        self.fw, self.fh = max(2, W // 2), max(2, H // 2)
        if self.effect == "Lens flare sweep":
            fw, fh = self.fw, self.fh
            self.streak = _soft_sprite(int(fw * 1.7), max(6, fh // 14), (110, 160, 255), 3.2, 9.0)
            core = max(8, fh // 3)
            self.core = _soft_sprite(core, core, (255, 236, 205), 6.0, 36.0) + \
                _soft_sprite(core * 2, core * 2, (255, 190, 120), 4.0, 16.0)[core // 2:core // 2 + core,
                                                                                 core // 2:core // 2 + core] * 0.4
            self.ghosts = [(_soft_sprite(g, g, c, 2.0, 4.0), f, 0.22)
                           for g, c, f in ((max(6, fh // 7), (90, 150, 255), 0.55), (max(6, fh // 4), (255, 160, 80), 1.25),
                                           (max(6, fh // 10), (150, 255, 170), 1.8))]

    @property
    def warps(self) -> bool:
        return self.effect == "Parallax depth"

    def adjust_quad(self, quad, time_s: float, SW: int, SH: int):
        s, dx, dy = 1.0, 0.0, 0.0
        if self.effect == "Subtle breathing zoom":
            s *= 1 - 0.028 * (0.5 - 0.5 * math.cos(6.283 * time_s / 4.2 + self.ph[0]))
        if self.weave:
            s *= 0.993
            dx = SW * 0.0010 * (math.sin(6.283 * 1.7 * time_s + self.ph[0]) + 0.5 * math.sin(6.283 * 4.3 * time_s + self.ph[1]))
            dy = SH * 0.0012 * (math.sin(6.283 * 2.3 * time_s + self.ph[2]) + 0.5 * math.sin(6.283 * 5.1 * time_s + self.ph[3]))
        if s == 1.0 and not dx and not dy:
            return quad
        cx, cy = sum(q[0] for q in quad) / 4, sum(q[1] for q in quad) / 4
        out = [(cx + (x - cx) * s + dx, cy + (y - cy) * s + dy) for x, y in quad]
        xs, ys = [q[0] for q in out], [q[1] for q in out]
        sx = (-min(xs) if min(xs) < 0 else 0) + (SW - max(xs) if max(xs) > SW else 0)
        sy = (-min(ys) if min(ys) < 0 else 0) + (SH - max(ys) if max(ys) > SH else 0)
        return [(x + sx, y + sy) for x, y in out]

    def warp(self, base: Image.Image, quad, t: float, SW: int, SH: int, resample) -> Image.Image:
        """2.5D parallax: the near subject drifts and pushes in faster than the far background."""
        W, H = self.W, self.H
        gx, gy = self.grid
        e = ease("inout", t)
        shift = 0.045 * SW * (e - 0.5) * self.dir
        push = 0.07 * e
        (x0, y0), (x1, y1), (x2, y2), (x3, y3) = quad
        qcx, qcy = (x0 + x1 + x2 + x3) / 4, (y0 + y1 + y2 + y3) / 4

        def src(u, v):
            tx, ty = x0 + (x1 - x0) * u, y0 + (y1 - y0) * u
            bx, by = x3 + (x2 - x3) * u, y3 + (y2 - y3) * u
            x, y = tx + (bx - tx) * v, ty + (by - ty) * v
            d = _depth(u, v)
            x = qcx + (x - qcx) * (1 - push * d) - shift * d
            y = qcy + (y - qcy) * (1 - push * d)
            return min(SW - 0.5, max(0.0, x)), min(SH - 0.5, max(0.0, y))

        mesh = []
        for j in range(gy):
            for i in range(gx):
                bx0, by0, bx1, by1 = i * W // gx, j * H // gy, (i + 1) * W // gx, (j + 1) * H // gy
                u0, v0, u1, v1 = bx0 / W, by0 / H, bx1 / W, by1 / H
                mesh.append(((bx0, by0, bx1, by1), src(u0, v0) + src(u0, v1) + src(u1, v1) + src(u1, v0)))
        return base.transform((W, H), Image.MESH, mesh, resample)

    def overlay(self, frame: Image.Image, t: float) -> Image.Image:
        if self.effect != "Lens flare sweep":
            return frame
        fw, fh = self.fw, self.fh
        env = math.sin(math.pi * min(1.0, max(0.0, t))) ** 0.6
        if env < 0.02:
            return frame
        px = fw * (0.5 + self.dir * (1.25 * t - 0.62))
        py = fh * (0.26 + 0.06 * math.sin(6.283 * t * 0.5 + self.ph[1]))
        canvas = np.zeros((fh, fw, 3), dtype=np.float32)
        _add_sprite(canvas, self.streak, px, py, 0.55 * env)
        _add_sprite(canvas, self.core, px, py, 0.9 * env)
        cx, cy = fw / 2, fh / 2
        for spr, f, g in self.ghosts:  # ghosts sit on the line through the frame centre, mirrored
            _add_sprite(canvas, spr, cx + (cx - px) * f * 0.6, cy + (cy - py) * f * 0.6, g * env)
        lay = Image.fromarray(np.clip(canvas, 0, 255).astype(np.uint8), "RGB").resize((self.W, self.H), Image.BILINEAR)
        return ImageChops.screen(frame, lay)


# --------------------------------------------------------------------------------------------- captions
@dataclass
class CaptionChunk:
    start: float
    end: float
    words: List[str]
    word_times: List[Tuple[float, float]] = field(default_factory=list)


def chunk_words(words: Sequence, max_words: int = 7, offset: float = 0.0, min_gap: float = 0.0) -> List[CaptionChunk]:
    """Groups timed words (objects with start/end/text) into caption chunks, breaking at punctuation."""
    chunks: List[CaptionChunk] = []
    cur: List = []
    for wd in words:
        cur.append(wd)
        txt = wd.text if hasattr(wd, "text") else wd["text"]
        if len(cur) >= max_words or (len(cur) >= 3 and txt[-1:] in ".,!?;:—"):
            chunks.append(cur)
            cur = []
    if cur:
        if chunks and len(cur) <= 2 and len(chunks[-1]) + len(cur) <= max_words + 2:
            chunks[-1].extend(cur)
        else:
            chunks.append(cur)
    out: List[CaptionChunk] = []

    def g(wd, k):
        return getattr(wd, k) if hasattr(wd, k) else wd[k]

    for i, c in enumerate(chunks):
        st = g(c[0], "start") + offset
        en = g(c[-1], "end") + offset + 0.25
        if i + 1 < len(chunks):
            en = min(en + 0.3, g(chunks[i + 1][0], "start") + offset)
        out.append(CaptionChunk(st, max(st + 0.4, en), [g(w, "text") for w in c],
                                [(g(w, "start") + offset, g(w, "end") + offset) for w in c]))
    return out


def _layout_words(words: List[str], font, max_w: int, space: int) -> List[List[Tuple[str, int]]]:
    lines: List[List[Tuple[str, int]]] = [[]]
    cur_w = 0
    for wd in words:
        ww = int(font.getlength(wd))
        if lines[-1] and cur_w + space + ww > max_w:
            lines.append([])
            cur_w = 0
        lines[-1].append((wd, ww))
        cur_w += (space if cur_w else 0) + ww
    return lines


def render_caption_png(words: List[str], style: str, W: int, H: int, active: int = -1) -> Tuple[Image.Image, int, int]:
    """Returns (RGBA image, x, y) for a caption chunk. active = index of highlighted word (-1 none)."""
    bold = style in ("Bold Social", "Karaoke")
    size = int(min(W, H) * (0.072 if style == "Bold Social" else 0.05 if style != "Minimal" else 0.036))
    text_all = " ".join(words)
    font = load_font(size, text_all, serif=(style == "Cinematic"))
    disp = [w.upper() for w in words] if style == "Bold Social" else list(words)
    space = int(font.getlength(" "))
    lines = _layout_words(disp, font, int(W * 0.84), space)
    asc, desc = font.getmetrics()
    lh = int((asc + desc) * 1.12)
    pad = int(size * 0.5)
    box_w = max(sum(ww for _, ww in ln) + space * (len(ln) - 1) for ln in lines) + pad * 2
    box_h = lh * len(lines) + pad * 2
    img = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    d = ImageDraw.Draw(img)
    if style == "Minimal":
        d.rounded_rectangle([0, 0, box_w - 1, box_h - 1], radius=int(size * 0.4), fill=(0, 0, 0, 120))
    idx = 0
    stroke = max(2, size // 14) if bold else max(1, size // 22)
    for li, ln in enumerate(lines):
        lw = sum(ww for _, ww in ln) + space * (len(ln) - 1)
        x = (box_w - lw) // 2
        y = pad + li * lh
        for wd, ww in ln:
            hi = idx == active
            fill = (255, 214, 74, 255) if hi and bold else (255, 255, 255, 255)
            if hi and style == "Karaoke":
                fill = (120, 220, 255, 255)
            sd.text((x + size // 16, y + size // 12), wd, font=font, fill=(0, 0, 0, 200))
            d.text((x, y), wd, font=font, fill=fill, stroke_width=stroke, stroke_fill=(0, 0, 0, 230))
            x += ww + space
            idx += 1
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=max(2, size // 10)))
    out = Image.alpha_composite(shadow, img)
    x = (W - box_w) // 2
    bottom = 0.86 if style != "Bold Social" else 0.74
    y = int(H * bottom - box_h / 2)
    return out, x, max(0, min(H - box_h, y))


def render_title_png(text: str, kind: str, W: int, H: int, accent=(56, 189, 248)) -> Tuple[Image.Image, int, int]:
    """Title / lower-third / chapter overlay. Returns (RGBA image, x, y)."""
    text = text.strip()
    if kind == "Lower third":
        size = int(min(W, H) * 0.045)
        font = load_font(size, text)
        tw = int(font.getlength(text))
        pad = size // 2
        img = Image.new("RGBA", (tw + pad * 3, size + pad * 2), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, img.width, img.height], fill=(8, 12, 24, 190))
        d.rectangle([0, 0, max(4, size // 7), img.height], fill=accent + (255,))
        d.text((pad * 2, pad - size // 10), text, font=font, fill=(255, 255, 255, 255))
        return img, int(W * 0.06), int(H * 0.72)
    if kind == "Chapter":
        size = int(min(W, H) * 0.034)
        font = load_font(size, text)
        t = text.upper()
        img = Image.new("RGBA", (int(font.getlength(t)) + size, size * 2), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.text((0, 0), t, font=font, fill=(255, 255, 255, 235), stroke_width=1, stroke_fill=(0, 0, 0, 200))
        d.rectangle([0, int(size * 1.35), int(size * 2.5), int(size * 1.35) + 3], fill=accent + (255,))
        return img, int(W * 0.06), int(H * 0.08)
    size = int(min(W, H) * (0.085 if kind == "Title" else 0.06))
    font = load_font(size, text, serif=True)
    lines = textwrap.wrap(text, width=max(10, int(W / (size * 0.55))))[:3]
    asc, desc = font.getmetrics()
    lh = int((asc + desc) * 1.1)
    bw = max(int(font.getlength(l)) for l in lines) + size
    bh = lh * len(lines) + size
    img = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd, d = ImageDraw.Draw(glow), ImageDraw.Draw(img)
    for i, l in enumerate(lines):
        x = (bw - int(font.getlength(l))) // 2
        y = size // 2 + i * lh
        gd.text((x, y), l, font=font, fill=accent + (210,))
        d.text((x, y), l, font=font, fill=(255, 255, 255, 255), stroke_width=max(1, size // 30),
               stroke_fill=(0, 0, 0, 160))
    glow = glow.filter(ImageFilter.GaussianBlur(radius=size // 5))
    out = Image.alpha_composite(glow, img)
    return out, (W - bw) // 2, (H - bh) // 2


def build_circular_logo_badge(logo_path: Optional[Any], channel_name: str, size: int = 56) -> Image.Image:
    """Creates a circular logo badge or monogram icon with a sleek cyan/neon glow border."""
    im = None
    if isinstance(logo_path, Image.Image):
        im = logo_path.convert("RGBA")
    elif logo_path and Path(str(logo_path)).exists():
        try:
            im = Image.open(str(logo_path)).convert("RGBA")
        except Exception:
            im = None
    if im is not None:
        try:
            dim = max(im.size)
            sq = Image.new("RGBA", (dim, dim), (0, 0, 0, 0))
            sq.paste(im, ((dim - im.width) // 2, (dim - im.height) // 2))
            sq = sq.resize((size, size), Image.LANCZOS)
            mask = Image.new("L", (size, size), 0)
            mdraw = ImageDraw.Draw(mask)
            mdraw.ellipse((0, 0, size - 1, size - 1), fill=255)
            out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            out.paste(sq, (0, 0), mask)
            od = ImageDraw.Draw(out)
            od.ellipse((0, 0, size - 1, size - 1), outline=(56, 189, 248, 240), width=2)
            return out
        except Exception:
            pass
    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(icon)
    draw.ellipse((0, 0, size - 1, size - 1), fill=(30, 27, 75, 235), outline=(56, 189, 248, 255), width=2)
    name = channel_name.strip()
    words = [w for w in re.split(r"[^A-Za-z0-9]+", name) if w]
    if len(words) >= 2:
        initials = (words[0][0] + words[1][0]).upper()
    elif words:
        initials = words[0][:2].upper()
    else:
        initials = "TF"
    fnt = load_font(int(size * 0.42), initials)
    bbox = draw.textbbox((0, 0), initials, font=fnt)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - tw) // 2, (size - th) // 2 - 2), initials, font=fnt, fill=(255, 255, 255, 255))
    return icon


def render_brand_watermark_png(width: int, height: int, is_short: bool,
                               logo_path: Optional[Path], channel_name: str,
                               brand_position: str = "Top-Right") -> Tuple[Image.Image, int, int]:
    """Renders a broadcast glassmorphic channel pill. Returns (RGBA Image, x, y)."""
    channel_name = channel_name.strip()
    has_brand = bool(channel_name) or bool(logo_path and Path(logo_path).exists())
    if not has_brand or brand_position in ("None", "Off", ""):
        return Image.new("RGBA", (1, 1), (0, 0, 0, 0)), 0, 0

    icon_size = 54 if is_short else 46
    icon_img = build_circular_logo_badge(logo_path, channel_name, icon_size)

    font_size = 22 if is_short else 20
    font = load_font(font_size, channel_name)
    bbox = font.getbbox(channel_name) if channel_name else (0, 0, 0, 0)
    text_w = (bbox[2] - bbox[0]) if channel_name else 0

    pad_x = 14
    pill_w = icon_size + (text_w + pad_x * 2 if channel_name else 8)
    pill_h = icon_size + 10

    canvas = Image.new("RGBA", (pill_w, pill_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    # Glassmorphic rounded pill with subtle neon outline
    draw.rounded_rectangle([0, 0, pill_w - 1, pill_h - 1], radius=pill_h // 2,
                           fill=(10, 14, 26, 185), outline=(139, 92, 246, 175), width=2)
    canvas.paste(icon_img, (5, 5), icon_img)

    if channel_name:
        ty = (pill_h - font_size) // 2 - 2
        draw.text((icon_size + 13, ty + 1), channel_name, font=font, fill=(0, 0, 0, 180))
        draw.text((icon_size + 12, ty), channel_name, font=font, fill=(249, 250, 251, 255))

    margin_x = int(width * 0.035)
    margin_y = int(height * 0.035)

    if "Top-Left" in brand_position:
        bx, by = margin_x, margin_y
    elif "Bottom-Left" in brand_position:
        bx, by = margin_x, height - margin_y - pill_h
    elif "Bottom-Right" in brand_position:
        bx, by = width - margin_x - pill_w, height - margin_y - pill_h
    else:  # Top-Right (default)
        bx, by = width - margin_x - pill_w, margin_y

    return canvas, bx, by


# ------------------------------------------------------------------------------------------ scene render
@dataclass
class SceneRenderSpec:
    out_path: Path
    width: int
    height: int
    fps: int
    duration: float
    source_type: str  # image | video
    source_path: Path
    camera: str = "Slow dolly in"
    intensity: float = 1.0
    reverse: bool = False
    grade: str = "Teal & Orange"
    atmosphere: str = "none"
    atmosphere_intensity: float = 1.0
    grain: float = 0.35
    vignette: float = 0.5
    bloom: float = 0.3
    letterbox: bool = False
    dof: float = 0.0
    chroma: float = 0.0
    sharpen: float = 0.3
    motion_blur: bool = False
    animation: str = "None"
    halation: float = 0.0
    gate_weave: bool = False
    overlay_png: Optional[Path] = None  # full-frame RGBA overlay (end card text), fades in
    caption_chunks: List[CaptionChunk] = field(default_factory=list)
    caption_style: str = "Cinematic"
    overlay_text: str = ""
    overlay_kind: str = "Title"
    overlay_start: float = 0.4
    overlay_end: float = 0.0
    seed: int = 1
    crf: int = 15
    preset: str = "veryfast"
    resample: str = "bilinear"  # bicubic is ~2.5x slower; visible only on very strong zooms
    channel_name: str = ""
    channel_logo_path: Optional[Path] = None
    brand_position: str = "Top-Right"
    brand_overlay: bool = True
    work_dir: Optional[Path] = None


def _video_frames(path: Path, W: int, H: int, fps: int, duration: float, slowmo: bool):
    """Yields RGB frames (W x H) decoded by FFmpeg, looping short footage."""
    speed = 0.5 if slowmo else 1.0
    vf = (f"setpts={1 / speed:.3f}*PTS,fps={fps},scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
          f"crop={W}:{H},format=rgb24")
    cmd = ["ffmpeg", "-v", "error", "-stream_loop", "-1", "-i", str(path), "-t", f"{duration + 0.5:.3f}", "-an",
           "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size = W * H * 3
    last = None
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            last = Image.frombuffer("RGB", (W, H), buf, "raw", "RGB", 0, 1)
            yield last
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()
    if last is None:
        raise RenderError(f"could not decode footage {path.name}")
    while True:
        yield last


def _dof_mask(W: int, H: int, strength: float, path: Path) -> Path:
    """Grey mask: 0 in the focal centre, 255 at the edges (where blur shows)."""
    if path.exists():
        return path
    small = Image.radial_gradient("L").resize((W // 4, H // 4), Image.BILINEAR)
    small = small.point(lambda v: int(max(0, min(255, (v - 70) * 2.2 * max(0.3, strength)))))
    small.resize((W, H), Image.BILINEAR).save(path)
    return path


def _look_filters(spec: SceneRenderSpec, mask_idx: Optional[int]) -> List[str]:
    """FFmpeg filter-graph segments for the film look, input [base] -> output [look]."""
    W, H = spec.width, spec.height
    segs = []
    cur = "[0:v]"
    chain = [cinema.GRADES.get(spec.grade, cinema.GRADES["Natural"])]
    if spec.motion_blur:
        chain.insert(0, "tmix=frames=3:weights='1 2 1'")
    if spec.animation == "Pulse glow":
        chain.append("eq=brightness='0.035*sin(2*PI*t/2.4)':saturation='1.06+0.06*sin(2*PI*t/2.4)':eval=frame")
    segs.append(f"{cur}{','.join(chain)}[g0]")
    cur = "[g0]"
    if spec.bloom > 0.01:
        segs.append(f"{cur}split[b1][b2];[b2]scale={max(2, W // 4)}:{max(2, H // 4)},"
                    f"curves=all='0/0 0.6/0.05 1/1',gblur=sigma={4 + 6 * spec.bloom:.1f},scale={W}:{H}[bb];"
                    f"[b1][bb]blend=all_mode=screen:all_opacity={min(0.6, 0.45 * spec.bloom + 0.1):.2f}[g1]")
        cur = "[g1]"
    if spec.halation > 0.01:  # film halation: warm red glow bleeding around bright highlights
        hk = min(1.0, spec.halation)
        segs.append(f"{cur}split[h1][h2];[h2]scale={max(2, W // 4)}:{max(2, H // 4)},curves=all='0/0 0.72/0 1/1',"
                    f"gblur=sigma={3 + 5 * hk:.1f},colorchannelmixer=rr=0.7:rg=0.25:rb=0.05:gr=0.16:gg=0.08:gb=0.02:"
                    f"br=0.03:bg=0.01:bb=0.01,scale={W}:{H}[hh];[h1][hh]blend=all_mode=screen:"
                    f"all_opacity={0.35 + 0.5 * hk:.2f}[gh]")
        cur = "[gh]"
    if spec.dof > 0.01 and mask_idx is not None:
        segs.append(f"{cur}split[d1][d2];[d2]gblur=sigma={3 + 9 * spec.dof:.1f}[dbl];"
                    f"[{mask_idx}:v]format=gray,scale={W}:{H}[dm];[dbl][dm]alphamerge[dba];[d1][dba]overlay=format=auto[g2]")
        cur = "[g2]"
    post = []
    if spec.chroma > 0.01:
        sh = max(1, int(W * 0.0012 * spec.chroma * 2))
        post.append(f"rgbashift=rh=-{sh}:bh={sh}")
    if spec.sharpen > 0.01:
        post.append(f"unsharp=5:5:{0.3 + 0.7 * spec.sharpen:.2f}")
    if spec.vignette > 0.01:
        post.append(f"vignette=angle={0.25 + 0.5 * spec.vignette:.3f}")
    if spec.grain > 0.01:
        post.append(f"noise=alls={int(4 + 16 * spec.grain)}:allf=t")
    if spec.letterbox and W > H:
        bar = max(0, int((H - W / 2.39) / 2))
        if bar > 2:
            post.append(f"drawbox=x=0:y=0:w=iw:h={bar}:color=black:t=fill,"
                        f"drawbox=x=0:y=ih-{bar}:w=iw:h={bar}:color=black:t=fill")
    post.append("format=yuv420p")
    segs.append(f"{cur}{','.join(post)}[look]")
    return segs


def render_scene(spec: SceneRenderSpec, progress: Optional[Callable[[float], None]] = None,
                 should_cancel: Optional[Callable[[], bool]] = None) -> Path:
    """Renders one scene clip (video only; audio is mixed at assembly)."""
    W, H, fps = spec.width, spec.height, spec.fps
    n_frames = max(1, int(round(spec.duration * fps)))
    move = cinema.CAMERA_MOVES.get(spec.camera, cinema.CAMERA_MOVES["Slow dolly in"])
    work = spec.work_dir or spec.out_path.parent
    work.mkdir(parents=True, exist_ok=True)
    zmax = max(_pair(move, "z", 1.0)) * max(1.0, spec.intensity)
    over = min(1.5, max(1.1, 1 + (zmax - 1) * 0.8))
    SW, SH = int(W * over) // 2 * 2, int(H * over) // 2 * 2
    intensity = spec.intensity
    if spec.source_type == "video":
        intensity *= 0.45  # footage already moves: keep the virtual camera subtle
        frames = _video_frames(spec.source_path, SW, SH, fps, spec.duration, bool(move.get("slowmo")))
        still = None
    else:
        src = Image.open(spec.source_path).convert("RGB")
        still = cover_resize(src, SW, SH)
        if still.width > src.width * 1.15:
            still = still.filter(ImageFilter.UnsharpMask(radius=2, percent=60, threshold=2))
        frames = None
    atmo = Atmosphere(spec.atmosphere, W, H, fps, spec.seed, spec.atmosphere_intensity)
    mfx = MotionFX(spec.animation, W, H, fps, spec.duration, spec.seed, spec.gate_weave)
    resample = Image.BICUBIC if spec.resample == "bicubic" else Image.BILINEAR

    inputs: List[str] = ["-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps), "-i", "-"]
    next_idx = 1
    mask_idx = None
    if spec.dof > 0.01:
        mask = _dof_mask(W, H, spec.dof, work / f"_dofmask_{W}x{H}_{int(spec.dof * 100)}.png")
        inputs += ["-loop", "1", "-framerate", str(fps), "-t", f"{spec.duration:.3f}", "-i", str(mask)]
        mask_idx = next_idx
        next_idx += 1
    segs = _look_filters(spec, mask_idx)
    cur = "[look]"
    overlay_n = 0
    stem = spec.out_path.stem

    def add_overlay(img: Image.Image, x: int, y: int, start: float, end: float, fade: float = 0.0):
        nonlocal next_idx, cur, overlay_n
        p = work / f"_{stem}_ov{overlay_n}.png"
        img.save(p)
        inputs.extend(["-loop", "1", "-framerate", str(fps), "-t", f"{spec.duration:.3f}", "-i", str(p)])
        src = f"[{next_idx}:v]format=rgba"
        if fade > 0:
            src += (f",fade=t=in:st={start:.3f}:d={fade:.2f}:alpha=1,"
                    f"fade=t=out:st={max(start, end - fade):.3f}:d={fade:.2f}:alpha=1")
        segs.append(f"{src}[ov{overlay_n}]")
        out = f"[o{overlay_n}]"
        segs.append(f"{cur}[ov{overlay_n}]overlay={x}:{y}:enable='between(t,{start:.3f},{end:.3f})'{out}")
        cur = out
        next_idx += 1
        overlay_n += 1

    if spec.overlay_png and Path(spec.overlay_png).exists():
        add_overlay(Image.open(spec.overlay_png).convert("RGBA"), 0, 0, 0.35, spec.duration, fade=0.8)
    if spec.overlay_text.strip():
        img, x, y = render_title_png(spec.overlay_text, spec.overlay_kind, W, H)
        end = spec.overlay_end if spec.overlay_end > spec.overlay_start else min(spec.duration - 0.2,
                                                                               spec.overlay_start + 3.5)
        add_overlay(img, x, y, spec.overlay_start, end, fade=0.5)
    if spec.brand_overlay and (spec.channel_name.strip() or (spec.channel_logo_path and Path(spec.channel_logo_path).exists())) and spec.brand_position not in ("None", "Off", ""):
        b_img, bx, by = render_brand_watermark_png(
            W, H, H > W,
            spec.channel_logo_path, spec.channel_name, spec.brand_position
        )
        if b_img.width > 1 and b_img.height > 1:
            add_overlay(b_img, bx, by, start=0.3, end=spec.duration, fade=0.4)
    if spec.caption_style != "None":
        for ch in spec.caption_chunks:
            st, en = max(0.0, ch.start), min(spec.duration, ch.end)
            if en - st < 0.05:
                continue
            if spec.caption_style in ("Bold Social", "Karaoke") and ch.word_times:
                for wi, (ws, we) in enumerate(ch.word_times):
                    a = st if wi == 0 else max(st, ws)
                    b = en if wi == len(ch.word_times) - 1 else max(a + 0.02, min(en, ch.word_times[wi + 1][0]))
                    img, x, y = render_caption_png(ch.words, spec.caption_style, W, H, active=wi)
                    add_overlay(img, x, y, a, b)
            else:
                img, x, y = render_caption_png(ch.words, spec.caption_style, W, H)
                add_overlay(img, x, y, st, en)
    if cur == "[look]":
        segs.append("[look]null[vout]")
    else:
        segs.append(f"{cur}format=yuv420p[vout]")
    tmp_out = spec.out_path.with_name(spec.out_path.stem + ".part.mp4")
    cmd = ["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(segs), "-map", "[vout]",
           "-frames:v", str(n_frames), "-c:v", "libx264", "-preset", spec.preset, "-crf", str(spec.crf),
           "-pix_fmt", "yuv420p", "-r", str(fps), "-movflags", "+faststart", str(tmp_out)]
    err_path = work / f"_{stem}_ffmpeg.log"
    with open(err_path, "wb") as errf:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=errf)
        try:
            for i in range(n_frames):
                if should_cancel and i % 10 == 0 and should_cancel():
                    raise Cancelled("render cancelled")
                base = next(frames) if frames is not None else still
                tn = i / max(1, n_frames - 1)
                quad = camera_quad(move, tn, i / fps, SW, SH, intensity, spec.seed, spec.reverse)
                quad = mfx.adjust_quad(quad, i / fps, SW, SH)
                if mfx.warps:
                    frame = mfx.warp(base, quad, tn, SW, SH, resample)
                else:
                    frame = base.transform((W, H), Image.PERSPECTIVE, perspective_coeffs(W, H, quad), resample)
                frame = atmo.apply(frame, i)
                frame = mfx.overlay(frame, tn)
                proc.stdin.write(frame.tobytes())
                if progress and i % 12 == 0:
                    progress(i / n_frames)
            proc.stdin.close()
            rc = proc.wait()
        except (BrokenPipeError, Cancelled) as e:
            proc.kill()
            proc.wait()
            if isinstance(e, Cancelled):
                raise
            rc = -1
        finally:
            if frames is not None:
                frames.close()
    if rc != 0 or not tmp_out.exists():
        msg = err_path.read_text(errors="ignore")[-600:] if err_path.exists() else ""
        raise RenderError(f"FFmpeg scene render failed: {msg}")
    tmp_out.replace(spec.out_path)
    for p in work.glob(f"_{stem}_ov*.png"):
        p.unlink()
    err_path.unlink(missing_ok=True)
    if progress:
        progress(1.0)
    return spec.out_path


# ----------------------------------------------------------------------------------------------- assembly
def timeline(durations: Sequence[float], trans: Sequence[float]) -> List[float]:
    """Start time of each clip when clip i overlaps clip i+1 by trans[i] seconds."""
    starts, t = [], 0.0
    for i, d in enumerate(durations):
        starts.append(t)
        t += d - (trans[i] if i < len(trans) else 0.0)
    return starts


def total_length(durations: Sequence[float], trans: Sequence[float]) -> float:
    return sum(durations) - sum(trans[: max(0, len(durations) - 1)])


def _ffmpeg_reason(stderr: str) -> str:
    """The lines that explain an FFmpeg failure (the tail alone is usually generic 'Invalid argument')."""
    lines = [ln for ln in stderr.splitlines() if ln.strip()]
    key = [ln for ln in lines if re.search(r"do not match|No such|Invalid|Error|error|failed|not found", ln)]
    return "\n".join((key[:3] or lines[-4:]))[-600:]


def _ffmpeg_interrupted(stderr: str) -> bool:
    compact = re.sub(r"[^a-z0-9]", "", stderr.casefold())
    return "immediateexitrequested" in compact or "1414092869" in compact


def _run_ffmpeg(cmd: List[str], should_cancel: Optional[Callable[[], bool]] = None) -> subprocess.CompletedProcess:
    if should_cancel is None:
        return subprocess.run(cmd, capture_output=True, text=True)

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    while True:
        if should_cancel():
            if proc.poll() is None:
                proc.terminate()
            proc.communicate()
            raise Cancelled("video assembly cancelled")
        try:
            _, stderr = proc.communicate(timeout=0.25)
            return subprocess.CompletedProcess(cmd, proc.returncode, "", stderr)
        except subprocess.TimeoutExpired:
            continue


def _xfade_group(clips: List[Path], durs: List[float], trans: List[Tuple[str, float]], out: Path, fps: int,
                 crf: int, preset: str, size: Optional[Tuple[int, int]] = None,
                 should_cancel: Optional[Callable[[], bool]] = None) -> float:
    if len(clips) == 1 and size is None:
        if should_cancel and should_cancel():
            raise Cancelled("video assembly cancelled")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(clips[0]), "-c", "copy", str(out)], check=True)
        return durs[0]
    inputs: List[str] = []
    for c in clips:
        inputs += ["-i", str(c)]
    segs = []
    # Every input is conformed to one size / pixel format / SAR, so a stray clip can never break xfade.
    norm = (f"scale={size[0]}:{size[1]}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={size[0]}:{size[1]}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p,") if size else "format=yuv420p,"
    segs.append(f"[0:v]{norm}null[s0]")
    prev = "[s0]"
    off = 0.0
    acc = durs[0]
    for i in range(1, len(clips)):
        name, td = trans[i - 1]
        td = max(0.04, min(td, durs[i - 1] - 0.1, durs[i] - 0.1))
        off = acc - td
        lab = f"[x{i}]"
        segs.append(f"{prev}settb=AVTB,fps={fps}[p{i}];[{i}:v]{norm}settb=AVTB,fps={fps}[n{i}];"
                    f"[p{i}][n{i}]xfade=transition={name}:duration={td:.3f}:offset={off:.3f}{lab}")
        prev = lab
        acc = off + durs[i]
    segs.append(f"{prev}format=yuv420p[vout]")
    cmd = ["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(segs), "-map", "[vout]", "-c:v",
           "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p", "-r", str(fps), str(out)]
    res = _run_ffmpeg(cmd, should_cancel)
    if res.returncode != 0:
        if (should_cancel and should_cancel()) or _ffmpeg_interrupted(res.stderr):
            raise Cancelled("video assembly cancelled")
        raise RenderError(f"FFmpeg transition assembly failed: {_ffmpeg_reason(res.stderr)}")
    return acc


def assemble_video(clips: List[Path], durations: List[float], transitions: List[Tuple[str, float]], out: Path,
                   fps: int, work: Path, group: int = 8, size: Optional[Tuple[int, int]] = None,
                   should_cancel: Optional[Callable[[], bool]] = None) -> float:
    """Joins scene clips with per-boundary xfade transitions (hierarchically, to bound memory)."""
    if not clips:
        raise RenderError("no clips to assemble")
    work.mkdir(parents=True, exist_ok=True)
    level_clips, level_durs, level_trans = list(clips), list(durations), list(transitions)
    level = 0
    while len(level_clips) > 1:
        nxt_clips, nxt_durs, nxt_trans = [], [], []
        for gi in range(0, len(level_clips), group):
            if should_cancel and should_cancel():
                raise Cancelled("video assembly cancelled")
            cl = level_clips[gi: gi + group]
            du = level_durs[gi: gi + group]
            tr = level_trans[gi: gi + len(cl) - 1]
            outp = work / f"_asm_L{level}_{gi // group:03d}.mp4"
            d = _xfade_group(cl, du, tr, outp, fps, 14, "veryfast", size if level == 0 else None,
                             should_cancel)
            nxt_clips.append(outp)
            nxt_durs.append(d)
            if gi + group < len(level_clips):
                nxt_trans.append(level_trans[gi + len(cl) - 1])
        if level > 0:
            for c in level_clips:
                Path(c).unlink(missing_ok=True)
        level_clips, level_durs, level_trans = nxt_clips, nxt_durs, nxt_trans
        level += 1
        if len(level_clips) == 1:
            break
    final = Path(level_clips[0])
    if final != out:
        if level == 0:
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(final), "-c", "copy", str(out)], check=True)
        else:
            final.replace(out)
    return level_durs[0]


def export_final(video_in: Path, audio_in: Optional[Path], out: Path, width: int, height: int, fps: int,
                 codec: str, crf: int, preset: str, fade_in: float = 0.6, fade_out: float = 1.2,
                 duration: Optional[float] = None) -> Path:
    """Final encode: optional upscale (4K), fade from/to black, audio mux, chosen container/codec."""
    vf = []
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0", str(video_in)], capture_output=True, text=True)
    try:
        iw, ih = [int(v) for v in probe.stdout.strip().split(",")[:2]]
    except Exception:
        iw, ih = width, height
    if (iw, ih) != (width, height):
        vf.append(f"scale={width}:{height}:flags=lanczos,unsharp=5:5:0.6")
    if duration is None:
        duration = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                                         "default=nw=1:nk=1", str(video_in)], capture_output=True,
                                        text=True).stdout.strip() or 0)
    if fade_in > 0:
        vf.append(f"fade=t=in:st=0:d={fade_in}")
    if fade_out > 0 and duration > fade_out * 2:
        vf.append(f"fade=t=out:st={duration - fade_out:.3f}:d={fade_out}")
    vf.append("format=yuv420p")
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(video_in)]
    if audio_in:
        cmd += ["-i", str(audio_in)]
    cmd += ["-vf", ",".join(vf), "-map", "0:v:0"]
    if audio_in:
        cmd += ["-map", "1:a:0"]
    if codec == "libvpx-vp9":
        cmd += ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", str(min(40, crf + 12)), "-row-mt", "1", "-deadline", "good",
                "-cpu-used", "4"]
        if audio_in:
            cmd += ["-c:a", "libopus", "-b:a", "192k"]
    else:
        cmd += ["-c:v", codec, "-preset", preset, "-crf", str(crf + (2 if codec == "libx265" else 0))]
        if codec == "libx265":
            cmd += ["-tag:v", "hvc1"]
        if audio_in:
            cmd += ["-c:a", "aac", "-b:a", "256k"]
    cmd += ["-r", str(fps), "-t", f"{duration:.3f}"]
    if out.suffix in (".mp4", ".mov"):
        cmd += ["-movflags", "+faststart"]
    cmd.append(str(out))
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RenderError(f"FFmpeg export failed: {res.stderr[-500:]}")
    return out


def make_thumbnail(image_path: Path, title: str, out: Path, W: int = 1280, H: int = 720,
                   channel_name: str = "", logo_path: Optional[Path] = None, hook: str = "",
                   variant: int = 0) -> Path:
    img = cover_resize(Image.open(image_path).convert("RGB"), W, H)
    img = ImageEnhance.Contrast(img).enhance(1.12)
    img = ImageEnhance.Color(img).enhance(1.2)
    img = ImageEnhance.Sharpness(img).enhance(1.4)
    shade = Image.linear_gradient("L").resize((W, H)).point(lambda v: int(v * 0.75))
    dark = Image.new("RGB", (W, H), (0, 0, 0))
    img = Image.composite(dark, img, shade)
    base = img.convert("RGBA")
    draw = ImageDraw.Draw(base)
    accents = [(255, 213, 74), (83, 220, 244), (255, 126, 112)]
    accent = accents[variant % len(accents)]
    label = textwrap.shorten(re.sub(r"\s+", " ", title.strip()), width=34, placeholder="…").upper()
    label_font = load_font(max(18, int(H * 0.033)), label)
    label_w = min(int(label_font.getlength(label)), int(W * 0.7))
    label_h = int(label_font.size * 1.65)
    label_x, label_y = int(W * 0.055), int(H * 0.075)
    draw.rounded_rectangle((label_x, label_y, label_x + label_w + 30, label_y + label_h),
                           radius=label_h // 4, fill=accent)
    draw.text((label_x + 15, label_y + (label_h - label_font.size) // 2 - 2), label,
              font=label_font, fill=(12, 16, 28, 255))

    copy = re.sub(r"\s+", " ", (hook or title).strip())
    copy = re.split(r"(?<=[.!?])\s+|\n+", copy, maxsplit=1)[0]
    copy = textwrap.shorten(copy, width=52, placeholder="…")
    copy_font_size = int(H * 0.115)
    lines = textwrap.wrap(copy, width=20, max_lines=3, placeholder="…") or [copy]
    max_width = int(W * 0.76)
    while copy_font_size > 32:
        copy_font = load_font(copy_font_size, copy)
        if max(int(copy_font.getlength(line)) for line in lines) <= max_width:
            break
        copy_font_size -= 2
    copy_font = load_font(copy_font_size, copy)
    line_height = int(copy_font_size * 1.12)
    copy_y = int(H * 0.52)
    for i, line in enumerate(lines):
        y = copy_y + i * line_height
        fill = accent + (255,) if i == len(lines) - 1 else (255, 255, 255, 255)
        draw.text((int(W * 0.055) + 3, y + 4), line, font=copy_font, fill=(0, 0, 0, 220),
                  stroke_width=5, stroke_fill=(0, 0, 0, 220))
        draw.text((int(W * 0.055), y), line, font=copy_font, fill=fill,
                  stroke_width=3, stroke_fill=(10, 12, 20, 230))
    draw.rounded_rectangle((int(W * 0.055), copy_y - 16, int(W * 0.055) + 12, copy_y - 4),
                           radius=6, fill=accent)
    if channel_name.strip() or (logo_path and Path(logo_path).exists()):
        b_img, bx, by = render_brand_watermark_png(W, H, False, logo_path, channel_name, "Top-Right")
        if b_img.width > 1 and b_img.height > 1:
            base.alpha_composite(b_img, (bx, by))
    base.convert("RGB").save(out, "JPEG", quality=92)
    return out


def write_srt(chunks: List[CaptionChunk], path: Path) -> Path:
    def ts(s: float) -> str:
        ms = int(round(s * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

    lines = []
    for i, c in enumerate(chunks, 1):
        lines += [str(i), f"{ts(c.start)} --> {ts(c.end)}", " ".join(c.words), ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def end_card_background(src: Path, out: Path, W: int, H: int) -> Path:
    """Blurred, darkened backdrop for the end card (from the last scene's image or frame)."""
    img = cover_resize(Image.open(src).convert("RGB"), W, H)
    img = img.filter(ImageFilter.GaussianBlur(radius=max(8, W // 70)))
    img = ImageEnhance.Brightness(ImageEnhance.Color(img).enhance(1.15)).enhance(0.42)
    img.save(out, "JPEG", quality=94)
    return out


def render_end_card_png(title: str, W: int, H: int, channel_name: str = "", logo_path: Optional[Path] = None,
                        cta: str = "", accent=(56, 189, 248)) -> Image.Image:
    """Full-frame RGBA end card: 'Thanks for watching', film title, subscribe button and channel badge."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = min(W, H)
    small = load_font(int(m * 0.032), "THANKS FOR WATCHING")
    head = "THANKS FOR WATCHING"
    spaced = " ".join(head)
    d.text(((W - small.getlength(spaced)) / 2, H * 0.24), spaced, font=small, fill=(255, 255, 255, 200))
    ttl, _, _ = render_title_png(title or "The End", "Title", W, H, accent)
    img.alpha_composite(ttl, ((W - ttl.width) // 2, int(H * 0.40 - ttl.height / 2)))
    y = int(H * 0.60)
    if channel_name.strip() or (logo_path and Path(logo_path).exists()):
        bs = int(m * 0.15)
        badge = build_circular_logo_badge(logo_path, channel_name, bs)
        f2 = load_font(int(m * 0.04), channel_name)
        name = channel_name.strip()
        nw = int(f2.getlength(name)) if name else 0
        bf = load_font(int(m * 0.036), "SUBSCRIBE")
        bt = "SUBSCRIBE"
        bw, bh = int(bf.getlength(bt) + m * 0.07), int(m * 0.075)
        total = bs + int(m * 0.03) + max(nw, bw)
        x0 = (W - total) // 2
        img.alpha_composite(badge.convert("RGBA").resize((bs, bs)), (x0, y))
        tx = x0 + bs + int(m * 0.03)
        if name:
            d.text((tx, y - int(m * 0.004)), name, font=f2, fill=(255, 255, 255, 255))
        by = y + int(m * 0.065)
        d.rounded_rectangle([tx, by, tx + bw, by + bh], radius=bh // 5, fill=(230, 33, 23, 255))
        d.text((tx + (bw - bf.getlength(bt)) / 2, by + (bh - bf.size) / 2 - bf.size * 0.12), bt, font=bf,
               fill=(255, 255, 255, 255))
        y += bs + int(m * 0.05)
    cta = cta.strip() or "Like  •  Share  •  Comment what happens next"
    f3 = load_font(int(m * 0.03), cta)
    d.text(((W - f3.getlength(cta)) / 2, y), cta, font=f3, fill=(255, 255, 255, 215))
    return img


def _has_audio(path: Path) -> bool:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of",
                        "csv=p=0", str(path)], capture_output=True, text=True)
    return bool(r.stdout.strip())


def make_vertical_teaser(video_in: Path, out: Path, title: str, seconds: float, channel_name: str = "",
                         height: int = 1920, crf: int = 20, preset: str = "medium") -> Path:
    """9:16 Shorts / Reels / TikTok teaser: blurred full-bleed backdrop, the film framed in the middle,
    a bold hook title on top and a 'full film' call to action at the bottom."""
    H = 1920 if height >= 1080 else 1280
    W = H * 9 // 16
    seconds = max(3.0, seconds)
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    size = int(W * 0.085)
    max_w = W * 0.88
    while True:  # shrink / re-wrap until every line fits inside the safe area
        font = load_font(size, title)
        words, lines = title.upper().split(), []
        for wd in words:
            if lines and font.getlength(lines[-1] + " " + wd) <= max_w:
                lines[-1] += " " + wd
            else:
                lines.append(wd)
        if (len(lines) <= 3 and all(font.getlength(ln) <= max_w for ln in lines)) or size <= int(W * 0.04):
            break
        size = int(size * 0.92)
    lines = lines[:3]
    y = int(H * 0.11)
    for line in lines:
        lw = font.getlength(line)
        d.text(((W - lw) / 2, y), line, font=font, fill=(255, 255, 255, 255), stroke_width=max(2, size // 12),
               stroke_fill=(0, 0, 0, 230))
        y += int(size * 1.15)
    cta = "▶ Full film on " + (channel_name.strip() or "our channel")
    f2 = load_font(int(W * 0.05), cta)
    cw, ch = f2.getlength(cta), int(W * 0.05)
    bx0, by0 = (W - cw) / 2 - ch * 0.6, H * 0.80
    d.rounded_rectangle([bx0, by0, bx0 + cw + ch * 1.2, by0 + ch * 1.9], radius=int(ch * 0.5), fill=(230, 33, 23, 235))
    d.text(((W - cw) / 2, by0 + ch * 0.38), cta, font=f2, fill=(255, 255, 255, 255))
    ov_path = out.with_name(out.stem + "_overlay.png")
    ov.save(ov_path)
    fc = (f"[0:v]trim=0:{seconds:.3f},setpts=PTS-STARTPTS,split[a][b];"
          f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},gblur=sigma=28,"
          f"eq=brightness=-0.10:saturation=1.25[bg];[b]scale={W}:-2:flags=lanczos[fg];"
          f"[bg][fg]overlay=(W-w)/2:(H-h)/2[v0];[v0][1:v]overlay=0:0:shortest=1,"
          f"fade=t=out:st={seconds - 0.8:.3f}:d=0.8,format=yuv420p[v]")
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(video_in), "-loop", "1", "-i", str(ov_path),
           "-filter_complex", fc, "-map", "[v]"]
    if _has_audio(video_in):
        cmd += ["-map", "0:a:0", "-af", f"atrim=0:{seconds:.3f},afade=t=out:st={seconds - 1.0:.3f}:d=1.0",
                "-c:a", "aac", "-b:a", "192k"]
    cmd += ["-t", f"{seconds:.3f}", "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(out)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    ov_path.unlink(missing_ok=True)
    if res.returncode != 0:
        raise RenderError(f"Shorts teaser failed: {res.stderr[-400:]}")
    return out
