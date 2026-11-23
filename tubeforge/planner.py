"""AI story & production planner.
Stage A (creative model): treatment - title, logline, hook, storyline, character bible, environments, visual identity,
music direction, ending, YouTube metadata and one beat per scene.
Stage B (fast models, parallel batches spread across providers): full cinematic scene breakdowns.
Also: Auto Cinematic Mode (AI picks settings), script import, prompt enhancement, single-scene regeneration."""
import concurrent.futures as cf
import json
import re
import uuid
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import cinema, llm

WORDS_PER_SEC = 2.35


def _s(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return ", ".join(_s(x) for x in v)
    if isinstance(v, dict):
        return "; ".join(f"{k}: {_s(x)}" for k, x in v.items())
    return str(v).strip()


class Character(BaseModel):
    name: str = ""
    role: str = ""
    look: str = Field("", description="consistent visual description: age, face, hair, clothing, palette")
    personality: str = ""
    voice: str = Field("", description="male/female/child/robotic + tone")

    @field_validator("*", mode="before")
    @classmethod
    def _str(cls, v):
        return _s(v)


class Environment(BaseModel):
    name: str = ""
    look: str = ""

    @field_validator("*", mode="before")
    @classmethod
    def _str(cls, v):
        return _s(v)


class DialogueLine(BaseModel):
    speaker: str = ""
    line: str = ""

    @field_validator("*", mode="before")
    @classmethod
    def _str(cls, v):
        return _s(v)


class Scene(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    title: str = ""
    act: str = ""
    narration: str = ""
    is_hero_scene: bool = False
    hero_clip: str = ""
    dialogue: List[DialogueLine] = Field(default_factory=list)
    emotion: str = ""
    image_prompt: str = ""
    video_prompt: str = ""
    negative_prompt: str = ""
    camera_move: str = "Slow dolly in"
    camera_angle: str = "Eye level"
    shot_type: str = "Wide establishing"
    lighting: str = "Volumetric god rays"
    atmosphere: str = "none"
    color_grade: str = ""
    animation: str = "None"
    transition: str = "Crossfade"
    music_cue: str = ""
    sfx: str = "none"
    ambience: str = "none"
    overlay_text: str = ""
    overlay_kind: str = "Title"
    characters: List[str] = Field(default_factory=list)
    environment: str = ""
    stock_query: str = ""
    duration: float = 0.0
    source: str = "original_motion"  # original_motion | ai_image | stock_video | ai_video | upload
    video_source: str = Field(default="original_motion", alias="videoSource")
    motion_preset: str = "dolly_in"
    depth_intensity: str = "medium"
    depth_dof: float = 0.15
    procedural_template: str = Field(default="3d_bar_chart", alias="proceduralTemplate")
    procedural_props: Dict[str, Any] = Field(default_factory=dict, alias="proceduralProps")

    @field_validator("dialogue", mode="before")
    @classmethod
    def _dlg(cls, v):
        if not v:
            return []
        if isinstance(v, str):
            out = []
            for ln in v.split("\n"):
                if ":" in ln:
                    a, b = ln.split(":", 1)
                    out.append({"speaker": a.strip(), "line": b.strip()})
            return out
        return [x if isinstance(x, dict) else {"speaker": "", "line": _s(x)} for x in v]

    @field_validator("characters", mode="before")
    @classmethod
    def _chars(cls, v):
        if not v:
            return []
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return [_s(x) for x in v]

    @field_validator("duration", mode="before")
    @classmethod
    def _dur(cls, v):
        try:
            return float(re.findall(r"[\d.]+", str(v))[0]) if v not in (None, "") else 0.0
        except (IndexError, ValueError):
            return 0.0

    @field_validator("title", "act", "narration", "emotion", "image_prompt", "video_prompt", "negative_prompt",
                     "overlay_text", "environment", "stock_query", "music_cue", mode="before")
    @classmethod
    def _str(cls, v):
        return _s(v)

    def normalise(self, style: str, default_duration: float) -> "Scene":
        """Snaps free-text LLM values onto the renderer's supported vocabulary."""
        self.camera_move = cinema.closest(self.camera_move, list(cinema.CAMERA_MOVES), "Slow dolly in")
        self.camera_angle = cinema.closest(self.camera_angle, cinema.CAMERA_ANGLES, "Eye level")
        self.shot_type = cinema.closest(self.shot_type, cinema.SHOT_TYPES, "Wide establishing")
        self.lighting = cinema.closest(self.lighting, cinema.LIGHTING, "Volumetric god rays")
        atmo = {v.lower(): k for k, v in cinema.ATMOSPHERES.items()}
        a = str(self.atmosphere or "none").lower()
        self.atmosphere = a if a in cinema.ATMOSPHERES else atmo.get(a) or next(
            (k for k in cinema.ATMOSPHERES if k != "none" and k.replace("_", " ") in a), "none")
        if self.color_grade:
            self.color_grade = cinema.closest(self.color_grade, list(cinema.GRADES), "")
        self.animation = cinema.closest(self.animation, cinema.ANIMATION_EFFECTS, "None")
        self.transition = cinema.closest(self.transition, list(cinema.TRANSITIONS), "Crossfade")
        self.sfx = cinema.closest(str(self.sfx or "none").lower(), cinema.SFX, "none")
        self.ambience = cinema.closest(str(self.ambience or "none").lower(), cinema.AMBIENCES, "none")
        cue = str(self.music_cue or "").lower()
        self.music_cue = next((c for c in ("calm", "build", "climax", "tension", "resolve", "silence") if c in cue), "")
        self.overlay_kind = cinema.closest(self.overlay_kind, ["Title", "Subtitle", "Lower third", "Chapter"], "Title")
        if self.source not in ("original_motion", "ai_image", "stock_video", "ai_video", "upload"):
            self.source = "ai_image"
        if self.duration <= 0:
            self.duration = default_duration
        self.duration = float(min(30.0, max(2.0, self.duration)))
        if not self.stock_query:
            self.stock_query = " ".join(re.findall(r"[A-Za-z]+", self.image_prompt)[:4])
        if not self.video_prompt:
            self.video_prompt = f"{self.image_prompt}. Camera: {self.camera_move.lower()}, {self.shot_type.lower()}."
        return self


class CinematicPlan(BaseModel):
    title: str = ""
    logline: str = ""
    genre: str = ""
    hook: str = ""
    storyline: str = ""
    characters: List[Character] = Field(default_factory=list)
    environments: List[Environment] = Field(default_factory=list)
    visual_identity: str = ""
    music_mood: str = "epic"
    ending: str = ""
    youtube_title: str = ""
    youtube_description: str = ""
    tags: List[str] = Field(default_factory=list)
    research_sources: List[str] = Field(default_factory=list)
    scenes: List[Scene] = Field(default_factory=list)
    models_used: List[str] = Field(default_factory=list)

    @field_validator("title", "logline", "genre", "hook", "storyline", "visual_identity", "ending", "youtube_title",
                     "youtube_description", mode="before")
    @classmethod
    def _str(cls, v):
        return _s(v)

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, v):
        if isinstance(v, str):
            return [t.strip().lstrip("#") for t in v.split(",") if t.strip()]
        return [_s(t).lstrip("#") for t in (v or [])]

    @field_validator("music_mood", mode="before")
    @classmethod
    def _mood(cls, v):
        v = _s(v).lower()
        return next((m for m in cinema.MUSIC_MOODS if m in v), "epic")


VOCAB = (
    f"camera_move one of: {', '.join(cinema.CAMERA_MOVES)}.\n"
    f"camera_angle one of: {', '.join(cinema.CAMERA_ANGLES)}.\n"
    f"shot_type one of: {', '.join(cinema.SHOT_TYPES)}.\n"
    f"lighting one of: {', '.join(cinema.LIGHTING)}.\n"
    f"atmosphere one of: {', '.join(cinema.ATMOSPHERES)}.\n"
    f"transition one of: {', '.join(cinema.TRANSITIONS)}.\n"
    f"animation one of: {', '.join(cinema.ANIMATION_EFFECTS)} (use sparingly: Parallax depth for scenic or "
    f"character reveals, Lens flare sweep for sun/neon/hero moments, Pulse glow for magic/tech, Subtle breathing "
    f"zoom for quiet emotional beats; None for most scenes).\n"
    f"music_cue one of: calm, build, tension, climax, resolve, silence.\n"
    f"sfx one of: {', '.join(cinema.SFX)}. ambience one of: {', '.join(cinema.AMBIENCES)}.\n"
    f"overlay_kind one of: Title, Subtitle, Lower third, Chapter (overlay_text usually empty; use for opening title, "
    f"chapter cards, place/date captions).\n"
)


def _treatment_prompt(idea: str, style: str, n: int, dur: float, language: str, tone: str, audience: str,
                      extra: str, channel_name: str = "", voice_branding: bool = False,
                      target_minutes: float = 0.0) -> str:
    if target_minutes <= 0.0:
        target_minutes = (n * dur) / 60.0
    total_words = int(target_minutes * 60 * WORDS_PER_SEC)
    words_per_scene = max(16, int(total_words / max(1, n)))

    brand_instr = ""
    if channel_name.strip() and voice_branding:
        brand_instr = (
            f"\nCHANNEL BRANDING (for '{channel_name.strip()}'):\n"
            f"- In Scene 1 after the opening hook, warmly introduce or welcome viewers to '{channel_name.strip()}'.\n"
            f"- In Scene {n} (final scene), naturally ask viewers to like and subscribe to '{channel_name.strip()}' for more.\n"
        )

    timing_instr = (
        f"\nTARGET DURATION & TIMING PACING:\n"
        f"- Target total runtime: {target_minutes:.1f} minutes (~{int(target_minutes * 60)} seconds across {n} scenes).\n"
        f"- Each scene is approximately {dur:.1f} seconds long, requiring ~{words_per_scene} spoken words per scene (~{total_words} words total).\n"
    )

    return (
        f"Create a cinematic film treatment.\nIDEA: {idea}\nVISUAL STYLE: {style}\nTONE: {tone or 'cinematic'}\n"
        f"AUDIENCE: {audience or 'general YouTube audience'}\nLANGUAGE for narration/titles: {language}\n"
        f"SCENES: exactly {n} (each ~{dur:.1f}s).\n{brand_instr}{timing_instr}{extra}\n"
        "Return JSON with keys: title, logline, genre, hook (first-5-seconds hook line), storyline (6-10 sentences "
        "with a clear beginning, rising conflict, climax and resolution), characters (array of {name, role, look, "
        "personality, voice}; 'look' must be a precise reusable visual description for consistent images; max 6), "
        "environments (array of {name, look}; max 8), visual_identity (palette, lenses, lighting signature), "
        "music_mood (one of epic, dark, uplifting, mysterious, emotional, tense, ambient), ending, youtube_title, "
        "youtube_description (2-3 sentences), tags (10 strings), beats (array of EXACTLY "
        f"{n} strings, one per scene, each 'Act N - what happens on screen', in story order, with escalating "
        "stakes, a climax around 75-85% and an emotional ending)."
    )


def _scenes_prompt(treat: dict, style: str, beats: List[str], start_index: int, total: int, dur: float,
                   language: str, prev_summary: str, fixed_narration: Optional[List[str]] = None,
                   channel_name: str = "", voice_branding: bool = False) -> str:
    chars = "\n".join(f"- {c.get('name')}: {c.get('look')}" for c in treat.get("characters", [])[:6]) or "- none"
    envs = "\n".join(f"- {e.get('name')}: {e.get('look')}" for e in treat.get("environments", [])[:8]) or "- none"
    words = max(16, int(dur * WORDS_PER_SEC))
    beat_lines = "\n".join(f"{start_index + i + 1}. {b}" for i, b in enumerate(beats))

    brand_notes = ""
    if channel_name.strip() and voice_branding:
        if start_index == 0:
            brand_notes += f" In Scene 1 narration, after the hook, briefly welcome viewers to '{channel_name.strip()}'."
        if start_index + len(beats) >= total:
            brand_notes += f" In Scene {total} (final scene) narration, include a smooth call to like and subscribe to '{channel_name.strip()}'."

    narr_rule = (f"narration: {language}, about {words} words (it is spoken over a {dur:.1f}s shot), vivid, "
                 f"cinematic, no scene numbers.{brand_notes}")
    if fixed_narration:
        narr_rule = "narration: copy the provided narration EXACTLY, do not change a word"
        beat_lines = "\n".join(f"{start_index + i + 1}. NARRATION: {t}" for i, t in enumerate(fixed_narration))
    return (
        f"FILM: {treat.get('title')} - {treat.get('logline')}\nSTYLE: {style}\nVISUAL IDENTITY: "
        f"{treat.get('visual_identity')}\nSTORY: {treat.get('storyline')}\n"
        f"RESEARCH SOURCES (use exact entries for quote_evidence_card only):\n"
        f"{json.dumps(treat.get('research_sources') or [], ensure_ascii=False)}\n"
        f"CHARACTERS:\n{chars}\nENVIRONMENTS:\n{envs}\n"
        f"PREVIOUS SCENE: {prev_summary or 'this is the opening'}\n\n"
        f"Write scenes {start_index + 1}-{start_index + len(beats)} of {total} for these beats:\n{beat_lines}\n\n"
        f"Return JSON {{\"scenes\": [...]}} with EXACTLY {len(beats)} objects in order. Each object keys: title, act, "
        f"{narr_rule}; dialogue (array of {{speaker, line}}, optional, short); emotion; image_prompt (60-90 words, "
        "a single frozen cinematic frame: subject, action, composition, lens, lighting, colour, mood, background, "
        "repeat the exact character look text for any character shown, no text/logos); video_prompt (motion "
        "description for an AI video model: subject motion + camera motion); negative_prompt (comma list of things "
        "to avoid); camera_move; camera_angle; shot_type; lighting; atmosphere; animation; transition (into the NEXT scene); "
        "music_cue; sfx; ambience; overlay_text; overlay_kind; characters (names shown); environment (name); "
        "stock_query (2-4 plain English words to find matching stock footage); videoSource (default "
        "\"original_motion\", use \"procedural_3d_2d\" for roughly 20-30% of scenes whose narration contains a "
        "statistic, money amount, date, comparison, or a verified fact); proceduralTemplate (one of "
        "\"3d_bar_chart\", \"3d_stat_callout\", \"comparison_split\", \"timeline_journey\", \"process_flow\", "
        "\"quote_evidence_card\", \"2d_character_explainer\", or \"kinetic_hook_title\"); proceduralProps (strict JSON matching that template; quote_evidence_card "
        "MUST cite an exact source supplied in Story/research sources and MUST NOT invent a citation). Use "
        "\"original_motion\" and empty proceduralProps for ordinary narrative scenes.\n"
        "Procedural property shapes: 3d_bar_chart={\"subtitle\":\"...\",\"unit\":\"...\",\"bars\":[{\"label\":\"...\","
        "\"value\":12}]}; 3d_stat_callout={\"value\":12,\"prefix\":\"$\",\"suffix\":\"\",\"subtitle\":\"...\"}; "
        "comparison_split={\"left\":{\"label\":\"A\",\"metrics\":[{\"label\":\"...\",\"value\":\"...\"}]},"
        "\"right\":{\"label\":\"B\",\"metrics\":[{\"label\":\"...\",\"value\":\"...\"}]},\"winner\":\"A\"}; "
        "timeline_journey={\"milestones\":[{\"year\":\"...\",\"label\":\"...\",\"caption\":\"...\"}]}; "
        "process_flow={\"steps\":[{\"label\":\"...\",\"caption\":\"...\"}]}; "
        "quote_evidence_card={\"quote\":\"...\",\"source\":\"exact user-provided source\",\"context\":\"...\"}.\n"
        "2d_character_explainer={\"characterName\":\"...\",\"emotion\":\"explaining\",\"mouthStateTimeline\":[]}; "
        "kinetic_hook_title={\"hookText\":\"...\",\"kicker\":\"WATCH THIS\"}. "
        "Vary shot types and camera moves for rhythm (wide -> medium -> close-up), use the opening scene for an "
        "establishing shot and a Title overlay with the film title, and keep continuity of characters and places.\n"
        + VOCAB
    )


SYSTEM = ("You are an award-winning film director, screenwriter and cinematographer. You plan visually stunning, "
          "emotionally engaging short films for YouTube. Reply with strict JSON only.")


def _split_even(items: List[Any], size: int) -> List[List[Any]]:
    return [items[i: i + size] for i in range(0, len(items), size)]


def extract_verified_facts(text: str, source: str = "User-provided transcript") -> Dict[str, Any]:
    """Extracts source-bound facts without elevating unsupported model guesses to verified claims."""
    content = str(text or "").strip()[:20000]
    if not content:
        raise ValueError("Paste article or transcript text first.")

    def validate(data):
        if not isinstance(data, dict) or not isinstance(data.get("facts"), list):
            raise ValueError("Fact extractor returned an invalid object.")
        facts = []
        for item in data["facts"][:20]:
            if not isinstance(item, dict):
                continue
            statement = _s(item.get("statement"))
            citation = source
            if statement:
                facts.append({"statement": statement[:500], "citation": citation[:500]})
        statistics = data.get("key_statistics", [])
        if not isinstance(statistics, list):
            statistics = []
        safe_statistics = []
        for item in statistics[:20]:
            if isinstance(item, dict):
                label, value = _s(item.get("label")), _s(item.get("value"))
                if label and value:
                    safe_statistics.append({"label": label[:150], "value": value[:100]})
        return {"facts": facts, "key_statistics": safe_statistics}

    prompt = (
        "Extract only facts explicitly present in this source. Return JSON with facts: "
        "[{statement,citation}] and key_statistics: [{label,value}]. "
        "Citations must be exactly the supplied source identifier. Do not infer, embellish, or invent facts. "
        "If the source does not support a claim, omit it. SOURCE: " + source + "\nTEXT:\n" + content
    )
    extracted, _ = llm.complete_json(
        "You are a careful source-grounded fact extractor. Return strict JSON only.",
        prompt, task="fast", max_tokens=1600, temperature=0.1, validate=validate, use_cache=False,
    )
    return extracted


def split_script(script: str, n: int) -> List[str]:
    """Deterministically splits a pasted script into n narration chunks at sentence boundaries."""
    sents = [s.strip() for s in re.split(r"(?<=[.!?।])\s+|\n{2,}", script.strip()) if s.strip()]
    if not sents:
        return []
    n = max(1, min(n, len(sents)))
    total = sum(len(s.split()) for s in sents)
    out, cur, acc = [], [], 0
    for i, s in enumerate(sents):
        cur.append(s)
        acc += len(s.split())
        slots_left = n - len(out) - 1  # chunks still needed after this one
        sents_left = len(sents) - i - 1
        if slots_left > 0 and (acc >= total * (len(out) + 1) / n or sents_left <= slots_left):
            out.append(" ".join(cur))
            cur = []
    if cur:
        out.append(" ".join(cur))
    return out


def generate_plan(
    idea: str,
    style: str = "Hollywood Blockbuster",
    n_scenes: int = 8,
    scene_duration: float = 6.0,
    language: str = "English",
    tone: str = "",
    audience: str = "",
    script: str = "",
    extra: str = "",
    log: Optional[Callable[[str], None]] = None,
    progress: Optional[Callable[[float, str], None]] = None,
    batch_size: int = 6,
    workers: int = 3,
    channel_name: str = "",
    voice_branding: bool = False,
    target_minutes: float = 0.0,
) -> CinematicPlan:
    n_scenes = int(max(1, min(50, n_scenes)))
    fixed: Optional[List[str]] = None
    if script.strip():
        fixed = split_script(script, n_scenes)
        n_scenes = len(fixed)
        extra += "\nThe narration script is provided by the user; beats must follow it:\n" + script[:6000]
    if progress:
        progress(0.03, "Writing story treatment, characters and beats")

    def validate_treat(d):
        if not isinstance(d, dict):
            raise ValueError("treatment is not an object")
        beats = d.get("beats") or []
        if not isinstance(beats, list) or len(beats) < max(1, int(n_scenes * 0.6)):
            raise ValueError(f"expected {n_scenes} beats, got {len(beats) if isinstance(beats, list) else 0}")
        return d

    treat, model_a = llm.complete_json(
        SYSTEM,
        _treatment_prompt(idea, style, n_scenes, scene_duration, language, tone, audience, extra,
                          channel_name=channel_name, voice_branding=voice_branding, target_minutes=target_minutes),
        task="creative", max_tokens=min(16000, 3500 + n_scenes * 90), temperature=0.85,
        validate=validate_treat, log=log)
    beats = [_s(b) for b in treat.get("beats", [])][:n_scenes]
    while len(beats) < n_scenes:
        beats.append(f"Continuation of the story, part {len(beats) + 1}")
    if log:
        log(f"📝 Treatment by {model_a}: “{treat.get('title', '')}” with {len(beats)} beats")
    plan = CinematicPlan(**{k: v for k, v in treat.items() if k in CinematicPlan.model_fields and k != "scenes"})
    plan.models_used.append(model_a)
    batches = _split_even(list(range(n_scenes)), batch_size)
    providers = llm.available_backends()
    # Spread batches across providers so no single free tier hits its rate limit.
    rotation = [p for p in ("groq", "gemini", "openrouter", "cloudflare") if p in providers] or [None]
    results: Dict[int, List[dict]] = {}
    done = [0]

    def run_batch(bi: int, idxs: List[int]) -> List[dict]:
        prev = beats[idxs[0] - 1] if idxs[0] > 0 else ""
        b_beats = [beats[i] for i in idxs]
        b_fixed = [fixed[i] for i in idxs] if fixed else None

        def validate(d):
            sc = d.get("scenes") if isinstance(d, dict) else d
            if not isinstance(sc, list) or len(sc) < len(idxs):
                raise ValueError(f"expected {len(idxs)} scenes, got {len(sc) if isinstance(sc, list) else 0}")
            return sc[: len(idxs)]

        data, model = llm.complete_json(
            SYSTEM,
            _scenes_prompt(treat, style, b_beats, idxs[0], n_scenes, scene_duration, language, prev, b_fixed,
                           channel_name=channel_name, voice_branding=voice_branding),
            task="scenes", max_tokens=min(12000, 900 + 520 * len(idxs)), temperature=0.75, validate=validate, log=log,
            start_provider=rotation[bi % len(rotation)])
        plan.models_used.append(model)
        return data

    with cf.ThreadPoolExecutor(max_workers=max(1, min(workers, len(batches)))) as ex:
        futs = {ex.submit(run_batch, bi, idxs): (bi, idxs) for bi, idxs in enumerate(batches)}
        for fut in cf.as_completed(futs):
            bi, idxs = futs[fut]
            try:
                results[bi] = fut.result()
            except Exception as e:
                # One retry in halves (smaller requests survive token limits better).
                if log:
                    log(f"⚠️ Scene batch {bi + 1} failed ({str(e)[:100]}), retrying in smaller parts")
                parts = []
                for half in _split_even(idxs, max(1, len(idxs) // 2)):
                    parts.extend(run_batch(bi + 1, half))
                results[bi] = parts
            done[0] += 1
            if progress:
                progress(0.1 + 0.85 * done[0] / len(batches), f"Scene breakdowns {done[0]}/{len(batches)} batches")
    scenes: List[Scene] = []
    for bi in range(len(batches)):
        for raw in results[bi]:
            raw_scene = raw or {}
            scene_values = {}
            for name, field_info in Scene.model_fields.items():
                if name == "id":
                    continue
                alias = field_info.alias
                if name in raw_scene:
                    scene_values[name] = raw_scene[name]
                elif alias and alias in raw_scene:
                    scene_values[name] = raw_scene[alias]
            sc = Scene(**scene_values)
            scenes.append(sc.normalise(style, scene_duration))
    if fixed:
        for sc, txt in zip(scenes, fixed):
            sc.narration = txt
    plan.scenes = scenes[:n_scenes]
    if plan.scenes and not plan.scenes[0].overlay_text:
        plan.scenes[0].overlay_text = plan.title
        plan.scenes[0].overlay_kind = "Title"
    if progress:
        progress(1.0, "Plan ready")
    return plan


def regenerate_scene(plan: CinematicPlan, index: int, style: str, scene_duration: float, language: str,
                     feedback: str = "", log=None) -> Scene:
    treat = plan.model_dump()
    sc = plan.scenes[index]
    beat = f"{sc.title}: {sc.narration}" + (f"\nDIRECTOR NOTES (apply these): {feedback}" if feedback else "")
    prev = plan.scenes[index - 1].narration if index > 0 else ""
    data, model = llm.complete_json(
        SYSTEM, _scenes_prompt(treat, style, [beat], index, len(plan.scenes), scene_duration, language, prev),
        task="scenes", max_tokens=1800, temperature=0.9, use_cache=False, log=log,
        validate=lambda d: (d.get("scenes") if isinstance(d, dict) else d)[:1] or (_ for _ in ()).throw(ValueError("empty")))
    raw_scene = data[0]
    scene_values = {}
    for name, field_info in Scene.model_fields.items():
        if name == "id":
            continue
        alias = field_info.alias
        if name in raw_scene:
            scene_values[name] = raw_scene[name]
        elif alias and alias in raw_scene:
            scene_values[name] = raw_scene[alias]
    new = Scene(**scene_values)
    new.id = sc.id
    new.source = sc.source
    return new.normalise(style, scene_duration)


def enhance_prompt(prompt: str, style: str, log=None) -> str:
    data, _ = llm.complete_json(
        "You are a world-class prompt engineer for cinematic AI image models. Reply with JSON only.",
        f"Rewrite this into a single vivid, highly detailed cinematic image prompt (70-110 words) for the style "
        f"'{style}': subject, action, composition, lens, lighting, colour palette, atmosphere, textures, mood. "
        f"No text, no logos.\nPROMPT: {prompt}\nReturn {{\"prompt\": \"...\"}}",
        task="fast", max_tokens=900, temperature=0.8, use_cache=False, log=log,
        validate=lambda d: d if isinstance(d, dict) and len(_s(d.get("prompt"))) > 20 else (_ for _ in ()).throw(
            ValueError("no prompt")))
    return _s(data["prompt"])


AUTO_KEYS = {
    "style": list(cinema.STYLES), "aspect": list(cinema.ASPECTS), "grade": list(cinema.GRADES),
    "atmosphere": list(cinema.ATMOSPHERES), "music_mood": cinema.MUSIC_MOODS,
    "subtitle_style": list(cinema.SUBTITLE_STYLES), "voice_gender": ["male", "female"],
}


def auto_cinematic(idea: str, log=None) -> Dict[str, Any]:
    """AI chooses optimal cinematic settings for the idea (Auto Cinematic Mode)."""
    opts = "\n".join(f"{k}: one of {v}" for k, v in AUTO_KEYS.items())
    data, model = llm.complete_json(
        SYSTEM,
        f"Choose the optimal production settings for this film idea.\nIDEA: {idea}\n{opts}\n"
        "scene_count: integer 4-50 (more for longer epic stories), scene_duration: seconds 4-10, "
        "camera_intensity: 0.5-1.6, letterbox: true/false (true for epic cinema), tone: short phrase, "
        "audience: short phrase, film_grain: 0-1, bloom: 0-1, halation: 0-0.5 (film highlight glow; 0 for clean/3D/"
        "commercial looks), gate_weave: true/false (true only for vintage/film-stock looks), reason: one sentence.\n"
        "Return JSON with these keys.",
        task="fast", max_tokens=900, temperature=0.4, log=log)
    out: Dict[str, Any] = {"model": model}
    for k, v in AUTO_KEYS.items():
        out[k] = cinema.closest(_s(data.get(k)), v, v[0])
    try:
        out["scene_count"] = int(max(4, min(50, int(float(_s(data.get("scene_count")) or 8)))))
    except ValueError:
        out["scene_count"] = 8
    for k, lo, hi, d in (("scene_duration", 4, 10, 6), ("camera_intensity", 0.5, 1.6, 1.0), ("film_grain", 0, 1, 0.35),
                         ("bloom", 0, 1, 0.3), ("halation", 0, 0.5, 0.2)):
        try:
            out[k] = float(max(lo, min(hi, float(re.findall(r"[\d.]+", _s(data.get(k)))[0]))))
        except (IndexError, ValueError):
            out[k] = d
    out["letterbox"] = _s(data.get("letterbox")).lower() in ("true", "yes", "1")
    out["gate_weave"] = _s(data.get("gate_weave")).lower() in ("true", "yes", "1")
    out["tone"] = _s(data.get("tone"))[:80]
    out["audience"] = _s(data.get("audience"))[:80]
    out["reason"] = _s(data.get("reason"))[:240]
    return out


def build_image_prompt(plan: CinematicPlan, scene: Scene, style: str, extra_style: str = "") -> str:
    parts = [scene.image_prompt or scene.narration]
    looks = {c.name.lower(): c.look for c in plan.characters if c.name}
    for name in scene.characters:
        look = looks.get(name.lower())
        if look and look.lower()[:30] not in scene.image_prompt.lower():
            parts.append(f"{name}: {look}")
    env = next((e.look for e in plan.environments if e.name and e.name.lower() == scene.environment.lower()), "")
    if env and env.lower()[:30] not in scene.image_prompt.lower():
        parts.append(f"Setting: {env}")
    parts.append(f"{scene.shot_type}, {scene.camera_angle.lower()} angle, {scene.lighting.lower()} lighting")
    if plan.visual_identity:
        parts.append(plan.visual_identity[:200])
    parts.append(cinema.style_prompt(style))
    if extra_style:
        parts.append(extra_style)
    parts.append("cinematic film still, no text, no watermark")
    return ". ".join(p.strip().rstrip(".") for p in parts if p and p.strip())[:1900]
