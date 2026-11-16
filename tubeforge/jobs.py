"""Projects + background production jobs.
Each project lives in output/cinematic/<id>/project.json (atomic writes) with its assets beside it.
Every scene asset (image / footage / voice / clip) stores a fingerprint of its inputs, so re-running a render only
regenerates what actually changed and an interrupted job resumes where it stopped."""
import concurrent.futures as cf
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import threading
import time
import traceback
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import audio, cinema, images, planner, quality, render, tts, video
from .config import STUDIO_ROOT

LEAD_IN = 0.35
TAIL = 0.45

DEFAULT_SETTINGS: Dict[str, Any] = {
    "style": "Hollywood Blockbuster", "aspect": "16:9 Landscape (YouTube)", "resolution": "1080p Full HD",
    "fps": 30, "format": "MP4 (H.264)", "quality": "High", "scene_count": 8, "scene_duration": 6.0,
    "language": "English", "tone": "", "audience": "",
    "grade": "", "atmosphere": "auto", "atmosphere_intensity": 1.0, "grain": 0.35, "vignette": 0.5, "bloom": 0.3,
    "letterbox": False, "dof": 0.0, "chroma": 0.0, "sharpen": 0.3, "camera_intensity": 1.0, "resample": "bilinear",
    "transition_override": "", "subtitle_style": "Cinematic", "caption_words": 7,
    "voice_engine": "edge", "voice": "en-US-AndrewMultilingualNeural", "voice_speed": 1.0, "voice_pitch": 0,
    "dialogue_voices": True, "character_voices": {},
    "music_mood": "ambient", "music_path": "", "music_vol": 0.35, "voice_vol": 1.0, "fx_vol": 0.5, "duck": 0.6,
    "ambience_on": True, "sfx_on": True, "loudness": -14.0,
    "image_engine": "auto", "openrouter_image_model": "", "allow_paid": False, "allow_stock": False,
    "default_source": "original_motion", "negative_global": "blurry, low quality, distorted, deformed, watermark, text, logo",
    "char_refs": {}, "ai_video_model": "", "ai_video_res": "720p", "ai_video_budget": 0.0,
    "research_notes": "",
    "channel_name": "", "channel_logo_path": "", "brand_position": "Top-Right",
    "voice_branding": True, "brand_overlay": True, "target_duration": 120.0,
    "fade_in": 0.6, "fade_out": 1.2, "workers": 3, "seed": 0, "title_card": True,
    "halation": 0.0, "gate_weave": False, "end_card": True, "end_card_cta": "", "end_card_len": 5.0,
    "shorts_teaser": True, "teaser_len": 45.0, "transition_sfx": True, "thumb_variants": True,
}
# New projects start with a richer film finish; older projects keep their look unless changed.
NEW_PROJECT_SETTINGS = {"halation": 0.25}

FAST_TRANSITIONS = ("smoothleft", "smoothright", "smoothup", "slideleft", "zoomin", "fadewhite", "hblur", "wiperight")

STEP_STATES = ("pending", "visual", "voice", "rendering", "done", "error", "cancelled")


def _fp(*parts) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _music_seed(project_id: str) -> int:
    return int(hashlib.sha256(f"tubeforge-music:{project_id}".encode()).hexdigest()[:16], 16)


def _file_sig(p: str) -> str:
    try:
        st = os.stat(p)
        return f"{p}:{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        return p


def _now() -> float:
    return time.time()


# ------------------------------------------------------------------------------------------- projects
class Project:
    def __init__(self, data: Dict[str, Any]):
        self.data = data
        self.lock = threading.RLock()
        self._last_save = 0.0

    # --- paths
    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def dir(self) -> Path:
        return STUDIO_ROOT / self.id

    def scene_dir(self, sid: str) -> Path:
        d = self.dir / "scenes" / sid
        d.mkdir(parents=True, exist_ok=True)
        return d

    # --- data helpers
    @property
    def settings(self) -> Dict[str, Any]:
        return self.data["settings"]

    @property
    def plan(self) -> Optional[planner.CinematicPlan]:
        p = self.data.get("plan")
        return planner.CinematicPlan(**p) if p else None

    def set_plan(self, plan: planner.CinematicPlan) -> None:
        with self.lock:
            self.data["plan"] = plan.model_dump()
            self.data["rev"] = int(self.data.get("rev") or 0) + 1
            states = self.data.setdefault("scene_state", {})
            for i, sc in enumerate(plan.scenes):
                st = states.setdefault(sc.id, {})
                st.setdefault("seed", self._seed_for(i))
            keep = {sc.id for sc in plan.scenes}
            for k in list(states):
                if k not in keep:
                    states.pop(k)

    def _seed_for(self, i: int) -> int:
        base = int(self.settings.get("seed") or 0) or int(hashlib.sha1(self.id.encode()).hexdigest()[:6], 16)
        return (base * 7919 + i * 104729) % 2_000_000_000 + 1

    def state(self, sid: str) -> Dict[str, Any]:
        with self.lock:
            return self.data.setdefault("scene_state", {}).setdefault(sid, {"seed": random.randint(1, 10 ** 9)})

    def log(self, msg: str) -> None:
        with self.lock:
            logs = self.data.setdefault("logs", [])
            logs.append(f"{time.strftime('%H:%M:%S')}  {msg}")
            del logs[:-400]

    def save(self, force: bool = True) -> None:
        with self.lock:
            if not force and _now() - self._last_save < 1.0:
                return
            self.data["updated"] = _now()
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.dir / f"project.json.{threading.get_ident()}.tmp"
            tmp.write_text(json.dumps(self.data, indent=1, default=str), encoding="utf-8")
            tmp.replace(self.dir / "project.json")
            self._last_save = _now()


def _new_id(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:32] or "film"
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}-{uuid.uuid4().hex[:4]}"


_OPEN: Dict[str, Project] = {}
_OPEN_LOCK = threading.Lock()


def create_project(idea: str, settings: Optional[Dict[str, Any]] = None) -> Project:
    data = {"id": _new_id(idea[:40]), "created": _now(), "updated": _now(), "idea": idea,
            "settings": {**DEFAULT_SETTINGS, **NEW_PROJECT_SETTINGS, **(settings or {})}, "plan": None, "scene_state": {}, "logs": [],
            "job": {"kind": "", "status": "idle", "progress": 0.0, "stage": ""}, "outputs": {}, "spent_usd": 0.0}
    p = Project(data)
    p.save()
    with _OPEN_LOCK:
        _OPEN[p.id] = p
    return p


def load_project(pid: str) -> Optional[Project]:
    if not pid or not re.fullmatch(r"[A-Za-z0-9_\-]+", str(pid)):
        return None
    with _OPEN_LOCK:
        if pid in _OPEN:
            return _OPEN[pid]
        f = STUDIO_ROOT / pid / "project.json"
        if not f.exists():
            return None
        data = json.loads(f.read_text(encoding="utf-8"))
        data["settings"] = {**DEFAULT_SETTINGS, **data.get("settings", {})}
        job = data.setdefault("job", {"status": "idle"})
        if job.get("status") == "running":  # process restarted mid-job
            job["status"] = "interrupted"
            job["stage"] = "Interrupted (app restarted) - press Resume"
            for st in data.get("scene_state", {}).values():
                if st.get("status") in ("visual", "voice", "rendering"):
                    st["status"] = "pending"
        p = Project(data)
        _OPEN[pid] = p
        return p


def list_projects() -> List[Dict[str, Any]]:
    out = []
    if not STUDIO_ROOT.exists():
        return out
    for f in STUDIO_ROOT.glob("*/project.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        plan = d.get("plan") or {}
        out.append({"id": d["id"], "title": plan.get("title") or d.get("idea", "")[:60], "idea": d.get("idea", ""),
                    "updated": d.get("updated", 0), "scenes": len(plan.get("scenes") or []),
                    "status": (d.get("job") or {}).get("status", "idle"),
                    "final": (d.get("outputs") or {}).get("video", ""),
                    "thumb": (d.get("outputs") or {}).get("thumbnail", "")})
    return sorted(out, key=lambda x: -x["updated"])


def delete_project(pid: str) -> None:
    if not pid or not re.fullmatch(r"[A-Za-z0-9_\-]+", str(pid)):
        raise ValueError("Invalid project ID")
    if MANAGER.is_running(pid):
        raise RuntimeError("stop the running job first")
    with _OPEN_LOCK:
        _OPEN.pop(pid, None)
    shutil.rmtree(STUDIO_ROOT / pid, ignore_errors=True)


def duplicate_project(pid: str) -> Project:
    if not pid or not re.fullmatch(r"[A-Za-z0-9_\-]+", str(pid)):
        raise ValueError("Invalid project ID")
    src = load_project(pid)
    if not src:
        raise ValueError(f"Project '{pid}' not found")
    data = json.loads(json.dumps(src.data))
    data["id"] = _new_id((src.plan.title if src.plan else src.data["idea"])[:30] + " copy")
    data["outputs"], data["job"], data["logs"] = {}, {"status": "idle"}, []
    for st in data["scene_state"].values():
        for k in list(st):
            if k not in ("seed", "locked_image", "upload", "variations"):
                st.pop(k)
    p = Project(data)
    for sid, st in data["scene_state"].items():
        for k in ("locked_image", "upload"):
            if st.get(k) and Path(st[k]).exists():
                dst = p.scene_dir(sid) / Path(st[k]).name
                shutil.copy2(st[k], dst)
                st[k] = str(dst)
        st["variations"] = []
    p.save()
    with _OPEN_LOCK:
        _OPEN[p.id] = p
    return p


# ------------------------------------------------------------------------------------- frame geometry
def render_size(s: Dict[str, Any]) -> Tuple[int, int, Tuple[int, int]]:
    """(render W, render H, final (W, H)). 4K renders at 1440p and is upscaled at export."""
    final = cinema.frame_size(s["aspect"], s["resolution"])
    if cinema.RESOLUTIONS.get(s["resolution"], 1080) > 1440:
        w, h = cinema.frame_size(s["aspect"], "1440p 2K")
        return w, h, final
    return final[0], final[1], final


def aspect_ratio_str(s: Dict[str, Any]) -> str:
    aw, ah = cinema.normalize_aspect(s.get("aspect", ""))
    return f"{aw}:{ah}"


# ----------------------------------------------------------------------------------------- scene steps
def scene_transition(p: Project, sc: planner.Scene) -> Tuple[str, float]:
    label = p.settings.get("transition_override") or sc.transition
    return cinema.TRANSITIONS.get(label, cinema.TRANSITIONS["Crossfade"])


def _voice_for_speaker(p: Project, speaker: str) -> str:
    s = p.settings
    mapping = s.get("character_voices") or {}
    if speaker in mapping and mapping[speaker]:
        return mapping[speaker]
    opts = [v for v, _ in tts.voice_options(s["language"]) if v != s["voice"]]
    plan = p.plan
    desc = ""
    if plan:
        desc = next((c.voice + " " + c.look for c in plan.characters if c.name.lower() == speaker.lower()), "")
    female = bool(re.search(r"\b(female|woman|girl|she|her)\b", desc.lower()))
    labels = dict(tts.voice_options(s["language"]))
    pool = [v for v in opts if ("(F)" in labels.get(v, "")) == female] or opts or [s["voice"]]
    return pool[int(hashlib.sha1(speaker.encode()).hexdigest(), 16) % len(pool)]


def voice_parts(p: Project, sc: planner.Scene) -> List[Tuple[str, str]]:
    s = p.settings
    parts = []
    if sc.narration.strip():
        parts.append((sc.narration.strip(), s["voice"]))
    for d in sc.dialogue:
        if d.line.strip():
            v = _voice_for_speaker(p, d.speaker) if s.get("dialogue_voices", True) else s["voice"]
            parts.append((d.line.strip(), v))
    return parts


def build_image_prompt(p: Project, sc: planner.Scene) -> str:
    plan = p.plan
    return planner.build_image_prompt(plan, sc, p.settings["style"]) if plan else sc.image_prompt


def scene_refs(p: Project, sc: planner.Scene) -> List[Path]:
    refs = []
    cref = p.settings.get("char_refs") or {}
    for name in sc.characters:
        path = cref.get(name) or next((v for k, v in cref.items() if k.lower() == name.lower()), None)
        if path and Path(path).exists():
            refs.append(Path(path))
    return refs[:4]


def _negative(p: Project, sc: planner.Scene) -> str:
    return ", ".join(x for x in (sc.negative_prompt, p.settings.get("negative_global", "")) if x)


def ensure_image(p: Project, sc: planner.Scene, log: Callable[[str], None]) -> Path:
    """AI still for the scene (or the user's locked upload / chosen variation)."""
    st = p.state(sc.id)
    if st.get("locked_image") and Path(st["locked_image"]).exists():
        return Path(st["locked_image"])
    W, H, _ = render_size(p.settings)
    prompt = build_image_prompt(p, sc)
    refs = scene_refs(p, sc)
    s = p.settings
    fp = _fp("img", prompt, _negative(p, sc), s["image_engine"], st.get("seed"), W, H, [_file_sig(str(r)) for r in refs],
             s.get("openrouter_image_model"), s["allow_paid"], s["allow_stock"])
    out = p.scene_dir(sc.id) / "image.jpg"
    if st.get("image_fp") == fp and out.exists():
        return out
    res = images.generate_image(prompt, out, W, H, seed=st.get("seed"), engine=s["image_engine"], refs=refs,
                                negative=_negative(p, sc), stock_query=sc.stock_query,
                                openrouter_model=s.get("openrouter_image_model", ""), allow_paid=s["allow_paid"],
                                allow_stock=s["allow_stock"], log=log)
    with p.lock:
        st.update(image_fp=fp, image=str(out), image_engine=res.engine, image_model=res.model,
                  image_warnings=res.warnings, image_prompt_used=prompt)
    return out


def generate_variations(p: Project, sc: planner.Scene, n: int = 3, log=None) -> List[str]:
    """Extra candidates with new seeds; the user can pick one to lock."""
    st = p.state(sc.id)
    W, H, _ = render_size(p.settings)
    s = p.settings
    prompt = build_image_prompt(p, sc)
    out = []
    for _ in range(n):
        seed = random.randint(1, 2 ** 31 - 1)
        path = p.scene_dir(sc.id) / f"var_{seed}.jpg"
        res = images.generate_image(prompt, path, W, H, seed=seed, engine=s["image_engine"], refs=scene_refs(p, sc),
                                    negative=_negative(p, sc), stock_query=sc.stock_query, allow_paid=s["allow_paid"],
                                    allow_stock=False, openrouter_model=s.get("openrouter_image_model", ""), log=log)
        out.append(str(path))
        with p.lock:
            st.setdefault("variations", []).append({"path": str(path), "seed": seed, "engine": res.engine})
    p.save()
    return out


def ensure_visual(p: Project, sc: planner.Scene, log: Callable[[str], None],
                  should_cancel: Callable[[], bool]) -> Tuple[str, Path]:
    """Returns ('image'|'video', path) for the renderer according to the scene's source type."""
    st = p.state(sc.id)
    s = p.settings
    src = sc.source
    W, H, _ = render_size(s)
    if src == "upload":
        up = st.get("upload")
        if up and Path(up).exists():
            kind = "video" if Path(up).suffix.lower() in (".mp4", ".mov", ".webm", ".mkv", ".m4v") else "image"
            return kind, Path(up)
        log(f"⚠️ Scene “{sc.title}”: no upload found, generating an AI image instead")
        src = "ai_image"
    if src == "stock_video":
        out = p.scene_dir(sc.id) / "stock.mp4"
        fp = _fp("stock", sc.stock_query, W, H, st.get("stock_variant", 0))
        if st.get("stock_fp") == fp and out.exists():
            return "video", out
        clip = video.fetch_stock_clip(sc.stock_query or sc.title, out, H > W, sc.duration, min(W, H) if H > W else W,
                                      st.get("stock_variant", 0))
        if clip:
            with p.lock:
                st.update(stock_fp=fp, stock=str(out), stock_credit=f"Pixabay video {clip.id} by {clip.user}",
                          stock_page=clip.page)
            return "video", out
        log(f"⚠️ No stock footage for “{sc.stock_query}”, using an AI image for scene “{sc.title}”")
        src = "ai_image"
    if src == "ai_video":
        log(f"🎥 Hosted AI video is disabled in free-tier-only mode for “{sc.title}”; using original local motion")
        src = "original_motion"
    if src == "original_motion":
        return "image", ensure_image(p, sc, log)
    return "image", ensure_image(p, sc, log)


def ensure_voice(p: Project, sc: planner.Scene, log: Callable[[str], None]) -> List[Dict[str, Any]]:
    """Synthesizes narration + dialogue parts. Returns [{path, offset, duration, words}] relative to voice start."""
    st = p.state(sc.id)
    s = p.settings
    parts = voice_parts(p, sc)
    fp = _fp("voice", parts, s["voice_engine"], s["voice_speed"], s["voice_pitch"], sc.emotion)
    if st.get("voice_fp") == fp and all(Path(x["path"]).exists() for x in st.get("voice_parts", [])):
        return st["voice_parts"]
    out_parts, cursor = [], 0.0
    for i, (text, voice_id) in enumerate(parts):
        path = p.scene_dir(sc.id) / f"voice_{i}.mp3"
        engine = "edge"
        edge_voices = {v for v, _ in tts.voice_options(s["language"])}
        voice_id = voice_id if voice_id in edge_voices else tts.voice_options(s["language"])[0][0]
        res = tts.synthesize(text, path, engine=engine, voice=voice_id, speed=s["voice_speed"],
                             pitch_hz=int(s["voice_pitch"]), emotion=sc.emotion, log=log)
        out_parts.append({"path": str(path), "offset": cursor, "duration": res.duration, "voice": res.voice,
                          "engine": res.engine, "words": [{"start": w.start, "end": w.end, "text": w.text}
                                                          for w in res.words]})
        cursor += res.duration + 0.35
    with p.lock:
        st.update(voice_fp=fp, voice_parts=out_parts)
    return out_parts


def scene_length(p: Project, sc: planner.Scene, parts: List[Dict[str, Any]], trans_out: float) -> float:
    voice_end = (parts[-1]["offset"] + parts[-1]["duration"]) if parts else 0.0
    speech = LEAD_IN + voice_end + TAIL if parts else 0.0
    return round(max(sc.duration, speech) + trans_out, 3)


def caption_chunks(p: Project, parts: List[Dict[str, Any]], clip_len: float, trans_out: float) -> List[render.CaptionChunk]:
    words = []
    for part in parts:
        for w in part["words"]:
            words.append({"start": w["start"] + part["offset"], "end": w["end"] + part["offset"], "text": w["text"]})
    chunks = render.chunk_words(words, int(p.settings.get("caption_words") or 7), LEAD_IN)
    limit = clip_len - trans_out - 0.05
    out = []
    for c in chunks:
        if c.start >= limit:
            break
        c.end = min(c.end, limit)
        out.append(c)
    return out


def render_spec(p: Project, sc: planner.Scene, idx: int, kind: str, src: Path, clip_len: float,
                chunks: List[render.CaptionChunk]) -> render.SceneRenderSpec:
    s = p.settings
    W, H, _ = render_size(s)
    style = cinema.STYLES.get(s["style"], cinema.STYLES["Hollywood Blockbuster"])
    grade = sc.color_grade or s.get("grade") or style["grade"]
    atmo = sc.atmosphere if s.get("atmosphere", "auto") == "auto" else s["atmosphere"]
    overlay = sc.overlay_text if (idx > 0 or s.get("title_card", True) or sc.overlay_kind != "Title") else ""
    anim = sc.animation
    if sc.source == "original_motion" and anim == "None":
        anim = "Parallax depth"
    return render.SceneRenderSpec(
        out_path=p.scene_dir(sc.id) / "clip.mp4", width=W, height=H, fps=int(s["fps"]), duration=clip_len,
        source_type=kind, source_path=src, camera=sc.camera_move, intensity=float(s["camera_intensity"]),
        grade=grade, atmosphere=atmo or "none", atmosphere_intensity=float(s["atmosphere_intensity"]),
        grain=float(s["grain"]), vignette=float(s["vignette"]),
        bloom=float(s["bloom"]) + (0.2 if anim == "Pulse glow" else 0.1 if anim == "Lens flare sweep" else 0.0),
        letterbox=bool(s["letterbox"]), dof=float(s["dof"]) + (0.15 if anim == "Parallax depth" else 0.0),
        chroma=float(s["chroma"]) + (0.6 if anim == "Chromatic aberration" else 0.0),
        sharpen=float(s["sharpen"]), motion_blur=anim == "Motion blur", animation=anim,
        halation=float(s.get("halation", 0.0)), gate_weave=bool(s.get("gate_weave", False)),
        caption_chunks=chunks, caption_style=s["subtitle_style"], overlay_text=overlay, overlay_kind=sc.overlay_kind,
        overlay_start=0.4, overlay_end=max(1.5, min(4.5, clip_len - 1.0)), seed=int(p.state(sc.id).get("seed", 1)),
        resample=s.get("resample", "bilinear"),
        channel_name=str(s.get("channel_name", "")),
        channel_logo_path=Path(s["channel_logo_path"]) if s.get("channel_logo_path") and Path(s["channel_logo_path"]).exists() else None,
        brand_position=str(s.get("brand_position", "Top-Right")),
        brand_overlay=bool(s.get("brand_overlay", True)),
        work_dir=p.scene_dir(sc.id) / "work")


def spec_fingerprint(spec: render.SceneRenderSpec) -> str:
    d = {k: (str(v) if isinstance(v, Path) else v) for k, v in spec.__dict__.items()
         if k not in ("out_path", "work_dir", "caption_chunks")}
    d["src"] = _file_sig(str(spec.source_path))
    d["captions"] = [(round(c.start, 2), round(c.end, 2), c.words) for c in spec.caption_chunks]
    return _fp("clip", d, render.__name__, render.PIPELINE_VERSION)


# ---------------------------------------------------------------------------------------------- jobs
class Cancelled(RuntimeError):
    pass


class JobManager:
    def __init__(self, max_jobs: int = 2):
        self.pool = cf.ThreadPoolExecutor(max_workers=max_jobs, thread_name_prefix="tf-job")
        self.futures: Dict[str, cf.Future] = {}
        self.cancel_flags: Dict[str, threading.Event] = {}
        self.lock = threading.Lock()

    def is_running(self, pid: str) -> bool:
        f = self.futures.get(pid)
        return bool(f and not f.done())

    def cancel(self, pid: str) -> None:
        ev = self.cancel_flags.get(pid)
        if ev:
            ev.set()

    def _submit(self, p: Project, kind: str, fn: Callable[[Project, threading.Event], None]) -> bool:
        with self.lock:
            if self.is_running(p.id):
                return False
            ev = threading.Event()
            self.cancel_flags[p.id] = ev
            with p.lock:
                p.data["job"] = {"kind": kind, "status": "running", "progress": 0.0, "stage": "Queued",
                                 "started": _now(), "error": ""}
            p.save()

            def runner():
                try:
                    fn(p, ev)
                    with p.lock:
                        if ev.is_set():
                            p.data["job"].update(status="cancelled", stage="Cancelled")
                        elif p.data["job"].get("status") == "running":
                            p.data["job"].update(status="done", progress=1.0)
                except (Cancelled, render.Cancelled):
                    with p.lock:
                        p.data["job"].update(status="cancelled", stage="Cancelled by user")
                    p.log("⏹️ Job cancelled")
                except Exception as e:  # noqa: BLE001 - surfaced to the UI
                    with p.lock:
                        p.data["job"].update(status="error", error=str(e)[:600], stage="Failed")
                    p.log(f"❌ {str(e)[:400]}")
                    traceback.print_exc()
                finally:
                    with p.lock:
                        p.data["job"]["finished"] = _now()
                    p.save()

            self.futures[p.id] = self.pool.submit(runner)
            return True

    # --- planning
    def start_plan(self, p: Project, script: str = "", extra: str = "") -> bool:
        return self._submit(p, "plan", lambda p, ev: run_plan(p, ev, script, extra))

    def start_full(self, p: Project, script: str = "", extra: str = "", auto_settings: bool = False) -> bool:
        """One-click: (Auto Cinematic settings) -> story & plan -> all assets -> final film."""
        def job(p: Project, ev: threading.Event):
            if auto_settings:
                apply_auto_cinematic(p)
            run_plan(p, ev, script, extra, progress_scale=0.15)
            if ev.is_set():
                raise Cancelled()
            produce(p, ev, None, True, False)

        return self._submit(p, "full", job)

    # --- production
    def start_render(self, p: Project, scene_ids: Optional[List[str]] = None, assemble: bool = True,
                     assets_only: bool = False) -> bool:
        def job(p: Project, ev: threading.Event):
            produce(p, ev, scene_ids, assemble and not assets_only, assets_only)

        return self._submit(p, "assets" if assets_only else ("scene" if scene_ids and not assemble else "render"), job)

    def start_task(self, p: Project, kind: str, fn: Callable[[Project, threading.Event], None]) -> bool:
        return self._submit(p, kind, fn)


MANAGER = JobManager()


def apply_auto_cinematic(p: Project) -> Dict[str, Any]:
    """Auto Cinematic Mode: the AI picks style, format, look, music and pacing for the idea."""
    auto = planner.auto_cinematic(p.data["idea"], log=p.log)
    s = p.settings
    with p.lock:
        for k in ("style", "aspect", "grade", "atmosphere", "music_mood", "subtitle_style", "scene_count",
                  "scene_duration", "camera_intensity", "letterbox", "tone", "audience"):
            if k in auto and auto[k] not in ("", None):
                s[k] = auto[k]
        s["grain"] = auto.get("film_grain", s["grain"])
        s["bloom"] = auto.get("bloom", s["bloom"])
        s["halation"] = auto.get("halation", s.get("halation", 0.0))
        s["gate_weave"] = bool(auto.get("gate_weave", s.get("gate_weave", False)))
        if auto.get("atmosphere") and auto["atmosphere"] != "none":
            s["atmosphere"] = "auto"  # keep per-scene atmosphere from the plan; style default fills gaps
        voices = tts.voice_options(s["language"])
        want_f = auto.get("voice_gender") == "female"
        match = [v for v, lab in voices if ("(F)" in lab) == want_f]
        if match and s["voice"] not in match:
            s["voice"] = match[0]
        p.data["auto_reason"] = auto.get("reason", "")
    p.log(f"🪄 Auto Cinematic: {s['style']}, {s['scene_count']} scenes × {s['scene_duration']:.0f}s, "
          f"{s['grade'] or 'style grade'}, music {s['music_mood']}. {auto.get('reason', '')}")
    p.save()
    return auto


def run_plan(p: Project, ev: threading.Event, script: str = "", extra: str = "", progress_scale: float = 1.0) -> None:
    s = p.settings

    def prog(f, msg):
        with p.lock:
            p.data["job"].update(progress=f * progress_scale, stage=msg)
        p.save(force=False)
        if ev.is_set():
            raise Cancelled()

    try:
        target_sec = float(s.get("target_duration") or 0)
    except (ValueError, TypeError):
        target_sec = 0.0
    if target_sec <= 0:
        target_sec = float(s.get("scene_count", 8)) * float(s.get("scene_duration", 6.0))
    target_min = target_sec / 60.0
    research_notes = str(s.get("research_notes", "")).strip()
    if research_notes:
        extra += (
            "\nFACTUAL SOURCES AND RESEARCH NOTES (user supplied; do not claim to have opened these links):\n"
            f"{research_notes[:5000]}\n"
            "Use only supported facts from these notes, preserve uncertainty, do not invent citations or precise "
            "claims, and do not present unsourced details as verified."
        )
    plan = planner.generate_plan(p.data["idea"], s["style"], int(s["scene_count"]), float(s["scene_duration"]),
                                 s["language"], s.get("tone", ""), s.get("audience", ""), script, extra,
                                 log=p.log, progress=prog,
                                 channel_name=str(s.get("channel_name", "")),
                                 voice_branding=bool(s.get("voice_branding", True)),
                                 target_minutes=target_min)
    for sc in plan.scenes:
        if sc.source == "ai_image" and s.get("default_source") in ("original_motion", "stock_video"):
            sc.source = s["default_source"]
        elif sc.source == "ai_video":
            sc.source = "original_motion"
    plan.research_sources = [line.strip() for line in research_notes.splitlines() if line.strip()]
    p.set_plan(plan)
    with p.lock:
        p.data["outputs"] = {}
        p.data["job"].update(stage="Plan ready", progress=progress_scale)
    p.log(f"✅ Plan ready: {len(plan.scenes)} scenes, models: {', '.join(dict.fromkeys(plan.models_used))}")
    p.save()


def _set_scene(p: Project, sid: str, **kw) -> None:
    with p.lock:
        p.state(sid).update(**kw)
    p.save(force=False)


def produce_scene(p: Project, sc: planner.Scene, idx: int, n: int, ev: threading.Event, assets_only: bool = False
                  ) -> Optional[Dict[str, Any]]:
    def log(m):
        p.log(f"[{idx + 1}/{n}] {m}")

    def cancelled():
        return ev.is_set()

    trans_out = scene_transition(p, sc)[1] if idx < n - 1 else 0.0
    try:
        _set_scene(p, sc.id, status="visual", progress=0.05, error="")
        kind, src = ensure_visual(p, sc, log, cancelled)
        if cancelled():
            raise Cancelled()
        _set_scene(p, sc.id, status="voice", progress=0.3, source_kind=kind, source_path=str(src))
        parts = ensure_voice(p, sc, log)
        clip_len = scene_length(p, sc, parts, trans_out)
        _set_scene(p, sc.id, clip_len=clip_len, trans_out=trans_out)
        if assets_only:
            _set_scene(p, sc.id, status="assets", progress=1.0)
            return {"len": clip_len}
        chunks = caption_chunks(p, parts, clip_len, trans_out)
        spec = render_spec(p, sc, idx, kind, src, clip_len, chunks)
        fp = spec_fingerprint(spec)
        st = p.state(sc.id)
        if not (st.get("clip_fp") == fp and spec.out_path.exists()):
            _set_scene(p, sc.id, status="rendering", progress=0.4)
            t0 = time.time()
            render.render_scene(spec, progress=lambda f: _set_scene(p, sc.id, progress=0.4 + 0.6 * f),
                                should_cancel=cancelled)
            log(f"🎬 Rendered “{sc.title}” ({clip_len:.1f}s) in {time.time() - t0:.0f}s")
        _set_scene(p, sc.id, status="done", progress=1.0, clip=str(spec.out_path), clip_fp=fp)
        return {"len": clip_len}
    except (Cancelled, render.Cancelled):
        _set_scene(p, sc.id, status="cancelled")
        raise
    except Exception as e:  # noqa: BLE001
        _set_scene(p, sc.id, status="error", error=str(e)[:500])
        log(f"❌ Scene “{sc.title}” failed: {str(e)[:300]}")
        return None


def produce(p: Project, ev: threading.Event, scene_ids: Optional[List[str]], assemble: bool, assets_only: bool) -> None:
    plan = p.plan
    if not plan or not plan.scenes:
        raise RuntimeError("generate or write a plan first")
    scenes = plan.scenes
    n = len(scenes)
    # When assembling, every scene is checked: unchanged clips are reused instantly (fingerprints), while clips made
    # with older settings (e.g. another aspect/resolution) are re-rendered so the film never mixes formats.
    todo = [(i, sc) for i, sc in enumerate(scenes) if assemble or not scene_ids or sc.id in scene_ids]
    p.log(f"▶️ Producing {len(todo)} scene(s){' + final film' if assemble else ''}")
    workers = max(1, int(p.settings.get("workers") or 3))
    done = [0]

    def upd():
        with p.lock:
            frac = sum(float(p.state(sc.id).get("progress") or 0) for _, sc in todo) / max(1, len(todo))
            p.data["job"].update(progress=frac * (0.85 if assemble else 1.0),
                                 stage=f"Scenes {done[0]}/{len(todo)} complete")

    with cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tf-scene") as ex:
        futs = {ex.submit(produce_scene, p, sc, i, n, ev, assets_only): sc for i, sc in todo}
        pending = set(futs)
        while pending:
            fin, pending = cf.wait(pending, timeout=1.0, return_when=cf.FIRST_COMPLETED)
            for f in fin:
                done[0] += 1
                try:
                    f.result()
                except (Cancelled, render.Cancelled):
                    pass
            upd()
            p.save(force=False)
    if ev.is_set():
        raise Cancelled()
    retry = [(i, sc) for i, sc in todo if p.state(sc.id).get("status") == "error"]
    if retry:  # transient provider throttling (Edge voice, Pollinations) usually clears after a short pause
        p.log(f"🔁 Retrying {len(retry)} failed scene(s) one at a time after a short cool-down")
        for _ in range(20):
            if ev.is_set():
                raise Cancelled()
            time.sleep(1)
        for i, sc in retry:
            if ev.is_set():
                raise Cancelled()
            produce_scene(p, sc, i, n, ev, assets_only)
            upd()
            p.save(force=False)
    failed = [sc.title for _, sc in todo if p.state(sc.id).get("status") == "error"]
    if failed:
        raise RuntimeError(f"{len(failed)} scene(s) failed: {', '.join(failed[:5])}. Fix or retry them, then render "
                           "again (finished scenes are reused).")
    qa = quality.inspect_project(plan, p.data.get("scene_state") or {}, p.settings.get("char_refs") or {},
                                 render_size(p.settings)[:2])
    with p.lock:
        p.data["visual_qa"] = qa
    for issue in qa["issues"]:
        p.log(f"⚠️ Visual QA: {issue}")
    if not qa["issues"]:
        p.log(f"✅ Visual QA passed for {qa['checked_scenes']} generated stills")
    if assemble:
        assemble_film(p, ev)
    p.save()


def _cue_for(sc: planner.Scene, i: int, n: int) -> str:
    if sc.music_cue:
        return sc.music_cue
    frac = i / max(1, n - 1)
    return "calm" if frac < 0.25 else "build" if frac < 0.6 else "climax" if frac < 0.88 else "resolve"


def _chapters(plan: planner.CinematicPlan, starts: List[float], total: float = 0.0) -> List[Tuple[float, str]]:
    """YouTube chapters: first at 0:00, at least 3, each ≥ 10 s (otherwise YouTube ignores them)."""
    cand, last_act = [], None
    for sc, st in zip(plan.scenes, starts):
        label = sc.act.split("-")[0].split(":")[0].strip() if sc.act else ""
        if label.isdigit():
            label = f"Act {label}"
        if sc.overlay_kind == "Chapter" and sc.overlay_text:
            cand.append((st, sc.overlay_text))
        elif label and label != last_act:
            cand.append((st, f"{label}: {sc.title}"))
        last_act = label or last_act
    if not cand or cand[0][0] > 0.5:
        cand.insert(0, (0.0, plan.scenes[0].title or "Opening"))
    cand[0] = (0.0, cand[0][1])
    out: List[Tuple[float, str]] = []
    for t, name in cand:
        if not out or t - out[-1][0] >= 10.0:
            out.append((t, name))
    if total and len(out) > 1 and total - out[-1][0] < 10.0:
        out.pop()
    return out if len(out) >= 3 else []


def _ts(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec // 60 % 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def assemble_film(p: Project, ev: threading.Event) -> None:
    plan = p.plan
    s = p.settings
    scenes = plan.scenes
    n = len(scenes)
    clips, durs, trans = [], [], []
    for i, sc in enumerate(scenes):
        st = p.state(sc.id)
        if st.get("status") != "done" or not Path(st.get("clip", "")).exists():
            raise RuntimeError(f"scene {i + 1} “{sc.title}” is not rendered yet")
        clips.append(Path(st["clip"]))
        durs.append(float(st["clip_len"]))
        if i < n - 1:
            trans.append(scene_transition(p, sc))
    out_dir = p.dir / "final"
    work = p.dir / "work"
    out_dir.mkdir(parents=True, exist_ok=True)
    if s.get("end_card", True) and n:
        p.log("🎬 Rendering end card")
        end_clip = _render_end_card(p, work)
        if end_clip:
            trans.append(cinema.TRANSITIONS["Fade through black"])
            clips.append(end_clip)
            durs.append(float(s.get("end_card_len") or 5.0))
    tdur = [t[1] for t in trans]
    starts = render.timeline(durs, tdur)
    total = render.total_length(durs, tdur)

    def stage(msg, f):
        with p.lock:
            p.data["job"].update(stage=msg, progress=f)
        p.save(force=False)
        p.log(msg)
        if ev.is_set():
            raise Cancelled()

    stage(f"🎞️ Assembling {len(clips)} clips with transitions ({total:.1f}s)", 0.86)
    silent = work / "assembled.mkv"
    RW, RH, _ = render_size(s)
    render.assemble_video(clips, durs, trans, silent, int(s["fps"]), work, size=(RW, RH),
                          should_cancel=ev.is_set)

    stage("🔊 Building voice, music and sound design", 0.9)
    voice_tr = audio.Track(work / "voice.wav", total)
    fx_tr = audio.Track(work / "fx.wav", total)
    all_chunks: List[render.CaptionChunk] = []
    cues = []
    has_voice = False
    for i, sc in enumerate(scenes):
        st = p.state(sc.id)
        S = starts[i]
        for part in st.get("voice_parts", []):
            voice_tr.add(audio.decode(Path(part["path"])), S + LEAD_IN + part["offset"], 1.0)
            has_voice = True
        for c in caption_chunks(p, st.get("voice_parts", []), durs[i], tdur[i] if i < n - 1 else 0.0):
            all_chunks.append(render.CaptionChunk(c.start + S, c.end + S, c.words,
                                                  [(a + S, b + S) for a, b in c.word_times]))
        end = S + durs[i]
        cues.append((S, end, _cue_for(sc, i, n)))
        if s.get("ambience_on", True) and sc.ambience and sc.ambience != "none":
            amb = audio.ambience_loop(sc.ambience)
            if amb:
                fx_tr.add_loop(audio.decode(amb), S, end, 0.55, fade=0.8, offset=S)
        if s.get("sfx_on", True) and sc.sfx and sc.sfx != "none":
            fxp = audio.sfx(sc.sfx)
            if fxp:
                sig = audio.decode(fxp)
                pre = {"whoosh": 0.35, "riser": sig.shape[0] / audio.SR - 0.1}.get(sc.sfx, 0.0)
                fx_tr.add(sig, max(0.0, S + (0.15 if i else 0.4) - pre), 0.8)
        if s.get("transition_sfx", True) and i < len(trans) and trans[i][0] in FAST_TRANSITIONS:
            nxt = scenes[i + 1] if i + 1 < n else None
            if not (nxt and s.get("sfx_on", True) and nxt.sfx and nxt.sfx != "none"):
                wp = audio.sfx("whoosh")
                if wp:  # whoosh peaks in the middle of the transition
                    fx_tr.add(audio.decode(wp), max(0.0, starts[i + 1] + tdur[i] / 2 - 0.35), 0.6)
    if len(clips) > n:  # end card
        cues.append((starts[n], total, "resolve"))
    voice_wav = voice_tr.write(work / "voice.wav")
    if not has_voice:
        voice_wav = None
    fx_wav = fx_tr.write(work / "fx.wav")
    mood = s.get("music_mood") if s.get("music_mood") not in ("auto", "", None) else plan.music_mood
    music_wav = None
    user_music = Path(s["music_path"]) if s.get("music_path") and Path(s["music_path"]).exists() else None
    if mood != "none" or user_music:
        music_wav = audio.build_music(work / "music.wav", total, mood if mood != "none" else "ambient", cues,
                                      seed=_music_seed(p.id), user_music=user_music)
    mix = audio.mix_final(voice_wav, music_wav, fx_wav, work / "mix.m4a", total, voice_vol=float(s["voice_vol"]),
                          music_vol=float(s["music_vol"]), fx_vol=float(s["fx_vol"]), duck=float(s["duck"]),
                          loudness=float(s["loudness"]))

    stage("📦 Final encode and export", 0.95)
    ext, codec = cinema.FORMATS.get(s["format"], ("mp4", "libx264"))
    q = cinema.QUALITY.get(s["quality"], cinema.QUALITY["High"])
    _, _, (FW, FH) = render_size(s)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", plan.title or "film").strip("_")[:40] or "film"
    final = out_dir / f"{slug}.{ext}"
    render.export_final(silent, mix, final, FW, FH, int(s["fps"]), codec, q["crf"], q["preset"],
                        float(s["fade_in"]), float(s["fade_out"]), total)
    srt = render.write_srt(all_chunks, out_dir / f"{slug}.srt")
    thumb_src = next((Path(p.state(sc.id)["image"]) for sc in scenes if p.state(sc.id).get("image")
                      and Path(p.state(sc.id)["image"]).exists()), None)
    if thumb_src is None:
        thumb_src = work / "frame.jpg"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", str(silent), "-frames:v", "1", str(thumb_src)],
                       check=False)
    logo_p = Path(s["channel_logo_path"]) if s.get("channel_logo_path") and Path(s["channel_logo_path"]).exists() else None
    thumb = render.make_thumbnail(thumb_src, plan.youtube_title or plan.title, out_dir / "thumbnail.jpg",
                                  channel_name=s.get("channel_name", ""), logo_path=logo_p, hook=plan.hook)
    thumbs = [str(thumb)]
    if s.get("thumb_variants", True):
        imgs = [Path(p.state(sc.id)["image"]) for sc in scenes if p.state(sc.id).get("image")
                and Path(p.state(sc.id)["image"]).exists() and Path(p.state(sc.id)["image"]) != thumb_src]
        climax = [i for i, sc in enumerate(scenes) if _cue_for(sc, i, n) == "climax"]
        if climax:  # prefer the most dramatic frames
            cimg = [Path(p.state(scenes[i].id).get("image", "")) for i in climax]
            imgs = [c for c in cimg if c in imgs] + [c for c in imgs if c not in cimg]
        for k, src in enumerate(imgs[:2], start=2):
            try:
                thumbs.append(str(render.make_thumbnail(src, plan.youtube_title or plan.title,
                                                        out_dir / f"thumbnail_{k}.jpg",
                                                        channel_name=s.get("channel_name", ""), logo_path=logo_p,
                                                        hook=plan.hook, variant=k - 1)))
            except Exception as e:  # noqa: BLE001
                p.log(f"⚠️ Thumbnail variant {k} skipped: {e}")
    short = ""
    if s.get("shorts_teaser", True) and FW > FH:
        stage("📱 Creating vertical Shorts / Reels teaser", 0.98)
        try:
            short = str(render.make_vertical_teaser(final, out_dir / f"{slug}_short.mp4", plan.title,
                                                    min(total, float(s.get("teaser_len") or 45.0), 59.0),
                                                    s.get("channel_name", ""), FH, crf=q["crf"] + 1, preset=q["preset"]))
        except Exception as e:  # noqa: BLE001
            p.log(f"⚠️ Shorts teaser skipped: {e}")
    chapters = _chapters(plan, starts, total)
    credits = [p.state(sc.id).get("stock_credit") for sc in scenes if p.state(sc.id).get("stock_credit")]
    sources = [line for line in plan.research_sources if line.strip()]
    source_text = "\n\nSources / fact-check notes:\n" + "\n".join(sources) if sources else ""
    desc = (plan.youtube_description + ("\n\n" + "\n".join(f"{_ts(t)} {name}" for t, name in chapters) if chapters else "")
            + source_text
            + (f"\n\nStock footage: {'; '.join(dict.fromkeys(credits))} (Pixabay License)" if credits else "")
            + "\n\n" + " ".join("#" + re.sub(r"\W", "", t) for t in plan.tags[:8]))
    meta = out_dir / "youtube_metadata.txt"
    meta.write_text(f"TITLE: {plan.youtube_title or plan.title}\n\nDESCRIPTION:\n{desc}\n\nTAGS: {', '.join(plan.tags)}\n",
                    encoding="utf-8")
    with p.lock:
        p.data["outputs"] = {"video": str(final), "srt": str(srt), "thumbnail": str(thumb), "metadata": str(meta),
                             "duration": total, "width": FW, "height": FH, "rendered": _now(),
                             "chapters": chapters, "description": desc, "thumbnails": thumbs, "short": short}
    for f in work.glob("*.f32"):
        f.unlink(missing_ok=True)
    stage(f"✅ Film ready: {final.name} ({total:.1f}s, {FW}x{FH})", 1.0)


def _render_end_card(p: Project, work: Path) -> Optional[Path]:
    """Animated end card: blurred last frame, film title, channel badge, subscribe button and CTA."""
    s, plan = p.settings, p.plan
    W, H, _ = render_size(s)
    work.mkdir(parents=True, exist_ok=True)
    last = plan.scenes[-1]
    st = p.state(last.id)
    src = Path(st["image"]) if st.get("image") and Path(st["image"]).exists() else None
    if src is None:
        src = work / "endcard_src.jpg"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-sseof", "-1.5", "-i", st.get("clip", ""), "-frames:v", "1",
                        str(src)], check=False)
        if not src.exists():
            return None
    bg = render.end_card_background(src, work / "endcard_bg.jpg", W, H)
    logo = Path(s["channel_logo_path"]) if s.get("channel_logo_path") and Path(s["channel_logo_path"]).exists() else None
    ov = work / "endcard_text.png"
    render.render_end_card_png(plan.title, W, H, s.get("channel_name", ""), logo, s.get("end_card_cta", "")).save(ov)
    style = cinema.STYLES.get(s["style"], cinema.STYLES["Hollywood Blockbuster"])
    spec = render.SceneRenderSpec(
        out_path=work / "endcard.mp4", width=W, height=H, fps=int(s["fps"]),
        duration=float(s.get("end_card_len") or 5.0), source_type="image", source_path=bg, camera="Slow dolly in",
        intensity=0.5, grade=s.get("grade") or style["grade"], atmosphere="dust", atmosphere_intensity=0.8,
        grain=float(s["grain"]), vignette=max(0.5, float(s["vignette"])), bloom=0.2, letterbox=bool(s["letterbox"]),
        caption_style="None", overlay_png=ov, seed=7, brand_overlay=False,
        work_dir=work)
    try:
        return render.render_scene(spec)
    except Exception as e:  # noqa: BLE001
        p.log(f"⚠️ End card skipped: {e}")
        return None


def generate_character_ref(p: Project, name: str, log=None) -> str:
    """Creates a clean reference portrait for a character; engines with reference support reuse it per scene."""
    plan = p.plan
    ch = next((c for c in plan.characters if c.name == name), None) if plan else None
    if not ch:
        raise RuntimeError(f"unknown character {name}")
    W, H = (1024, 1024)
    prompt = (f"Character reference portrait of {ch.name}, {ch.look}. Front-facing, upper body, neutral background, "
              f"even soft lighting, sharp focus on face and costume. {cinema.style_prompt(p.settings['style'])}")
    d = p.dir / "characters"
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{re.sub(r'[^A-Za-z0-9]+', '_', name)}_{random.randint(1000, 9999)}.jpg"
    s = p.settings
    images.generate_image(prompt, out, W, H, engine=s["image_engine"], negative=s.get("negative_global", ""),
                          allow_paid=s["allow_paid"], allow_stock=False,
                          openrouter_model=s.get("openrouter_image_model", ""), log=log)
    with p.lock:
        s.setdefault("char_refs", {})[name] = str(out)
    p.save()
    return str(out)


def package_zip(p: Project) -> Path:
    """Downloadable bundle: final film, subtitles, thumbnail, metadata, plan and all scene assets."""
    out = p.dir / f"{p.id}_package.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        outs = p.data.get("outputs") or {}
        files = [outs.get(k) for k in ("video", "short", "srt", "thumbnail", "metadata")] + list(outs.get("thumbnails") or [])
        for f in dict.fromkeys(f for f in files if f):
            if Path(f).exists():
                z.write(f, f"final/{Path(f).name}")
        z.writestr("plan.json", json.dumps(p.data.get("plan"), indent=2))
        plan = p.plan
        if plan:
            for i, sc in enumerate(plan.scenes, 1):
                st = p.state(sc.id)
                for key in ("image", "stock", "ai_video"):
                    f = st.get(key)
                    if f and Path(f).exists():
                        z.write(f, f"scenes/{i:02d}_{key}{Path(f).suffix}")
                for j, part in enumerate(st.get("voice_parts", [])):
                    if Path(part["path"]).exists():
                        z.write(part["path"], f"scenes/{i:02d}_voice_{j}.mp3")
    return out
