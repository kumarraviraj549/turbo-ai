"""Projects + background production jobs.
Each project lives in output/cinematic/<id>/project.json (atomic writes) with its assets beside it.
Every scene asset (image / footage / voice / clip) stores a fingerprint of its inputs, so re-running a render only
regenerates what actually changed and an interrupted job resumes where it stopped."""
import concurrent.futures as cf
import copy
import hashlib
import html
import html.parser
import ipaddress
import json
import os
import random
import re
import shutil
import socket
import subprocess
import threading
import time
import traceback
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import alignment, audio, cinema, images, planner, procedural, quality, render, tts, video
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
    "kinetic_captions": False, "caption_color": "#22D3EE", "caption_preset": "Hormozi Bounce",
    "alignment_model": "tiny",
    "voice_engine": "kokoro", "voice": "af_bella", "voice_speed": 1.0, "voice_pitch": 0,
    "external_voiceover_path": "",
    "dialogue_voices": True, "character_voices": {},
    "music_mood": "ambient", "music_path": "", "music_vol": 0.35, "voice_vol": 1.0, "fx_vol": 0.5, "duck": 0.6,
    "duck_db": -24.0,
    "ambience_on": True, "sfx_on": True, "loudness": -14.0,
    "image_engine": "auto", "openrouter_image_model": "", "allow_paid": False, "allow_stock": False,
    "default_source": "original_motion", "default_video_source": "original_motion",
    "brand_primary_color": "#8B5CF6", "brand_secondary_color": "#22D3EE", "brand_accent_color": "#F472B6",
    "negative_global": "blurry, low quality, distorted, deformed, watermark, text, logo",
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
VISUAL_BIBLE_NEGATIVE = (
    "text, lettering, watermark, logo, deformed hands, extra fingers, inconsistent character design, "
    "inconsistent art style, blurry, low quality"
)


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


def _reference_sig(path: str) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return path


def _now() -> float:
    return time.time()


def channel_id(name: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
    return value[:64] or "default-channel"


def channel_dir(name: str) -> Path:
    return STUDIO_ROOT.parent / "channels" / channel_id(name)


def load_channel_library(name: str) -> Dict[str, Any]:
    path = channel_dir(name) / "library.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {
        "visualBible": data.get("visualBible", {}),
        "characters": data.get("characters", []),
        "locations": data.get("locations", []),
        "brandPalette": data.get("brandPalette", {}),
        "captionPreset": data.get("captionPreset", "Hormozi Bounce"),
        "narratorVoice": data.get("narratorVoice", {}),
    }


def save_channel_library(name: str, library: Dict[str, Any]) -> Path:
    path = channel_dir(name) / "library.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = {
        "visualBible": library.get("visualBible", {}),
        "characters": library.get("characters", []),
        "locations": library.get("locations", []),
        "brandPalette": library.get("brandPalette", {}),
        "captionPreset": library.get("captionPreset", "Hormozi Bounce"),
        "narratorVoice": library.get("narratorVoice", {}),
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def load_series_history(name: str, limit: int = 25) -> List[dict]:
    path = channel_dir(name) / "series_history.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data.get("videos", []) if isinstance(data, dict) else []
    except (OSError, ValueError):
        rows = []
    return rows[-max(0, min(25, int(limit))):]


def append_series_history(name: str, project_id: str, plan: planner.CinematicPlan) -> Path:
    path = channel_dir(name) / "series_history.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
        rows = stored.get("videos", []) if isinstance(stored, dict) else []
    except (OSError, ValueError):
        rows = []
    if not any(item.get("project_id") == project_id for item in rows):
        rows.append({
            "project_id": project_id,
            "completed_at": _now(),
            "title": plan.title,
            "topic": plan.logline or plan.storyline[:240],
            "opening_hook": plan.hook,
        })
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"videos": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def flag_hero_scenes(plan: planner.CinematicPlan) -> None:
    """Mark the first three to five hook scenes and up to two explicit climax scenes."""
    elapsed = 0.0
    hook_candidates = []
    climax_candidates = []
    for index, scene in enumerate(plan.scenes):
        scene_start = elapsed
        elapsed += max(0.0, float(scene.duration or 0))
        if scene_start < 30 and len(hook_candidates) < 5:
            hook_candidates.append(scene)
        words = f"{scene.act} {scene.title} {scene.emotion}".lower()
        if any(term in words for term in ("climax", "final reveal", "turning point", "showdown")):
            climax_candidates.append(scene)
    target_hook_count = min(5, max(3, len(hook_candidates))) if hook_candidates else 0
    for index, scene in enumerate(hook_candidates):
        scene.is_hero_scene = index < target_hook_count
    for scene in climax_candidates[-2:]:
        scene.is_hero_scene = True


def hero_beat_rows(p: "Project") -> List[dict]:
    """Return stable scene-index records for Hero clip controls and project resume."""
    if not p.plan:
        return []
    existing = p.data.get("visual_beats") or []
    by_scene = {str(item.get("scene_id")): item for item in existing if item.get("scene_id")}
    elapsed = 0.0
    rows = []
    for index, scene in enumerate(p.plan.scenes):
        end = elapsed + max(0.0, float(scene.duration or 0))
        old = by_scene.get(scene.id, {})
        row = dict(old)
        row.update({"scene_id": scene.id, "scene_index": index, "start": elapsed, "end": end,
                    "title": scene.title, "text": scene.narration, "visual_prompt": scene.video_prompt or scene.image_prompt,
                    "is_hero_scene": bool(scene.is_hero_scene or old.get("is_hero_scene"))})
        if scene.hero_clip:
            row["hero_clip"] = scene.hero_clip
        rows.append(row)
        elapsed = end
    p.data["hero_beats"] = rows
    return rows


class _ArticleTextParser(html.parser.HTMLParser):
    _SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "header", "head"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self.skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data):
        if not self.skip_depth:
            text = re.sub(r"\s+", " ", html.unescape(data)).strip()
            if text:
                self.parts.append(text)


def _validate_public_article_url(url: str) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise ValueError("Enter a valid http(s) article URL.")
    if parsed.username or parsed.password or parsed.port not in (None, 80, 443):
        raise ValueError("Article URLs with credentials or non-standard ports are not supported.")
    host = parsed.hostname.rstrip(".").lower()
    if host in ("localhost", "localhost.localdomain"):
        raise ValueError("Localhost URLs are not supported.")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses = [ipaddress.ip_address(result[4][0])
                         for result in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))]
        except OSError as exc:
            raise ValueError("The article host could not be resolved.") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("Private and local network URLs are not supported.")
    return parsed


class _PublicArticleRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_public_article_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_article_text(url: str) -> str:
    _validate_public_article_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "TubeForgeStudio/1.0"})
    opener = urllib.request.build_opener(_PublicArticleRedirectHandler())
    with opener.open(request, timeout=20) as response:
        if response.status >= 400:
            raise RuntimeError(f"Article fetch failed with HTTP {response.status}.")
        body = response.read(2_000_000)
        content_type = response.headers.get("Content-Type", "")
    if "html" in content_type.lower():
        parser = _ArticleTextParser()
        parser.feed(body.decode("utf-8", errors="replace"))
        text = "\n".join(parser.parts)
    else:
        text = body.decode("utf-8", errors="replace")
    text = re.sub(r"[ \t]+\n", "\n", text)
    if len(text.strip()) < 50:
        raise RuntimeError("The page did not contain enough readable article text.")
    return text[:20000]


def attach_hero_clip(p: "Project", beat_index: int, uploaded_bytes: bytes, filename: str) -> Path:
    if not uploaded_bytes:
        raise ValueError("Hero clip upload is empty.")
    if len(uploaded_bytes) > 500 * 1024 * 1024:
        raise ValueError("Hero clips are limited to 500 MB.")
    beats = p.data.get("hero_beats") or hero_beat_rows(p)
    if not 0 <= int(beat_index) < len(beats):
        raise ValueError("Unknown hero beat.")
    directory = p.dir / "hero_clips"
    directory.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).name)[:80] or "hero.mp4"
    target = directory / f"beat_{int(beat_index) + 1:02d}_{safe}"
    temporary = target.with_suffix(target.suffix + ".part")
    temporary.write_bytes(uploaded_bytes)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_type",
         "-of", "csv=p=0", str(temporary)], capture_output=True, text=True, timeout=30,
    )
    if probe.returncode or "video" not in probe.stdout:
        temporary.unlink(missing_ok=True)
        raise ValueError("Uploaded Hero asset is not a readable video.")
    temporary.replace(target)
    beats[int(beat_index)]["hero_clip"] = str(target)
    beats[int(beat_index)]["hero_clip_sha256"] = hashlib.sha256(uploaded_bytes).hexdigest()
    p.data["hero_beats"] = beats
    scene_id = beats[int(beat_index)].get("scene_id")
    if p.plan and scene_id:
        for scene in p.plan.scenes:
            if scene.id == scene_id:
                scene.hero_clip = str(target)
                break
        p.set_plan(p.plan)
    p.save()
    return target


def extract_hero_last_frame(clip: Path, output: Path) -> Path:
    clip, output = Path(clip), Path(output)
    if not clip.is_file():
        raise FileNotFoundError(f"Hero clip not found: {clip}")
    output.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["ffmpeg", "-y", "-sseof", "-0.05", "-i", str(clip), "-frames:v", "1",
         "-update", "1", str(output)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode or not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError(f"Could not extract Hero clip final frame: {result.stderr[-300:]}")
    return output


def save_project_channel_library(p: "Project") -> Path:
    bible = copy.deepcopy(ensure_visual_bible(p))
    root = channel_dir(str(p.settings.get("channel_name", "")))
    asset_dir = root / "assets"
    asset_dir.mkdir(parents=True, exist_ok=True)
    if bible.get("masterReferencePath") and Path(bible["masterReferencePath"]).is_file():
        target = asset_dir / f"master_{Path(bible['masterReferencePath']).name}"
        shutil.copy2(bible["masterReferencePath"], target)
        bible["masterReferencePath"] = str(target)
    for character in bible.get("characters", []):
        source = character.get("referenceImagePath")
        if source and Path(source).is_file():
            target = asset_dir / f"{character.get('id', 'character')}_{Path(source).name}"
            shutil.copy2(source, target)
            character["referenceImagePath"] = str(target)
    return save_channel_library(str(p.settings.get("channel_name", "")), {
        "visualBible": bible,
        "characters": bible.get("characters", []),
        "locations": p.data.get("channelLocations", []),
        "brandPalette": {key: p.settings.get(key) for key in
                         ("brand_primary_color", "brand_secondary_color", "brand_accent_color")},
        "captionPreset": p.settings.get("caption_preset", "Hormozi Bounce"),
        "narratorVoice": {"engine": p.settings.get("voice_engine"), "voice": p.settings.get("voice")},
    })


def apply_channel_library(p: "Project") -> bool:
    name = str(p.settings.get("channel_name", ""))
    library = load_channel_library(name)
    bible = library.get("visualBible") or {}
    if not bible:
        return False
    bible = copy.deepcopy(bible)
    project_assets = p.dir / "visual_bible" / "channel_library"
    project_assets.mkdir(parents=True, exist_ok=True)
    for key in ("masterReferencePath",):
        source = bible.get(key)
        if source and Path(source).is_file():
            target = project_assets / Path(source).name
            shutil.copy2(source, target)
            bible[key] = str(target)
    for character in bible.get("characters", []):
        source = character.get("referenceImagePath")
        if source and Path(source).is_file():
            target = project_assets / Path(source).name
            shutil.copy2(source, target)
            character["referenceImagePath"] = str(target)
    p.data["visualBible"] = bible
    palette = library.get("brandPalette") or {}
    for key, value in palette.items():
        if value:
            p.settings[key] = value
    p.settings["caption_preset"] = library.get("captionPreset", p.settings.get("caption_preset"))
    narrator = library.get("narratorVoice") or {}
    p.settings["voice_engine"] = narrator.get("engine") or p.settings.get("voice_engine")
    p.settings["voice"] = narrator.get("voice") or p.settings.get("voice")
    p.data["channelLocations"] = library.get("locations", [])
    p.save()
    return True


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
            self.data["plan"] = plan.model_dump(by_alias=True)
            self.data["rev"] = int(self.data.get("rev") or 0) + 1
            states = self.data.setdefault("scene_state", {})
            for i, sc in enumerate(plan.scenes):
                st = states.setdefault(sc.id, {})
                st.setdefault("seed", self._seed_for(i))
            keep = {sc.id for sc in plan.scenes}
            for k in list(states):
                if k not in keep:
                    states.pop(k)
            ensure_visual_bible(self)

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


def _default_visual_bible(project_id: str, required: bool = True) -> Dict[str, Any]:
    return {
        "visualMode": "stylized_3d",
        "stylePrefix": "",
        "globalNegativePrompt": VISUAL_BIBLE_NEGATIVE,
        "seedBase": int(hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:8], 16),
        "characters": [],
        "masterReferencePath": "",
        "approved": False,
        "required": required,
    }


def ensure_visual_bible(p: Project) -> Dict[str, Any]:
    """Backfills the Visual Bible without discarding legacy character references."""
    data = getattr(p, "data", None)
    if not isinstance(data, dict):
        return _default_visual_bible(str(getattr(p, "id", "project")), required=False)
    bible = data.get("visualBible")
    if not isinstance(bible, dict):
        bible = _default_visual_bible(str(data.get("id", "project")), required=False)
        data["visualBible"] = bible
    defaults = _default_visual_bible(str(data.get("id", "project")))
    for key, value in defaults.items():
        bible.setdefault(key, copy.deepcopy(value))
    if bible.get("visualMode") not in ("stylized_3d", "stylized_2d", "cinematic_doc"):
        bible["visualMode"] = "stylized_3d"
    try:
        bible["seedBase"] = int(bible["seedBase"]) % (2 ** 31 - 1)
    except (TypeError, ValueError):
        bible["seedBase"] = defaults["seedBase"]

    plan = p.plan if hasattr(p, "plan") else None
    if plan:
        existing = {str(item.get("name", "")).lower(): item for item in bible.get("characters", [])
                    if isinstance(item, dict)}
        old_refs = getattr(p, "settings", {}).get("char_refs", {})
        characters = []
        for character in plan.characters:
            previous = existing.get(character.name.lower(), {})
            reference = old_refs.get(character.name) or previous.get("referenceImagePath", "")
            if reference and not Path(reference).is_file():
                reference = previous.get("referenceImagePath", "")
            characters.append({
                "id": previous.get("id") or hashlib.sha256(character.name.lower().encode("utf-8")).hexdigest()[:12],
                "name": character.name,
                "description": character.look,
                "referenceImagePath": reference,
            })
        if bible.get("approved") and characters != bible.get("characters", []):
            bible["approved"] = False
        bible["characters"] = characters
    return bible


_OPEN: Dict[str, Project] = {}
_OPEN_LOCK = threading.Lock()


def create_project(idea: str, settings: Optional[Dict[str, Any]] = None) -> Project:
    project_id = _new_id(idea[:40])
    data = {"id": project_id, "created": _now(), "updated": _now(), "idea": idea,
            "settings": {**DEFAULT_SETTINGS, **NEW_PROJECT_SETTINGS, **(settings or {})},
            "visualBible": _default_visual_bible(project_id), "plan": None, "scene_state": {}, "logs": [],
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
        had_visual_bible = isinstance(data.get("visualBible"), dict)
        if not had_visual_bible:
            data["visualBible"] = _default_visual_bible(pid, required=False)
        job = data.setdefault("job", {"status": "idle"})
        if job.get("status") == "running":  # process restarted mid-job
            job["status"] = "interrupted"
            job["stage"] = "Interrupted (app restarted) - press Resume"
            for st in data.get("scene_state", {}).values():
                if st.get("status") in ("visual", "voice", "rendering"):
                    st["status"] = "pending"
        p = Project(data)
        original_bible = copy.deepcopy(data.get("visualBible"))
        ensure_visual_bible(p)
        if original_bible != p.data.get("visualBible"):
            p.save()
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
    data.pop("alignment_word_count", None)
    data.pop("visual_beats", None)
    for st in data["scene_state"].values():
        for k in list(st):
            if k not in ("seed", "locked_image", "upload", "variations"):
                st.pop(k)
    p = Project(data)
    bible = ensure_visual_bible(p)
    master = bible.get("masterReferencePath", "")
    if master and Path(master).is_file():
        target = p.dir / "visual_bible" / Path(master).name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(master, target)
        bible["masterReferencePath"] = str(target)
    for character in bible.get("characters", []):
        source = character.get("referenceImagePath", "")
        if source and Path(source).is_file():
            target = p.dir / "visual_bible" / "characters" / Path(source).name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            character["referenceImagePath"] = str(target)
            p.settings.setdefault("char_refs", {})[character["name"]] = str(target)
        elif character.get("referenceImagePath"):
            character["referenceImagePath"] = ""
    external_voiceover = data.get("settings", {}).get("external_voiceover_path", "")
    if external_voiceover and Path(external_voiceover).exists():
        voiceover_copy = p.dir / "audio" / Path(external_voiceover).name
        voiceover_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(external_voiceover, voiceover_copy)
        p.settings["external_voiceover_path"] = str(voiceover_copy)
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
    engine = s.get("voice_engine", "edge")
    opts = [v for v, _ in tts.voice_options(s["language"], engine) if v != s["voice"]]
    plan = p.plan
    desc = ""
    if plan:
        desc = next((c.voice + " " + c.look for c in plan.characters if c.name.lower() == speaker.lower()), "")
    female = bool(re.search(r"\b(female|woman|girl|she|her)\b", desc.lower()))
    labels = dict(tts.voice_options(s["language"], engine))
    pool = [v for v in opts if ("(F)" in labels.get(v, "")) == female] or opts or [s["voice"]]
    return pool[int(hashlib.sha1(speaker.encode()).hexdigest(), 16) % len(pool)]


def voice_parts(p: Project, sc: planner.Scene) -> List[Tuple[str, str]]:
    s = p.settings
    engine = s.get("voice_engine", "kokoro")
    available = {v for v, _ in tts.voice_options(s.get("language", "English"), engine)}
    default_voice = s.get("voice", "")
    if default_voice not in available and available:
        default_voice = next(iter(available))
    parts = []
    if sc.narration.strip():
        parts.append((sc.narration.strip(), default_voice))
    for d in sc.dialogue:
        if d.line.strip():
            v = _voice_for_speaker(p, d.speaker) if s.get("dialogue_voices", True) else default_voice
            if v not in available:
                v = default_voice
            parts.append((d.line.strip(), v))
    return parts


def build_image_prompt(p: Project, sc: planner.Scene) -> str:
    plan = p.plan
    prompt = planner.build_image_prompt(plan, sc, p.settings["style"]) if plan else sc.image_prompt
    bible = ensure_visual_bible(p)
    if bible.get("approved") and bible.get("stylePrefix"):
        prompt = f"{bible['stylePrefix']}. {prompt}"
    return prompt[:2000]


def scene_refs(p: Project, sc: planner.Scene) -> List[Path]:
    refs = []
    bible = ensure_visual_bible(p)
    if bible.get("approved"):
        master = bible.get("masterReferencePath", "")
        if master and Path(master).exists():
            refs.append(Path(master))
    cref = p.settings.get("char_refs") or {}
    for name in sc.characters:
        visual_character = next((item for item in bible.get("characters", [])
                                 if item.get("name", "").lower() == name.lower()), {})
        path = (visual_character.get("referenceImagePath") or cref.get(name)
                or next((v for k, v in cref.items() if k.lower() == name.lower()), None))
        if path and Path(path).exists():
            refs.append(Path(path))
    return list(dict.fromkeys(refs))[:4]


def _negative(p: Project, sc: planner.Scene) -> str:
    bible = ensure_visual_bible(p)
    visual_negative = bible.get("globalNegativePrompt", "") if bible.get("approved") else ""
    return ", ".join(x for x in (sc.negative_prompt, p.settings.get("negative_global", ""), visual_negative) if x)


MOTION_PRESETS = ("dolly_in", "dolly_out", "orbital_left", "orbital_right", "circle", "vertical_rise")
MOTION_CAMERA_MOVES = {
    "dolly_in": "Slow dolly in",
    "dolly_out": "Dolly out (reveal)",
    "orbital_left": "Tracking shot left",
    "orbital_right": "Tracking shot right",
    "circle": "Orbit",
    "vertical_rise": "Crane up",
}


def auto_choreograph(plan: planner.CinematicPlan) -> None:
    for index, scene in enumerate(plan.scenes):
        preset = MOTION_PRESETS[index % len(MOTION_PRESETS)]
        if index and preset == plan.scenes[index - 1].motion_preset:
            preset = MOTION_PRESETS[(index + 1) % len(MOTION_PRESETS)]
        scene.motion_preset = preset
        scene.camera_move = MOTION_CAMERA_MOVES[preset]


def visual_bible_fingerprint(bible: Dict[str, Any]) -> str:
    return _fp(
        bible.get("visualMode"), bible.get("stylePrefix"), bible.get("globalNegativePrompt"),
        bible.get("seedBase"), _reference_sig(bible.get("masterReferencePath", "")),
        [(item.get("id"), item.get("description"), _reference_sig(item.get("referenceImagePath", "")))
         for item in bible.get("characters", [])],
    )


def ensure_image(p: Project, sc: planner.Scene, log: Callable[[str], None]) -> Path:
    """AI still for the scene (or the user's locked upload / chosen variation)."""
    st = p.state(sc.id)
    if st.get("locked_image") and Path(st["locked_image"]).exists():
        return Path(st["locked_image"])
    W, H, _ = render_size(p.settings)
    prompt = build_image_prompt(p, sc)
    refs = scene_refs(p, sc)
    s = p.settings
    bible = ensure_visual_bible(p)
    seed = st.get("seed")
    if bible.get("approved"):
        plan = p.plan
        index = next((i for i, scene in enumerate(plan.scenes) if scene.id == sc.id), 0) if plan else 0
        seed = (int(bible["seedBase"]) + index * 104729) % (2 ** 31 - 1) or 1
        st["seed"] = seed
    if bible.get("approved"):
        visual_fingerprint = (bible.get("visualMode"), bible.get("stylePrefix"), bible.get("globalNegativePrompt"),
                              bible.get("seedBase"))
        fp = _fp("img", prompt, _negative(p, sc), s["image_engine"], seed, visual_fingerprint, W, H,
                 [_reference_sig(str(r)) for r in refs], s.get("openrouter_image_model"), s["allow_paid"],
                 s["allow_stock"])
    else:
        fp = _fp("img", prompt, _negative(p, sc), s["image_engine"], seed, W, H,
                 [_file_sig(str(r)) for r in refs], s.get("openrouter_image_model"), s["allow_paid"],
                 s["allow_stock"])
    out = p.scene_dir(sc.id) / "image.jpg"
    if st.get("image_fp") == fp and out.exists():
        return out
    res = images.generate_image(prompt, out, W, H, seed=seed, engine=s["image_engine"], refs=refs,
                                negative=_negative(p, sc), stock_query=sc.stock_query,
                                openrouter_model=s.get("openrouter_image_model", ""), allow_paid=s["allow_paid"],
                                allow_stock=s["allow_stock"], log=log)
    with p.lock:
        st.update(image_fp=fp, image=str(out), image_engine=res.engine, image_model=res.model,
                  image_warnings=res.warnings, image_prompt_used=prompt)
        if res.engine in ("pexels", "pixabay"):
            st.update(image_provider="Pexels" if res.engine == "pexels" else "Pixabay",
                      image_credit=res.model)
        else:
            st.pop("image_provider", None)
            st.pop("image_credit", None)
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
            credit = f"{clip.provider} video #{clip.id} by {clip.user} ({clip.page})"
            with p.lock:
                st.update(stock_fp=fp, stock=str(out), stock_credit=credit,
                          stock_page=clip.page, stock_provider=clip.provider)
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
    external_voiceover = s.get("external_voiceover_path", "")
    if external_voiceover:
        if not Path(external_voiceover).exists():
            raise RuntimeError(f"Imported voiceover is missing: {external_voiceover}")
        return []
    parts = voice_parts(p, sc)
    engine = s.get("voice_engine", "kokoro")
    fp = _fp("voice", parts, engine, s["voice_speed"], s["voice_pitch"], sc.emotion)
    if st.get("voice_fp") == fp and all(Path(x["path"]).exists() for x in st.get("voice_parts", [])):
        return st["voice_parts"]
    out_parts, cursor = [], 0.0
    for i, (text, voice_id) in enumerate(parts):
        suffix = ".wav" if engine in ("kokoro", "sarvam", "groq_orpheus") else ".mp3"
        path = p.scene_dir(sc.id) / f"voice_{i}{suffix}"
        res = tts.synthesize(text, path, engine=engine, voice=voice_id, speed=s["voice_speed"],
                             pitch_hz=int(s["voice_pitch"]), emotion=sc.emotion, log=log,
                             language=s.get("language", "English"))
        for warning in res.warnings:
            log(f"⚠️ {warning}")
        out_parts.append({"path": str(res.path), "offset": cursor, "duration": res.duration, "voice": res.voice,
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
        caption_chunks=chunks,
        caption_style="None" if s.get("kinetic_captions", False) else s["subtitle_style"],
        overlay_text=overlay, overlay_kind=sc.overlay_kind,
        overlay_start=0.4, overlay_end=max(1.5, min(4.5, clip_len - 1.0)), seed=int(p.state(sc.id).get("seed", 1)),
        video_source=sc.video_source, motion_preset=sc.motion_preset,
        depth_intensity=sc.depth_intensity, depth_dof=float(sc.depth_dof), scene_index=idx + 1,
        procedural_props=procedural.scene_props(
            sc, s, clip_len, p.plan.research_sources if p.plan else [],
        ) if sc.video_source == "procedural_3d_2d" else {},
        resample=s.get("resample", "bilinear"),
        channel_name=str(s.get("channel_name", "")),
        channel_logo_path=Path(s["channel_logo_path"]) if s.get("channel_logo_path") and Path(s["channel_logo_path"]).exists() else None,
        brand_position=str(s.get("brand_position", "Top-Right")),
        brand_overlay=bool(s.get("brand_overlay", True)),
        work_dir=p.scene_dir(sc.id) / "work")


def preview_procedural_scene(p: Project, scene_id: str, should_cancel: Callable[[], bool]) -> Path:
    plan = p.plan
    if not plan:
        raise RuntimeError("Generate a storyboard before previewing a motion graphic.")
    index = next((i for i, scene in enumerate(plan.scenes) if scene.id == scene_id), None)
    if index is None:
        raise RuntimeError("The selected scene is no longer in this project.")
    sc = plan.scenes[index]
    width, height, _ = render_size(p.settings)
    duration = max(1.0, min(30.0, float(sc.duration)))
    output = p.scene_dir(sc.id) / f"scene_{index + 1:02d}_procedural_preview.png"
    payload = {
        "mode": "still",
        "scene_dir": str(p.scene_dir(sc.id)),
        "output": str(output),
        "scene_index": index + 1,
        "frame": int(duration * p.settings["fps"] / 2),
        "props": procedural.scene_props(sc, p.settings, duration, plan.research_sources),
        "duration": duration,
        "width": width,
        "height": height,
        "fps": int(p.settings["fps"]),
    }
    worker = Path(__file__).resolve().parents[1] / "workers" / "procedural_worker.py"
    render._run_motion_worker(worker, payload, p.scene_dir(sc.id) / "work", should_cancel)
    p.save()
    return output


def spec_fingerprint(spec: render.SceneRenderSpec) -> str:
    d = {k: (str(v) if isinstance(v, Path) else v) for k, v in spec.__dict__.items()
         if k not in ("out_path", "work_dir", "caption_chunks", "video_source", "motion_preset",
                      "depth_intensity", "depth_dof", "scene_index", "procedural_props")}
    if spec.video_source != "original_motion":
        d["videoSource"] = spec.video_source
        d["motion_preset"] = spec.motion_preset
        d["depth_intensity"] = spec.depth_intensity
        d["depth_dof"] = spec.depth_dof
        if spec.video_source == "procedural_3d_2d":
            d["procedural_props"] = spec.procedural_props
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
        voices = tts.voice_options(s["language"], s.get("voice_engine", "kokoro"))
        if not voices:
            voices = tts.voice_options(s["language"], "edge")
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
    history = load_series_history(str(s.get("channel_name", "")), 25)
    if history:
        extra += (
            "\nCHANNEL SERIES HISTORY (avoid repeating topics, titles, and especially opening lines; "
            "do not refer to old episodes unless asked):\n"
            + json.dumps(history, ensure_ascii=False)[:12000]
        )
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
    flag_hero_scenes(plan)
    for sc in plan.scenes:
        if sc.source == "ai_image" and s.get("default_source") in ("original_motion", "stock_video"):
            sc.source = s["default_source"]
        elif sc.source == "ai_video":
            sc.source = "original_motion"
        if sc.video_source == "original_motion" and s.get("default_video_source") in (
                "depthflow_3d", "cutout_2d"):
            sc.video_source = s["default_video_source"]
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
        cached = st.get("clip_fp") == fp and spec.out_path.exists()
        if cached and not render.video_is_decodable(spec.out_path, cancelled):
            log(f"⚠️ Cached clip for “{sc.title}” is damaged; rendering it again")
            cached = False
        if not cached:
            _set_scene(p, sc.id, status="rendering", progress=0.4)
            t0 = time.time()
            render.render_scene(spec, progress=lambda f: _set_scene(p, sc.id, progress=0.4 + 0.6 * f),
                                should_cancel=cancelled, log=log)
            if not render.video_is_decodable(spec.out_path, cancelled):
                spec.out_path.unlink(missing_ok=True)
                raise RuntimeError(f"Rendered clip for “{sc.title}” is incomplete or cannot be decoded.")
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


def _align_project_voice(p: Project) -> List[dict]:
    """Aligns a completed voice track as soon as every scene voice asset is ready."""
    plan = p.plan
    if not plan or not plan.scenes:
        return []
    external_voiceover = p.settings.get("external_voiceover_path", "")
    scene_runtime = 0.0
    if external_voiceover:
        source = Path(external_voiceover)
        if not source.is_file():
            raise RuntimeError(f"Imported voiceover is missing: {source}")
        durations = [max(float(scene.duration), 0.1) for scene in plan.scenes]
        transitions = [scene_transition(p, scene)[1] for scene in plan.scenes[:-1]]
        scene_runtime = render.total_length(durations, transitions)
    else:
        if any("voice_parts" not in p.state(scene.id) for scene in plan.scenes):
            return []
        scenes = plan.scenes
        transitions = [scene_transition(p, scene) for scene in scenes[:-1]]
        durations = [
            float(p.state(scene.id).get("clip_len") or
                  scene_length(p, scene, p.state(scene.id).get("voice_parts", []),
                               transitions[index][1] if index < len(transitions) else 0.0))
            for index, scene in enumerate(scenes)
        ]
        if p.settings.get("end_card", True):
            transitions.append(cinema.TRANSITIONS["Fade through black"])
            durations.append(float(p.settings.get("end_card_len") or 5.0))
        transition_durations = [transition[1] for transition in transitions]
        starts = render.timeline(durations, transition_durations)
        total = render.total_length(durations, transition_durations)
        scene_runtime = render.total_length(durations[:len(scenes)], transition_durations[:max(0, len(scenes) - 1)])
        track = audio.Track(p.dir / "work" / "alignment_voice.wav", total)
        has_voice = False
        for index, scene in enumerate(scenes):
            for part in p.state(scene.id).get("voice_parts", []):
                part_path = Path(part["path"])
                if not part_path.is_file():
                    raise RuntimeError(f"Generated voice part is missing: {part_path}")
                track.add(audio.decode(part_path), starts[index] + LEAD_IN + float(part["offset"]), 1.0)
                has_voice = True
        if not has_voice:
            return []
        source = track.write(p.dir / "work" / "alignment_voice.wav")
    language = {"English": "en", "Hindi": "hi"}.get(p.settings.get("language", "English"))
    words = alignment.align_project_audio(
        source, p.dir, language=language, log=p.log,
        model_size=str(p.settings.get("alignment_model", "tiny")),
    )
    p.data["alignment_word_count"] = len(words)
    if words and scene_runtime > 0:
        aligned = [
            {"start": beat["start"], "end": beat["end"],
             "text": " ".join(word["word"] for word in beat["words"])}
            for beat in alignment.visual_beat_intervals(words, scene_runtime)
        ]
        alignment.persist_distinct_beats(p.data, aligned, alignment.audio_sha256(source))
    p.save(force=False)
    return words


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
    if not scene_ids or assemble:
        _align_project_voice(p)
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
    return f"{sec // 3600:02d}:{sec // 60 % 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60:02d}:{sec % 60:02d}"


def _alignment_chapters(words: List[dict], total: float) -> List[Tuple[float, str]]:
    """Build readable chapter boundaries from word timing, with a scene-plan fallback."""
    valid = [item for item in words if item.get("word") and float(item.get("end", 0)) > float(item.get("start", 0))]
    if not valid:
        return []
    starts = [0.0]
    for index, item in enumerate(valid):
        end = float(item["end"])
        next_start = float(valid[index + 1]["start"]) if index + 1 < len(valid) else total
        boundary = bool(re.search(r"[.!?][\"')\]]?$", str(item["word"]))) or next_start - end >= 0.8
        if boundary and end - starts[-1] >= 18 and end < total - 10:
            starts.append(end)
        elif end - starts[-1] >= 45 and end < total - 10:
            starts.append(end)
    starts = sorted(set(round(value, 3) for value in starts))
    if len(starts) < 3 and total >= 30:
        interval = max(10.0, total / 3.0)
        starts = [0.0, round(interval, 3), round(interval * 2.0, 3)]
    chapters = []
    for start in starts:
        following = [item["word"] for item in valid if start <= float(item["start"]) < start + 8]
        title = " ".join(following[:5]).strip(" ,.!?;:")
        if title:
            chapters.append((start, title[:64]))
    return chapters


def _render_hero_clip(p: Project, scene: planner.Scene, duration: float) -> Optional[Path]:
    source = Path(scene.hero_clip) if scene.hero_clip else None
    if not source or not source.is_file():
        return None
    width, height, _ = render_size(p.settings)
    digest_builder = hashlib.sha256()
    with source.open("rb") as source_file:
        for block in iter(lambda: source_file.read(1024 * 1024), b""):
            digest_builder.update(block)
    digest = digest_builder.hexdigest()
    key = hashlib.sha256(f"{digest}:{duration:.3f}:{width}:{height}:{p.settings['fps']}".encode()).hexdigest()[:20]
    target = p.dir / "hero_clips" / "rendered" / f"{key}.mp4"
    if target.is_file() and target.stat().st_size and render.video_is_decodable(target):
        return target
    target.unlink(missing_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.stem + ".part.mp4")
    temporary.unlink(missing_ok=True)
    result = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-stream_loop", "-1", "-i", str(source), "-t", f"{duration:.4f}",
         "-an", "-vf", f"fps={int(p.settings['fps'])},scale={width}:{height}:force_original_aspect_ratio=increase:"
                         f"flags=lanczos,crop={width}:{height},format=yuv420p",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-r", str(int(p.settings["fps"])),
         "-movflags", "+faststart", str(temporary)],
        capture_output=True, text=True, timeout=max(120, int(duration * 20)),
    )
    if result.returncode or not temporary.is_file() or temporary.stat().st_size == 0:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Hero clip normalization failed for {scene.title}: {result.stderr[-300:]}")
    if not render.video_is_decodable(temporary):
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Normalized Hero clip for {scene.title} is incomplete or cannot be decoded.")
    temporary.replace(target)
    return target


def monetization_audit(p: Project) -> Dict[str, Any]:
    """Non-blocking readiness checks based only on persisted project settings and assets."""
    plan = p.plan
    scenes = plan.scenes if plan else []
    states = [p.state(scene.id) for scene in scenes]
    beats = p.data.get("visual_beats") or []
    hook_beats = [beat for beat in beats if float(beat.get("start", 0)) < 25]
    engines = {scene.video_source for scene in scenes if scene.video_source}
    fingerprints = [
        (state.get("image_hash") or state.get("image") or state.get("stock") or "",
         scene.video_source, scene.camera_move)
        for scene, state in zip(scenes, states)
    ]
    repeated = any(left == right for left, right in zip(fingerprints, fingerprints[1:]))
    max_static = max((float(scene.duration or 0) for scene in scenes
                      if str(scene.camera_move or "").strip().lower() in ("none", "static", "hold", "no movement")),
                     default=0.0)
    bible = ensure_visual_bible(p)
    checks = [
        {"name": "Production TTS", "passed": p.settings.get("voice_engine") not in ("edge", "edge_tts"),
         "detail": "Edge TTS is draft preview only." if p.settings.get("voice_engine") in ("edge", "edge_tts")
         else "A production voice engine is selected."},
        {"name": "Visual Bible", "passed": bool(bible.get("approved")),
         "detail": "Visual Bible is locked." if bible.get("approved") else "Approve and lock the Visual Bible."},
        {"name": "Scene assets", "passed": bool(scenes) and all(
            state.get("status") == "done" and state.get("clip") and Path(state["clip"]).is_file()
            for state in states), "detail": "Every planned scene has a rendered clip."},
        {"name": "Hook pacing", "passed": len(hook_beats) >= 5 and not repeated,
         "detail": f"{len(hook_beats)} distinct persisted beats in the first 25 seconds."},
        {"name": "Static beat duration", "passed": max_static <= 7.5,
         "detail": f"Longest static beat: {max_static:.1f}s."},
        {"name": "Visual diversity", "passed": len(engines) >= 2,
         "detail": f"{len(engines)} motion engine(s) in the current plan."},
        {"name": "Audio mastering", "passed": abs(float(p.settings.get("loudness", -14.0)) + 14.0) <= 0.5
         and -26.0 <= float(p.settings.get("duck_db", -24.0)) <= -22.0,
         "detail": f"Target {float(p.settings.get('loudness', -14.0)):.1f} LUFS; music duck {float(p.settings.get('duck_db', -24.0)):.1f} dB."},
    ]
    return {"passed": all(item["passed"] for item in checks), "checks": checks,
            "ai_disclosure": "Stylized 2D/3D" if bible.get("visualMode") in ("stylized_2d", "stylized_3d")
            else "Photorealistic or mixed; review disclosure requirements."}


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
        duration = float(st["clip_len"])
        clips.append(_render_hero_clip(p, sc, duration) or Path(st["clip"]))
        durs.append(duration)
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
    external_voiceover = s.get("external_voiceover_path", "")
    has_voice = bool(external_voiceover)
    if external_voiceover:
        if not Path(external_voiceover).exists():
            raise RuntimeError(f"Imported voiceover is missing: {external_voiceover}")
        voiceover_duration = tts.probe_duration(Path(external_voiceover))
        if voiceover_duration <= 0:
            raise RuntimeError(f"Could not read imported voiceover duration: {external_voiceover}")
        if voiceover_duration > total + 0.05:
            raise RuntimeError(
                f"Imported voiceover is {voiceover_duration:.1f}s but the film timeline is {total:.1f}s; "
                "extend the project duration before rendering."
            )
        voice_tr.add(audio.decode(Path(external_voiceover)), 0.0, 1.0)
    for i, sc in enumerate(scenes):
        st = p.state(sc.id)
        S = starts[i]
        for part in st.get("voice_parts", []):
            if not external_voiceover:
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
                effect_duration = sig.shape[0] / audio.SR
                peak_offset = (effect_duration / 2.0 if sc.sfx == "whoosh"
                               else max(0.0, effect_duration - 0.1) if sc.sfx == "riser" else 0.0)
                fx_tr.add(sig, max(0.0, S - peak_offset), 0.8)
        if s.get("transition_sfx", True) and i < len(trans) and trans[i][0] in FAST_TRANSITIONS:
            nxt = scenes[i + 1] if i + 1 < n else None
            if not (nxt and s.get("sfx_on", True) and nxt.sfx and nxt.sfx != "none"):
                wp = audio.sfx("whoosh")
                if wp:
                    signal = audio.decode(wp)
                    peak_offset = signal.shape[0] / audio.SR / 2.0
                    fx_tr.add(signal, max(0.0, starts[i + 1] - peak_offset), 0.6)
    if len(clips) > n:  # end card
        cues.append((starts[n], total, "resolve"))
    voice_wav = voice_tr.write(work / "voice.wav")
    if not has_voice:
        voice_wav = None
    aligned_words = []
    if voice_wav:
        language = {"English": "en", "Hindi": "hi"}.get(s.get("language", "English"))
        alignment_source = Path(external_voiceover) if external_voiceover else voice_wav
        aligned_words = alignment.align_project_audio(
            alignment_source, p.dir, language=language, log=p.log,
            model_size=str(s.get("alignment_model", "tiny")),
        )
        p.data["alignment_word_count"] = len(aligned_words)
        if aligned_words:
            aligned_captions = [{"start": word["start"], "end": word["end"], "text": word["word"]}
                                for word in aligned_words]
            all_chunks = render.chunk_words(aligned_captions, int(s.get("caption_words") or 4))
            scene_runtime = render.total_length(durs[:n], tdur[:max(0, n - 1)])
            visual_beats = alignment.visual_beat_intervals(aligned_words, scene_runtime)
            source_hash = alignment.audio_sha256(alignment_source)
            aligned = [
                {"start": beat["start"], "end": beat["end"],
                 "text": " ".join(word["word"] for word in beat["words"])}
                for beat in visual_beats
            ]
            alignment.persist_distinct_beats(p.data, aligned, source_hash)
            if s.get("transition_sfx", True) and s.get("sfx_on", True) and len(visual_beats) > 1:
                scene_boundaries = starts[1:n]
                for beat in visual_beats[1:]:
                    boundary = float(beat["start"])
                    if any(abs(boundary - scene_start) < 0.35 for scene_start in scene_boundaries):
                        continue
                    beat_index = max(
                        (index for index, scene_start in enumerate(starts[:n]) if scene_start <= boundary),
                        default=0,
                    )
                    scene_effect = scenes[beat_index].sfx
                    effect_kind = scene_effect if scene_effect in ("whoosh", "impact", "riser") else "whoosh"
                    effect_path = audio.sfx(effect_kind)
                    if effect_path:
                        effect_signal = audio.decode(effect_path)
                        effect_duration = effect_signal.shape[0] / audio.SR
                        peak_offset = (
                            effect_duration / 2.0 if effect_kind == "whoosh"
                            else max(0.0, effect_duration - 0.1) if effect_kind == "riser" else 0.0
                        )
                        fx_tr.add(effect_signal, max(0.0, boundary - peak_offset), 0.45)
            expanded_clips, expanded_durs, expanded_trans = [], [], []
            for scene_index in range(n):
                scene_start = starts[scene_index]
                scene_end = scene_start + durs[scene_index]
                local_cuts = [
                    beat["end"] - scene_start
                    for beat in visual_beats
                    if scene_start + 0.05 < beat["end"] < scene_end - 0.05
                ]
                clip_parts = render.split_video_clip(
                    clips[scene_index], [0.0] + local_cuts + [durs[scene_index]],
                    p.scene_dir(scenes[scene_index].id) / "beats", int(s["fps"]),
                )
                for part_index, part in enumerate(clip_parts):
                    if expanded_clips:
                        if part_index:
                            expanded_trans.append(("fade", 0.0))
                        elif scene_index:
                            expanded_trans.append(trans[scene_index - 1])
                    expanded_clips.append(part)
                    expanded_durs.append(max(0.05, tts.probe_duration(part)))
            if len(clips) > n:
                expanded_trans.append(trans[n - 1])
                expanded_clips.extend(clips[n:])
                expanded_durs.extend(durs[n:])
            clips, durs, trans = expanded_clips, expanded_durs, expanded_trans
            p.save(force=False)
    fx_wav = fx_tr.write(work / "fx.wav")
    mood = s.get("music_mood") if s.get("music_mood") not in ("auto", "", None) else plan.music_mood
    music_wav = None
    user_music = Path(s["music_path"]) if s.get("music_path") and Path(s["music_path"]).exists() else None
    if mood != "none" or user_music:
        music_wav = audio.build_music(work / "music.wav", total, mood if mood != "none" else "ambient", cues,
                                      seed=_music_seed(p.id), user_music=user_music)
    mix = audio.mix_final(voice_wav, music_wav, fx_wav, work / "mix.m4a", total, voice_vol=float(s["voice_vol"]),
                          music_vol=float(s["music_vol"]), fx_vol=float(s["fx_vol"]), duck=float(s["duck"]),
                          loudness=float(s["loudness"]), duck_db=float(s.get("duck_db", -24.0)))

    stage("📦 Final encode and export", 0.95)
    ext, codec = cinema.FORMATS.get(s["format"], ("mp4", "libx264"))
    q = cinema.QUALITY.get(s["quality"], cinema.QUALITY["High"])
    _, _, (FW, FH) = render_size(s)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", plan.title or "film").strip("_")[:40] or "film"
    final = out_dir / f"{slug}.{ext}"
    kinetic_ass = None
    if s.get("kinetic_captions", False) and aligned_words:
        kinetic_ass = render.write_kinetic_ass(aligned_words, out_dir / f"{slug}.ass", FW, FH,
                                               str(s.get("caption_color", "#22D3EE")),
                                               str(s.get("caption_preset", "Hormozi Bounce")))
    render.export_final(silent, mix, final, FW, FH, int(s["fps"]), codec, q["crf"], q["preset"],
                        float(s["fade_in"]), float(s["fade_out"]), total, subtitle_ass=kinetic_ass)
    srt = render.write_srt(all_chunks, out_dir / f"{slug}.srt")
    vtt = render.write_vtt(all_chunks, out_dir / f"{slug}.vtt")
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
    thumbnail_sources = [thumb_src]
    if s.get("thumb_variants", True):
        imgs = [Path(p.state(sc.id)["image"]) for sc in scenes if p.state(sc.id).get("image")
                and Path(p.state(sc.id)["image"]).exists() and Path(p.state(sc.id)["image"]) != thumb_src]
        climax = [i for i, sc in enumerate(scenes) if _cue_for(sc, i, n) == "climax"]
        if climax:  # prefer the most dramatic frames
            cimg = [Path(p.state(scenes[i].id).get("image", "")) for i in climax]
            imgs = [c for c in cimg if c in imgs] + [c for c in imgs if c not in cimg]
        for k, src in enumerate(imgs[:2], start=2):
            try:
                thumbnail_sources.append(src)
                thumbs.append(str(render.make_thumbnail(src, plan.youtube_title or plan.title,
                                                        out_dir / f"thumbnail_{k}.jpg",
                                                        channel_name=s.get("channel_name", ""), logo_path=logo_p,
                                                        hook=plan.hook, variant=k - 1)))
            except Exception as e:  # noqa: BLE001
                p.log(f"⚠️ Thumbnail variant {k} skipped: {e}")
    thumb_pngs = []
    for index, source_image in enumerate(thumbnail_sources[:3]):
        try:
            thumb_pngs.append(str(render.make_thumbnail(
                source_image, plan.youtube_title or plan.title, out_dir / f"thumbnail_{index + 1}.png",
                channel_name=s.get("channel_name", ""), logo_path=logo_p, hook=plan.hook, variant=index,
            )))
        except Exception as e:  # noqa: BLE001
            p.log(f"⚠️ PNG thumbnail variant {index + 1} skipped: {e}")
    short = ""
    if s.get("shorts_teaser", True) and FW > FH:
        stage("📱 Creating vertical Shorts / Reels teaser", 0.98)
        try:
            teaser_ass = None
            teaser_source = final
            teaser_audio = None
            if s.get("kinetic_captions", False) and aligned_words:
                teaser_ass = render.write_kinetic_ass(
                    aligned_words, work / f"{slug}_short.ass", FH * 9 // 16, FH,
                    str(s.get("caption_color", "#22D3EE")),
                    str(s.get("caption_preset", "Hormozi Bounce")),
                )
                teaser_source, teaser_audio = silent, mix
            short = str(render.make_vertical_teaser(
                teaser_source, out_dir / f"{slug}_short.mp4", plan.title,
                min(total, float(s.get("teaser_len") or 45.0), 59.0),
                s.get("channel_name", ""), FH, crf=q["crf"] + 1, preset=q["preset"],
                audio_in=teaser_audio, subtitle_ass=teaser_ass,
            ))
        except Exception as e:  # noqa: BLE001
            p.log(f"⚠️ Shorts teaser skipped: {e}")
    chapters = _alignment_chapters(aligned_words, total) if aligned_words else []
    if len(chapters) < 3:
        chapters = _chapters(plan, starts, total)
    credits = []
    stock_providers = set()
    for scene in scenes:
        scene_state = p.state(scene.id)
        credits.extend(value for value in (scene_state.get("stock_credit"), scene_state.get("image_credit")) if value)
        stock_providers.update(value for value in (
            scene_state.get("stock_provider"), scene_state.get("image_provider")) if value)
    stock_attribution = []
    if "Pexels" in stock_providers:
        stock_attribution.append("Photos and videos provided by Pexels: https://www.pexels.com")
    if "Pixabay" in stock_providers:
        stock_attribution.append("Pixabay stock footage")
    sources = [line for line in plan.research_sources if line.strip()]
    source_text = "\n\nSources / fact-check notes:\n" + "\n".join(sources) if sources else ""
    desc = (plan.youtube_description + ("\n\n" + "\n".join(f"{_ts(t)} {name}" for t, name in chapters) if chapters else "")
            + source_text
            + (f"\n\nStock media credits: {'; '.join(dict.fromkeys(credits))}" if credits else "")
            + ("\n\n" + "\n".join(stock_attribution) if stock_attribution else "")
            + "\n\n" + " ".join("#" + re.sub(r"\W", "", t) for t in plan.tags[:8]))
    pinned_comment = (
        f"What stood out to you most about {plan.title}? Share your take below.\n\n"
        "Resources mentioned: [add your affiliate or resource link here]"
    )
    meta = out_dir / "youtube_metadata.txt"
    meta.write_text(f"TITLE: {plan.youtube_title or plan.title}\n\nDESCRIPTION:\n{desc}\n\n"
                    f"TAGS: {', '.join(plan.tags)}\n\nPINNED COMMENT:\n{pinned_comment}\n",
                    encoding="utf-8")
    with p.lock:
        p.data["outputs"] = {"video": str(final), "srt": str(srt), "vtt": str(vtt),
                             "ass": str(kinetic_ass) if kinetic_ass else "",
                             "alignment": str(p.dir / "alignment.json") if aligned_words else "",
                             "thumbnail": str(thumb), "metadata": str(meta),
                             "duration": total, "width": FW, "height": FH, "rendered": _now(),
                             "chapters": chapters, "description": desc, "pinned_comment": pinned_comment,
                             "thumbnails": thumbs, "thumbnails_png": thumb_pngs, "short": short}
    if plan and s.get("channel_name"):
        append_series_history(str(s["channel_name"]), p.id, plan)
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
    bible = ensure_visual_bible(p)
    style_prefix = bible.get("stylePrefix") if bible.get("approved") else ""
    prompt = (f"Character reference portrait of {ch.name}, {ch.look}. Front-facing, upper body, neutral background, "
              f"even soft lighting, sharp focus on face and costume. {style_prefix or cinema.style_prompt(p.settings['style'])}")
    d = p.dir / "characters"
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{re.sub(r'[^A-Za-z0-9]+', '_', name)}_{random.randint(1000, 9999)}.jpg"
    s = p.settings
    images.generate_image(prompt, out, W, H, engine=s["image_engine"], negative=s.get("negative_global", ""),
                          allow_paid=s["allow_paid"], allow_stock=False,
                          openrouter_model=s.get("openrouter_image_model", ""), log=log)
    with p.lock:
        s.setdefault("char_refs", {})[name] = str(out)
        entry = next((item for item in bible.get("characters", []) if item.get("name") == name), None)
        if entry:
            entry["referenceImagePath"] = str(out)
    p.save()
    return str(out)


def generate_visual_bible(p: Project, log: Optional[Callable[[str], None]] = None, regenerate: bool = False) -> Dict[str, Any]:
    """Generates resumable master/character references; explicit user approval is a separate UI action."""
    plan = p.plan
    if not plan:
        raise RuntimeError("Generate a story plan before creating a Visual Bible")
    bible = ensure_visual_bible(p)
    mode_prompts = {
        "stylized_3d": "stylized 3D animation, tactile materials, expressive cinematic lighting",
        "stylized_2d": "stylized 2D illustration, confident shapes, painterly cinematic lighting",
        "cinematic_doc": "cinematic documentary imagery, naturalistic light, restrained color grading",
    }
    if not bible.get("stylePrefix"):
        brand = p.settings.get("channel_name", "").strip()
        identity = plan.visual_identity.strip()
        bible["stylePrefix"] = ", ".join(part for part in (
            mode_prompts[bible["visualMode"]],
            f"channel identity: {brand}" if brand else "",
            identity,
            f"production style: {p.settings.get('style', '')}",
            "consistent palette, lighting, lens character, and texture across every shot",
        ) if part)
    generated_fingerprint = visual_bible_fingerprint(bible)
    regenerate = regenerate or bible.get("generatedFingerprint") != generated_fingerprint
    bible_dir = p.dir / "visual_bible"
    bible_dir.mkdir(parents=True, exist_ok=True)
    seed_base = int(bible["seedBase"])
    master_path = bible_dir / f"master_{seed_base}.jpg"
    if regenerate or not master_path.is_file():
        if log:
            log("🎨 Generating the Visual Bible master style reference")
        result = images.generate_image(
            f"Master visual style reference board, no text or labels. {bible['stylePrefix']}. "
            f"Story identity: {plan.visual_identity or plan.logline}",
            master_path, 1024, 1024, seed=seed_base, engine=p.settings.get("image_engine", "auto"),
            negative=bible["globalNegativePrompt"], allow_paid=p.settings.get("allow_paid", False),
            allow_stock=False, openrouter_model=p.settings.get("openrouter_image_model", ""), log=log)
        bible["masterReferencePath"] = str(result.path)
        p.save()
    else:
        bible["masterReferencePath"] = str(master_path)

    refs_engine = "cf_klein4b" if images.engine_available("cf_klein4b")[0] else p.settings.get("image_engine", "auto")
    char_dir = bible_dir / "characters"
    char_dir.mkdir(parents=True, exist_ok=True)
    for index, character in enumerate(plan.characters):
        entry = next((item for item in bible["characters"] if item.get("name") == character.name), None)
        if entry is None:
            entry = {"id": hashlib.sha256(character.name.lower().encode("utf-8")).hexdigest()[:12],
                     "name": character.name, "description": character.look, "referenceImagePath": ""}
            bible["characters"].append(entry)
        if entry.get("referenceImagePath") and Path(entry["referenceImagePath"]).is_file() and not regenerate:
            continue
        seed = (seed_base + (index + 1) * 104729) % (2 ** 31 - 1) or 1
        out = char_dir / f"{entry['id']}_{seed}.jpg"
        if log:
            log(f"🎭 Generating Visual Bible character sheet: {character.name}")
        result = images.generate_image(
            f"Character reference sheet for {character.name}. {character.look}. Show one character only, "
            "front-facing full figure with a clear face and costume, plain neutral background, no text, no labels. "
            f"{bible['stylePrefix']}",
            out, 1024, 1024, seed=seed, engine=refs_engine,
            refs=[Path(bible["masterReferencePath"])],
            negative=bible["globalNegativePrompt"], allow_paid=p.settings.get("allow_paid", False),
            allow_stock=False, openrouter_model=p.settings.get("openrouter_image_model", ""), log=log)
        entry.update(description=character.look, referenceImagePath=str(result.path))
        p.settings.setdefault("char_refs", {})[character.name] = str(result.path)
        p.save()
    bible["generatedAt"] = _now()
    bible["generatedFingerprint"] = visual_bible_fingerprint(bible)
    p.data["visualBibleRevision"] = int(p.data.get("visualBibleRevision", 0)) + 1
    p.save()
    return bible


def package_zip(p: Project) -> Path:
    """Downloadable bundle: final film, subtitles, thumbnail, metadata, plan and all scene assets."""
    out = p.dir / f"{p.id}_package.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        outs = p.data.get("outputs") or {}
        files = [outs.get(k) for k in ("video", "short", "srt", "vtt", "ass", "alignment",
                                       "thumbnail", "metadata")]
        files += list(outs.get("thumbnails") or [])
        files += list(outs.get("thumbnails_png") or [])
        files += list(outs.get("autoclips") or [])
        files.append(p.settings.get("external_voiceover_path", ""))
        for f in dict.fromkeys(f for f in files if f):
            if Path(f).exists():
                z.write(f, f"final/{Path(f).name}")
        z.writestr("plan.json", json.dumps(p.data.get("plan"), indent=2))
        bible = ensure_visual_bible(p)
        z.writestr("visualBible.json", json.dumps(bible, indent=2))
        refs = [bible.get("masterReferencePath", "")]
        refs.extend(item.get("referenceImagePath", "") for item in bible.get("characters", []))
        for ref in dict.fromkeys(path for path in refs if path and Path(path).is_file()):
            z.write(ref, f"visual_bible/{Path(ref).name}")
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
                        z.write(part["path"], f"scenes/{i:02d}_voice_{j}{Path(part['path']).suffix}")
        for short_path in outs.get("autoclips") or []:
            if Path(short_path).is_file():
                z.write(short_path, f"shorts/{Path(short_path).name}")
    return out
