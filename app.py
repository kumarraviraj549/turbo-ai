import asyncio
import base64
import hmac
import html
import io
import json
import os
import re
import random
import secrets
import shutil
import subprocess
import textwrap
import time
import urllib.parse
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional

import edge_tts
import pandas as pd
import requests
import streamlit as st
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
from pydantic import BaseModel, Field

# =====================================================================
# 🔑 1. .ENV LOADER (FOR API KEYS) & UI PREFERENCES STORAGE
# =====================================================================
ENV_FILE = Path(".env")
UI_PREFS_FILE = Path(".saved_keys.json")


def load_env_keys() -> dict:
    """Reads GEMINI_API_KEY and PIXABAY_API_KEY from your .env file."""
    if ENV_FILE.exists():
        try:
            for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export ") :].strip()
                k, v = line.split("=", 1)
                v = v.strip()
                # Strip inline comments, but only on unquoted values.
                if " #" in v and not v.startswith(('"', "'")):
                    v = v.split(" #", 1)[0].strip()
                os.environ[k.strip()] = v.strip('"').strip("'").strip()
        except Exception:
            pass

    return {
        "gemini_key": os.getenv("GEMINI_API_KEY", "").strip(),
        "pixabay_key": (
            os.getenv("PIXABAY_API_KEY", "").strip()
            or os.getenv("PEXELS_API_KEY", "").strip()
        ),
        "cf_account": os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip().lower(),
        "cf_token": os.getenv("CLOUDFLARE_API_TOKEN", "").strip(),
        "pollinations_key": os.getenv("POLLINATIONS_API_KEY", "").strip(),
    }


def is_valid_gemini_key(key: str) -> bool:
    """Accepts classic 'AIza' keys (39 chars) and newer 'AQ.' format keys."""
    return bool(
        re.fullmatch(r"AIza[0-9A-Za-z_\-]{35}|AQ\.[0-9A-Za-z_\-.]{20,200}", key.strip())
    )


def save_env_key(key_name: str, key_val: str) -> None:
    """Writes/updates a single KEY="value" line in .env without touching other lines."""
    new_line = f'{key_name}="{key_val.strip()}"'
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    pattern = re.compile(rf"^\s*(export\s+)?{re.escape(key_name)}\s*=")
    replaced = False
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = new_line
            replaced = True
    if not replaced:
        lines.append(new_line)
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[key_name] = key_val.strip()


def load_ui_brand_prefs() -> dict:
    """Loads Channel Name and Brand Position last selected in the UI."""
    saved = {}
    if UI_PREFS_FILE.exists():
        try:
            saved = json.loads(UI_PREFS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    return {
        "channel_name": saved.get("channel_name", "theMedNurses"),
        "brand_position": saved.get("brand_position", "Top-Right"),
    }


def save_ui_brand_prefs(channel_name: str, brand_position: str) -> None:
    """Saves Channel Name and Brand Position selected in the UI."""
    # On a shared public deployment, one visitor's branding must not become everyone's default.
    if os.getenv("TUBEFORGE_PUBLIC_MODE", "").strip().lower() in ("1", "true", "yes", "on"):
        return
    existing = {}
    if UI_PREFS_FILE.exists():
        try:
            existing = json.loads(UI_PREFS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    existing["channel_name"] = channel_name.strip()
    existing["brand_position"] = brand_position
    UI_PREFS_FILE.write_text(json.dumps(existing, indent=2), encoding="utf-8")


# =====================================================================
# 2. PAGE CONFIG & CYBER-CINEMA GLASSMORPHISM UI
# =====================================================================
st.set_page_config(
    page_title="TubeForge Studio Pro",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    .stApp {
        background: radial-gradient(circle at top right, #1e1b4b 0%, #090d16 45%, #050811 100%);
    }
    .hero-box {
        background: linear-gradient(135deg, rgba(124, 58, 237, 0.22) 0%, rgba(6, 182, 212, 0.15) 100%);
        border: 1px solid rgba(139, 92, 246, 0.35);
        border-radius: 18px;
        padding: 24px 28px;
        margin-bottom: 22px;
        box-shadow: 0 12px 32px rgba(0, 0, 0, 0.45);
        backdrop-filter: blur(12px);
    }
    .hero-title {
        font-size: 2.1rem;
        font-weight: 800;
        background: linear-gradient(90deg, #A78BFA, #38BDF8, #F472B6);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        margin-bottom: 6px;
    }
    .hero-sub {
        color: #9CA3AF;
        font-size: 0.98rem;
        margin: 0;
    }
    .metric-card {
        background: rgba(17, 24, 39, 0.78);
        border: 1px solid rgba(255, 255, 255, 0.08);
        border-radius: 14px;
        padding: 14px 18px;
        text-align: center;
        box-shadow: 0 4px 16px rgba(0, 0, 0, 0.3);
    }
    .metric-label {
        color: #9CA3AF;
        font-size: 0.75rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        margin-bottom: 4px;
    }
    .metric-val {
        color: #F3F4F6;
        font-size: 1.1rem;
        font-weight: 700;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .scene-card {
        background: rgba(17, 24, 39, 0.85);
        border: 1px solid rgba(124, 58, 237, 0.28);
        border-radius: 14px;
        padding: 14px;
        margin-bottom: 14px;
    }
    .badge-safe {
        background: rgba(16, 185, 129, 0.18);
        color: #34D399;
        border: 1px solid rgba(16, 185, 129, 0.4);
        padding: 4px 12px;
        border-radius: 999px;
        font-size: 0.78rem;
        font-weight: 600;
    }
    div.stButton > button[kind="primary"] {
        background: linear-gradient(90deg, #7C3AED 0%, #2563EB 50%, #06B6D4 100%);
        border: none;
        border-radius: 12px;
        padding: 0.75rem 1.5rem;
        font-weight: 700;
        font-size: 1.05rem;
        color: white;
        box-shadow: 0 8px 24px rgba(124, 58, 237, 0.4);
        transition: all 0.2s ease-in-out;
        width: 100%;
    }
    div.stButton > button[kind="primary"]:hover {
        transform: translateY(-2px);
        box-shadow: 0 12px 28px rgba(6, 182, 212, 0.55);
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# =====================================================================
# 3. STRUCTURED DATA SCHEMAS (STRICT SCENE-IMAGE LOCK + HASHTAGS)
# =====================================================================
class SceneItem(BaseModel):
    scene_number: int = Field(description="Sequential scene number starting at 1")
    narration: str = Field(
        description="Spoken voiceover script for this specific scene (2 to 4 engaging sentences)."
    )
    visual_prompt: str = Field(
        description=(
            "Highly specific visual description depicting ONLY the exact subject, setting, and action "
            "spoken in THIS scene's `narration`. No text in image."
        )
    )
    stock_search_keyword: str = Field(
        description="2 to 3 specific English keywords matching this exact scene's subject."
    )
    subtitle_summary: str = Field(
        description="Punchy on-screen caption text summarizing this scene (max 10 words)."
    )
    chapter_title: str = Field(
        description="Short 2-to-4 word chapter title for YouTube timestamps."
    )
    camera_motion: str = Field(
        default="zoom_in",
        description="Camera movement: 'zoom_in', 'zoom_out', 'pan_right' or 'pan_left'.",
    )
    preferred_media: str = Field(
        default="ai_image",
        description="'ai_image' or 'stock_video' (used in Hybrid mode).",
    )


CAMERA_MOTIONS = ["zoom_in", "zoom_out", "pan_right", "pan_left"]
MEDIA_TYPES = ["ai_image", "stock_video"]
AI_AUTO_STYLE = "🤖 AI Auto Art Direction (best style for the topic)"


class VideoProductionPlan(BaseModel):
    primary_title: str = Field(
        description="The single best high-CTR YouTube title for this video"
    )
    viral_titles: List[str] = Field(
        default_factory=list,
        description="3 additional high-CTR viral YouTube title options",
    )
    youtube_description: str = Field(
        default="",
        description="Comprehensive SEO-optimized YouTube description summarizing the video",
    )
    hashtags: List[str] = Field(
        default_factory=list,
        description="15 relevant viral YouTube hashtags starting with #",
    )
    tags: List[str] = Field(
        default_factory=list,
        description="15 high-volume YouTube search tags without #",
    )
    thumbnail_prompt: str = Field(
        default="",
        description="Eye-catching visual prompt strictly representing the main topic and title",
    )
    auto_visual_style: str = Field(
        default="",
        description="AI-chosen art style and lighting (only when AI Auto Art Direction is on)",
    )
    scenes: List[SceneItem] = Field(description="Ordered list of scenes")


# =====================================================================
# 4. FAST, NON-BLOCKING DIRECT REST SCRIPT GENERATOR
# =====================================================================
def extract_clean_json(raw_text: str) -> str:
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        return text[start : end + 1]
    return text


def parse_llm_json(raw_text: str) -> dict:
    """Parses LLM JSON, repairing the most common defects (missing commas, trailing commas)."""
    text = extract_clean_json(raw_text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        repaired = re.sub(r",\s*([}\]])", r"\1", text)
        repaired = re.sub(r'(["\]}0-9el])\s*\n(\s*["{\[])', r"\1,\n\2", repaired)
        repaired = re.sub(r"}\s*{", "},{", repaired)
        return json.loads(repaired)


GEMINI_MODEL_RE = re.compile(r"gemini-(\d+)(?:\.(\d+))?-(flash|flash-lite|pro)(-preview)?")


def gemini_model_rank(name: str) -> tuple:
    """Newest version first, stable before preview, flash before flash-lite before pro."""
    m = GEMINI_MODEL_RE.fullmatch(name)
    major, minor = int(m.group(1)), int(m.group(2) or 0)
    tier = {"flash": 0, "flash-lite": 1, "pro": 2}[m.group(3)]
    return (-major, -minor, 1 if m.group(4) else 0, tier)


class GeminiAuthError(RuntimeError):
    pass


def parse_plan_text(raw_text: str) -> "VideoProductionPlan":
    return VideoProductionPlan.model_validate(normalize_plan_dict(parse_llm_json(raw_text)))


def _request_gemini(model_name: str, prompt: str, auth_headers: dict, parse):
    """Runs in a worker thread, so it must not call any Streamlit APIs."""
    resp = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent",
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.65,
                "responseMimeType": "application/json",
            },
        },
        headers=auth_headers,
        timeout=60,
    )
    if resp.status_code in (401, 403) or "API key not valid" in resp.text:
        raise GeminiAuthError(
            "Gemini rejected the API key (invalid, revoked, or API not enabled)."
        )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} (model busy or unavailable)")
    raw_text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    return parse(raw_text)


def discover_gemini_models(clean_key: str) -> List[str]:
    """Text-capable Gemini models on this key, newest first."""
    candidates = [
        "gemini-flash-latest",
        "gemini-2.5-flash",
        "gemini-flash-lite-latest",
        "gemini-2.5-flash-lite",
    ]
    try:
        r_list = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": clean_key},
            params={"pageSize": 200},
            timeout=8,
        )
        if r_list.status_code == 200:
            available = [
                m.get("name", "").replace("models/", "")
                for m in r_list.json().get("models", [])
                if "generateContent" in m.get("supportedGenerationMethods", [])
            ]
            # Allowlist plain text models only (skips tts/image/omni/robotics variants).
            ranked = sorted(
                (n for n in available if GEMINI_MODEL_RE.fullmatch(n)),
                key=gemini_model_rank,
            )
            if ranked:
                candidates = ranked + [
                    n for n in ("gemini-flash-latest", "gemini-flash-lite-latest") if n in available
                ]
    except Exception:
        pass
    return candidates


def race_gemini(clean_key: str, prompt: str, parse, status_box=None):
    """Asks the top models at once (quotas are per-model) and returns the first valid parsed answer."""
    auth_headers = {"x-goog-api-key": clean_key}
    wave = discover_gemini_models(clean_key)[:6]
    if status_box:
        status_box.write(
            "⚡ Racing " + ", ".join(f"`{m}`" for m in wave) + " in parallel (max 60s)..."
        )
    pool = ThreadPoolExecutor(max_workers=len(wave))
    futures = {pool.submit(_request_gemini, m, prompt, auth_headers, parse): m for m in wave}
    last_error_msg = ""
    try:
        for fut in as_completed(futures):
            model_name = futures[fut]
            try:
                result = fut.result()
                if status_box:
                    status_box.write(f"✅ `{model_name}` answered.")
                return result
            except GeminiAuthError as e:
                if status_box:
                    status_box.write(f"🔑 {e}")
                raise
            except requests.Timeout:
                last_error_msg = f"{model_name} timed out"
            except Exception as e:
                last_error_msg = f"{model_name}: {str(e)[:140]}"
            if status_box:
                status_box.write(f"⚠️ {last_error_msg}")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    raise RuntimeError(last_error_msg or "No Gemini model answered.")


def generate_production_plan(
    api_key: str,
    topic: str,
    channel_name: str,
    custom_video_title: str,
    include_voice_branding: bool,
    target_minutes: float,
    num_scenes: int,
    visual_style: str,
    is_short: bool,
    language: str = "English",
    status_box=None,
    ai_auto_visuals: bool = False,
) -> VideoProductionPlan:
    clean_key = api_key.strip()
    total_words = int(target_minutes * 145)
    words_per_scene = max(18, int(total_words / num_scenes))
    format_label = (
        "9:16 Vertical YouTube Short" if is_short else "16:9 Long-Form YouTube Video"
    )

    brand_instructions = ""
    if channel_name.strip():
        brand_instructions += f"\n- CHANNEL NAME: '{channel_name.strip()}'."
        if include_voice_branding:
            brand_instructions += (
                f" Welcome viewers to '{channel_name.strip()}' in Scene 1 after the hook, "
                f"and ask viewers to subscribe to '{channel_name.strip()}' in Scene {num_scenes}."
            )
    if custom_video_title.strip():
        brand_instructions += f"\n- LOCKED VIDEO TITLE: '{custom_video_title.strip()}'. Every scene and visual must stay 100% focused on this title."

    if ai_auto_visuals:
        style_rule = (
            "4. AI ART DIRECTION: Pick the single most attractive, high-retention art style, color palette and "
            "lighting for this topic, write it in `auto_visual_style`, and bake that style plus a rich, detailed "
            "atmospheric background into EVERY `visual_prompt` (always ending with 'no text')."
        )
    else:
        style_rule = f'4. Style every `visual_prompt` with: "{visual_style}, ultra-detailed, cinematic lighting, no text".'

    prompt = f"""You are an elite YouTube Director, Scriptwriter, and Visual Storyboard Artist.
Create a complete production plan for a {format_label} about:
TOPIC: "{topic}"
{brand_instructions}

STRICT SCENE & IMAGE ALIGNMENT RULES:
0. LANGUAGE: Write `narration`, `subtitle_summary`, `chapter_title`, all titles, the description, hashtags and tags in {language}. Keep `visual_prompt`, `thumbnail_prompt` and `stock_search_keyword` in English.
1. Generate exactly {num_scenes} sequential scenes (`scene_number` 1 to {num_scenes}).
2. Target total duration: {target_minutes} minutes (~{total_words} spoken words total, ~{words_per_scene} words per scene).
3. STRICT VISUAL LOCK: For EVERY scene, write the `narration` first, then a `visual_prompt` (45-80 words, English) that literally depicts the EXACT subject, setting and action spoken in that scene's `narration`, written in this order: main subject + action; environment and detailed background; camera shot (extreme close-up / close-up / medium / wide / aerial / macro) and angle; lighting; color palette and mood. Describe only things that can be drawn: no abstract concepts, no on-screen text, no logos, no real people's names. If a character recurs, repeat the same appearance details (age, clothing, hair) in every scene so they stay consistent.
{style_rule}
5. Provide exactly 3 `viral_titles`, 15 `hashtags` (each starting with #), and 15 `tags`. The `thumbnail_prompt` must show one dramatic, high-contrast close-up subject with a striking detail or strong emotion, bold complementary colors and a clean uncluttered area at the top for a title, with no text in the image.
6. For each scene choose `camera_motion` (zoom_in, zoom_out, pan_right, pan_left) to fit the action, varying it between scenes, and `preferred_media`: "stock_video" when real-world footage fits better (people, places, nature, everyday actions), otherwise "ai_image".
7. PROOFREAD: The topic may contain spelling mistakes. Understand the intended meaning and never copy typos. Before answering, proofread every title, narration, caption, chapter title, description, hashtag and tag for perfect spelling, grammar and punctuation in {language}, using correct technical and medical terminology. Hashtags must be single words with no spaces or punctuation.
8. Return ONLY valid JSON matching this exact structure:
{{
  "primary_title": "Main Viral Title",
  "viral_titles": ["Title Option 1", "Title Option 2", "Title Option 3"],
  "youtube_description": "Full SEO description paragraph...",
  "hashtags": ["#hashtag1", "#hashtag2", "#hashtag3"],
  "tags": ["tag1", "tag2", "tag3"],
  "thumbnail_prompt": "High contrast thumbnail visual description...",
  "auto_visual_style": "{'AI-chosen art style and lighting' if ai_auto_visuals else ''}",
  "scenes": [
    {{
      "scene_number": 1,
      "narration": "Spoken voiceover for scene 1...",
      "visual_prompt": "Exact visual illustration of scene 1 narration...",
      "stock_search_keyword": "emergency hospital",
      "subtitle_summary": "SHORT SCENE CAPTION",
      "chapter_title": "Scene 1 Title",
      "camera_motion": "zoom_in",
      "preferred_media": "ai_image"
    }}
  ]
}}"""

    last_error_msg = ""
    if clean_key:
        try:
            return race_gemini(clean_key, prompt, parse_plan_text, status_box)
        except Exception as e:
            last_error_msg = str(e)[:200]

    if status_box:
        status_box.write(
            "🔄 Using free backup AI script engine to complete your request..."
        )
    for attempt in range(1, 4):
        if attempt > 1:
            # Free tier allows 1 reply per ~45s per IP, counted from the previous reply.
            if status_box:
                status_box.write(f"⏳ Waiting 50s for backup engine cooldown (attempt {attempt}/3)...")
            time.sleep(50)
        try:
            backup_resp = requests.post(
                "https://text.pollinations.ai/openai",
                json={
                    "model": "openai-fast",
                    "reasoning_effort": "low",
                    "max_tokens": 12000,
                    "response_format": {"type": "json_object"},
                    "seed": random.randint(1, 10**6),
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are a YouTube director. Output strictly valid JSON matching the requested schema. No reasoning, no commentary.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=120,
            )
            if backup_resp.status_code != 200:
                last_error_msg += f" | Backup HTTP {backup_resp.status_code}: {backup_resp.text[:120]}"
                continue
            content = backup_resp.json()["choices"][0]["message"]["content"]
            return VideoProductionPlan.model_validate(
                normalize_plan_dict(parse_llm_json(content))
            )
        except Exception as e:
            last_error_msg += f" | Backup attempt {attempt}: {str(e)[:160]}"

    raise RuntimeError(
        "Could not generate script. Add a valid GEMINI_API_KEY to `.env` for reliable, fast scripts "
        f"(free at aistudio.google.com/apikey). Details: {last_error_msg.strip(' |')}"
    )


def normalize_plan_dict(data: dict) -> dict:
    """Coerces common LLM JSON quirks (strings instead of lists, missing numbers)."""
    for list_key in ("viral_titles", "hashtags", "tags"):
        val = data.get(list_key)
        if isinstance(val, str):
            data[list_key] = [v.strip() for v in re.split(r"[,\n]|\s(?=#)", val) if v.strip()]
    scenes = data.get("scenes") or []
    for i, sc in enumerate(scenes, start=1):
        sc.setdefault("scene_number", i)
        narration = str(sc.get("narration", "")).strip()
        sc.setdefault("visual_prompt", narration)
        sc.setdefault("chapter_title", f"Part {i}")
        sc.setdefault("stock_search_keyword", sc["chapter_title"])
        sc.setdefault("subtitle_summary", textwrap.shorten(narration or sc["chapter_title"], 60, placeholder="..."))
        if sc.get("camera_motion") not in CAMERA_MOTIONS:
            sc["camera_motion"] = CAMERA_MOTIONS[(i - 1) % len(CAMERA_MOTIONS)]
        if sc.get("preferred_media") not in MEDIA_TYPES:
            sc["preferred_media"] = "ai_image"
    if not isinstance(data.get("auto_visual_style"), str):
        data["auto_visual_style"] = ""
    return data


def proofread_plan(
    api_key: str, plan: VideoProductionPlan, language: str, status_box=None
) -> VideoProductionPlan:
    """Fixes spelling/grammar in every viewer-facing text field without changing meaning."""
    texts = {
        "primary_title": plan.primary_title,
        "viral_titles": plan.viral_titles,
        "youtube_description": plan.youtube_description,
        "hashtags": plan.hashtags,
        "tags": plan.tags,
        "scenes": [
            {
                "narration": s.narration,
                "subtitle_summary": s.subtitle_summary,
                "chapter_title": s.chapter_title,
            }
            for s in plan.scenes
        ],
    }
    prompt = (
        f"You are a meticulous {language} copy editor. Fix ONLY spelling, grammar, punctuation and "
        "wrongly spelled technical or medical terms in the JSON below. Do not change meaning, tone, length, "
        "order or the number of items. Hashtags stay single words starting with #. Return ONLY the corrected "
        "JSON with exactly the same keys and structure.\n\n" + json.dumps(texts, ensure_ascii=False)
    )

    def parse(raw_text: str) -> dict:
        data = parse_llm_json(raw_text)
        if len(data.get("scenes") or []) != len(plan.scenes):
            raise ValueError("proofreader changed the number of scenes")
        return data

    fixed = race_gemini(api_key.strip(), prompt, parse, status_box)
    merged = plan.model_dump()
    for key in ("primary_title", "youtube_description"):
        if isinstance(fixed.get(key), str) and fixed[key].strip():
            merged[key] = fixed[key].strip()
    for key in ("viral_titles", "hashtags", "tags"):
        if isinstance(fixed.get(key), list) and fixed[key]:
            merged[key] = [str(x).strip() for x in fixed[key] if str(x).strip()]
    for scene, fixed_scene in zip(merged["scenes"], fixed["scenes"]):
        if not isinstance(fixed_scene, dict):
            continue
        for key in ("narration", "subtitle_summary", "chapter_title"):
            value = fixed_scene.get(key)
            if isinstance(value, str) and value.strip():
                scene[key] = value.strip()
    return VideoProductionPlan.model_validate(merged)


# =====================================================================
# 5. SCENE-LOCKED IMAGE GENERATOR & MEDIA PIPELINE
# =====================================================================
async def synthesize_voiceover(
    text: str, voice: str, rate: str, audio_path: Path
) -> None:
    communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate)
    await communicate.save(str(audio_path))


def get_media_duration(media_path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(media_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return float(res.stdout.strip())


IMAGE_ENGINE_LABELS = {
    "auto": "🤖 Auto (best engine available)",
    "cloudflare": "☁️ Cloudflare FLUX.2 (free daily quota, HD)",
    "pollinations_key": "🌸 Pollinations API key (HD, no logo)",
    "gemini": "💎 Gemini Image / Nano Banana (needs billing)",
    "pollinations_free": "🆓 Pollinations free (576px, 1 image per 45s)",
}
GEMINI_IMAGE_MODELS = ["gemini-2.5-flash-image", "gemini-3.1-flash-image"]
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}
# Engines that failed for a non-transient reason (bad key, no quota) are skipped for the rest of the run.
_disabled_engines: set = set()


class EngineUnavailable(RuntimeError):
    pass


def build_image_prompt(visual_prompt: str, visual_style: str, portrait: bool, max_len: int = 1800) -> str:
    """Subject first, then style, composition, lighting and quality cues; the suffix is never truncated."""
    frame = "vertical 9:16 portrait composition" if portrait else "wide 16:9 cinematic composition"
    suffix = (
        f". Art style: {visual_style}. {frame}, one clear focal subject, rule of thirds, layered depth "
        "with a rich detailed background, cinematic volumetric lighting, high dynamic range, professional "
        "color grading, tack-sharp focus, intricate fine detail, 8k masterpiece. Absolutely no text, letters, "
        "words, captions, numbers, watermark, logo or signature; no blur, no distortion, anatomically correct."
    )
    head = re.sub(r"\s+", " ", visual_prompt).strip().rstrip(".")
    return head[: max(200, max_len - len(suffix))] + suffix


def has_cloudflare_keys(keys: dict) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{32}", keys.get("cf_account", "")) and keys.get("cf_token"))


def available_image_engines(keys: dict) -> List[str]:
    engines = []
    if has_cloudflare_keys(keys):
        engines.append("cloudflare")
    if keys.get("pollinations_key"):
        engines.append("pollinations_key")
    if keys.get("gemini_key"):
        engines.append("gemini")
    engines.append("pollinations_free")
    return engines


def _img_cloudflare(prompt: str, portrait: bool, seed: int, keys: dict, log) -> bytes:
    if not has_cloudflare_keys(keys):
        raise EngineUnavailable("CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN missing or malformed")
    w, h = (864, 1536) if portrait else (1536, 864)
    base = f"https://api.cloudflare.com/client/v4/accounts/{keys['cf_account']}/ai/run/@cf/black-forest-labs/"
    headers = {"Authorization": f"Bearer {keys['cf_token']}"}
    resp = requests.post(
        base + "flux-2-klein-4b",
        headers=headers,
        files={
            "prompt": (None, prompt[:2048]),
            "width": (None, str(w)),
            "height": (None, str(h)),
            "seed": (None, str(seed % 2**31)),
        },
        timeout=120,
    )
    if resp.status_code in (401, 403):
        raise EngineUnavailable(f"Cloudflare rejected the API token (HTTP {resp.status_code})")
    if resp.status_code == 429 or (not resp.ok and "neurons" in resp.text.lower()):
        raise EngineUnavailable("Cloudflare free daily quota (10,000 neurons) is used up; resets 00:00 UTC")
    if resp.ok:
        image_b64 = (resp.json().get("result") or {}).get("image")
        if image_b64:
            return base64.b64decode(image_b64)
    # FLUX.1 schnell only outputs squares; FFmpeg crops it to the frame later.
    resp = requests.post(
        base + "flux-1-schnell",
        headers=headers,
        json={"prompt": prompt[:2048], "steps": 8, "seed": seed % 2**31},
        timeout=120,
    )
    resp.raise_for_status()
    return base64.b64decode(resp.json()["result"]["image"])


def _img_pollinations_key(prompt: str, portrait: bool, seed: int, keys: dict, log) -> bytes:
    if not keys.get("pollinations_key"):
        raise EngineUnavailable("POLLINATIONS_API_KEY missing")
    w, h = (864, 1536) if portrait else (1536, 864)
    resp = requests.get(
        "https://gen.pollinations.ai/image/" + urllib.parse.quote(prompt[:1800], safe=""),
        params={
            "model": os.getenv("POLLINATIONS_IMAGE_MODEL", "black-forest-labs/flux.1-schnell"),
            "width": w,
            "height": h,
            "seed": seed,
            "nologo": "true",
        },
        headers={"Authorization": f"Bearer {keys['pollinations_key']}"},
        timeout=120,
    )
    if resp.status_code in (401, 402, 403):
        raise EngineUnavailable(f"Pollinations key rejected or out of pollen (HTTP {resp.status_code})")
    resp.raise_for_status()
    return resp.content


def _img_gemini(prompt: str, portrait: bool, seed: int, keys: dict, log) -> bytes:
    if not keys.get("gemini_key"):
        raise EngineUnavailable("GEMINI_API_KEY missing")
    quota_errors = 0
    for model in GEMINI_IMAGE_MODELS:
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": keys["gemini_key"]},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "responseModalities": ["IMAGE"],
                    "imageConfig": {"aspectRatio": "9:16" if portrait else "16:9"},
                },
            },
            timeout=120,
        )
        if resp.status_code == 429:
            quota_errors += 1
            continue
        if not resp.ok:
            continue
        for part in resp.json()["candidates"][0]["content"].get("parts", []):
            if "inlineData" in part:
                return base64.b64decode(part["inlineData"]["data"])
    if quota_errors == len(GEMINI_IMAGE_MODELS):
        raise EngineUnavailable(
            "Gemini image models have no quota on this key (enable billing in Google AI Studio)"
        )
    raise RuntimeError("Gemini returned no image")


def _img_pollinations_free(prompt: str, portrait: bool, seed: int, keys: dict, log) -> bytes:
    # Anonymous tier: max 768x768 pixel area and one image per ~45s per IP.
    w, h = (576, 1024) if portrait else (1024, 576)
    url = "https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt[:900], safe="")
    for attempt in range(2):
        resp = requests.get(
            url,
            params={"width": w, "height": h, "seed": seed, "nologo": "true"},
            headers=BROWSER_HEADERS,
            timeout=90,
        )
        if resp.status_code == 402 and attempt == 0:
            try:
                wait = min(60, int(resp.headers.get("Retry-After", "46"))) + 1
            except ValueError:
                wait = 47
            if log:
                log(f"⏳ Free image engine allows 1 image per 45s, waiting {wait}s...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.content
    raise RuntimeError("free image engine still rate-limited")


IMAGE_ENGINE_FUNCS = {
    "cloudflare": _img_cloudflare,
    "pollinations_key": _img_pollinations_key,
    "gemini": _img_gemini,
    "pollinations_free": _img_pollinations_free,
}


def _is_valid_image(data: bytes) -> bool:
    try:
        Image.open(io.BytesIO(data)).verify()
        return len(data) > 5000
    except Exception:
        return False


def generate_scene_image(
    visual_prompt: str,
    narration_context: str,
    topic_context: str,
    visual_style: str,
    width: int,
    height: int,
    is_short: bool,
    seed: int,
    output_path: Path,
    engine: str = "auto",
    keys: Optional[dict] = None,
    log=None,
) -> str:
    """Saves the best available AI image (or a fallback card) and returns the engine name used."""
    keys = keys or {}
    portrait = height > width
    prompt = build_image_prompt(visual_prompt, visual_style, portrait)
    order = available_image_engines(keys) if engine == "auto" else [engine]
    if "pollinations_free" not in order:
        order.append("pollinations_free")

    for name in order:
        if name in _disabled_engines:
            continue
        try:
            data = IMAGE_ENGINE_FUNCS[name](prompt, portrait, seed, keys, log)
            if data and _is_valid_image(data):
                Image.open(io.BytesIO(data)).convert("RGB").save(output_path, "JPEG", quality=95)
                return name
        except EngineUnavailable as e:
            _disabled_engines.add(name)
            if log:
                log(f"⚠️ {IMAGE_ENGINE_LABELS[name]}: {e}")
        except Exception as e:
            if log:
                log(f"⚠️ {IMAGE_ENGINE_LABELS[name]} failed: {str(e)[:120]}")

    img = Image.new("RGB", (width, height), color=(10, 18, 36))
    draw = ImageDraw.Draw(img)
    draw.rectangle([28, 28, width - 28, height - 28], outline=(6, 182, 212), width=4)
    draw.rectangle([42, 42, width - 42, height - 42], outline=(236, 72, 153), width=2)

    title_fnt = load_font(int(height * 0.036), visual_prompt)
    body_fnt = load_font(int(height * 0.028), narration_context)

    header_lines = textwrap.wrap(visual_prompt, width=28 if is_short else 46)[:3]
    y_pos = int(height * 0.24)
    for hl in header_lines:
        bb = draw.textbbox((0, 0), hl, font=title_fnt)
        lw = bb[2] - bb[0]
        draw.text(((width - lw) // 2, y_pos), hl, font=title_fnt, fill=(56, 189, 248))
        y_pos += int(height * 0.045)

    y_pos += 25
    wrapped = textwrap.wrap(narration_context, width=32 if is_short else 52)[:5]
    for line in wrapped:
        bb = draw.textbbox((0, 0), line, font=body_fnt)
        lw = bb[2] - bb[0]
        draw.text(((width - lw) // 2, y_pos), line, font=body_fnt, fill=(241, 245, 249))
        y_pos += int(height * 0.042)

    img.save(output_path, "JPEG", quality=92)
    return "placeholder"


def download_pexels_stock_video(
    pexels_key: str, keyword: str, is_short: bool, output_path: Path
) -> bool:
    api_key = pexels_key.strip()
    if not api_key:
        return False

    try:
        resp = requests.get(
            "https://pixabay.com/api/videos/",
            params={"key": api_key, "q": keyword, "per_page": 5, "safesearch": "true"},
            timeout=18,
        )
        if resp.status_code == 200:
            hits = resp.json().get("hits", [])
            for hit in hits:
                vids = hit.get("videos", {})
                for tier in ["medium", "large", "small"]:
                    video_url = vids.get(tier, {}).get("url")
                    if video_url:
                        vid_data = requests.get(video_url, timeout=30)
                        if vid_data.status_code == 200 and len(vid_data.content) > 10000:
                            output_path.write_bytes(vid_data.content)
                            return True
    except Exception:
        pass
    return False


LATIN_FONTS = [
    "C:/Windows/Fonts/arialbd.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
]
UNICODE_FONTS = [
    "C:/Windows/Fonts/arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
]
DEVANAGARI_FONTS = [
    "/System/Library/Fonts/Kohinoor.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
]
CJK_FONTS = [
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]


def load_font(size: int, text: str = "") -> ImageFont.FreeTypeFont:
    candidates = LATIN_FONTS
    if any("\u0900" <= ch <= "\u097f" for ch in text):
        candidates = UNICODE_FONTS + DEVANAGARI_FONTS + LATIN_FONTS
    elif any(ord(ch) >= 0x3000 for ch in text):
        candidates = UNICODE_FONTS + CJK_FONTS + LATIN_FONTS
    elif any(ord(ch) > 0x024F for ch in text):
        candidates = UNICODE_FONTS + DEVANAGARI_FONTS + CJK_FONTS + LATIN_FONTS
    for font_candidate in candidates:
        if Path(font_candidate).exists():
            try:
                return ImageFont.truetype(font_candidate, size)
            except Exception:
                pass
    return ImageFont.load_default()


def build_circular_logo_icon(
    logo_path: Optional[Path], channel_name: str, icon_size: int
) -> Image.Image:
    icon = Image.new("RGBA", (icon_size, icon_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(icon)

    if logo_path and logo_path.exists():
        try:
            raw_logo = Image.open(logo_path).convert("RGBA")
            raw_logo = raw_logo.resize((icon_size, icon_size), Image.Resampling.LANCZOS)
            mask = Image.new("L", (icon_size, icon_size), 0)
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.ellipse((0, 0, icon_size - 1, icon_size - 1), fill=255)
            icon.paste(raw_logo, (0, 0), mask)
            draw.ellipse(
                (1, 1, icon_size - 2, icon_size - 2),
                outline=(56, 189, 248, 240),
                width=3,
            )
            return icon
        except Exception:
            pass

    draw.ellipse(
        (0, 0, icon_size - 1, icon_size - 1),
        fill=(124, 58, 237, 235),
        outline=(56, 189, 248, 255),
        width=3,
    )
    initials = (
        "".join([w[0].upper() for w in channel_name.split()[:2]])
        if channel_name.strip()
        else "TF"
    )
    fnt = load_font(int(icon_size * 0.44))
    bbox = draw.textbbox((0, 0), initials, font=fnt)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        ((icon_size - tw) // 2, (icon_size - th) // 2 - 2),
        initials,
        font=fnt,
        fill=(255, 255, 255, 255),
    )
    return icon


def create_scene_broadcast_overlay(
    width: int,
    height: int,
    is_short: bool,
    logo_path: Optional[Path],
    channel_name: str,
    video_title_banner: str,
    brand_position: str,
    caption_text: str,
    enable_captions: bool,
    output_png: Path,
) -> None:
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    margin_x = int(width * 0.04)
    margin_y = int(height * 0.04)

    has_brand = bool(channel_name.strip()) or (logo_path and logo_path.exists())
    if has_brand:
        icon_size = 64 if is_short else 58
        icon_img = build_circular_logo_icon(logo_path, channel_name, icon_size)

        display_name = channel_name.strip()
        brand_font = load_font(26 if is_short else 24, display_name)
        text_w = 0
        if display_name:
            bbox = draw.textbbox((0, 0), display_name, font=brand_font)
            text_w = bbox[2] - bbox[0]

        pad_x = 16
        pill_w = icon_size + (text_w + pad_x * 2 if display_name else 0)
        pill_h = icon_size + 12

        if "Top-Left" in brand_position:
            bx, by = margin_x, margin_y
        elif "Bottom-Left" in brand_position:
            bx, by = margin_x, height - margin_y - pill_h
        elif "Bottom-Right" in brand_position:
            bx, by = width - margin_x - pill_w, height - margin_y - pill_h
        else:
            bx, by = width - margin_x - pill_w, margin_y

        draw.rounded_rectangle(
            [bx, by, bx + pill_w, by + pill_h],
            radius=pill_h // 2,
            fill=(10, 14, 26, 195),
            outline=(139, 92, 246, 190),
            width=2,
        )
        canvas.paste(icon_img, (bx + 6, by + 6), icon_img)

        if display_name:
            ty = by + (pill_h - (26 if is_short else 24)) // 2 - 2
            draw.text(
                (bx + icon_size + 14, ty),
                display_name,
                font=brand_font,
                fill=(249, 250, 251, 255),
            )

    if video_title_banner.strip():
        title_font = load_font(24 if is_short else 26, video_title_banner)
        short_title = textwrap.shorten(
            video_title_banner.strip().upper(),
            width=38 if is_short else 65,
            placeholder="...",
        )
        tb = draw.textbbox((0, 0), short_title, font=title_font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]

        tx = margin_x if "Right" in brand_position else (width - tw - 36) // 2
        ty = margin_y + (84 if is_short and "Top" in brand_position else 0)

        draw.rounded_rectangle(
            [tx, ty, tx + tw + 32, ty + th + 20],
            radius=12,
            fill=(124, 58, 237, 210),
            outline=(56, 189, 248, 220),
            width=2,
        )
        draw.text(
            (tx + 16, ty + 8),
            short_title,
            font=title_font,
            fill=(255, 255, 255, 255),
        )

    if enable_captions and caption_text.strip():
        cap_size = int(height * 0.030) if is_short else int(height * 0.038)
        cap_font = load_font(cap_size, caption_text)
        wrap_chars = 26 if is_short else 48
        lines = textwrap.wrap(caption_text.upper(), width=wrap_chars)[:3]
        line_h = int(cap_size * 1.35)
        total_h = len(lines) * line_h

        y_start = int(height * 0.75) if is_short else int(height * 0.79)
        pad = 20
        draw.rounded_rectangle(
            [
                int(width * 0.07),
                y_start - pad,
                int(width * 0.93),
                y_start + total_h + pad,
            ],
            radius=16,
            fill=(8, 12, 24, 205),
            outline=(124, 58, 237, 190),
            width=2,
        )
        for i, line in enumerate(lines):
            bb = draw.textbbox((0, 0), line, font=cap_font)
            lw = bb[2] - bb[0]
            lx = (width - lw) // 2
            ly = y_start + i * line_h
            draw.text((lx, ly), line, font=cap_font, fill=(250, 204, 21, 255))

    canvas.save(output_png, "PNG")


def stamp_brand_on_image(
    base_image_path: Path,
    overlay_png_path: Path,
) -> None:
    if not base_image_path.exists() or not overlay_png_path.exists():
        return
    base_img = Image.open(base_image_path).convert("RGBA")
    ov_img = Image.open(overlay_png_path).convert("RGBA")
    if ov_img.size != base_img.size:
        ov_img = ov_img.resize(base_img.size, Image.Resampling.LANCZOS)
    merged = Image.alpha_composite(base_img, ov_img).convert("RGB")
    merged.save(base_image_path, "JPEG", quality=95)


def fit_image_to_frame(image_path: Path, width: int, height: int) -> None:
    """Center-crops and resizes to the exact frame (YouTube wants 1280x720 thumbnails)."""
    img = Image.open(image_path).convert("RGB")
    ImageOps.fit(img, (width, height), Image.Resampling.LANCZOS).save(image_path, "JPEG", quality=95)


def add_thumbnail_headline(image_path: Path, headline: str) -> None:
    """Draws a bold, high-contrast headline in the lower third, the classic high-CTR thumbnail layout."""
    img = Image.open(image_path).convert("RGB")
    w, h = img.size
    gradient = Image.linear_gradient("L").resize((w, h))
    shade = gradient.point(lambda v: max(0, int((v - 110) * 1.5)))
    img = Image.composite(Image.new("RGB", (w, h), (0, 0, 0)), img, shade)

    text = textwrap.shorten(headline.strip().upper(), 52, placeholder="...")
    portrait = h > w
    font_size = int(h * (0.055 if portrait else 0.1))
    font = load_font(font_size, text)
    stroke = max(3, font_size // 11)
    lines = textwrap.wrap(text, width=16 if portrait else 22)[:3]
    line_h = int(font_size * 1.15)
    y = h - int(h * (0.12 if portrait else 0.07)) - line_h * len(lines)
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        bb = draw.textbbox((0, 0), line, font=font, stroke_width=stroke)
        x = (w - (bb[2] - bb[0])) // 2
        color = (255, 214, 0) if i == len(lines) - 1 and len(lines) > 1 else (255, 255, 255)
        draw.text((x, y), line, font=font, fill=color, stroke_width=stroke, stroke_fill=(0, 0, 0))
        y += line_h
    img.filter(ImageFilter.UnsharpMask(radius=2, percent=60, threshold=3)).save(
        image_path, "JPEG", quality=95
    )


def run_ffmpeg(cmd: List[str]) -> None:
    """Runs an ffmpeg command and raises with the real error text on failure."""
    full_cmd = [cmd[0], "-hide_banner", "-loglevel", "error", *cmd[1:]]
    proc = subprocess.run(full_cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-8:])
        raise RuntimeError(f"FFmpeg failed ({Path(cmd[-1]).name}):\n{tail}")


def build_video_filter(
    source_chain: str, duration: float, fade: bool, has_audio: bool
) -> str:
    fade_chain = ""
    if fade and duration > 1.2:
        fade_chain = (
            f"fade=t=in:st=0:d=0.35,"
            f"fade=t=out:st={duration - 0.35:.2f}:d=0.35,"
        )
    graph = f"{source_chain};[bg][1:v]overlay=0:0,{fade_chain}format=yuv420p[vout]"
    if has_audio:
        # apad keeps the clip exactly `duration` long so subtitles stay in sync.
        graph += ";[2:a]apad,aformat=sample_rates=44100:channel_layouts=stereo[aout]"
    return graph


def finish_scene_cmd(
    cmd: List[str], duration: float, has_audio: bool, output_clip: Path
) -> List[str]:
    cmd += [
        "-map",
        "[vout]",
        "-t",
        f"{duration:.2f}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "21",
        "-r",
        "30",
    ]
    if has_audio:
        cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-an"]
    cmd.append(str(output_clip))
    return cmd


def render_scene_from_image(
    image_path: Path,
    overlay_png: Path,
    audio_path: Optional[Path],
    duration: float,
    width: int,
    height: int,
    motion: str,
    output_clip: Path,
    fade: bool = True,
) -> None:
    fps = 30
    frames = max(30, int(duration * fps))

    center_x, center_y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    z_expr, x_expr, y_expr = {
        "zoom_out": ("if(eq(on,1),1.18,max(1.001,zoom-0.0007))", center_x, center_y),
        "pan_right": ("1.12", "if(eq(on,1),0,min(iw-iw/zoom,x+2))", center_y),
        "pan_left": ("1.12", "if(eq(on,1),iw-iw/zoom,max(0,x-2))", center_y),
    }.get(motion, ("min(zoom+0.0007,1.18)", center_x, center_y))

    has_audio = bool(audio_path and audio_path.exists())
    source_chain = (
        f"[0:v]scale={width*2}:{height*2}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={width*2}:{height*2},"
        f"zoompan=z='{z_expr}':x='{x_expr}':y='{y_expr}':"
        f"d={frames}:s={width}x{height}:fps={fps},"
        # Light sharpening restores detail lost when smaller AI images are upscaled to 1080p.
        f"unsharp=5:5:0.6:5:5:0.0[bg]"
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-loop",
        "1",
        "-i",
        str(image_path),
        "-i",
        str(overlay_png),
    ]
    if has_audio:
        cmd += ["-i", str(audio_path)]
    cmd += [
        "-filter_complex",
        build_video_filter(source_chain, duration, fade, has_audio),
    ]
    run_ffmpeg(finish_scene_cmd(cmd, duration, has_audio, output_clip))


def render_scene_from_stock_video(
    raw_video: Path,
    overlay_png: Path,
    audio_path: Optional[Path],
    duration: float,
    width: int,
    height: int,
    output_clip: Path,
    fade: bool = True,
) -> None:
    has_audio = bool(audio_path and audio_path.exists())
    source_chain = (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={width}:{height},fps=30,setsar=1[bg]"
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-stream_loop",
        "-1",
        "-i",
        str(raw_video),
        "-i",
        str(overlay_png),
    ]
    if has_audio:
        cmd += ["-i", str(audio_path)]
    cmd += [
        "-filter_complex",
        build_video_filter(source_chain, duration, fade, has_audio),
    ]
    run_ffmpeg(finish_scene_cmd(cmd, duration, has_audio, output_clip))


def combine_clips_and_mix_bgm(
    clip_paths: List[Path],
    bgm_path: Optional[Path],
    final_output: Path,
    has_voice: bool = True,
    bgm_volume: float = 0.12,
    auto_duck: bool = True,
    total_duration: float = 0.0,
) -> None:
    concat_txt = final_output.parent / "concat_list.txt"
    concat_txt.write_text(
        "\n".join([f"file '{c.resolve().as_posix()}'" for c in clip_paths]), encoding="utf-8"
    )

    stitched_temp = final_output.parent / "stitched_raw.mp4"
    cmd_concat = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_txt),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(stitched_temp),
    ]
    run_ffmpeg(cmd_concat)

    if bgm_path and bgm_path.exists():
        stereo = "aformat=sample_rates=44100:channel_layouts=stereo"
        bgm_fade = ""
        if total_duration > 4:
            bgm_fade = f",afade=t=out:st={total_duration - 2.5:.2f}:d=2.5"
        if not has_voice:
            fc = f"[1:a]{stereo},volume={min(1.0, bgm_volume * 4):.2f}{bgm_fade}[aout]"
        elif auto_duck:
            # Sidechain compression lowers the music whenever the narrator speaks.
            fc = (
                f"[0:a]{stereo},asplit=2[voice][sc];"
                f"[1:a]{stereo},volume={bgm_volume:.2f}{bgm_fade}[bgm];"
                f"[bgm][sc]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[ducked];"
                f"[voice][ducked]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[aout]"
            )
        else:
            fc = (
                f"[0:a]{stereo}[voice];"
                f"[1:a]{stereo},volume={bgm_volume:.2f}{bgm_fade}[bgm];"
                f"[voice][bgm]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[aout]"
            )
        cmd_bgm = [
            "ffmpeg",
            "-y",
            "-i",
            str(stitched_temp),
            "-stream_loop",
            "-1",
            "-i",
            str(bgm_path),
            "-filter_complex",
            fc,
            "-map",
            "0:v",
            "-map",
            "[aout]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            "-shortest",
            str(final_output),
        ]
        run_ffmpeg(cmd_bgm)
        stitched_temp.unlink(missing_ok=True)
    else:
        shutil.move(str(stitched_temp), str(final_output))


def split_subtitle_chunks(text: str, start: float, duration: float) -> List[tuple]:
    """Splits narration into short, readable subtitle lines timed by length."""
    sentences = [
        s.strip()
        for s in re.split(r"(?<=[.!?।])\s+|(?<=[。！？])", text)
        if s and s.strip()
    ]
    chunks: List[str] = []
    carry = ""
    for s in sentences:
        s = f"{carry} {s}".strip() if carry else s
        # Keep abbreviations like "Dr." attached to the following sentence.
        if len(s) <= 4:
            carry = s
            continue
        carry = ""
        chunks.extend(textwrap.wrap(s, width=84) or [s])
    if carry:
        chunks.append(carry)
    total_chars = sum(len(c) for c in chunks) or 1
    entries, t = [], start
    for c in chunks:
        seg = duration * len(c) / total_chars
        entries.append((t, t + seg, c))
        t += seg
    return entries


def build_project_zip(work_dir: Path, zip_path: Path) -> None:
    keep_suffixes = {".mp4", ".srt", ".jpg", ".json", ".txt"}
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(work_dir.iterdir()):
            if f == zip_path or f.suffix not in keep_suffixes:
                continue
            if f.name.startswith(("clip_", "stock_", "concat_list")):
                continue
            zf.write(f, arcname=f.name)


async def _collect_voice_sample(text: str, voice: str, rate: str) -> bytes:
    audio = bytearray()
    async for chunk in edge_tts.Communicate(text=text, voice=voice, rate=rate).stream():
        if chunk["type"] == "audio":
            audio.extend(chunk["data"])
    return bytes(audio)


def format_srt_time(seconds: float) -> str:
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{hrs:02d}:{mins:02d}:{secs:02d},{ms:03d}"


def format_yt_timestamp(seconds: float) -> str:
    return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}"


# =====================================================================
# 6. LOAD .ENV KEYS & UI-SELECTABLE BRANDING CONTROLS
# =====================================================================
VOICE_LIBRARY = {
    "English": [
        "en-US-AriaNeural (Professional Female English)",
        "en-US-ChristopherNeural (Deep Documentary Male)",
        "en-US-GuyNeural (Fast Viral Host Male)",
        "en-US-JennyNeural (Friendly Female Host)",
        "en-GB-RyanNeural (British Cinematic Narrator)",
        "en-GB-SoniaNeural (British Female Narrator)",
        "en-IN-PrabhatNeural (Clear Indian English Male)",
        "en-IN-NeerjaNeural (Clear Indian English Female)",
    ],
    "Hindi": ["hi-IN-MadhurNeural (Male)", "hi-IN-SwaraNeural (Female)"],
    "Spanish": ["es-MX-JorgeNeural (Mexico Male)", "es-ES-ElviraNeural (Spain Female)"],
    "French": ["fr-FR-HenriNeural (Male)", "fr-FR-DeniseNeural (Female)"],
    "German": ["de-DE-ConradNeural (Male)", "de-DE-KatjaNeural (Female)"],
    "Portuguese": ["pt-BR-AntonioNeural (Male)", "pt-BR-FranciscaNeural (Female)"],
    "Japanese": ["ja-JP-KeitaNeural (Male)", "ja-JP-NanamiNeural (Female)"],
}
VOICE_SAMPLES = {
    "English": "Welcome back to the channel. Let's dive right in.",
    "Hindi": "चैनल में आपका स्वागत है। चलिए शुरू करते हैं।",
    "Spanish": "Bienvenidos de nuevo al canal. Empecemos.",
    "French": "Bienvenue sur la chaîne. Commençons tout de suite.",
    "German": "Willkommen zurück auf dem Kanal. Legen wir los.",
    "Portuguese": "Bem-vindos de volta ao canal. Vamos começar.",
    "Japanese": "チャンネルへようこそ。さっそく始めましょう。",
}
OUTPUT_ROOT = Path("output")

env_keys = load_env_keys()
ui_prefs = load_ui_brand_prefs()
POS_OPTIONS = ["Top-Right", "Top-Left", "Bottom-Right", "Bottom-Left"]
PUBLIC_MODE = os.getenv("TUBEFORGE_PUBLIC_MODE", "").strip().lower() in ("1", "true", "yes", "on")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")

if APP_PASSWORD and not st.session_state.get("authenticated"):
    st.markdown("## 🔒 TubeForge Studio Pro")
    with st.form("login"):
        typed_pw = st.text_input("Password", type="password")
        if st.form_submit_button("Unlock", type="primary"):
            if hmac.compare_digest(typed_pw.encode(), APP_PASSWORD.encode()):
                st.session_state["authenticated"] = True
                st.rerun()
            time.sleep(1.5)
            st.error("Wrong password.")
    st.stop()

STUDIO_MODES = ["🎬 Cinematic AI Studio (new)", "🧰 Classic Studio"]
studio_mode = st.sidebar.radio("Studio", STUDIO_MODES, key="studio_mode", horizontal=False)
if studio_mode == STUDIO_MODES[0]:
    from tubeforge.studio_ui import render_studio

    render_studio(public_mode=PUBLIC_MODE)
    st.stop()


def _project_timestamp(folder_name: str) -> Optional[int]:
    try:
        return int(folder_name.split("_")[1])
    except (IndexError, ValueError):
        return None


if PUBLIC_MODE and OUTPUT_ROOT.exists():
    # Free hosts have small disks: drop renders older than 24 hours.
    for old_dir in OUTPUT_ROOT.glob("project_*"):
        stamp = _project_timestamp(old_dir.name)
        if stamp and time.time() - stamp > 86400:
            shutil.rmtree(old_dir, ignore_errors=True)

# Seed widget state once; afterwards the keyed widgets own their values.
st.session_state.setdefault("ui_channel_name", ui_prefs["channel_name"])
st.session_state.setdefault(
    "ui_brand_position",
    ui_prefs["brand_position"] if ui_prefs["brand_position"] in POS_OPTIONS else POS_OPTIONS[0],
)


def _persist_brand_prefs() -> None:
    save_ui_brand_prefs(
        st.session_state["ui_channel_name"], st.session_state["ui_brand_position"]
    )


with st.sidebar:
    st.markdown("### 🔐 1. API Keys (Loaded from `.env`)")
    env_gemini = env_keys["gemini_key"]
    if is_valid_gemini_key(env_gemini):
        st.success("✅ `GEMINI_API_KEY` loaded from `.env`")
        gemini_key = env_gemini
    else:
        if env_gemini:
            st.warning(
                "⚠️ `GEMINI_API_KEY` in `.env` is a placeholder or malformed. "
                "Paste your real key below (get one free at aistudio.google.com/apikey)."
            )
        typed_key = st.text_input(
            "Google Gemini API Key (Optional)",
            type="password",
            help="A valid key pasted here is saved to your local .env file. Leave blank to use the free backup engine.",
        ).strip()
        gemini_key = typed_key if is_valid_gemini_key(typed_key) else ""
        if gemini_key and PUBLIC_MODE:
            st.success("✅ Key active for this browser session only (never stored on the server).")
        elif gemini_key:
            save_env_key("GEMINI_API_KEY", gemini_key)
            st.success("✅ Key saved to `.env`.")
        elif typed_key:
            st.error("That doesn't look like a valid Gemini key (expected `AIza…` or `AQ.…`).")
        else:
            st.caption("ℹ️ No valid Gemini key — scripts will use the free backup engine.")

    pexels_key = env_keys["pixabay_key"]
    if pexels_key:
        st.caption("✅ `PIXABAY_API_KEY` loaded from `.env`")
    image_keys = {
        "cf_account": env_keys["cf_account"],
        "cf_token": env_keys["cf_token"],
        "pollinations_key": env_keys["pollinations_key"],
        "gemini_key": gemini_key,
    }
    if has_cloudflare_keys(image_keys):
        st.caption("✅ Cloudflare Workers AI keys loaded (HD FLUX images)")
    if image_keys["pollinations_key"]:
        st.caption("✅ `POLLINATIONS_API_KEY` loaded (HD images, no logo)")
    if st.get_option("server.address") not in ("localhost", "127.0.0.1") and not APP_PASSWORD:
        st.warning(
            "🌐 This app is reachable from the network without a password. "
            "Set `APP_PASSWORD` to protect your API keys."
        )

    st.markdown("---")
    st.markdown("### 📺 2. Channel Logo & UI Brand Controls")
    logo_upload = st.file_uploader(
        "Upload Channel Logo (.png / .jpg)",
        type=["png", "jpg", "jpeg", "webp"],
    )
    custom_video_title = st.text_input(
        "Custom Video Title Header (Optional)",
        value="",
        placeholder="Leave blank for AI auto-title",
    )
    include_voice_branding = st.checkbox(
        "AI Mentions Channel Name in Intro & Outro",
        value=True,
    )

    st.markdown("---")
    st.markdown("### 📐 3. Format & Visual Engine")
    aspect_choice = st.radio(
        "Output Aspect Ratio",
        options=[
            "9:16 YouTube Shorts / Reels (1080x1920)",
            "16:9 Long-Form YouTube (1920x1080)",
        ],
    )
    is_short = "9:16" in aspect_choice

    visual_mode = st.selectbox(
        "Visual Generation Mode",
        options=[
            "AI Storyboard + 30fps Camera Motion (Scene-Locked)",
            "Hybrid Mix (AI Scene Images + Pixabay Stock Video)",
            "Pixabay HD Stock Videos Only",
        ],
    )

    visual_style = st.selectbox(
        "Cinematic Art Direction",
        options=[
            AI_AUTO_STYLE,
            "3D Medical & Clinical Animation, dark navy background with cyan and magenta accents",
            "Cinematic Photorealistic Documentary, 35mm lens, dramatic lighting",
            "Dark Moody Historical Realism, atmospheric volumetric fog",
            "Futuristic Sci-Fi Unreal Engine 5 Concept Art, neon lighting",
            "3D Stylized Pixar-Grade Render, soft global illumination",
        ],
    )

    configured_engines = available_image_engines(image_keys)
    image_engine = st.selectbox(
        "AI Image Engine",
        options=["auto"] + configured_engines,
        format_func=lambda k: IMAGE_ENGINE_LABELS[k],
        help="Auto tries HD engines first and falls back to the free engine.",
    )
    if configured_engines == ["pollinations_free"]:
        st.caption(
            "ℹ️ Only the free 576px engine is configured (1 image per 45s). "
            "Add `CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN` to `.env` for free HD images."
        )

    target_minutes = st.slider(
        "Target Video Length (Minutes)",
        min_value=1.0,
        max_value=5.0,
        value=2.0 if is_short else 4.0,
        step=0.5,
    )
    num_scenes = st.slider(
        "Total Scenes to Stitch",
        min_value=6,
        max_value=50,
        value=12,
        step=1,
    )

    st.markdown("---")
    st.markdown("### 🎙️ 4. Voiceover & Audio Suite")
    enable_voice = st.checkbox("Enable Neural AI Voiceover", value=True)
    narration_language = st.selectbox(
        "Narration, Captions & SEO Language",
        options=list(VOICE_LIBRARY.keys()),
    )
    voice_option = st.selectbox(
        "Narrator Voice Profile",
        options=VOICE_LIBRARY[narration_language],
    )
    selected_voice = voice_option.split(" ")[0]
    voice_rate = st.select_slider(
        "Narration Pacing",
        options=["-10%", "-5%", "+0%", "+5%", "+10%", "+15%"],
        value="+5%",
    )
    if st.button("🔊 Preview Voice", width="stretch"):
        try:
            sample = asyncio.run(
                _collect_voice_sample(
                    VOICE_SAMPLES[narration_language], selected_voice, voice_rate
                )
            )
            st.audio(sample, format="audio/mp3")
        except Exception as e:
            st.warning(f"Voice preview unavailable: {e}")
    burn_captions = st.checkbox("Burn Viral Captions on Video", value=True)
    auto_proofread = st.checkbox(
        "✨ AI Proofread Script (fix spelling & grammar)",
        value=bool(gemini_key),
        disabled=not gemini_key,
        help="Runs a second Gemini pass that corrects spelling, grammar and terminology before rendering.",
    )
    fade_transitions = st.checkbox("Smooth Fade Transitions Between Scenes", value=True)
    bgm_upload = st.file_uploader(
        "Background Music Track (.mp3 / .wav)", type=["mp3", "wav"]
    )
    bgm_volume = (
        st.slider("Background Music Volume", min_value=2, max_value=40, value=12, step=1)
        / 100.0
    )
    auto_duck = st.checkbox(
        "Auto-Duck Music Under Voice (Sidechain)",
        value=True,
        help="Automatically lowers background music while the narrator is speaking.",
    )

    st.markdown("---")
    st.markdown("### 📂 5. Project History")
    past_projects = (
        sorted(OUTPUT_ROOT.glob("project_*/project.json"), reverse=True)[:30]
        if OUTPUT_ROOT.exists()
        else []
    )
    if PUBLIC_MODE:
        # Visitors only see their own renders on a shared deployment.
        my_runs = set(st.session_state.get("my_runs", []))
        past_projects = [pj for pj in past_projects if pj.parent.name in my_runs]
    if past_projects:
        history_labels = {}
        for pj in past_projects:
            try:
                title = json.loads(pj.read_text(encoding="utf-8"))["plan"]["primary_title"]
            except Exception:
                title = "Untitled"
            stamp = _project_timestamp(pj.parent.name)
            when = time.strftime("%d %b %H:%M", time.localtime(stamp)) if stamp else pj.parent.name
            history_labels[str(pj)] = f"{when} • {textwrap.shorten(title, 40)}"
        picked = st.selectbox(
            "Previous Renders",
            options=list(history_labels.keys()),
            format_func=lambda k: history_labels[k],
        )
        if st.button("📂 Open Project", width="stretch"):
            st.session_state["last_render"] = json.loads(
                Path(picked).read_text(encoding="utf-8")
            )
            st.rerun()
    else:
        st.caption("Completed renders will appear here.")

# =====================================================================
# 7. HERO HEADER & MAIN UI BRAND SELECTORS
# =====================================================================
st.markdown(
    """
    <div class="hero-box">
        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap;">
            <div>
                <div class="hero-title">🎬 TubeForge Studio Pro</div>
                <p class="hero-sub">Scene-Locked AI Visuals, .env API Security, UI Channel Branding & YouTube Title/Description/#Hashtag Engine</p>
            </div>
            <div>
                <span class="badge-safe">🔒 .env Keys Active • UI Brand Selector Ready</span>
            </div>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# Direct UI Selection Bar for channel_name and brand_position
b_col1, b_col2 = st.columns([3, 2])
with b_col1:
    channel_name = st.text_input(
        "📺 Channel Name (Select / Edit in UI — Auto-Saved):",
        key="ui_channel_name",
        on_change=_persist_brand_prefs,
        placeholder="e.g., theMedNurses",
    )
with b_col2:
    brand_position = st.selectbox(
        "📌 Brand Logo & Title Position (Select in UI):",
        options=POS_OPTIONS,
        key="ui_brand_position",
        on_change=_persist_brand_prefs,
    )

m1, m2, m3, m4, m5 = st.columns(5)
with m1:
    st.markdown(
        f'<div class="metric-card"><div class="metric-label">Channel Brand</div><div class="metric-val">{html.escape(channel_name.strip()) or "Unbranded"}</div></div>',
        unsafe_allow_html=True,
    )
with m2:
    st.markdown(
        f'<div class="metric-card"><div class="metric-label">Brand Position</div><div class="metric-val">{brand_position}</div></div>',
        unsafe_allow_html=True,
    )
with m3:
    st.markdown(
        f'<div class="metric-card"><div class="metric-label">Aspect Ratio</div><div class="metric-val">{"9:16 Shorts" if is_short else "16:9 Long-Form"}</div></div>',
        unsafe_allow_html=True,
    )
with m4:
    st.markdown(
        f'<div class="metric-card"><div class="metric-label">Scene Count</div><div class="metric-val">{num_scenes} Clips ({target_minutes:.1f}m)</div></div>',
        unsafe_allow_html=True,
    )
with m5:
    key_ready = "✅ Gemini Key" if gemini_key else "🆓 Backup Engine"
    st.markdown(
        f'<div class="metric-card"><div class="metric-label">API Key Status</div><div class="metric-val">{key_ready}</div></div>',
        unsafe_allow_html=True,
    )

st.write("")

# =====================================================================
# 8. RENDER PIPELINE (VOICE -> VISUALS -> THUMBNAIL -> MASTER)
# =====================================================================
def run_render_pipeline(plan: VideoProductionPlan, topic_text: str) -> None:
    save_ui_brand_prefs(channel_name, brand_position)

    vid_w, vid_h = (1080, 1920) if is_short else (1920, 1080)
    img_w, img_h = (768, 1344) if is_short else (1344, 768)

    run_id = f"project_{int(time.time())}_{secrets.token_hex(3)}"
    work_dir = OUTPUT_ROOT / run_id
    work_dir.mkdir(parents=True, exist_ok=True)
    st.session_state.setdefault("my_runs", []).append(run_id)
    base_seed = random.randint(10000, 999999)

    saved_logo_path: Optional[Path] = None
    if logo_upload is not None:
        saved_logo_path = work_dir / "channel_logo.png"
        saved_logo_path.write_bytes(logo_upload.getvalue())

    bgm_file_path: Optional[Path] = None
    if bgm_upload is not None:
        bgm_suffix = Path(bgm_upload.name).suffix.lower()
        bgm_file_path = work_dir / f"bgm{bgm_suffix if bgm_suffix in ('.mp3', '.wav') else '.mp3'}"
        bgm_file_path.write_bytes(bgm_upload.getvalue())

    status = st.status(
        "🎬 Initializing TubeForge Scene-Locked Render Engine...", expanded=True
    )

    try:
        status.write(
            f"🧠 **Stage 1/4:** Using script `{plan.primary_title}` ({len(plan.scenes)} scenes, {narration_language})."
        )

        active_header_title = custom_video_title.strip() or plan.primary_title
        render_style = (
            plan.auto_visual_style.strip() or "Cinematic, ultra-detailed, dramatic lighting"
            if visual_style == AI_AUTO_STYLE
            else visual_style
        )
        # If the AI flagged no scenes for stock footage, Hybrid falls back to alternating.
        ai_picked_stock = any(s.preferred_media == "stock_video" for s in plan.scenes)

        def wants_stock(idx: int, scene: SceneItem) -> bool:
            if not pexels_key:
                return False
            if "Stock Videos Only" in visual_mode:
                return True
            if "Hybrid Mix" in visual_mode:
                return scene.preferred_media == "stock_video" if ai_picked_stock else idx % 2 == 0
            return False

        _disabled_engines.clear()
        engines_used: dict = {}
        image_jobs = {
            i: sc for i, sc in enumerate(plan.scenes, start=1) if not wants_stock(i, sc)
        }
        hd_ready = image_engine != "pollinations_free" and len(available_image_engines(image_keys)) > 1
        if hd_ready and image_jobs:
            status.write(f"🖼️ Generating {len(image_jobs)} HD scene images in parallel...")
            engine_msgs: List[str] = []
            with ThreadPoolExecutor(max_workers=3) as pool:
                futs = {
                    pool.submit(
                        generate_scene_image,
                        visual_prompt=sc.visual_prompt,
                        narration_context=sc.narration,
                        topic_context=active_header_title,
                        visual_style=render_style,
                        width=img_w,
                        height=img_h,
                        is_short=is_short,
                        seed=base_seed + i,
                        output_path=work_dir / f"scene_{i:02d}.jpg",
                        engine=image_engine,
                        keys=image_keys,
                        log=engine_msgs.append,
                    ): i
                    for i, sc in image_jobs.items()
                }
                for fut in as_completed(futs):
                    try:
                        engines_used[futs[fut]] = fut.result()
                    except Exception as e:
                        engine_msgs.append(f"⚠️ Scene {futs[fut]} image failed: {str(e)[:120]}")
            for msg in dict.fromkeys(engine_msgs):
                status.write(msg)

        clip_files: List[Path] = []
        scene_preview_cards = []
        srt_blocks: List[str] = []
        chapters: List[str] = []
        elapsed = 0.0
        progress = st.progress(0)

        for idx, scene in enumerate(plan.scenes, start=1):
            status.write(
                f"🎨 **Stage 2/4 (Scene {idx}/{len(plan.scenes)}):** `{scene.chapter_title}` — generating voiceover, visual & motion clip..."
            )
            aud_path = work_dir / f"scene_{idx:02d}.mp3"
            img_path = work_dir / f"scene_{idx:02d}.jpg"
            overlay_path = work_dir / f"overlay_{idx:02d}.png"
            stock_path = work_dir / f"stock_{idx:02d}.mp4"
            clip_path = work_dir / f"clip_{idx:02d}.mp4"

            ts_label = format_yt_timestamp(elapsed)
            chapters.append(f"{ts_label} - {scene.chapter_title}")

            if enable_voice:
                asyncio.run(
                    synthesize_voiceover(
                        scene.narration, selected_voice, voice_rate, aud_path
                    )
                )
                dur = get_media_duration(aud_path) + 0.30
            else:
                aud_path = None
                dur = (target_minutes * 60.0) / len(plan.scenes)

            for s, e, line in split_subtitle_chunks(scene.narration, elapsed, dur):
                srt_blocks.append(
                    f"{len(srt_blocks) + 1}\n{format_srt_time(s)} --> {format_srt_time(e)}\n{line}\n"
                )
            elapsed += dur

            create_scene_broadcast_overlay(
                width=vid_w,
                height=vid_h,
                is_short=is_short,
                logo_path=saved_logo_path,
                channel_name=channel_name,
                video_title_banner=custom_video_title.strip(),
                brand_position=brand_position,
                caption_text=scene.subtitle_summary,
                enable_captions=burn_captions,
                output_png=overlay_path,
            )

            use_stock = wants_stock(idx, scene)

            rendered = False
            if use_stock:
                if download_pexels_stock_video(
                    pexels_key, scene.stock_search_keyword, is_short, stock_path
                ):
                    render_scene_from_stock_video(
                        stock_path,
                        overlay_path,
                        aud_path,
                        dur,
                        vid_w,
                        vid_h,
                        clip_path,
                        fade=fade_transitions,
                    )
                    rendered = True

            if not rendered:
                if not img_path.exists():
                    engines_used[idx] = generate_scene_image(
                        visual_prompt=scene.visual_prompt,
                        narration_context=scene.narration,
                        topic_context=active_header_title,
                        visual_style=render_style,
                        width=img_w,
                        height=img_h,
                        is_short=is_short,
                        seed=base_seed + idx,
                        output_path=img_path,
                        engine=image_engine,
                        keys=image_keys,
                        log=status.write,
                    )
                render_scene_from_image(
                    img_path,
                    overlay_path,
                    aud_path,
                    dur,
                    vid_w,
                    vid_h,
                    scene.camera_motion,
                    clip_path,
                    fade=fade_transitions,
                )
                stamp_brand_on_image(img_path, overlay_path)

            clip_files.append(clip_path)
            scene_preview_cards.append(
                {
                    "num": idx,
                    "title": scene.chapter_title,
                    "timestamp": ts_label,
                    "duration": f"{dur:.1f}s",
                    "narration": scene.narration,
                    "visual_prompt": scene.visual_prompt,
                    "image": str(img_path) if img_path.exists() else None,
                    "clip": str(clip_path),
                }
            )
            progress.progress(idx / len(plan.scenes))

        srt_path = work_dir / "subtitles.srt"
        srt_path.write_text("\n".join(srt_blocks), encoding="utf-8")

        engine_counts: dict = {}
        for used in engines_used.values():
            engine_counts[used] = engine_counts.get(used, 0) + 1
        if engine_counts:
            status.write(
                "🖼️ Image engines used: "
                + ", ".join(
                    f"{IMAGE_ENGINE_LABELS.get(e, '🪧 Fallback text card')} ×{n}"
                    for e, n in engine_counts.items()
                )
            )
        if engine_counts.get("placeholder"):
            status.write(
                f"⚠️ {engine_counts['placeholder']} scene(s) used a fallback text card because no image "
                "engine answered. Add Cloudflare keys to `.env` for reliable HD images."
            )

        status.write("🖼️ **Stage 3/4:** Generating branded YouTube thumbnail...")
        thumb_path = work_dir / "thumbnail.jpg"
        thumb_overlay = work_dir / "thumb_overlay.png"
        t_w, t_h = (1080, 1920) if is_short else (1280, 720)
        generate_scene_image(
            visual_prompt=plan.thumbnail_prompt or active_header_title,
            narration_context=active_header_title,
            topic_context=topic_text,
            visual_style=render_style,
            width=t_w,
            height=t_h,
            is_short=is_short,
            seed=base_seed,
            output_path=thumb_path,
            engine=image_engine,
            keys=image_keys,
            log=status.write,
        )
        fit_image_to_frame(thumb_path, t_w, t_h)
        add_thumbnail_headline(thumb_path, active_header_title)
        create_scene_broadcast_overlay(
            width=t_w,
            height=t_h,
            is_short=is_short,
            logo_path=saved_logo_path,
            channel_name=channel_name,
            video_title_banner="",
            brand_position=brand_position,
            caption_text="",
            enable_captions=False,
            output_png=thumb_overlay,
        )
        stamp_brand_on_image(thumb_path, thumb_overlay)

        status.write("🎞️ **Stage 4/4:** Combining all branded clips & mastering audio...")
        final_mp4 = work_dir / "final_youtube_video.mp4"
        combine_clips_and_mix_bgm(
            clip_files,
            bgm_file_path,
            final_mp4,
            has_voice=enable_voice,
            bgm_volume=bgm_volume,
            auto_duck=auto_duck,
            total_duration=elapsed,
        )

        clean_hashtags = [
            h if h.startswith("#") else f"#{h.replace(' ', '')}"
            for h in plan.hashtags
        ]

        render_record = {
            "run_id": run_id,
            "work_dir": str(work_dir),
            "final_mp4": str(final_mp4),
            "srt_path": str(srt_path),
            "thumb_path": str(thumb_path),
            "zip_path": str(work_dir / f"tubeforge_{run_id}_assets.zip"),
            "elapsed_str": format_yt_timestamp(elapsed),
            "language": narration_language,
            "image_engines": engine_counts,
            "plan": plan.model_dump(),
            "hashtags_str": " ".join(clean_hashtags),
            "chapters": chapters,
            "scenes": scene_preview_cards,
        }
        (work_dir / "project.json").write_text(
            json.dumps(render_record, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (work_dir / "youtube_metadata.txt").write_text(
            f"TITLE:\n{plan.primary_title}\n\n"
            f"DESCRIPTION:\n{plan.youtube_description}\n\n"
            "CHAPTERS:\n" + "\n".join(chapters) + "\n\n"
            f"HASHTAGS:\n{' '.join(clean_hashtags)}\n\n"
            f"TAGS:\n{', '.join(plan.tags)}\n",
            encoding="utf-8",
        )
        build_project_zip(work_dir, Path(render_record["zip_path"]))
        st.session_state["last_render"] = render_record

        status.update(
            label=f"✅ Branded Video Ready! Duration: {format_yt_timestamp(elapsed)}",
            state="complete",
            expanded=False,
        )

    except Exception as err:
        status.update(label="❌ Pipeline Error", state="error")
        st.exception(err)


# =====================================================================
# 9. PROMPT INPUT, SCRIPT REVIEW & EDIT, RENDER CONTROLS
# =====================================================================
ffmpeg_ready = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
FFMPEG_MISSING_MSG = "⚠️ FFmpeg is missing! Run `brew install ffmpeg` in your Mac Terminal."

topic_input = st.text_area(
    "📝 Enter Your Video Topic, Storyline, or Detailed Prompt:",
    height=120,
    placeholder="Describe the exact video topic you want to generate...",
)

btn_col, opt_col = st.columns([3, 2])
with opt_col:
    auto_render = st.toggle(
        "⚡ Skip script review (one-click render)",
        value=True,
        help="On: script + video in one click. Off: review and edit scenes before rendering.",
    )
with btn_col:
    launch_btn = st.button(
        "🚀 Generate & Render Full Video"
        if auto_render
        else "🧠 Step 1: Generate Script, Title & #Hashtags",
        type="primary",
    )

if launch_btn:
    if not topic_input.strip():
        st.error("📝 Please enter a video topic or prompt above.")
    elif auto_render and not ffmpeg_ready:
        st.error(FFMPEG_MISSING_MSG)
    else:
        new_plan: Optional[VideoProductionPlan] = None
        with st.status(
            f"🧠 Writing {num_scenes} scene-locked scenes in {narration_language}...",
            expanded=True,
        ) as script_status:
            try:
                new_plan = generate_production_plan(
                    api_key=gemini_key,
                    topic=topic_input,
                    channel_name=channel_name,
                    custom_video_title=custom_video_title,
                    include_voice_branding=include_voice_branding,
                    target_minutes=target_minutes,
                    num_scenes=num_scenes,
                    visual_style=visual_style,
                    is_short=is_short,
                    language=narration_language,
                    status_box=script_status,
                    ai_auto_visuals=visual_style == AI_AUTO_STYLE,
                )
                script_status.update(
                    label=f"✅ Script ready: {new_plan.primary_title} ({len(new_plan.scenes)} scenes)",
                    state="complete",
                    expanded=False,
                )
            except Exception as err:
                script_status.update(label="❌ Script generation failed", state="error")
                st.error(str(err))

            if new_plan and auto_proofread and gemini_key:
                script_status.update(label="✨ Proofreading spelling & grammar...", state="running")
                try:
                    new_plan = proofread_plan(
                        gemini_key, new_plan, narration_language, status_box=script_status
                    )
                    script_status.write("✨ Spelling, grammar and terminology proofread.")
                except Exception as err:
                    script_status.write(f"⚠️ Proofreading skipped: {str(err)[:140]}")
                script_status.update(
                    label=f"✅ Script ready: {new_plan.primary_title} ({len(new_plan.scenes)} scenes)",
                    state="complete",
                    expanded=False,
                )

        if new_plan:
            st.session_state["draft_plan"] = new_plan.model_dump()
            st.session_state["draft_topic"] = topic_input
            st.session_state["draft_version"] = st.session_state.get("draft_version", 0) + 1
            if auto_render:
                run_render_pipeline(new_plan, topic_input)


def _cell(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def build_plan_from_editor(
    draft: dict, rows: List[dict], title: str, thumb_prompt: str
) -> VideoProductionPlan:
    edited_scenes = []
    for i, r in enumerate(rows, start=1):
        narration = _cell(r.get("narration"))
        chapter = _cell(r.get("chapter_title")) or f"Part {i}"
        motion = _cell(r.get("camera_motion"))
        media = _cell(r.get("preferred_media"))
        edited_scenes.append(
            SceneItem(
                scene_number=i,
                narration=narration,
                visual_prompt=_cell(r.get("visual_prompt")) or narration,
                stock_search_keyword=_cell(r.get("stock_search_keyword")) or chapter,
                subtitle_summary=_cell(r.get("subtitle_summary"))
                or textwrap.shorten(narration, 60, placeholder="..."),
                chapter_title=chapter,
                camera_motion=motion
                if motion in CAMERA_MOTIONS
                else CAMERA_MOTIONS[(i - 1) % len(CAMERA_MOTIONS)],
                preferred_media=media if media in MEDIA_TYPES else "ai_image",
            )
        )
    final_title = title.strip() or draft["primary_title"]
    return VideoProductionPlan(
        **{
            **draft,
            "primary_title": final_title,
            "thumbnail_prompt": thumb_prompt.strip() or final_title,
            "scenes": edited_scenes,
        }
    )


if "draft_plan" in st.session_state:
    draft = st.session_state["draft_plan"]
    ver = st.session_state.get("draft_version", 0)
    scene_cols = [
        "chapter_title",
        "narration",
        "subtitle_summary",
        "visual_prompt",
        "stock_search_keyword",
        "camera_motion",
        "preferred_media",
    ]

    st.markdown("---")
    # Only the editing widgets are collapsible; render progress below always stays visible.
    with st.expander("✍️ Step 2: Review & Edit Your Script", expanded=not auto_render):
        st.caption(
            "Edit any cell, click ➕ to add a scene, or select rows and delete them. "
            "Visual prompts control exactly what each scene image shows."
        )
        ed_title = st.text_input(
            "🎯 Video Title", value=draft["primary_title"], key=f"ed_title_{ver}"
        )
        ed_thumb = st.text_area(
            "🖼️ Thumbnail Visual Prompt",
            value=draft.get("thumbnail_prompt", ""),
            key=f"ed_thumb_{ver}",
            height=80,
        )
        edited_df = st.data_editor(
            pd.DataFrame(draft["scenes"], columns=scene_cols),
            key=f"ed_scenes_{ver}",
            num_rows="dynamic",
            width="stretch",
            hide_index=True,
            column_config={
                "chapter_title": st.column_config.TextColumn("📑 Chapter", width="small"),
                "narration": st.column_config.TextColumn(
                    "🎙️ Narration", width="large", required=True
                ),
                "subtitle_summary": st.column_config.TextColumn(
                    "💬 On-Screen Caption", width="medium"
                ),
                "visual_prompt": st.column_config.TextColumn("🎨 Visual Prompt", width="large"),
                "stock_search_keyword": st.column_config.TextColumn(
                    "🔎 Stock Keyword", width="small"
                ),
                "camera_motion": st.column_config.SelectboxColumn(
                    "🎥 Motion", options=CAMERA_MOTIONS, width="small"
                ),
                "preferred_media": st.column_config.SelectboxColumn(
                    "🖼️ Media", options=MEDIA_TYPES, width="small"
                ),
            },
        )

    draft_rows = [r for r in edited_df.to_dict("records") if _cell(r.get("narration"))]
    word_count = sum(len(_cell(r["narration"]).split()) for r in draft_rows)
    pace_factor = 1 + int(voice_rate.rstrip("%")) / 100
    est_seconds = word_count / (150 * pace_factor) * 60

    k1, k2, k3 = st.columns(3)
    k1.metric("Scenes", len(draft_rows))
    k2.metric("Spoken Words", word_count)
    k3.metric("Est. Duration", format_yt_timestamp(est_seconds))

    r1, r_fix, r2 = st.columns([3, 2, 1])
    render_btn = r1.button(
        "🎬 Step 3: Render Video From This Script",
        type="primary",
        width="stretch",
    )
    proofread_btn = r_fix.button(
        "✨ AI Proofread Spelling & Grammar",
        width="stretch",
        disabled=not gemini_key,
        help="Needs a Gemini key. Fixes spelling, grammar and terminology in every scene.",
    )
    if r2.button("🗑️ Discard Draft", width="stretch"):
        del st.session_state["draft_plan"]
        st.rerun()

    if proofread_btn and draft_rows:
        with st.status("✨ Proofreading your script...", expanded=True) as fix_status:
            try:
                fixed = proofread_plan(
                    gemini_key,
                    build_plan_from_editor(draft, draft_rows, ed_title, ed_thumb),
                    narration_language,
                    status_box=fix_status,
                )
                st.session_state["draft_plan"] = fixed.model_dump()
                st.session_state["draft_version"] = ver + 1
                st.rerun()
            except Exception as err:
                fix_status.update(label="❌ Proofreading failed", state="error")
                st.error(str(err)[:300])

    if render_btn:
        if not draft_rows:
            st.error("Add at least one scene with narration before rendering.")
        elif not ffmpeg_ready:
            st.error(FFMPEG_MISSING_MSG)
        else:
            run_render_pipeline(
                build_plan_from_editor(draft, draft_rows, ed_title, ed_thumb),
                st.session_state.get("draft_topic", topic_input),
            )


# =====================================================================
# 10. MULTI-TAB STUDIO RESULTS WORKSPACE
# =====================================================================
if "last_render" in st.session_state:
    res = st.session_state["last_render"]
    st.markdown("---")

    tab1, tab2, tab3, tab4 = st.tabs(
        [
            "🎥 Master Video, Title, Description & #Hashtags",
            "🖼️ Scene-by-Scene Visual Storyboard",
            "📈 Extra SEO & Search Tags",
            "🛡️ Security & Workspace Cleaner",
        ]
    )

    with tab1:
        v_col, seo_col = st.columns([3, 3])
        with v_col:
            st.subheader(f"🎬 Master Branded Video ({res['elapsed_str']})")
            if Path(res["final_mp4"]).exists():
                st.video(res["final_mp4"])

            d1, d2 = st.columns(2)
            with d1:
                if Path(res["final_mp4"]).exists():
                    with open(res["final_mp4"], "rb") as f:
                        st.download_button(
                            "⬇️ Download MP4 Video",
                            data=f,
                            file_name=f"tubeforge_{res['run_id']}.mp4",
                            mime="video/mp4",
                            key=f"dl_mp4_{res['run_id']}",
                            width="stretch",
                        )
            with d2:
                if Path(res["srt_path"]).exists():
                    with open(res["srt_path"], "rb") as sf:
                        st.download_button(
                            "⬇️ Download Subtitles (.SRT)",
                            data=sf,
                            file_name=f"subtitles_{res['run_id']}.srt",
                            mime="text/plain",
                            key=f"dl_srt_{res['run_id']}",
                            width="stretch",
                        )

            zip_file = Path(res.get("zip_path", ""))
            if res.get("zip_path") and zip_file.exists():
                with open(zip_file, "rb") as zf:
                    st.download_button(
                        "📦 Download All Assets (.ZIP: video, SRT, thumbnail, scene images, metadata)",
                        data=zf,
                        file_name=zip_file.name,
                        mime="application/zip",
                        key=f"dl_zip_{res['run_id']}",
                        width="stretch",
                    )

            if Path(res["thumb_path"]).exists():
                st.markdown("#### 🖼️ Branded Thumbnail")
                st.image(res["thumb_path"], width="stretch")
                with open(res["thumb_path"], "rb") as tf:
                    st.download_button(
                        "⬇️ Download Thumbnail (.JPG)",
                        data=tf,
                        file_name=f"thumbnail_{res['run_id']}.jpg",
                        mime="image/jpeg",
                        key=f"dl_thumb_{res['run_id']}",
                        width="stretch",
                    )

        with seo_col:
            st.subheader("📌 1. Video Title (Copy-Paste)")
            st.code(res["plan"]["primary_title"], language=None)

            st.subheader("#️⃣ 2. Viral Hashtags")
            st.code(res["hashtags_str"], language=None)

            st.subheader("📝 3. Full YouTube Description + Chapters + Hashtags")
            full_description_block = (
                f"{res['plan']['primary_title']}\n\n"
                f"{res['plan']['youtube_description']}\n\n"
                f"⏱️ TIMESTAMPS / CHAPTERS:\n"
                + "\n".join(res["chapters"])
                + f"\n\n{res['hashtags_str']}"
            )
            st.text_area(
                "Copy and paste directly into YouTube Studio Description box:",
                value=full_description_block,
                height=340,
                key=f"desc_{res['run_id']}",
            )

    with tab2:
        st.subheader("🖼️ Scene-Locked Storyboard (Every Image Matches Its Scene Narration)")
        grid_cols = st.columns(2)
        for i, sc in enumerate(res["scenes"]):
            with grid_cols[i % 2]:
                st.markdown(
                    f"""
                    <div class="scene-card">
                        <b>Scene {sc['num']} • {html.escape(sc['title'])}</b>
                        <span style="float:right; color:#38BDF8;">⏱️ {sc['timestamp']} ({sc['duration']})</span>
                        <p style="color:#F3F4F6; font-size:0.92rem; margin-top:8px;"><b>🎙️ Voiceover:</b> "{html.escape(sc['narration'])}"</p>
                        <p style="color:#9CA3AF; font-size:0.8rem; margin-bottom:4px;"><b>🎨 Scene Visual Prompt:</b> {html.escape(sc['visual_prompt'])}</p>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
                if sc["image"] and Path(sc["image"]).exists():
                    st.image(sc["image"], width="stretch")

    with tab3:
        st.subheader("🔥 Alternative Viral Title Ideas")
        for title_opt in res["plan"]["viral_titles"]:
            st.code(title_opt, language=None)

        st.subheader("🏷️ YouTube Studio Comma-Separated Search Tags")
        # YouTube caps the combined tags field at 500 characters.
        fitted_tags: List[str] = []
        for tag in res["plan"]["tags"]:
            if len(", ".join(fitted_tags + [tag.strip()])) <= 500:
                fitted_tags.append(tag.strip())
        st.code(", ".join(fitted_tags), language=None)

        st.subheader("✅ YouTube Upload Compliance Check")
        title_len = len(res["plan"]["primary_title"])
        desc_len = len(full_description_block)
        hashtag_count = len(res["hashtags_str"].split())
        short_chapters = sum(
            1 for sc in res["scenes"] if float(sc["duration"].rstrip("s")) < 10
        )
        checks = [
            (title_len <= 100, f"Title length {title_len}/100 characters"),
            (desc_len <= 5000, f"Description length {desc_len}/5000 characters"),
            (hashtag_count <= 15, f"{hashtag_count} hashtags (YouTube ignores all if more than 60; 15 or fewer recommended)"),
            (len(res["chapters"]) >= 3, f"{len(res['chapters'])} chapters (YouTube needs at least 3)"),
            (short_chapters == 0, f"{short_chapters} chapter(s) shorter than 10s (YouTube requires 10s+ per chapter)"),
        ]
        for ok, msg in checks:
            (st.success if ok else st.warning)(("✔️ " if ok else "⚠️ ") + msg)

    with tab4:
        st.subheader("🛡️ Security & Storage Controls")
        bind_address = st.get_option("server.address")
        if bind_address in ("localhost", "127.0.0.1"):
            st.success("Loopback binding active: the studio is only reachable from this computer.")
        elif APP_PASSWORD:
            st.info("Network-accessible deployment protected by `APP_PASSWORD`.")
        else:
            st.warning("Reachable from the network without a password. Set `APP_PASSWORD`.")
        st.write(f"**Current Render Folder:** `{res['work_dir']}`")
        wipe_label = (
            "🗑️ Delete My Renders From This Server"
            if PUBLIC_MODE
            else "🗑️ Wipe Local Output Cache Folder Now"
        )
        if st.button(wipe_label):
            if PUBLIC_MODE:
                for run in st.session_state.get("my_runs", []):
                    shutil.rmtree(OUTPUT_ROOT / run, ignore_errors=True)
                st.session_state["my_runs"] = []
            elif OUTPUT_ROOT.exists():
                shutil.rmtree(OUTPUT_ROOT)
            del st.session_state["last_render"]
            st.toast("All generated video & audio cache files deleted from disk!")
            st.rerun()