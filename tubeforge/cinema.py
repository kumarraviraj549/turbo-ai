"""Cinematic vocabulary: visual styles, camera moves, lighting, colour grades, atmosphere, transitions,
subtitle styles and export presets. Used by the planner (allowed values) and the renderer (parameters)."""
from typing import Dict, List

STYLES: Dict[str, Dict] = {
    "Hollywood Blockbuster": {"prompt": "epic hollywood blockbuster film still, anamorphic lens, dramatic volumetric lighting, "
                              "highly detailed, cinematic composition, shot on ARRI Alexa 65",
                              "grade": "Teal & Orange", "atmosphere": "dust", "music": "epic"},
    "Hyper-Realistic": {"prompt": "hyper-realistic photograph, natural light, 85mm lens, sharp focus, true-to-life textures, 8k detail",
                        "grade": "Natural", "atmosphere": "none", "music": "uplifting"},
    "Pixar-Style 3D": {"prompt": "3D animated feature film still, stylized characters, soft global illumination, subsurface "
                       "scattering, vibrant colours, high quality render", "grade": "Vibrant", "atmosphere": "none",
                       "music": "uplifting"},
    "Anime": {"prompt": "high quality anime key visual, detailed cel shading, dramatic sky, studio-quality illustration",
              "grade": "Vibrant", "atmosphere": "light_leaks", "music": "uplifting"},
    "Sci-Fi Cyberpunk": {"prompt": "cyberpunk sci-fi film still, neon reflections, rain-slick streets, holograms, moody "
                         "volumetric haze, blade runner aesthetic", "grade": "Neon Noir", "atmosphere": "rain",
                         "music": "dark"},
    "Epic Fantasy": {"prompt": "epic fantasy film still, sweeping landscapes, magical light rays, ornate details, "
                     "lord of the rings cinematography", "grade": "Golden Hour", "atmosphere": "embers", "music": "epic"},
    "Dark Fantasy": {"prompt": "dark fantasy film still, gothic atmosphere, low-key lighting, mist, ominous mood",
                     "grade": "Bleach Bypass", "atmosphere": "fog", "music": "dark"},
    "Documentary": {"prompt": "documentary photography, authentic, natural light, photojournalistic composition",
                    "grade": "Natural", "atmosphere": "none", "music": "mysterious"},
    "Nature Documentary": {"prompt": "nature documentary still, telephoto lens, wildlife and landscapes, golden light, "
                           "national geographic quality", "grade": "Golden Hour", "atmosphere": "dust",
                           "music": "uplifting"},
    "Film Noir": {"prompt": "film noir still, high contrast black and white, hard shadows, venetian blind light, 1940s",
                  "grade": "Noir B&W", "atmosphere": "fog", "music": "mysterious"},
    "Horror": {"prompt": "horror film still, unsettling atmosphere, low-key lighting, desaturated, fog, dread",
               "grade": "Cold Thriller", "atmosphere": "fog", "music": "dark"},
    "Historical Epic": {"prompt": "historical epic film still, period-accurate costumes and architecture, painterly light",
                        "grade": "Vintage Film", "atmosphere": "dust", "music": "epic"},
    "Retro Synthwave": {"prompt": "retro 80s synthwave aesthetic, neon grid, sunset gradient, chrome, VHS glow",
                        "grade": "Neon Noir", "atmosphere": "light_leaks", "music": "dark"},
    "Watercolor Storybook": {"prompt": "watercolor storybook illustration, soft washes, paper texture, whimsical",
                             "grade": "Soft Pastel", "atmosphere": "none", "music": "uplifting"},
    "Luxury Commercial": {"prompt": "luxury commercial photography, studio lighting, glossy reflections, premium product look",
                          "grade": "Clean Commercial", "atmosphere": "light_leaks", "music": "uplifting"},
}

# Camera moves: z = zoom start/end (>=1 crops into the overscanned frame), x/y = pan (fraction of the free margin,
# -1..1), rot = degrees, yaw/pitch = keystone (fake 3D), shake = handheld amplitude, ease = easing curve.
CAMERA_MOVES: Dict[str, Dict] = {
    "Static (locked off)": {"z": (1.04, 1.04), "ease": "linear"},
    "Slow dolly in": {"z": (1.0, 1.18), "ease": "inout"},
    "Dolly out (reveal)": {"z": (1.22, 1.0), "ease": "inout"},
    "Push-in zoom (dramatic)": {"z": (1.02, 1.35), "ease": "in"},
    "Pull-back zoom": {"z": (1.35, 1.02), "ease": "out"},
    "Pan left": {"z": (1.15, 1.15), "x": (0.85, -0.85), "ease": "inout"},
    "Pan right": {"z": (1.15, 1.15), "x": (-0.85, 0.85), "ease": "inout"},
    "Tilt up": {"z": (1.18, 1.18), "y": (0.85, -0.85), "ease": "inout"},
    "Tilt down": {"z": (1.18, 1.18), "y": (-0.85, 0.85), "ease": "inout"},
    "Tracking shot left": {"z": (1.2, 1.24), "x": (0.9, -0.9), "yaw": (0.02, 0.02), "ease": "linear"},
    "Tracking shot right": {"z": (1.2, 1.24), "x": (-0.9, 0.9), "yaw": (-0.02, -0.02), "ease": "linear"},
    "Crane up": {"z": (1.2, 1.08), "y": (0.8, -0.6), "pitch": (0.03, -0.01), "ease": "inout"},
    "Crane down": {"z": (1.08, 1.2), "y": (-0.6, 0.8), "pitch": (-0.01, 0.03), "ease": "inout"},
    "Drone flyover": {"z": (1.12, 1.32), "y": (0.6, -0.4), "pitch": (0.045, 0.02), "rot": (-0.6, 0.6), "ease": "linear"},
    "Orbit": {"z": (1.22, 1.22), "x": (-0.7, 0.7), "yaw": (0.05, -0.05), "rot": (0.8, -0.8), "ease": "inout"},
    "Handheld": {"z": (1.1, 1.12), "shake": 1.0, "ease": "linear"},
    "Close-up reveal": {"z": (1.45, 1.12), "y": (0.3, 0.0), "ease": "out"},
    "Establishing wide": {"z": (1.12, 1.0), "y": (-0.3, 0.0), "ease": "out"},
    "Ken Burns diagonal": {"z": (1.05, 1.25), "x": (-0.6, 0.5), "y": (0.5, -0.4), "ease": "inout"},
    "Dutch tilt drift": {"z": (1.2, 1.25), "rot": (-3.0, -1.5), "x": (0.3, -0.3), "ease": "inout"},
    "Slow motion drift": {"z": (1.06, 1.12), "x": (-0.25, 0.25), "ease": "inout", "slowmo": True},
    "Vertigo (dolly zoom)": {"z": (1.0, 1.3), "pitch": (0.0, 0.025), "yaw": (0.0, 0.0), "ease": "in"},
}
CAMERA_ANGLES = ["Eye level", "Low angle", "High angle", "Bird's-eye", "Worm's-eye", "Over-the-shoulder",
                 "Dutch angle", "Point of view", "Aerial"]
SHOT_TYPES = ["Extreme wide", "Wide establishing", "Full shot", "Medium shot", "Medium close-up", "Close-up",
              "Extreme close-up", "Two-shot", "Insert detail"]
LIGHTING = ["Golden hour", "Blue hour", "Volumetric god rays", "Neon practicals", "Moonlight", "Candlelight",
            "Rim lighting", "Low-key chiaroscuro", "High-key soft", "Overcast diffuse", "Harsh midday sun",
            "Fire glow", "Bioluminescent", "Studio three-point"]

# FFmpeg filter snippets for colour grades (curves/eq/colorbalance only; all verified available).
GRADES: Dict[str, str] = {
    "Natural": "eq=contrast=1.04:saturation=1.05",
    "Teal & Orange": "colorbalance=rs=-0.08:gs=-0.02:bs=0.10:rh=0.10:gh=0.02:bh=-0.08,eq=contrast=1.10:saturation=1.18",
    "Golden Hour": "colorbalance=rs=0.04:bs=-0.06:rm=0.08:gm=0.03:bm=-0.08:rh=0.06:bh=-0.05,eq=contrast=1.06:saturation=1.12:gamma=1.02",
    "Neon Noir": "colorbalance=rs=0.06:bs=0.14:rm=0.06:bm=0.10,eq=contrast=1.18:saturation=1.30:brightness=-0.03",
    "Bleach Bypass": "eq=contrast=1.28:saturation=0.55:brightness=-0.02",
    "Cold Thriller": "colorbalance=rs=-0.06:bs=0.08:rm=-0.04:bm=0.06,eq=contrast=1.12:saturation=0.75:brightness=-0.03",
    "Vintage Film": "curves=preset=vintage,eq=saturation=0.9:contrast=1.04",
    "Noir B&W": "hue=s=0,eq=contrast=1.35:brightness=-0.02,curves=preset=strong_contrast",
    "Vibrant": "eq=contrast=1.08:saturation=1.38",
    "Soft Pastel": "eq=contrast=0.92:saturation=0.85:brightness=0.04,colorbalance=rh=0.03:bh=0.03",
    "Clean Commercial": "eq=contrast=1.06:saturation=1.10:brightness=0.02,unsharp=5:5:0.4",
    "Desert Heat": "colorbalance=rs=0.08:gs=0.03:bs=-0.10:rm=0.08:bm=-0.08,eq=saturation=1.1:contrast=1.08",
    "Matrix Green": "colorbalance=rs=-0.06:gs=0.08:bs=-0.04:rm=-0.04:gm=0.08,eq=contrast=1.15:saturation=0.9",
}

ATMOSPHERES: Dict[str, str] = {
    "none": "None", "fog": "Rolling fog", "rain": "Rain", "snow": "Snow", "embers": "Fire embers / sparks",
    "dust": "Floating dust motes", "light_leaks": "Film light leaks", "fireflies": "Fireflies / magic particles",
}

# Transition label -> (xfade name, default duration). "Cut" uses a very short dissolve.
TRANSITIONS: Dict[str, tuple] = {
    "Crossfade": ("fade", 0.8), "Dissolve": ("dissolve", 0.9), "Fade through black": ("fadeblack", 1.0),
    "Flash (white)": ("fadewhite", 0.5), "Cut": ("fade", 0.06), "Whip pan left": ("smoothleft", 0.4),
    "Whip pan right": ("smoothright", 0.4), "Slide left": ("slideleft", 0.6), "Wipe right": ("wiperight", 0.7),
    "Circle open": ("circleopen", 0.8), "Radial": ("radial", 0.8), "Zoom in": ("zoomin", 0.6),
    "Motion blur": ("hblur", 0.5), "Pixelize": ("pixelize", 0.7), "Diagonal": ("diagtl", 0.7),
    "Smooth up": ("smoothup", 0.6),
}

ANIMATION_EFFECTS = ["None", "Subtle breathing zoom", "Parallax depth", "Lens flare sweep", "Pulse glow",
                     "Chromatic aberration", "Motion blur"]

SUBTITLE_STYLES = {
    "Cinematic": "Clean white serif-like captions with soft shadow, lower third",
    "Bold Social": "Large uppercase captions, active word highlighted in yellow",
    "Karaoke": "Word-by-word highlight synced to the voice",
    "Minimal": "Small, subtle lower captions",
    "None": "No burned-in subtitles (SRT still exported)",
}

MUSIC_MOODS = ["epic", "dark", "uplifting", "mysterious", "emotional", "tense", "ambient", "none"]
AMBIENCES = ["none", "rain", "wind", "ocean", "city", "fire", "forest", "space", "crowd", "storm"]
SFX = ["none", "whoosh", "impact", "riser", "boom", "shimmer", "heartbeat"]

ASPECTS = {"16:9 Landscape (YouTube)": (16, 9), "9:16 Vertical (Shorts/Reels/TikTok)": (9, 16),
           "1:1 Square": (1, 1), "21:9 Ultra-wide cinema": (21, 9), "4:5 Portrait feed": (4, 5)}
RESOLUTIONS = {"720p": 720, "1080p Full HD": 1080, "1440p 2K": 1440, "2160p 4K (upscaled)": 2160}
FPS_OPTIONS = [24, 30, 60]
FORMATS = {"MP4 (H.264)": ("mp4", "libx264"), "MP4 (H.265/HEVC)": ("mp4", "libx265"),
           "MOV (H.264)": ("mov", "libx264"), "WebM (VP9)": ("webm", "libvpx-vp9")}
QUALITY = {"Draft (fast)": {"crf": 26, "preset": "veryfast"}, "High": {"crf": 19, "preset": "medium"},
           "Cinema (slow, best)": {"crf": 16, "preset": "slow"}}


def normalize_aspect(aspect: str) -> tuple:
    """Normalize any aspect label, shorthand ('9:16', '9x16', '16:9', '16x9') to (w_ratio, h_ratio)."""
    if not aspect:
        return (16, 9)
    if aspect in ASPECTS:
        return ASPECTS[aspect]
    a = str(aspect).strip().lower().replace("x", ":")
    if "9:16" in a or "vertical" in a or "short" in a or "reel" in a or "tiktok" in a:
        return (9, 16)
    if "16:9" in a or "landscape" in a or "youtube" in a:
        return (16, 9)
    if "1:1" in a or "square" in a:
        return (1, 1)
    if "21:9" in a or "ultra" in a or "cinema" in a:
        return (21, 9)
    if "4:5" in a or "portrait" in a:
        return (4, 5)
    return (16, 9)


def frame_size(aspect: str, resolution: str) -> tuple:
    """Even pixel dimensions for an aspect + quality label. Short side = resolution height."""
    aw, ah = normalize_aspect(aspect)
    short = RESOLUTIONS.get(resolution, 1080)
    if aw >= ah:
        h = short
        w = int(round(h * aw / ah))
    else:
        w = short
        h = int(round(w * ah / aw))
    return w - w % 2, h - h % 2


def style_prompt(style: str) -> str:
    return STYLES.get(style, STYLES["Hollywood Blockbuster"])["prompt"]


def closest(value: str, options: List[str], default: str) -> str:
    """Maps a free-text LLM value onto the nearest allowed option (case-insensitive containment)."""
    if not value:
        return default
    v = value.strip().lower()
    for o in options:
        if o.lower() == v:
            return o
    for o in options:
        ol = o.lower()
        if v in ol or ol.split(" (")[0] in v:
            return o
    words = set(v.replace("-", " ").split())
    best, score = default, 0
    for o in options:
        s = len(words & set(o.lower().replace("-", " ").replace("(", "").replace(")", "").split()))
        if s > score:
            best, score = o, s
    return best
