"""TubeForge Cinematic AI Studio - Streamlit UI.
Pages (only the active one runs, so 50-scene projects stay responsive): Dashboard, Create, Story, Storyboard,
Timeline & Look, Audio, Render & Export, Providers. Long work runs as background jobs (jobs.MANAGER) and the live
progress panel polls them with an auto-refreshing fragment."""
import random
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, List, Optional

import streamlit as st

import base64
import io
import re
from . import alignment, audio, cache, cinema, config, http, images, jobs, llm, planner, procedural, render, tts, video
from . import youtube_upload

CSS = """
<style>
:root { --tf-violet:#8B5CF6; --tf-cyan:#22D3EE; --tf-pink:#F472B6; --tf-card:rgba(15,20,35,.82); }
.stApp { background: radial-gradient(1200px 600px at 85% -10%, rgba(139,92,246,.25), transparent 60%),
         radial-gradient(900px 500px at -10% 110%, rgba(34,211,238,.12), transparent 60%), #05070f; }
.tf-hero { padding: 26px 30px; border-radius: 22px; margin-bottom: 14px;
  background: linear-gradient(135deg, rgba(139,92,246,.20), rgba(34,211,238,.08) 60%, rgba(244,114,182,.10));
  border: 1px solid rgba(139,92,246,.35); box-shadow: 0 20px 60px rgba(0,0,0,.55), inset 0 1px 0 rgba(255,255,255,.05); }
.tf-hero h1 { font-size: 2.15rem; font-weight: 850; margin: 0; letter-spacing: -.02em;
  background: linear-gradient(90deg,#C4B5FD,#67E8F9 45%,#F9A8D4); -webkit-background-clip:text; -webkit-text-fill-color:transparent; }
.tf-hero p { color:#A1A1AA; margin:.35rem 0 0 0; }
.tf-steps { display:flex; gap:6px; flex-wrap:wrap; margin: 4px 0 14px 0; }
.tf-step { flex:1 1 0; min-width:92px; padding:8px 10px; border-radius:12px; font-size:.74rem; text-align:center;
  background:rgba(255,255,255,.03); border:1px solid rgba(255,255,255,.07); color:#71717A; }
.tf-step.done { color:#A7F3D0; border-color:rgba(16,185,129,.45); background:rgba(16,185,129,.10); }
.tf-step.now { color:#E9D5FF; border-color:rgba(139,92,246,.7); background:rgba(139,92,246,.18); }
.tf-card { background:var(--tf-card); border:1px solid rgba(255,255,255,.07); border-radius:16px; padding:14px 16px;
  box-shadow:0 8px 30px rgba(0,0,0,.35); margin-bottom:10px; }
.tf-kpi { font-size:1.35rem; font-weight:800; color:#F4F4F5; } .tf-kpi-l { font-size:.7rem; color:#A1A1AA;
  text-transform:uppercase; letter-spacing:.09em; }
.tf-pill { display:inline-block; padding:2px 10px; border-radius:999px; font-size:.72rem; margin:0 4px 4px 0;
  background:rgba(139,92,246,.16); color:#DDD6FE; border:1px solid rgba(139,92,246,.35); }
.tf-pill.ok { background:rgba(16,185,129,.14); color:#6EE7B7; border-color:rgba(16,185,129,.4); }
.tf-pill.warn { background:rgba(245,158,11,.14); color:#FCD34D; border-color:rgba(245,158,11,.4); }
.tf-pill.err { background:rgba(239,68,68,.14); color:#FCA5A5; border-color:rgba(239,68,68,.4); }
.tf-log { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size:.74rem; color:#C4C4CC;
  background:#0A0D18; border:1px solid rgba(255,255,255,.06); border-radius:12px; padding:10px 12px;
  max-height:260px; overflow-y:auto; white-space:pre-wrap; }
div.stButton > button[kind="primary"], div.stFormSubmitButton > button[kind="primary"] {
  background: linear-gradient(90deg,#7C3AED,#2563EB 55%,#0891B2); border:0; border-radius:12px; font-weight:700; }
div[data-testid="stExpander"] { border-radius:14px; border:1px solid rgba(255,255,255,.08); background:rgba(12,16,30,.6); }
.tf-timeline-bar { display:flex; width:100%; height:46px; border-radius:12px; overflow:hidden; border:1px solid rgba(255,255,255,0.12); margin:12px 0; background:#070a14; box-shadow: inset 0 2px 8px rgba(0,0,0,0.5); }
.tf-timeline-seg { display:flex; flex-direction:column; align-items:center; justify-content:center; font-size:0.72rem; font-weight:700; color:#fff; overflow:hidden; white-space:nowrap; text-overflow:ellipsis; padding:2px 4px; border-right:1px solid rgba(0,0,0,0.35); transition:all 0.15s ease-in-out; cursor:default; }
.tf-timeline-seg:hover { filter:brightness(1.25); transform:scaleY(1.05); }
.tf-badge-preview { display:inline-flex; align-items:center; gap:12px; background:rgba(15,20,35,0.85); border:1px solid rgba(139,92,246,0.35); border-radius:999px; padding:6px 18px 6px 6px; margin:6px 0; }
.tf-badge-preview img { width:38px; height:38px; border-radius:50%; object-fit:cover; border:1px solid #38bdf8; }
.tf-badge-title { font-size:0.92rem; font-weight:700; color:#f3f4f6; letter-spacing:0.02em; }
</style>
"""

PAGES = ["🏠 Dashboard", "✨ Create", "📖 Story", "🎞️ Storyboard", "🎬 Timeline & Look", "🔊 Audio",
         "🚀 Render & Export", "🔌 Providers"]
STEPS = ["Idea", "Story", "Characters", "Scenes", "Visuals", "Voice", "Music", "Edit", "Render", "Export"]
STATUS_ICON = {"pending": "⏳", "visual": "🖼️", "voice": "🎙️", "rendering": "🎬", "done": "✅", "error": "❌",
               "cancelled": "⏹️", "assets": "📦"}
SOURCES = {"ai_image": "AI image + camera move", "stock_video": "Stock footage (Pixabay + Pexels)",
           "original_motion": "Original AI scene + free local motion",
           "ai_video": "Hosted AI video (explicit opt-in)",
           "comfyui_local": "ComfyUI local GPU (imported workflow)",
           "comfyui_remote": "ComfyUI remote tunnel (explicit opt-in)",
           "flow_manual": "Google Flow pack + manual MP4 import",
           "upload": "My upload"}
FREE_SOURCES = ["original_motion", "ai_image", "stock_video", "ai_video",
                "comfyui_local", "comfyui_remote", "flow_manual", "upload"]
VIDEO_SOURCES = {
    "original_motion": "Built-in FFmpeg parallax",
    "depthflow_3d": "DepthFlow 3D depth parallax",
    "cutout_2d": "2D layered cutout",
    "procedural_3d_2d": "Remotion procedural 3D/2D graphics",
}
DURATION_PRESETS = [
    ("⚡ 30s Short (Fast Hook)", 30.0, 4),
    ("⏱️ 1 Minute (60s Quick Story)", 60.0, 8),
    ("🎬 2 Minutes (120s YouTube Standard)", 120.0, 12),
    ("🎥 3 Minutes (180s Cinematic Deep-Dive)", 180.0, 18),
    ("📽️ 5 Minutes (300s Mini-Doc)", 300.0, 26),
    ("⚙️ Custom Duration", -1.0, 12),
]
BRAND_POSITIONS = ["Top-Right", "Top-Left", "Bottom-Right", "Bottom-Left", "None (Off)"]


def _brand_pos_index(val: str) -> int:
    if not val:
        return 0
    val_clean = str(val).strip().lower().replace("_", "-")
    for idx, bp in enumerate(BRAND_POSITIONS):
        if bp.lower().replace("_", "-") == val_clean or val_clean in bp.lower():
            return idx
    return 0
QUICK_IDEAS = [
    "🤖 The AI Revolution of 2050: How Sentient Machines Rebuilt Earth",
    "🌌 Mysteries of the Deep Cosmos: Beyond the Observable Universe",
    "⏳ Lost Technology of Ancient Rome: Inventions Forgotten for Centuries",
    "🩺 Anatomy of the Human Heart: A Cinematic Journey Inside Blood Flow",
    "⚔️ The Rise and Fall of the Samurai: Honor, Loyalty, and Betrayal",
]
ALLOWED_UPLOAD_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".webm", ".m4v", ".mkv", ".mp3", ".wav", ".ogg", ".m4a"}


def _display_brand_badge(channel_name: str, logo_source: Optional[Any] = None) -> None:
    logo_input = logo_source
    if hasattr(logo_source, "getvalue"):
        try:
            from PIL import Image as PILImage
            logo_input = PILImage.open(io.BytesIO(logo_source.getvalue())).convert("RGBA")
        except Exception:
            logo_input = None
    badge = render.build_circular_logo_badge(logo_input, channel_name, 56)
    buf = io.BytesIO()
    badge.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    name_display = channel_name.strip() or "TubeForge Channel"
    st.markdown(
        f"<div class='tf-badge-preview'>"
        f"<img src='data:image/png;base64,{b64}'>"
        f"<span class='tf-badge-title'>{_esc(name_display)}</span>"
        f"</div>",
        unsafe_allow_html=True
    )
    if s.get("youtube_playlist_title", "").strip():
        playlist_id = youtube_upload.insert_playlist(
            [video_id], client, token,
            s["youtube_playlist_title"].strip(),
            description=(plan.youtube_description if plan else "")[:5000],
        )
        st.success(f"Added private draft to playlist: {playlist_id}")


# ---------------------------------------------------------------------------------------------- helpers
def _pid() -> Optional[str]:
    return st.session_state.get("tf_pid")


def _project() -> Optional[jobs.Project]:
    pid = _pid()
    return jobs.load_project(pid) if pid else None


def _reset_widgets(pid: str) -> None:
    for k in list(st.session_state.keys()):
        if isinstance(k, str) and k.startswith(f"tf_{pid}_"):
            del st.session_state[k]


def _flash(msg: str, kind: str = "success") -> None:
    """Queue a message that survives the next st.rerun()."""
    st.session_state.setdefault("tf_flash", []).append((kind, msg))


def _show_flash() -> None:
    for kind, msg in st.session_state.pop("tf_flash", []):
        if kind == "error":
            st.error(msg)
        else:
            st.toast(msg)


def _scene_thumb(p: jobs.Project, sc) -> Optional[str]:
    """Best still for a scene: locked/generated image, else a cached poster frame of its video."""
    st_ = p.state(sc.id)
    img = st_.get("locked_image") or st_.get("image")
    if img and Path(img).exists():
        return img
    vid = st_.get("upload") or st_.get("ai_video") or st_.get("stock") or st_.get("clip")
    if not vid or not Path(vid).exists() or Path(vid).suffix.lower() not in (".mp4", ".mov", ".webm", ".m4v", ".mkv"):
        return img if img and Path(img).exists() else (vid if vid and Path(vid).exists() else None)
    poster = Path(vid).with_suffix(".poster.jpg")
    if not poster.exists():
        import subprocess

        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "0.5", "-i", str(vid), "-frames:v", "1",
                        "-vf", "scale=480:-2", str(poster)], check=False, timeout=30)
    return str(poster) if poster.exists() else None


def _running(p: Optional[jobs.Project]) -> bool:
    return bool(p and jobs.MANAGER.is_running(p.id))


def setting(p: jobs.Project, key: str, kind: str, label: str, **kw) -> Any:
    """A widget bound to a project setting (persisted immediately on change)."""
    wk = f"tf_{p.id}_{key}"
    cur = p.settings.get(key)
    disabled = kw.pop("disabled", False) or _running(p)
    if kind == "select":
        opts = list(kw.pop("options"))
        if cur not in opts:
            match = next((o for o in opts if str(o).lower() == str(cur).lower()), None)
            cur = match if match else opts[0]
        val = st.selectbox(label, opts, index=opts.index(cur), key=wk, disabled=disabled, **kw)
    elif kind == "slider":
        val = st.slider(label, value=type(kw.get("min_value", 0.0))(cur), key=wk, disabled=disabled, **kw)
    elif kind == "toggle":
        val = st.toggle(label, value=bool(cur), key=wk, disabled=disabled, **kw)
    elif kind == "number":
        val = st.number_input(label, value=type(kw.get("min_value", 0.0))(cur), key=wk, disabled=disabled, **kw)
    elif kind == "text":
        val = st.text_input(label, value=str(cur or ""), key=wk, disabled=disabled, **kw)
    else:
        raise ValueError(kind)
    if val != p.settings.get(key):
        with p.lock:
            p.settings[key] = val
        p.save()
    return val


def _save_upload(p: jobs.Project, up, sub: str, name: str) -> str:
    ext = Path(up.name).suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXTS:
        raise ValueError(f"File extension '{ext}' is not permitted.")
    if getattr(up, "size", 0) > 150 * 1024 * 1024:
        raise ValueError("Upload exceeds maximum size limit (150MB).")
    clean_sub = re.sub(r"[^A-Za-z0-9_\-/]", "", sub).strip("/")
    clean_name = re.sub(r"[^A-Za-z0-9_\-]", "", name)
    d = (p.dir / clean_sub).resolve()
    if not str(d).startswith(str(p.dir.resolve())):
        raise ValueError("Invalid upload path destination.")
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{clean_name}{ext}"
    out.write_bytes(up.getbuffer())
    return str(out)


def _fmt_time(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def _visual_qa_panel(p: jobs.Project) -> None:
    qa = p.data.get("visual_qa")
    if not qa:
        return
    status = qa.get("status", "review")
    st.markdown(f"**Local visual QA: {'✅ Passed' if status == 'passed' else '⚠️ Review recommended'}** "
                f"({qa.get('checked_scenes', 0)} stills checked)")
    for issue in qa.get("issues", []):
        st.warning(issue)
    if not qa.get("issues"):
        st.caption("No low-detail, near-duplicate, or missing recurring-character reference warnings were found.")


def _alignment_beats_panel(p: jobs.Project) -> None:
    path = p.dir / "alignment.json"
    if not path.is_file():
        return
    try:
        words = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st.warning("The saved word alignment could not be read.")
        return
    beats = p.data.get("visual_beats") or alignment.dynamic_beats(words)
    if not beats:
        return
    hook = [beat for beat in beats if beat["start"] < 30.0]
    body = [beat for beat in beats if beat["start"] >= 30.0]
    max_hook = max((beat["end"] - beat["start"] for beat in hook), default=0.0)
    max_body = max((beat["end"] - beat["start"] for beat in body), default=0.0)
    st.caption(
        f"Word-aligned pacing map: {len(beats)} beats · hook max {max_hook:.1f}s / 4.5s · "
        f"body max {max_body:.1f}s / 7.0s. Transition effects are placed at these word boundaries."
    )
    with st.expander("Preview aligned visual beats"):
        st.dataframe(
            [{"Start": round(beat["start"], 2), "End": round(beat["end"], 2),
              "Duration": round(beat["end"] - beat["start"], 2), "Visual cue": beat["text"]}
             for beat in beats],
            hide_index=True, width="stretch",
        )


def _est_runtime(p: jobs.Project, plan: planner.CinematicPlan) -> float:
    total = 0.0
    for i, sc in enumerate(plan.scenes):
        st_ = p.state(sc.id)
        tr = jobs.scene_transition(p, sc)[1] if i < len(plan.scenes) - 1 else 0.0
        if st_.get("clip_len"):
            total += float(st_["clip_len"]) - tr
        else:
            words = len(" ".join([sc.narration] + [d.line for d in sc.dialogue]).split())
            total += max(sc.duration, words / planner.WORDS_PER_SEC + jobs.LEAD_IN + jobs.TAIL)
    return total


def _workflow_state(p: Optional[jobs.Project]) -> List[str]:
    if not p:
        return ["now"] + [""] * 9
    plan = p.plan
    states = [s for s in (p.data.get("scene_state") or {}).values()]
    has_plan = bool(plan and plan.scenes)
    done = [
        bool(p.data.get("idea")),
        has_plan,
        has_plan and bool(plan.characters),
        has_plan,
        has_plan and states and all(s.get("image") or s.get("stock") or s.get("upload") or s.get("ai_video")
                                    for s in states),
        has_plan and states and all(s.get("voice_parts") is not None for s in states),
        bool((p.data.get("outputs") or {}).get("video")),
        bool((p.data.get("outputs") or {}).get("video")),
        bool((p.data.get("outputs") or {}).get("video")),
        bool((p.data.get("outputs") or {}).get("video")),
    ]
    out, now_set = [], False
    for d in done:
        if d:
            out.append("done")
        elif not now_set:
            out.append("now")
            now_set = True
        else:
            out.append("")
    return out


def _stepper(p: Optional[jobs.Project]) -> None:
    cls = _workflow_state(p)
    st.markdown("<div class='tf-steps'>" + "".join(
        f"<div class='tf-step {c}'>{'✓ ' if c == 'done' else ''}{i + 1}. {n}</div>"
        for i, (n, c) in enumerate(zip(STEPS, cls))) + "</div>", unsafe_allow_html=True)


# --------------------------------------------------------------------------------------- live job panel
def _live_panel(pid: str) -> None:
    p = jobs.load_project(pid)
    if not p:
        return
    running = jobs.MANAGER.is_running(pid)
    flag = f"tf_was_running_{pid}"
    if running:
        st.session_state[flag] = True

    def body():
        p2 = jobs.load_project(pid)
        job = p2.data.get("job") or {}
        is_run = jobs.MANAGER.is_running(pid)
        if not is_run and st.session_state.get(flag):
            st.session_state[flag] = False
            _reset_widgets(pid)
            st.rerun(scope="app")
        status = job.get("status", "idle")
        if status == "idle":
            return
        with st.container(border=True):
            c1, c2 = st.columns([5, 1])
            kind = {"plan": "Story & plan", "render": "Film render", "full": "One-click film", "assets": "Assets",
                    "scene": "Scene render", "image": "Images", "variations": "Variations",
                    "character": "Character reference"}.get(job.get("kind"), "Job")
            icon = {"running": "⚙️", "done": "✅", "error": "❌", "cancelled": "⏹️", "interrupted": "⏸️"}.get(status, "•")
            c1.markdown(f"**{icon} {kind} · {status}** — {job.get('stage', '')}")
            if is_run:
                if c2.button("⏹ Cancel", key=f"tf_cancel_{pid}", width="stretch"):
                    jobs.MANAGER.cancel(pid)
                    st.toast("Cancelling after the current step…")
            elif status in ("interrupted", "cancelled", "error") and p2.plan:
                if c2.button("▶ Resume", key=f"tf_resume_{pid}", width="stretch", type="primary"):
                    jobs.MANAGER.start_render(p2)
                    st.session_state[flag] = True
                    st.rerun(scope="app")
            st.progress(min(1.0, float(job.get("progress") or 0)))
            if job.get("error"):
                st.error(job["error"])
            plan = p2.plan
            if plan and job.get("kind") in ("render", "full", "assets", "scene") and is_run:
                cells = []
                for i, sc in enumerate(plan.scenes):
                    s = p2.state(sc.id)
                    stt = s.get("status", "pending")
                    pr = int(100 * float(s.get("progress") or 0))
                    cells.append(f"<span class='tf-pill {'ok' if stt == 'done' else 'err' if stt == 'error' else ''}'>"
                                 f"{STATUS_ICON.get(stt, '•')} {i + 1}{'' if stt in ('done', 'pending') else f' {pr}%'}</span>")
                st.markdown("".join(cells), unsafe_allow_html=True)
            logs = (p2.data.get("logs") or [])[-14:]
            if logs:
                with st.expander("Live log", expanded=is_run):
                    st.markdown("<div class='tf-log'>" + "<br>".join(_esc(x) for x in reversed(logs)) + "</div>",
                                unsafe_allow_html=True)

    st.fragment(body, run_every=2.0 if running else None)()


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ------------------------------------------------------------------------------------------- pages
def page_dashboard() -> None:
    projects = jobs.list_projects()
    c1, c2, c3, c4 = st.columns(4)
    usage = cache.usage_today()
    for col, lab, val in ((c1, "Projects", len(projects)),
                          (c2, "Finished films", sum(1 for x in projects if x["final"])),
                          (c3, "LLM backends", len(llm.available_backends())),
                          (c4, "CF neurons today", f"{usage.get('cloudflare_neurons', 0):.0f}/{cache.CLOUDFLARE_FREE_NEURONS}")):
        col.markdown(f"<div class='tf-card'><div class='tf-kpi-l'>{lab}</div><div class='tf-kpi'>{val}</div></div>",
                     unsafe_allow_html=True)
    if st.button("➕ New cinematic project", type="primary"):
        st.session_state["tf_pid"] = None
        st.session_state["tf_nav_next"] = "✨ Create"
        st.rerun()
    if not projects:
        st.info("No projects yet. Go to **✨ Create**, describe your idea and let the AI direct your film.")
        return
    st.markdown("#### Your projects")
    cols = st.columns(3)
    for i, pr in enumerate(projects[:30]):
        with cols[i % 3]:
            with st.container(border=True):
                if pr["thumb"] and Path(pr["thumb"]).exists():
                    st.image(pr["thumb"], width="stretch")
                st.markdown(f"**{_esc(pr['title'] or 'Untitled')}**")
                st.caption(f"{pr['scenes']} scenes · {pr['status']} · "
                           f"{time.strftime('%d %b %H:%M', time.localtime(pr['updated']))}")
                b1, b2, b3 = st.columns(3)
                if b1.button("Open", key=f"tf_open_{pr['id']}", width="stretch", type="primary"):
                    st.session_state["tf_pid"] = pr["id"]
                    st.session_state["tf_nav_next"] = "🎞️ Storyboard" if pr["scenes"] else "✨ Create"
                    st.rerun()
                if b2.button("Duplicate", key=f"tf_dup_{pr['id']}", width="stretch"):
                    np_ = jobs.duplicate_project(pr["id"])
                    st.session_state["tf_pid"] = np_.id
                    st.rerun()
                if b3.button("Delete", key=f"tf_del_{pr['id']}", width="stretch",
                             disabled=jobs.MANAGER.is_running(pr["id"])):
                    st.session_state["tf_confirm_del"] = pr["id"]
                if st.session_state.get("tf_confirm_del") == pr["id"]:
                    st.warning("Delete this project and all its files?")
                    y, n = st.columns(2)
                    if y.button("Yes, delete", key=f"tf_dely_{pr['id']}", type="primary"):
                        jobs.delete_project(pr["id"])
                        if _pid() == pr["id"]:
                            st.session_state["tf_pid"] = None
                        st.session_state.pop("tf_confirm_del", None)
                        st.rerun()
                    if n.button("Cancel", key=f"tf_deln_{pr['id']}"):
                        st.session_state.pop("tf_confirm_del", None)
                        st.rerun()


def page_create() -> None:
    p = _project()
    running = _running(p)
    saved_bp = config.load_brand_prefs()

    st.markdown("#### 1 · Your Idea & Story")
    
    st.caption("Quick inspiration: click any topic to instantly load it")
    cols_idea = st.columns(len(QUICK_IDEAS))
    for i, q_idea in enumerate(QUICK_IDEAS):
        short_label = q_idea.split(":")[0] if ":" in q_idea else q_idea[:22]
        if cols_idea[i].button(short_label, key=f"tf_qidea_{i}", disabled=running):
            st.session_state[f"tf_idea_{_pid()}"] = q_idea
            st.rerun()

    cur_idea = st.session_state.get(f"tf_idea_{_pid()}", (p.data["idea"] if p else ""))
    idea = st.text_area("Describe the film you want", value=cur_idea, height=105,
                        placeholder="e.g. A futuristic city where humans and AI live together, discovering an ancient energy source beneath the neon streets",
                        key=f"tf_idea_{_pid()}", disabled=running)
    with st.expander("📜 I already have a script or director notes (optional)", expanded=False):
        script = st.text_area("Paste a narration script: the AI keeps your words and directs the visuals", height=140,
                              key=f"tf_script_{_pid()}", disabled=running)
        extra = st.text_area("Extra director notes (optional)", height=70, key=f"tf_extra_{_pid()}",
                             placeholder="e.g. end on a hopeful sunrise, include a chase scene, dramatic lighting", disabled=running)
        research_notes = st.text_area(
            "Research notes and source links (optional)",
            value=str((p.settings if p else {}).get("research_notes", "")),
            height=110,
            key=f"tf_research_{_pid()}",
            placeholder="Paste verified facts and the URLs/titles they came from. The planner will be told not to invent citations.",
            disabled=running,
        )
        st.caption("Import an article or transcript to extract source-bounded facts and statistics.")
        import_url = st.text_input("Article URL (optional)", key=f"tf_import_url_{_pid()}")
        import_text = st.text_area("Or paste article / transcript text", height=90,
                                   key=f"tf_import_text_{_pid()}", disabled=running)
        if st.button("Import source facts", key=f"tf_import_source_{_pid()}", disabled=running):
            try:
                source_text = jobs.fetch_article_text(import_url.strip()) if import_url.strip() else import_text.strip()
                if not source_text:
                    raise ValueError("Paste article or transcript text, or provide an article URL.")
                source_label = import_url.strip() or "User-provided transcript"
                extracted = planner.extract_verified_facts(source_text, source_label)
                fact_text = "Imported source facts:\n" + "\n".join(
                    f"- {fact['statement']} [Source: {fact['citation']}]"
                    for fact in extracted.get("facts", [])
                )
                if extracted.get("key_statistics"):
                    fact_text += "\nKey statistics:\n" + json.dumps(
                        extracted["key_statistics"], ensure_ascii=False
                    )
                st.session_state[f"tf_imported_research_{_pid() or 'new'}"] = fact_text
                _flash(f"Imported {len(extracted.get('facts', []))} source-grounded facts")
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(f"Source import failed: {exc}")
        imported_research = st.session_state.get(f"tf_imported_research_{_pid() or 'new'}", "")

    if not p:
        st.markdown("#### 2 · Target Duration & Timeline Pacing")
        st.caption("Choose your desired total runtime (e.g. 1-minute, 2-minute, 3-minute video). Scene pacing and word count automatically adapt.")
        
        d_col1, d_col2, d_col3 = st.columns([2, 1, 1])
        dur_preset_names = [d[0] for d in DURATION_PRESETS]
        chosen_preset = d_col1.selectbox("Target Video Duration", dur_preset_names, index=2, key="tf_new_dur_preset")
        
        preset_info = next(d for d in DURATION_PRESETS if d[0] == chosen_preset)
        if preset_info[1] > 0:
            target_sec = preset_info[1]
            rec_scenes = preset_info[2]
        else:
            c_m = d_col2.number_input("Minutes", min_value=0, max_value=60, value=2, step=1, key="tf_new_cust_m")
            c_s = d_col3.number_input("Seconds", min_value=0, max_value=59, value=30, step=5, key="tf_new_cust_s")
            target_sec = max(10.0, float(c_m * 60 + c_s))
            rec_scenes = max(2, min(50, int(round(target_sec / 9.0))))
        
        t_c1, t_c2 = st.columns(2)
        n = t_c1.slider("Scene Count (Timeline Cuts)", min_value=1, max_value=50, value=rec_scenes, key="tf_new_n")
        calc_dur = max(2.5, min(30.0, round(target_sec / max(1, n), 1)))
        dur = t_c2.slider("Seconds per Scene (Pacing)", min_value=2.0, max_value=30.0, value=calc_dur, step=0.5, key="tf_new_dur")
        
        target_words = int(target_sec * 2.35)
        words_per_scene = max(12, int(target_words / max(1, n)))
        st.markdown(
            f"<div class='tf-card' style='display:flex; justify-content:space-around; align-items:center; text-align:center; padding:12px; margin-bottom:12px;'>"
            f"<div><div class='tf-kpi-l'>Target Runtime</div><div class='tf-kpi' style='color:#38BDF8;'>{_fmt_time(target_sec)}</div></div>"
            f"<div><div class='tf-kpi-l'>Timeline Scenes</div><div class='tf-kpi'>{n} cuts</div></div>"
            f"<div><div class='tf-kpi-l'>Pacing / Scene</div><div class='tf-kpi'>{dur:.1f}s</div></div>"
            f"<div><div class='tf-kpi-l'>Script Target</div><div class='tf-kpi'>{target_words} words <span style='font-size:0.8rem; color:#A1A1AA;'>(~{words_per_scene}/scene)</span></div></div>"
            f"</div>",
            unsafe_allow_html=True
        )

        st.markdown("#### 3 · Channel Branding & Watermark")
        st.caption("Configure your YouTube channel branding. Broadcast glassmorphic watermark and channel mentions are applied automatically.")
        b_c1, b_c2, b_c3 = st.columns([2, 1, 1])
        channel_name = b_c1.text_input("Channel Name", value=saved_bp.get("channel_name", ""),
                                       placeholder="e.g. theMedNurses, CyberCinema, TechForge", key="tf_new_channel_name")
        brand_pos = b_c2.selectbox("Watermark Position", BRAND_POSITIONS,
                                   index=_brand_pos_index(saved_bp.get("brand_position", "Top-Right")), key="tf_new_brand_pos")
        logo_up = b_c3.file_uploader("Upload Channel Logo (.png / .jpg)", type=["png", "jpg", "jpeg", "webp"], key="tf_new_logo_up",
                                     help="Square PNG/JPG logo. Transparent PNG recommended.")

        b_c4, b_c5 = st.columns(2)
        voice_branding = b_c4.toggle("🎙️ AI Mentions Channel Name in Voice (Intro & Outro CTA)",
                                     value=saved_bp.get("voice_branding", True), key="tf_new_voice_branding",
                                     help="Scene 1 welcomes viewers to your channel; the final scene asks viewers to subscribe.")
        brand_overlay = b_c5.toggle("🏷️ Burn Glassmorphic Channel Watermark on Video",
                                    value=True, key="tf_new_brand_overlay",
                                    help="Non-intrusive broadcast channel badge overlay in the chosen corner.")

        preview_logo = logo_up if logo_up is not None else saved_bp.get("channel_logo_path")
        if channel_name.strip() or preview_logo:
            _display_brand_badge(channel_name, preview_logo)

        st.markdown("#### 4 · Format & Direction")
        c1, c2, c3 = st.columns(3)
        aspect = c1.selectbox("Format", list(cinema.ASPECTS), index=0, key="tf_new_aspect")
        style = c2.selectbox("Visual style", list(cinema.STYLES), key="tf_new_style")
        lang = c3.selectbox("Language", list(tts.EDGE_VOICES), key="tf_new_lang")
        
        c4, c5 = st.columns(2)
        default_src = c4.selectbox("Default visual source", FREE_SOURCES,
                                   format_func=lambda k: SOURCES[k], key="tf_new_src")
        auto = c5.toggle("🪄 Auto Cinematic Mode: let the AI choose style, pacing, look and music",
                         value=True, key="tf_new_auto",
                         help="When on, the AI fine-tunes camera moves, lighting, color grading and atmosphere for your idea.")
        
        b1, b2 = st.columns(2)
        go_plan = b1.button("📝 Generate story & scene plan", width="stretch", disabled=not idea.strip())
        go_full = b2.button("🎬 Generate the full film automatically", type="primary", width="stretch",
                            disabled=not idea.strip())
        last = st.session_state.get("tf_last_create")
        if (go_plan or go_full) and last and last[0] == idea.strip() and time.time() - last[1] < 15:
            go_plan = go_full = False  # double-click: the project was already created a moment ago
        if go_plan or go_full:
            st.session_state["tf_last_create"] = (idea.strip(), time.time())
            voice_engine = "kokoro" if lang == "English" else (
                "sarvam" if lang == "Hindi" and config.sarvam_key() else "edge"
            )
            voice = tts.voice_options(lang, voice_engine)[0][0]
            combined_research = "\n\n".join(part for part in (research_notes.strip(), imported_research) if part)
            pr = jobs.create_project(idea.strip(), {
                "style": style, "scene_count": n, "scene_duration": float(dur),
                "target_duration": float(target_sec),
                "aspect": aspect, "language": lang, "voice": voice, "voice_engine": voice_engine,
                "channel_name": channel_name.strip(),
                "brand_position": brand_pos,
                "voice_branding": voice_branding,
                "brand_overlay": brand_overlay,
                "default_source": default_src,
                "research_notes": combined_research,
            })
            saved_logo_path = saved_bp.get("channel_logo_path", "")
            if logo_up is not None:
                try:
                    saved_logo_path = _save_upload(pr, logo_up, "branding", "channel_logo")
                    pr.settings["channel_logo_path"] = saved_logo_path
                    pr.save()
                except Exception as e:
                    st.warning(f"Logo upload note: {e}")
            elif saved_logo_path and Path(saved_logo_path).exists():
                pr.settings["channel_logo_path"] = saved_logo_path
                pr.save()
            config.save_brand_prefs(channel_name, brand_pos, saved_logo_path, voice_branding)
            st.session_state["tf_pid"] = pr.id
            if go_full:
                jobs.MANAGER.start_full(pr, script, extra, auto_settings=auto)
            else:
                if auto:
                    with st.spinner("Auto Cinematic Mode is choosing settings…"):
                        try:
                            jobs.apply_auto_cinematic(pr)
                        except Exception as e:  # noqa: BLE001
                            st.warning(f"Auto settings failed ({e}); using your choices.")
                jobs.MANAGER.start_plan(pr, script, extra)
            st.session_state[f"tf_was_running_{pr.id}"] = True
            st.session_state["tf_nav_next"] = "📖 Story" if go_plan else "🚀 Render & Export"
            st.rerun()
        return

    # If project already exists:
    if idea.strip() and idea.strip() != p.data["idea"] and not running:
        p.data["idea"] = idea.strip()
        p.save()
    imported_research = st.session_state.get(f"tf_imported_research_{p.id}", "")
    if research_notes != p.settings.get("research_notes", "") or (
            imported_research and imported_research not in p.settings.get("research_notes", "")):
        base_research = research_notes.strip()
        if imported_research and imported_research not in base_research:
            base_research = "\n\n".join(part for part in (base_research, imported_research.strip()) if part)
        p.settings["research_notes"] = base_research
        p.save()

    st.markdown("#### 2 · Target Duration & Timeline Pacing")
    t_col1, t_col2 = st.columns(2)
    try:
        cur_tgt = float(p.settings.get("target_duration") or 0)
    except (ValueError, TypeError):
        cur_tgt = 0.0
    if cur_tgt <= 0:
        cur_tgt = float(p.settings.get("scene_count", 8)) * float(p.settings.get("scene_duration", 6.0))
    dur_preset_names = [d[0] for d in DURATION_PRESETS]
    cur_preset_name = next((d[0] for d in DURATION_PRESETS if abs(d[1] - cur_tgt) < 1.0), "⚙️ Custom Duration")
    
    preset_sel = t_col1.selectbox("Target Video Duration Preset", dur_preset_names,
                                  index=dur_preset_names.index(cur_preset_name), key=f"tf_{p.id}_durpreset", disabled=running)
    preset_match = next((d for d in DURATION_PRESETS if d[0] == preset_sel), None)
    if preset_match and preset_match[1] > 0:
        new_tgt = preset_match[1]
    else:
        new_tgt = t_col2.number_input("Custom Target Duration (Seconds)", min_value=10.0, max_value=3600.0,
                                      value=float(cur_tgt), step=10.0, key=f"tf_{p.id}_custom_sec", disabled=running)
    if new_tgt != p.settings.get("target_duration"):
        p.settings["target_duration"] = float(new_tgt)
        p.save()

    c1, c2 = st.columns(2)
    with c1:
        setting(p, "scene_count", "slider", "Scenes (Timeline Cuts)", min_value=1, max_value=50, step=1)
    with c2:
        calc_s_dur = max(2.5, min(30.0, round(float(p.settings["target_duration"]) / max(1, p.settings["scene_count"]), 1)))
        setting(p, "scene_duration", "slider", "Seconds per scene (minimum)", min_value=2.0, max_value=30.0, step=0.5)

    target_words = int(p.settings["target_duration"] * 2.35)
    words_per_sc = max(12, int(target_words / max(1, p.settings["scene_count"])))
    st.markdown(
        f"<div class='tf-card' style='display:flex; justify-content:space-around; align-items:center; text-align:center; padding:12px; margin-bottom:12px;'>"
        f"<div><div class='tf-kpi-l'>Target Runtime</div><div class='tf-kpi' style='color:#38BDF8;'>{_fmt_time(p.settings['target_duration'])}</div></div>"
        f"<div><div class='tf-kpi-l'>Timeline Cuts</div><div class='tf-kpi'>{p.settings['scene_count']} scenes</div></div>"
        f"<div><div class='tf-kpi-l'>Pacing / Scene</div><div class='tf-kpi'>{p.settings['scene_duration']:.1f}s</div></div>"
        f"<div><div class='tf-kpi-l'>Target Spoken Words</div><div class='tf-kpi'>{target_words} words <span style='font-size:0.8rem; color:#A1A1AA;'>(~{words_per_sc}/scene)</span></div></div>"
        f"</div>",
        unsafe_allow_html=True
    )

    if p.plan and p.plan.scenes:
        cur_scenes_dur = sum(sc.duration for sc in p.plan.scenes)
        if abs(cur_scenes_dur - p.settings["target_duration"]) > 5.0:
            if st.button(f"🎯 Auto-Scale Existing {len(p.plan.scenes)} Scenes to Match Target Duration ({_fmt_time(p.settings['target_duration'])})", disabled=running):
                scale = float(p.settings["target_duration"]) / max(1.0, cur_scenes_dur)
                for sc in p.plan.scenes:
                    sc.duration = round(max(2.0, min(30.0, sc.duration * scale)), 1)
                p.set_plan(p.plan)
                p.save()
                _flash(f"Rescaled {len(p.plan.scenes)} scenes to {_fmt_time(p.settings['target_duration'])}")
                st.rerun()

    st.markdown("#### 3 · Channel Branding & Watermark")
    b_c1, b_c2, b_c3 = st.columns([2, 1, 1])
    with b_c1:
        setting(p, "channel_name", "text", "Channel Name", placeholder="e.g. theMedNurses")
    with b_c2:
        setting(p, "brand_position", "select", "Watermark Position", options=BRAND_POSITIONS)
    with b_c3:
        logo_up = st.file_uploader("Upload / Replace Logo", type=["png", "jpg", "jpeg", "webp"], key=f"tf_{p.id}_logoup")
        if logo_up is not None and st.button("Use Logo Upload", key=f"tf_{p.id}_applylogo"):
            try:
                saved_logo = _save_upload(p, logo_up, "branding", "channel_logo")
                p.settings["channel_logo_path"] = saved_logo
                config.save_brand_prefs(p.settings["channel_name"], p.settings["brand_position"], saved_logo, p.settings["voice_branding"])
                p.save()
                _flash("Channel logo updated")
                st.rerun()
            except Exception as e:
                st.error(f"Logo upload failed: {e}")

    b_c4, b_c5 = st.columns(2)
    with b_c4:
        setting(p, "voice_branding", "toggle", "🎙️ AI Mentions Channel Name in Voice (Intro Hook & Outro CTA)")
    with b_c5:
        setting(p, "brand_overlay", "toggle", "🏷️ Burn Glassmorphic Channel Watermark on Video")

    if p.settings.get("channel_name") or p.settings.get("channel_logo_path"):
        _display_brand_badge(p.settings.get("channel_name", ""), p.settings.get("channel_logo_path"))

    st.markdown("#### 4 · Direction & Cinematic Look")
    if p.data.get("auto_reason"):
        st.caption(f"🪄 Auto Cinematic: {p.data['auto_reason']}")
    c1, c2, c3 = st.columns(3)
    with c1:
        setting(p, "style", "select", "Visual style", options=list(cinema.STYLES))
        setting(p, "aspect", "select", "Format", options=list(cinema.ASPECTS))
    with c2:
        setting(p, "language", "select", "Narration language", options=list(tts.EDGE_VOICES))
        setting(p, "default_source", "select", "Default visual source", options=FREE_SOURCES,
                format_func=lambda k: SOURCES[k])
    with c3:
        setting(p, "tone", "text", "Tone", placeholder="awe-inspiring, hopeful, tense…")
        setting(p, "audience", "text", "Audience", placeholder="general YouTube audience, sci-fi fans…")

    b0, b1, b2 = st.columns(3)
    if b0.button("🪄 AI: choose optimal settings", width="stretch", disabled=running):
        with st.spinner("Analysing your idea…"):
            try:
                jobs.apply_auto_cinematic(p)
                _reset_widgets(p.id)
                st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(f"Auto Cinematic failed: {e}")
    has_plan = bool(p.plan and p.plan.scenes)
    if b1.button("📝 " + ("Re-generate story & plan" if has_plan else "Generate story & plan"), width="stretch",
                 disabled=running or not p.data["idea"]):
        jobs.MANAGER.start_plan(p, script, extra)
        st.session_state[f"tf_was_running_{p.id}"] = True
        st.rerun()
    if b2.button("🎬 Generate full film (plan → render)", type="primary", width="stretch",
                 disabled=running or not p.data["idea"]):
        jobs.MANAGER.start_full(p, script, extra)
        st.session_state[f"tf_was_running_{p.id}"] = True
        st.rerun()
    if has_plan:
        st.info("A plan already exists: re-generating replaces the story and scenes (rendered files stay in the "
                "project folder until overwritten).")


def page_story() -> None:
    p = _project()
    plan = p.plan if p else None
    if not plan:
        st.info("Generate a plan in **✨ Create** first.")
        return
    running = _running(p)
    rev = p.data.get("rev", 0)
    with st.form(f"tf_{p.id}_story_{rev}"):
        c1, c2 = st.columns([2, 1])
        title = c1.text_input("Film title", plan.title)
        genre = c2.text_input("Genre", plan.genre)
        logline = st.text_input("Logline", plan.logline)
        hook = st.text_input("Opening hook (first 5 seconds)", plan.hook)
        storyline = st.text_area("Storyline", plan.storyline, height=140)
        c3, c4 = st.columns(2)
        visual = c3.text_area("Visual identity", plan.visual_identity, height=90)
        ending = c4.text_area("Ending", plan.ending, height=90)
        mood = st.selectbox("Soundtrack mood", cinema.MUSIC_MOODS[:-1],
                            index=cinema.MUSIC_MOODS.index(plan.music_mood) if plan.music_mood in cinema.MUSIC_MOODS else 0)
        st.markdown("**YouTube metadata**")
        yt_title = st.text_input("YouTube title", plan.youtube_title)
        yt_desc = st.text_area("YouTube description", plan.youtube_description, height=80)
        tags = st.text_input("Tags (comma separated)", ", ".join(plan.tags))
        if st.form_submit_button("💾 Save story", type="primary", disabled=running):
            plan.title, plan.genre, plan.logline, plan.hook, plan.storyline = title, genre, logline, hook, storyline
            plan.visual_identity, plan.ending, plan.music_mood = visual, ending, mood
            plan.youtube_title, plan.youtube_description = yt_title, yt_desc
            plan.tags = [t.strip() for t in tags.split(",") if t.strip()]
            p.set_plan(plan)
            p.save()
            _flash("Story saved")
            st.rerun()
    st.markdown("#### 📊 Story motion-graphic treatment")
    st.caption("Planner-suggested factual beats are retained automatically. You can also apply a template to selected scenes.")
    with st.container(border=True):
        story_scenes = st.multiselect(
            "Story scenes",
            options=[scene.id for scene in plan.scenes],
            default=[scene.id for scene in plan.scenes if scene.video_source == "procedural_3d_2d"],
            format_func=lambda scene_id: next(
                (f"{index + 1}. {scene.title}" for index, scene in enumerate(plan.scenes) if scene.id == scene_id),
                scene_id,
            ),
            key=f"tf_{p.id}_story_procedural_scenes",
        )
        template = st.selectbox("Procedural template", procedural.TEMPLATES,
                                key=f"tf_{p.id}_story_procedural_template")
        if st.button("Apply template to selected story scenes", disabled=running or not story_scenes):
            for scene in plan.scenes:
                if scene.id in story_scenes:
                    scene.video_source = "procedural_3d_2d"
                    if scene.procedural_template != template:
                        scene.procedural_props = {}
                    scene.procedural_template = template
            p.set_plan(plan)
            p.save()
            _flash(f"Applied {template} to {len(story_scenes)} scene(s)")
            st.rerun()
    st.markdown("#### 🎭 Characters: consistent looks and reference images")
    st.caption("Each scene prompt repeats the character's look text. A reference image is also sent to engines that "
               "support references (Cloudflare FLUX.2, OpenRouter, Gemini) for stronger consistency.")
    refs = p.settings.get("char_refs") or {}
    voices = tts.voice_options(p.settings["language"], p.settings.get("voice_engine", "edge"))
    vlabels = {v: lab for v, lab in voices}
    for ci, ch in enumerate(plan.characters):
        with st.container(border=True):
            a, b = st.columns([1, 3])
            with a:
                ref = refs.get(ch.name)
                if ref and Path(ref).exists():
                    st.image(ref, width="stretch")
                else:
                    st.caption("No reference image")
                up = st.file_uploader("Upload reference", type=["png", "jpg", "jpeg", "webp"],
                                      key=f"tf_{p.id}_chup_{ci}_{rev}", label_visibility="collapsed")
                if up is not None and st.button("Use upload", key=f"tf_{p.id}_chuse_{ci}"):
                    path = _save_upload(p, up, "characters", f"{ci}_{int(time.time())}")
                    p.settings.setdefault("char_refs", {})[ch.name] = path
                    p.save()
                    st.rerun()
                if st.button("🎨 Generate reference", key=f"tf_{p.id}_chgen_{ci}", disabled=running,
                             width="stretch"):
                    jobs.MANAGER.start_task(p, "character",
                                            lambda pr, ev, nm=ch.name: jobs.generate_character_ref(pr, nm, pr.log))
                    st.session_state[f"tf_was_running_{p.id}"] = True
                    st.rerun()
                if ref and st.button("Remove", key=f"tf_{p.id}_chrm_{ci}"):
                    p.settings["char_refs"].pop(ch.name, None)
                    p.save()
                    st.rerun()
            with b:
                with st.form(f"tf_{p.id}_char_{ci}_{rev}"):
                    n1, n2 = st.columns(2)
                    name = n1.text_input("Name", ch.name)
                    role = n2.text_input("Role", ch.role)
                    look = st.text_area("Visual look (reused in every prompt)", ch.look, height=80)
                    pers = st.text_input("Personality", ch.personality)
                    cv = p.settings.get("character_voices", {}).get(ch.name, "")
                    opts = [""] + [v for v, _ in voices]
                    voice = st.selectbox("Dialogue voice", opts, index=opts.index(cv) if cv in opts else 0,
                                         format_func=lambda v: "Auto (by gender)" if not v else vlabels.get(v, v))
                    if st.form_submit_button("💾 Save character", disabled=running):
                        old = ch.name
                        ch.name, ch.role, ch.look, ch.personality = name, role, look, pers
                        if old != name:
                            for sc in plan.scenes:
                                sc.characters = [name if x == old else x for x in sc.characters]
                                for d in sc.dialogue:
                                    if d.speaker == old:
                                        d.speaker = name
                            if old in refs:
                                p.settings["char_refs"][name] = p.settings["char_refs"].pop(old)
                        p.settings.setdefault("character_voices", {})[name] = voice
                        p.set_plan(plan)
                        p.save()
                        _flash(f"Character {name} saved")
                        st.rerun()
    if st.button("➕ Add character", disabled=running):
        plan.characters.append(planner.Character(name=f"Character {len(plan.characters) + 1}"))
        p.set_plan(plan)
        p.save()
        st.rerun()
    if plan.environments:
        st.markdown("#### 🏙️ Environments")
        with st.form(f"tf_{p.id}_envs_{rev}"):
            new_env = []
            for ei, e in enumerate(plan.environments):
                c1, c2 = st.columns([1, 3])
                new_env.append((c1.text_input("Name", e.name, key=f"en_{ei}"), c2.text_area("Look", e.look, height=68,
                                                                                            key=f"el_{ei}")))
            if st.form_submit_button("💾 Save environments", disabled=running):
                plan.environments = [planner.Environment(name=a, look=b) for a, b in new_env if a.strip()]
                p.set_plan(plan)
                p.save()
                _flash("Environments saved")
                st.rerun()


def _scene_editor(p: jobs.Project, plan: planner.CinematicPlan, i: int, running: bool) -> None:
    sc = plan.scenes[i]
    st_ = p.state(sc.id)
    rev = p.data.get("rev", 0)
    bible = jobs.ensure_visual_bible(p)
    visual_locked = not bible.get("required") or bible.get("approved")
    status = st_.get("status", "pending")
    head = f"{STATUS_ICON.get(status, '•')} Scene {i + 1} · {sc.title or 'Untitled'} · {sc.duration:.0f}s · {SOURCES.get(sc.source, sc.source)}"
    with st.expander(head, expanded=st.session_state.get("tf_open_scene") == sc.id):
        left, right = st.columns([2, 3])
        with left:
            img = st_.get("locked_image") or st_.get("image")
            vid = st_.get("ai_video") if sc.source == "ai_video" else st_.get("stock") if sc.source == "stock_video" else None
            if sc.source == "upload" and st_.get("upload"):
                up = st_["upload"]
                (st.video if Path(up).suffix.lower() in (".mp4", ".mov", ".webm", ".m4v", ".mkv") else st.image)(up)
            elif vid and Path(vid).exists():
                st.video(vid)
            elif img and Path(img).exists():
                st.image(img, width="stretch")
            else:
                st.markdown("<div class='tf-card' style='text-align:center;color:#71717A;padding:48px 0'>No visual yet</div>",
                            unsafe_allow_html=True)
            meta = []
            if st_.get("image_engine"):
                meta.append(f"image: {images.ENGINES[st_['image_engine']].label if st_['image_engine'] in images.ENGINES else st_['image_engine']}")
            if st_.get("image_credit"):
                meta.append(st_["image_credit"])
            if st_.get("locked_image"):
                meta.append("🔒 locked image")
            if st_.get("stock_credit"):
                meta.append(st_["stock_credit"])
            if st_.get("image_engine") == "pollinations_free":
                meta.append("Pollinations free tier may add a small logo")
            if st_.get("ai_video_error"):
                meta.append(f"AI video fallback: {st_['ai_video_error'][:90]}")
            if st_.get("error"):
                st.error(st_["error"][:300])
            if meta:
                st.caption(" · ".join(meta))
            if st_.get("clip") and Path(st_["clip"]).exists() and status == "done":
                with st.popover("▶ Rendered clip", width="stretch"):
                    st.video(st_["clip"])
            vars_ = [v for v in st_.get("variations", []) if Path(v["path"]).exists()]
            if vars_:
                st.caption("Variations: pick one to lock it for this scene")
                vc = st.columns(min(3, len(vars_)))
                for vi, v in enumerate(vars_[-3:]):
                    with vc[vi % len(vc)]:
                        st.image(v["path"], width="stretch")
                        if st.button("Use", key=f"tf_{p.id}_usevar_{sc.id}_{vi}", disabled=running):
                            with p.lock:
                                st_["locked_image"] = v["path"]
                                st_["seed"] = v["seed"]
                            p.save()
                            st.rerun()
            r1, r2 = st.columns(2)
            if st_.get("locked_image") and r1.button("🔓 Unlock image", key=f"tf_{p.id}_unlock_{sc.id}", disabled=running):
                st_.pop("locked_image", None)
                p.save()
                st.rerun()
            if sc.source == "stock_video" and r2.button("🔁 Other clip", key=f"tf_{p.id}_stockvar_{sc.id}",
                                                         disabled=running):
                st_["stock_variant"] = int(st_.get("stock_variant", 0)) + 1
                p.save()
                jobs.MANAGER.start_render(p, [sc.id], assemble=False, assets_only=True)
                st.session_state[f"tf_was_running_{p.id}"] = True
                st.rerun()
        with right:
            with st.form(f"tf_{p.id}_scene_{sc.id}_{rev}"):
                c1, c2, c3 = st.columns([3, 2, 1])
                title = c1.text_input("Title", sc.title)
                source_options = FREE_SOURCES
                source_value = sc.source if sc.source in source_options else "original_motion"
                source = c2.selectbox("Visual source", source_options, index=source_options.index(source_value),
                                      format_func=lambda k: SOURCES[k])
                duration = c3.number_input("Min sec", 2.0, 30.0, float(sc.duration), 0.5)
                narration = st.text_area("Narration (voice-over)", sc.narration, height=90)
                dialogue = st.text_area("Dialogue (one line per speaker: `Name: line`)",
                                        "\n".join(f"{d.speaker}: {d.line}" for d in sc.dialogue), height=60)
                image_prompt = st.text_area("Image prompt", sc.image_prompt, height=110)
                n1, n2 = st.columns(2)
                negative = n1.text_input("Negative prompt", sc.negative_prompt)
                stock_q = n2.text_input("Stock footage keywords", sc.stock_query)
                video_prompt = st.text_input("Motion direction (local free motion)", sc.video_prompt)
                mv1, mv2, mv3, mv4 = st.columns(4)
                video_source_value = sc.video_source if sc.video_source in VIDEO_SOURCES else "original_motion"
                video_source = mv1.selectbox(
                    "Scene motion engine", list(VIDEO_SOURCES),
                    index=list(VIDEO_SOURCES).index(video_source_value),
                    format_func=lambda key: VIDEO_SOURCES[key],
                )
                preset_value = sc.motion_preset if sc.motion_preset in jobs.MOTION_PRESETS else "dolly_in"
                motion_preset = mv2.selectbox("Motion preset", list(jobs.MOTION_PRESETS),
                                              index=list(jobs.MOTION_PRESETS).index(preset_value))
                intensity_value = sc.depth_intensity if sc.depth_intensity in ("subtle", "medium", "cinematic") else "medium"
                depth_intensity = mv3.selectbox("Depth intensity", ["subtle", "medium", "cinematic"],
                                                index=["subtle", "medium", "cinematic"].index(intensity_value))
                depth_dof = mv4.slider("DepthFlow DOF", 0.0, 1.0, max(0.0, min(1.0, float(sc.depth_dof))), 0.05,
                                       help="Subtle lens blur for the DepthFlow 3D engine.")
                procedural_template = st.selectbox(
                    "Motion-graphic template", procedural.TEMPLATES,
                    index=procedural.TEMPLATES.index(sc.procedural_template)
                    if sc.procedural_template in procedural.TEMPLATES else 0,
                    disabled=video_source != "procedural_3d_2d",
                )
                procedural_props_text = ""
                if video_source == "procedural_3d_2d":
                    procedural_props_text = st.text_area(
                        "Template props (strict JSON)",
                        json.dumps(sc.procedural_props, ensure_ascii=False, indent=2),
                        height=150,
                        help="Edit template-specific fields as JSON. Brand colors, channel name, title, aspect and duration "
                             "are inherited from the project and scene.",
                    )
                    st.caption("Supported: 3d_bar_chart, 3d_stat_callout, comparison_split, timeline_journey, "
                               "process_flow, quote_evidence_card. Evidence citations must match Story research sources.")
                g1, g2, g3, g4 = st.columns(4)
                cam = g1.selectbox("Camera move", list(cinema.CAMERA_MOVES), index=list(cinema.CAMERA_MOVES).index(sc.camera_move))
                shot = g2.selectbox("Shot", cinema.SHOT_TYPES, index=cinema.SHOT_TYPES.index(sc.shot_type))
                angle = g3.selectbox("Angle", cinema.CAMERA_ANGLES, index=cinema.CAMERA_ANGLES.index(sc.camera_angle))
                light = g4.selectbox("Lighting", cinema.LIGHTING, index=cinema.LIGHTING.index(sc.lighting))
                h1, h2, h3, h4 = st.columns(4)
                atmo = h1.selectbox("Atmosphere", list(cinema.ATMOSPHERES), index=list(cinema.ATMOSPHERES).index(sc.atmosphere),
                                    format_func=lambda k: cinema.ATMOSPHERES[k])
                grades = [""] + list(cinema.GRADES)
                grade = h2.selectbox("Colour grade", grades, index=grades.index(sc.color_grade) if sc.color_grade in grades else 0,
                                     format_func=lambda g: g or "Project default")
                anim = h3.selectbox("Animation effect", cinema.ANIMATION_EFFECTS,
                                    index=cinema.ANIMATION_EFFECTS.index(sc.animation))
                trans = h4.selectbox("Transition out", list(cinema.TRANSITIONS), index=list(cinema.TRANSITIONS).index(sc.transition))
                k1, k2, k3, k4 = st.columns(4)
                cues = ["", "calm", "build", "tension", "climax", "resolve", "silence"]
                cue = k1.selectbox("Music cue", cues, index=cues.index(sc.music_cue) if sc.music_cue in cues else 0,
                                   format_func=lambda c: c or "Auto (story arc)")
                sfx = k2.selectbox("Sound effect", cinema.SFX, index=cinema.SFX.index(sc.sfx))
                amb = k3.selectbox("Ambience", cinema.AMBIENCES, index=cinema.AMBIENCES.index(sc.ambience))
                ok = ["Title", "Subtitle", "Lower third", "Chapter"]
                okind = k4.selectbox("Overlay type", ok, index=ok.index(sc.overlay_kind) if sc.overlay_kind in ok else 0)
                overlay = st.text_input("On-screen text overlay (optional)", sc.overlay_text)
                upl = st.file_uploader("Upload your own image / video for this scene", key=f"tf_{p.id}_upl_{sc.id}_{rev}",
                                       type=["png", "jpg", "jpeg", "webp", "mp4", "mov", "webm", "m4v"])
                notes = st.text_input("Director notes for AI rewrite / enhance (optional)",
                                      placeholder="more tension, show the robot's face, rain at night…")
                b = st.columns(5)
                refresh = b[0].form_submit_button("↻ Refresh scene", disabled=running or not visual_locked,
                                                  width="stretch")
                save = b[1].form_submit_button("💾 Save", type="primary", disabled=running, width="stretch")
                gen = b[2].form_submit_button("🖼️ New image", disabled=running or not visual_locked, width="stretch")
                var = b[3].form_submit_button("🎲 3 variations", disabled=running or not visual_locked,
                                              width="stretch")
                rend = b[4].form_submit_button("🎥 Preview this scene", disabled=running or not visual_locked,
                                               width="stretch")
                b2 = st.columns(4)
                enh = b2[0].form_submit_button("✨ Enhance prompt", disabled=running, width="stretch")
                rew = b2[1].form_submit_button("🤖 Rewrite scene (AI)", disabled=running, width="stretch")
                voi = b2[2].form_submit_button("🔊 Preview voice", disabled=running, width="stretch")
                proc_preview = b2[3].form_submit_button(
                    "🧊 Motion still", disabled=running or video_source != "procedural_3d_2d", width="stretch",
                )
                st.caption("Preview renders only this scene at the current output size. Render the full film afterward to update the export.")
            if refresh or save or gen or var or rend or enh or rew or voi or proc_preview:
                parsed_proc_props = None
                if video_source == "procedural_3d_2d":
                    try:
                        parsed_proc_props = json.loads(procedural_props_text or "{}")
                        if not isinstance(parsed_proc_props, dict):
                            raise ValueError("Template props must be a JSON object.")
                    except (json.JSONDecodeError, ValueError) as error:
                        _flash(f"Invalid procedural props: {error}", "error")
                        return
                st.session_state["tf_open_scene"] = sc.id
                sc.title, sc.source, sc.duration, sc.narration = title, source, float(duration), narration
                sc.dialogue = planner.Scene(dialogue=dialogue).dialogue
                sc.image_prompt, sc.negative_prompt, sc.stock_query, sc.video_prompt = image_prompt, negative, stock_q, video_prompt
                sc.video_source, sc.motion_preset = video_source, motion_preset
                sc.depth_intensity, sc.depth_dof = depth_intensity, float(depth_dof)
                sc.procedural_template = procedural_template
                if parsed_proc_props is not None:
                    sc.procedural_props = parsed_proc_props
                sc.camera_move, sc.shot_type, sc.camera_angle, sc.lighting = cam, shot, angle, light
                sc.atmosphere, sc.color_grade, sc.animation, sc.transition = atmo, grade, anim, trans
                sc.music_cue, sc.sfx, sc.ambience, sc.overlay_kind, sc.overlay_text = cue, sfx, amb, okind, overlay
                if upl is not None:
                    st_["upload"] = _save_upload(p, upl, f"scenes/{sc.id}", f"upload_{int(time.time())}")
                    sc.source = "upload"
                    st_.pop("locked_image", None)
                if enh:
                    with st.spinner("Enhancing prompt…"):
                        try:
                            sc.image_prompt = planner.enhance_prompt(sc.image_prompt + (f". {notes}" if notes else ""),
                                                                     p.settings["style"], log=p.log)
                        except Exception as e:  # noqa: BLE001
                            _flash(f"Enhance failed: {e}", "error")
                if rew:
                    with st.spinner("The AI director is rewriting this scene…"):
                        try:
                            new = planner.regenerate_scene(plan, i, p.settings["style"], p.settings["scene_duration"],
                                                           p.settings["language"], notes, log=p.log)
                            plan.scenes[i] = new
                        except Exception as e:  # noqa: BLE001
                            _flash(f"Rewrite failed: {e}", "error")
                if gen:
                    st_["seed"] = random.randint(1, 2 ** 31 - 1)
                    st_.pop("locked_image", None)
                    if sc.source in ("upload", "stock_video"):
                        sc.source = "original_motion"
                if refresh:
                    st_["seed"] = random.randint(1, 2 ** 31 - 1)
                    st_.pop("locked_image", None)
                    st_.pop("ai_video_fp", None)
                    st_.pop("ai_video", None)
                    sc.source = "original_motion"
                    p.data["outputs"] = {}
                    p.data.pop("visual_qa", None)
                p.set_plan(plan)
                p.save()
                if refresh:
                    jobs.MANAGER.start_render(p, [sc.id], assemble=False)
                    st.session_state[f"tf_was_running_{p.id}"] = True
                elif gen:
                    jobs.MANAGER.start_render(p, [sc.id], assemble=False, assets_only=True)
                    st.session_state[f"tf_was_running_{p.id}"] = True
                elif var:
                    jobs.MANAGER.start_task(p, "variations", lambda pr, ev, sid=sc.id: jobs.generate_variations(
                        pr, next(s for s in pr.plan.scenes if s.id == sid), 3, pr.log))
                    st.session_state[f"tf_was_running_{p.id}"] = True
                elif rend:
                    jobs.MANAGER.start_render(p, [sc.id], assemble=False)
                    st.session_state[f"tf_was_running_{p.id}"] = True
                elif voi:
                    with st.spinner("Synthesising voice…"):
                        try:
                            parts = jobs.ensure_voice(p, plan.scenes[i], p.log)
                            p.save()
                            st.session_state[f"tf_voice_{sc.id}"] = [x["path"] for x in parts]
                        except Exception as e:  # noqa: BLE001
                            _flash(f"Voice failed: {e}", "error")
                elif proc_preview:
                    jobs.MANAGER.start_task(
                        p, "procedural_preview",
                        lambda pr, ev, sid=sc.id: jobs.preview_procedural_scene(pr, sid, ev.is_set),
                    )
                    st.session_state[f"tf_was_running_{p.id}"] = True
                elif save:
                    _flash(f"Scene {i + 1} saved")
                st.rerun()
            for vp in st.session_state.get(f"tf_voice_{sc.id}", []):
                if Path(vp).exists():
                    st.audio(vp)
            preview_image = p.scene_dir(sc.id) / f"scene_{i + 1:02d}_procedural_preview.png"
            if preview_image.is_file():
                st.image(str(preview_image), caption="Remotion frame preview", width="stretch")
            m = st.columns(5)
            if m[0].button("⬆️ Up", key=f"tf_{p.id}_up_{sc.id}", disabled=running or i == 0, width="stretch") and i > 0:
                plan.scenes[i - 1], plan.scenes[i] = plan.scenes[i], plan.scenes[i - 1]
                p.set_plan(plan); p.save(); st.rerun()  # noqa: E702
            if m[1].button("⬇️ Down", key=f"tf_{p.id}_dn_{sc.id}", disabled=running or i == len(plan.scenes) - 1,
                           width="stretch") and i < len(plan.scenes) - 1:
                plan.scenes[i + 1], plan.scenes[i] = plan.scenes[i], plan.scenes[i + 1]
                p.set_plan(plan); p.save(); st.rerun()  # noqa: E702
            if m[2].button("⧉ Duplicate", key=f"tf_{p.id}_dupsc_{sc.id}", disabled=running or len(plan.scenes) >= 50,
                           width="stretch") and len(plan.scenes) < 50:
                cp = planner.Scene(**{**sc.model_dump(), "id": planner.uuid.uuid4().hex[:8]})
                plan.scenes.insert(i + 1, cp)
                p.set_plan(plan); p.save(); st.rerun()  # noqa: E702
            if m[3].button("➕ Insert after", key=f"tf_{p.id}_ins_{sc.id}", disabled=running or len(plan.scenes) >= 50,
                           width="stretch") and len(plan.scenes) < 50:
                plan.scenes.insert(i + 1, planner.Scene(title="New scene", narration="", image_prompt=sc.image_prompt,
                                                        duration=p.settings["scene_duration"]).normalise(
                    p.settings["style"], p.settings["scene_duration"]))
                p.set_plan(plan); p.save(); st.rerun()  # noqa: E702
            if m[4].button("🗑️ Delete", key=f"tf_{p.id}_delsc_{sc.id}", disabled=running or len(plan.scenes) <= 1,
                           width="stretch") and len(plan.scenes) > 1:
                plan.scenes.pop(i)
                p.set_plan(plan); p.save(); st.rerun()  # noqa: E702


def _visual_bible_panel(p: jobs.Project, running: bool) -> None:
    bible = jobs.ensure_visual_bible(p)
    st.markdown("#### 🎨 Generate & Lock Visual Bible")
    st.caption("Set one visual mode, locked style prefix, negative prompt, and project seed. Generate a master style "
               "reference and character sheets, review them, then approve before batch scene images use them.")
    revision = f"{p.data.get('rev', 0)}_{p.data.get('visualBibleRevision', 0)}"
    modes = ["stylized_3d", "stylized_2d", "cinematic_doc"]
    mode_labels = {"stylized_3d": "Stylized 3D", "stylized_2d": "Stylized 2D",
                   "cinematic_doc": "Cinematic documentary"}
    with st.form(f"tf_{p.id}_visual_bible_{revision}"):
        mode = st.selectbox("Visual mode", modes, index=modes.index(bible["visualMode"]),
                            format_func=lambda value: mode_labels[value])
        prefix = st.text_area("Locked style prefix", bible.get("stylePrefix", ""), height=90,
                              help="Lighting, lens, texture, and palette guidance repeated on each scene prompt.")
        negative = st.text_area("Global negative prompt", bible.get("globalNegativePrompt", ""), height=60)
        seed_base = st.number_input("Project seed", min_value=0, max_value=2 ** 31 - 2,
                                    value=int(bible["seedBase"]), step=1)
        save_draft = st.form_submit_button("Save Visual Bible draft", disabled=running)
        generate = st.form_submit_button("Generate Visual Bible references", type="primary", disabled=running)
    if save_draft or generate:
        changed = (mode != bible["visualMode"] or prefix != bible.get("stylePrefix", "")
                   or negative != bible.get("globalNegativePrompt", "")
                   or int(seed_base) != int(bible["seedBase"]))
        bible.update(visualMode=mode, stylePrefix=prefix, globalNegativePrompt=negative, seedBase=int(seed_base))
        if changed:
            bible["approved"] = False
        p.data["visualBibleRevision"] = int(p.data.get("visualBibleRevision", 0)) + 1
        p.save()
        if generate:
            jobs.MANAGER.start_task(p, "visual_bible",
                                    lambda project, _event: jobs.generate_visual_bible(project, project.log))
            st.session_state[f"tf_was_running_{p.id}"] = True
        st.rerun()

    bible = jobs.ensure_visual_bible(p)
    master = bible.get("masterReferencePath", "")
    master_ready = bool(master and Path(master).is_file())
    chars_ready = all(not item.get("name") or (item.get("referenceImagePath")
                      and Path(item["referenceImagePath"]).is_file()) for item in bible.get("characters", []))
    refs_current = bible.get("generatedFingerprint") == jobs.visual_bible_fingerprint(bible)
    if master_ready:
        st.image(master, caption="Master style reference", width="stretch")
    if bible.get("characters"):
        columns = st.columns(min(4, len(bible["characters"])))
        for index, character in enumerate(bible["characters"]):
            with columns[index % len(columns)]:
                path = character.get("referenceImagePath", "")
                if path and Path(path).is_file():
                    st.image(path, caption=character.get("name", "Character"), width="stretch")
                else:
                    st.caption(f"{character.get('name', 'Character')}: reference not generated")
    if bible.get("approved"):
        st.success("Visual Bible approved and locked. Its prefix, seed, and available references will be used for scene images.")
    elif master_ready and chars_ready and refs_current:
        if st.button("✅ Approve & Lock Visual Bible", key=f"tf_{p.id}_visual_bible_approve", disabled=running):
            bible["approved"] = True
            p.data["visualBibleRevision"] = int(p.data.get("visualBibleRevision", 0)) + 1
            p.save()
            st.rerun()
    elif master_ready and chars_ready:
        st.warning("Visual Bible settings changed since these references were generated. Regenerate references before approval.")
    if master_ready and st.button("🔁 Regenerate Visual Bible references", key=f"tf_{p.id}_visual_bible_regen",
                                  disabled=running):
        bible["seedBase"] = (int(bible["seedBase"]) + 7919) % (2 ** 31 - 1) or 1
        bible["approved"] = False
        p.data["visualBibleRevision"] = int(p.data.get("visualBibleRevision", 0)) + 1
        p.save()
        jobs.MANAGER.start_task(p, "visual_bible",
                                lambda project, _event: jobs.generate_visual_bible(project, project.log, regenerate=True))
        st.session_state[f"tf_was_running_{p.id}"] = True
        st.rerun()


def _channel_library_panel(p: jobs.Project, running: bool) -> None:
    name = str(p.settings.get("channel_name", "")).strip()
    with st.expander("🎨 Channel Asset Library & Series Memory", expanded=False):
        if not name:
            st.info("Set a Channel Name in Create before saving or loading reusable channel assets.")
        else:
            history = jobs.load_series_history(name)
            st.caption(f"Library: {jobs.channel_dir(name) / 'library.json'} · "
                       f"series memory: {len(history)} completed project(s), last 25 injected during planning.")
            save_col, load_col = st.columns(2)
            if save_col.button("Save Visual Bible, palette & voice", key=f"tf_{p.id}_lib_save",
                               disabled=running or not p.plan):
                try:
                    path = jobs.save_project_channel_library(p)
                    _flash(f"Saved channel assets to {path}")
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Could not save channel assets: {exc}")
            if load_col.button("Load channel assets into project", key=f"tf_{p.id}_lib_load",
                               disabled=running):
                try:
                    if jobs.apply_channel_library(p):
                        _flash("Channel assets loaded and saved to project state")
                        st.rerun()
                    else:
                        st.warning("No Visual Bible is saved for this channel yet.")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Could not load channel assets: {exc}")
            if history:
                st.dataframe([{"Title": item.get("title", ""), "Topic": item.get("topic", ""),
                               "Opening hook": item.get("opening_hook", "")} for item in history[-5:]],
                             hide_index=True, width="stretch")


def _style_reference_panel(p: jobs.Project, running: bool) -> None:
    with st.expander("🧭 Screenshot-to-Original-Style Extractor", expanded=False):
        st.caption("Upload 1–3 references. Gemini derives broad visual qualities and invents an original identity; "
                   "it is instructed not to reproduce characters, logos, or distinctive compositions.")
        uploads = st.file_uploader("Reference screenshots", type=["png", "jpg", "jpeg", "webp"],
                                   accept_multiple_files=True, key=f"tf_{p.id}_style_refs",
                                   disabled=running)
        if st.button("Extract original Visual Bible", key=f"tf_{p.id}_extract_style",
                     disabled=running or not uploads):
            saved = []
            try:
                if not 1 <= len(uploads) <= 3:
                    raise ValueError("Choose one to three screenshots.")
                for index, upload in enumerate(uploads):
                    saved.append(Path(_save_upload(p, upload, "style_references", f"reference_{index + 1}")))
                extracted = images.extract_original_style(saved)
                bible = jobs.ensure_visual_bible(p)
                bible["stylePrefix"] = ". ".join(str(extracted.get(key, "")).strip() for key in
                                                 ("art_medium", "lighting", "style_prefix") if extracted.get(key))
                bible["globalNegativePrompt"] = str(extracted.get("global_negative_prompt") or
                                                    jobs.VISUAL_BIBLE_NEGATIVE)
                bible["styleExtraction"] = extracted
                bible["approved"] = False
                character = extracted.get("original_character") or {}
                if isinstance(character, dict) and character.get("name"):
                    bible["characters"] = (bible.get("characters") or []) + [{
                        "id": re.sub(r"[^a-z0-9]+", "-", str(character["name"]).lower()).strip("-"),
                        "name": str(character["name"]), "look": str(character.get("description", "")),
                    }]
                palette = extracted.get("palette") or []
                palette_keys = ("brand_primary_color", "brand_secondary_color", "brand_accent_color")
                for key, color in zip(palette_keys, palette):
                    if re.fullmatch(r"#[0-9A-Fa-f]{6}", str(color)):
                        p.settings[key] = color
                p.data["visualBible"] = bible
                p.data["visualBibleRevision"] = int(p.data.get("visualBibleRevision", 0)) + 1
                p.save()
                st.success("Original style extracted. Review it, then approve and lock the Visual Bible.")
                st.json(extracted)
            except Exception as exc:  # noqa: BLE001
                st.error(f"Style extraction failed: {exc}")


def _hero_clips_panel(p: jobs.Project, running: bool) -> None:
    with st.expander("🎞️ Hero Clips Drawer (Free Motion Hook)", expanded=False):
        if not p.plan:
            st.info("Generate a storyboard first.")
            return
        if not any(scene.is_hero_scene for scene in p.plan.scenes):
            jobs.flag_hero_scenes(p.plan)
            p.set_plan(p.plan)
            p.save()
        rows = jobs.hero_beat_rows(p)
        hero_rows = [item for item in rows if item.get("is_hero_scene")]
        prompts = "\n\n".join(re.sub(r"\s+", " ", str(item.get("visual_prompt") or item.get("text") or "")).strip()
                              for item in hero_rows)
        st.caption("Copy the clean hero prompts, then attach any externally generated MP4 to its scene. Imported clips "
                   "are normalized and used in final assembly; the generated scene remains cached for resume.")
        if prompts:
            st.code(prompts, language=None)
        for item in hero_rows:
            index = int(item["scene_index"])
            st.markdown(f"**Hero {index + 1}: {_esc(item.get('title', 'Scene'))}** · "
                        f"{_fmt_time(item['start'])}–{_fmt_time(item['end'])}")
            uploaded = st.file_uploader("Drop an MP4 hero clip here", type=["mp4", "mov", "m4v"],
                                        key=f"tf_{p.id}_hero_upload_{index}", disabled=running)
            if uploaded is not None and st.button("Attach clip", key=f"tf_{p.id}_hero_attach_{index}",
                                                  disabled=running):
                try:
                    path = jobs.attach_hero_clip(p, index, uploaded.getvalue(), uploaded.name)
                    _flash(f"Attached hero clip: {path.name}")
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Hero clip import failed: {exc}")
            clip = item.get("hero_clip")
            if clip and Path(clip).is_file():
                st.video(clip)
                if st.button("Extract Last Frame PNG", key=f"tf_{p.id}_hero_frame_{index}", disabled=running):
                    try:
                        frame = Path(clip).with_name(Path(clip).stem + "_last_frame.png")
                        jobs.extract_hero_last_frame(Path(clip), frame)
                        st.session_state[f"tf_{p.id}_hero_frame_path_{index}"] = str(frame)
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Could not extract last frame: {exc}")
                frame_path = st.session_state.get(f"tf_{p.id}_hero_frame_path_{index}")
                if frame_path and Path(frame_path).is_file():
                    st.image(frame_path, caption="Final frame for continuity reference")
                    with open(frame_path, "rb") as frame_file:
                        st.download_button("Download final frame", frame_file, file_name=Path(frame_path).name,
                                           mime="image/png", key=f"tf_{p.id}_hero_frame_dl_{index}")


def page_storyboard() -> None:
    p = _project()
    plan = p.plan if p else None
    if not plan:
        st.info("Generate a plan in **✨ Create** first.")
        return
    running = _running(p)
    n = len(plan.scenes)
    done = sum(1 for sc in plan.scenes if p.state(sc.id).get("status") == "done")
    c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
    c1.markdown(f"### {_esc(plan.title)}")
    c1.caption(plan.logline)
    c2.metric("Scenes", n)
    c3.metric("Est. runtime", _fmt_time(_est_runtime(p, plan)))
    c4.metric("Rendered", f"{done}/{n}")
    _alignment_beats_panel(p)
    _visual_bible_panel(p, running)
    _channel_library_panel(p, running)
    _style_reference_panel(p, running)
    _hero_clips_panel(p, running)
    bible = jobs.ensure_visual_bible(p)
    visual_gate = bool(bible.get("required") and not bible.get("approved"))
    if visual_gate:
        st.info("Approve the Visual Bible above before generating or rendering scene images.")
    engine_col, apply_col, choreo_col = st.columns([2, 1, 1])
    bulk_engine = engine_col.selectbox(
        "Bulk scene motion engine", list(VIDEO_SOURCES),
        format_func=lambda key: VIDEO_SOURCES[key], key=f"tf_{p.id}_storyboard_bulk_engine",
    )
    if apply_col.button("Apply to all", disabled=running, width="stretch"):
        for scene in plan.scenes:
            scene.video_source = bulk_engine
        p.set_plan(plan)
        p.save()
        _flash(f"Applied {VIDEO_SOURCES[bulk_engine]} to all scenes")
        st.rerun()
    if choreo_col.button("Auto-Choreograph", disabled=running or not plan.scenes, width="stretch",
                         help="Assigns non-repeating camera presets to adjacent scenes."):
        jobs.auto_choreograph(plan)
        p.set_plan(plan)
        p.save()
        _flash("Camera presets assigned without adjacent repeats")
        st.rerun()
    a1, a2, a3 = st.columns(3)
    if a1.button("🖼️ Generate all missing visuals + voices", width="stretch", disabled=running or visual_gate):
        jobs.MANAGER.start_render(p, assets_only=True)
        st.session_state[f"tf_was_running_{p.id}"] = True
        st.rerun()
    if a2.button("🎬 Render all scenes + final film", type="primary", width="stretch",
                 disabled=running or visual_gate):
        jobs.MANAGER.start_render(p)
        st.session_state[f"tf_was_running_{p.id}"] = True
        st.rerun()
    failed = [sc.id for sc in plan.scenes if p.state(sc.id).get("status") == "error"]
    if a3.button(f"🔁 Retry failed scenes ({len(failed)})", width="stretch",
                 disabled=running or not failed or visual_gate):
        jobs.MANAGER.start_render(p, failed, assemble=False)
        st.session_state[f"tf_was_running_{p.id}"] = True
        st.rerun()
    per = 8
    pages = max(1, (n + per - 1) // per)
    pg = st.session_state.get(f"tf_{p.id}_page", 0)
    if pages > 1:
        pg = st.radio("Scenes", list(range(pages)), horizontal=True, key=f"tf_{p.id}_page",
                      format_func=lambda k: f"{k * per + 1}–{min(n, (k + 1) * per)}")
    for i in range(pg * per, min(n, (pg + 1) * per)):
        _scene_editor(p, plan, i, running)
    if st.button("➕ Add scene at end", disabled=running or n >= 50):
        plan.scenes.append(planner.Scene(title="New scene", image_prompt=plan.scenes[-1].image_prompt if n else "",
                                         duration=p.settings["scene_duration"]).normalise(p.settings["style"],
                                                                                          p.settings["scene_duration"]))
        p.set_plan(plan)
        p.save()
        st.rerun()


def _autoclip_panel(p: jobs.Project, running: bool) -> None:
    outputs = p.data.get("outputs") or {}
    with st.expander("📱 AutoClip — standalone subject-aware Shorts", expanded=False):
        alignment_path = outputs.get("alignment")
        source = outputs.get("video")
        if not source or not Path(source).is_file() or not alignment_path or not Path(alignment_path).is_file():
            st.info("Render the film with word alignment first. AutoClip uses the saved word timestamps.")
            return
        try:
            words = json.loads(Path(alignment_path).read_text(encoding="utf-8"))
            candidates = alignment.score_viral_moments(words, float(outputs.get("duration") or 0), limit=5)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Could not score Shorts moments: {exc}")
            return
        if not candidates:
            st.warning("No eligible word-timed windows were found. Longer narration (at least about 30 seconds) is needed.")
            return
        labels = {
            f"{item['score']}/100 · {_fmt_time(item['start'])}–{_fmt_time(item['end'])} · {item['title']}": index
            for index, item in enumerate(candidates)
        }
        selected = st.multiselect("Choose 1–5 standalone moments", list(labels),
                                  default=list(labels)[:min(2, len(labels))], key=f"tf_{p.id}_autoclip_choice")
        st.caption("The heuristic virality score is a ranking aid, not a prediction of audience performance. Face detection "
                   "tracks the largest face; visual saliency is used when no face is found.")
        presets = ["Hormozi Bounce", "Clean Documentary", "Karaoke Glow", "Minimalist White", "Shorts Pop"]
        current_preset = p.settings.get("caption_preset", presets[0])
        preset = st.selectbox("Shorts caption preset", presets,
                              index=presets.index(current_preset) if current_preset in presets else 0,
                              key=f"tf_{p.id}_autoclip_caption")
        if p.settings.get("caption_preset") != preset:
            p.settings["caption_preset"] = preset
            p.save()
        if st.button("Render selected Shorts to project/shorts", disabled=running or not selected,
                     key=f"tf_{p.id}_autoclip_render"):
            directory = p.dir / "shorts"
            directory.mkdir(parents=True, exist_ok=True)
            saved = []
            try:
                accent = p.settings.get("brand_accent_color", "#22D3EE")
                for selection in selected:
                    item = candidates[labels[selection]]
                    filename = re.sub(r"[^a-z0-9]+", "-", item["title"].lower()).strip("-")[:48] or "short"
                    target = directory / f"{filename}_{int(item['start']):04d}.mp4"
                    render.make_autoclip_short(Path(source), target, float(item["start"]), float(item["end"]),
                                               words=words, accent=accent, caption_preset=preset)
                    saved.append(str(target))
                outputs["autoclips"] = list(dict.fromkeys((outputs.get("autoclips") or []) + saved))
                p.data["outputs"] = outputs
                p.data["autoclip_candidates"] = candidates
                p.save()
                _flash(f"Rendered {len(saved)} subject-reframed Short(s)")
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(f"AutoClip render failed: {exc}")
        for path in outputs.get("autoclips") or []:
            if Path(path).is_file():
                st.video(path)
                with open(path, "rb") as short_file:
                    st.download_button("Download Short", short_file, file_name=Path(path).name,
                                       mime="video/mp4", key=f"tf_{p.id}_autoclip_dl_{Path(path).name}")


def page_timeline() -> None:
    p = _project()
    plan = p.plan if p else None
    if not plan:
        st.info("Generate a plan in **✨ Create** first.")
        return
    running = _running(p)
    st.markdown("#### 🎞️ Timeline & Visual Pacing")
    _alignment_beats_panel(p)

    est_total = _est_runtime(p, plan)
    try:
        tgt_total = float(p.settings.get("target_duration") or 0)
    except (ValueError, TypeError):
        tgt_total = 0.0
    if tgt_total <= 0:
        tgt_total = float(len(plan.scenes) * p.settings.get("scene_duration", 6.0))
    all_words = sum(len(sc.narration.split()) for sc in plan.scenes)

    # Top KPI Bar
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Target Runtime", _fmt_time(tgt_total))
    k2.metric("Est. Runtime", _fmt_time(est_total), delta=f"{est_total - tgt_total:+.0f}s" if abs(est_total - tgt_total) >= 1.0 else "on target")
    k3.metric("Scenes Count", f"{len(plan.scenes)} cuts")
    k4.metric("Total Spoken Words", f"{all_words} words", help="Based on standard ~2.35 words/sec narration speed")
    if st.button("🧪 Run free local visual QA", disabled=running):
        qa = jobs.quality.inspect_project(plan, p.data.get("scene_state") or {},
                                          p.settings.get("char_refs") or {}, jobs.render_size(p.settings)[:2])
        p.data["visual_qa"] = qa
        p.save()
    _visual_qa_panel(p)
    engine_col, apply_col, choreo_col = st.columns([2, 1, 1])
    bulk_engine = engine_col.selectbox(
        "Bulk videoSource", list(VIDEO_SOURCES),
        format_func=lambda key: VIDEO_SOURCES[key], key=f"tf_{p.id}_timeline_bulk_engine",
    )
    if apply_col.button("Apply engine", disabled=running, width="stretch"):
        for scene in plan.scenes:
            scene.video_source = bulk_engine
        p.set_plan(plan)
        p.save()
        _flash(f"Applied {VIDEO_SOURCES[bulk_engine]} to all scenes")
        st.rerun()
    if choreo_col.button("Auto-Choreograph", disabled=running or not plan.scenes, width="stretch"):
        jobs.auto_choreograph(plan)
        p.set_plan(plan)
        p.save()
        _flash("Camera presets assigned without adjacent repeats")
        st.rerun()

    # Visual Multi-Segment Timeline Bar
    SEG_GRADIENTS = [
        "linear-gradient(135deg, #7C3AED, #4F46E5)",
        "linear-gradient(135deg, #06B6D4, #0284C7)",
        "linear-gradient(135deg, #EC4899, #DB2777)",
        "linear-gradient(135deg, #10B981, #059669)",
        "linear-gradient(135deg, #F59E0B, #D97706)",
        "linear-gradient(135deg, #8B5CF6, #6366F1)",
        "linear-gradient(135deg, #14B8A6, #0D9488)",
        "linear-gradient(135deg, #F43F5E, #E11D48)",
    ]
    tot_scenes_dur = max(1.0, sum(sc.duration for sc in plan.scenes))
    segments_html = []
    for i, sc in enumerate(plan.scenes):
        pct = (sc.duration / tot_scenes_dur) * 100
        grad = SEG_GRADIENTS[i % len(SEG_GRADIENTS)]
        title_esc = _esc(sc.title or f"Scene {i + 1}")
        status = p.state(sc.id).get("status", "pending")
        icon = STATUS_ICON.get(status, "•")
        segments_html.append(
            f"<div class='tf-timeline-seg' style='width:{pct:.2f}%; background:{grad};' "
            f"title='Scene {i + 1}: {title_esc} ({sc.duration:.1f}s) - {status}'>"
            f"<span>{icon} #{i + 1}</span>"
            f"<span style='font-size:0.64rem; opacity:0.9;'>{sc.duration:.0f}s</span>"
            f"</div>"
        )
    st.markdown(f"<div class='tf-timeline-bar'>{' '.join(segments_html)}</div>", unsafe_allow_html=True)

    # Scale button if there is a duration difference
    t_btn1, t_btn2 = st.columns([2, 1])
    if t_btn1.button(f"🎯 Auto-Scale All Scenes to Match Target Duration ({_fmt_time(tgt_total)})", disabled=running or not plan.scenes):
        if tot_scenes_dur > 0:
            scale = tgt_total / tot_scenes_dur
            for sc in plan.scenes:
                sc.duration = round(max(2.0, min(30.0, sc.duration * scale)), 1)
            p.set_plan(plan)
            p.save()
            _flash(f"All {len(plan.scenes)} scenes scaled to match {_fmt_time(tgt_total)}")
            st.rerun()

    thumbs = st.columns(min(10, max(1, len(plan.scenes))))
    for i, sc in enumerate(plan.scenes[:10]):
        img = _scene_thumb(p, sc)
        with thumbs[i]:
            if img:
                st.image(img, width="stretch")
            st.caption(f"{i + 1}. {sc.title[:16]}")

    import pandas as pd
    rows = []
    for i, sc in enumerate(plan.scenes):
        video_source = sc.video_source if sc.video_source in VIDEO_SOURCES else "original_motion"
        motion_preset = sc.motion_preset if sc.motion_preset in jobs.MOTION_PRESETS else "dolly_in"
        rows.append({"#": i + 1, "Title": sc.title, "Source": sc.source, "Video engine": video_source,
                     "Motion preset": motion_preset, "Min sec": sc.duration,
                     "Camera": sc.camera_move, "Shot": sc.shot_type, "Transition": sc.transition,
                     "Atmosphere": sc.atmosphere, "Music cue": sc.music_cue or "", "SFX": sc.sfx, "Ambience": sc.ambience,
                     "Status": p.state(sc.id).get("status", "pending")})
    df = pd.DataFrame(rows)
    cfg = {
        "#": st.column_config.NumberColumn(disabled=True, width="small"),
        "Source": st.column_config.SelectboxColumn(options=list(SOURCES)),
        "Video engine": st.column_config.SelectboxColumn(options=list(VIDEO_SOURCES)),
        "Motion preset": st.column_config.SelectboxColumn(options=list(jobs.MOTION_PRESETS)),
        "Min sec": st.column_config.NumberColumn(min_value=2.0, max_value=30.0, step=0.5),
        "Camera": st.column_config.SelectboxColumn(options=list(cinema.CAMERA_MOVES)),
        "Shot": st.column_config.SelectboxColumn(options=cinema.SHOT_TYPES),
        "Transition": st.column_config.SelectboxColumn(options=list(cinema.TRANSITIONS)),
        "Atmosphere": st.column_config.SelectboxColumn(options=list(cinema.ATMOSPHERES)),
        "Music cue": st.column_config.SelectboxColumn(options=["", "calm", "build", "tension", "climax", "resolve", "silence"]),
        "SFX": st.column_config.SelectboxColumn(options=cinema.SFX),
        "Ambience": st.column_config.SelectboxColumn(options=cinema.AMBIENCES),
        "Status": st.column_config.TextColumn(disabled=True),
    }
    edited = st.data_editor(df, column_config=cfg, hide_index=True, width="stretch",
                            key=f"tf_{p.id}_tl_{p.data.get('rev', 0)}", disabled=running)
    if st.button("💾 Apply timeline edits", type="primary", disabled=running):
        for i, row in edited.iterrows():
            sc = plan.scenes[i]
            sc.title, sc.source, sc.duration = str(row["Title"]), str(row["Source"]), float(row["Min sec"])
            sc.video_source, sc.motion_preset = str(row["Video engine"]), str(row["Motion preset"])
            sc.camera_move, sc.shot_type, sc.transition = row["Camera"], row["Shot"], row["Transition"]
            sc.atmosphere, sc.music_cue, sc.sfx, sc.ambience = row["Atmosphere"], row["Music cue"] or "", row["SFX"], row["Ambience"]
        p.set_plan(plan)
        p.save()
        _flash("Timeline updated")
        st.rerun()

    st.markdown("#### 📺 Channel Branding & Watermark")
    b1, b2, b3 = st.columns(3)
    with b1:
        setting(p, "channel_name", "text", "Channel Name", placeholder="e.g. theMedNurses")
    with b2:
        setting(p, "brand_position", "select", "Watermark Position", options=BRAND_POSITIONS)
    with b3:
        setting(p, "brand_overlay", "toggle", "Burn Watermark on Video")
    if p.settings.get("channel_name") or p.settings.get("channel_logo_path"):
        _display_brand_badge(p.settings.get("channel_name", ""), p.settings.get("channel_logo_path"))

    st.markdown("#### 🎨 Cinematic look (whole film)")
    c1, c2, c3 = st.columns(3)
    with c1:
        setting(p, "grade", "select", "Colour grade", options=[""] + list(cinema.GRADES),
                format_func=lambda g: g or f"Style default ({cinema.STYLES.get(p.settings['style'], {}).get('grade')})")
        setting(p, "atmosphere", "select", "Atmosphere", options=["auto"] + list(cinema.ATMOSPHERES),
                format_func=lambda a: "Per scene (AI)" if a == "auto" else cinema.ATMOSPHERES[a])
        setting(p, "atmosphere_intensity", "slider", "Atmosphere intensity", min_value=0.0, max_value=2.0, step=0.1)
        setting(p, "camera_intensity", "slider", "Camera motion intensity", min_value=0.3, max_value=2.0, step=0.1)
    with c2:
        setting(p, "grain", "slider", "Film grain", min_value=0.0, max_value=1.0, step=0.05)
        setting(p, "bloom", "slider", "Bloom / glow", min_value=0.0, max_value=1.0, step=0.05)
        setting(p, "vignette", "slider", "Vignette", min_value=0.0, max_value=1.0, step=0.05)
        setting(p, "dof", "slider", "Depth of field (edge blur)", min_value=0.0, max_value=1.0, step=0.05)
        setting(p, "halation", "slider", "Film halation (warm highlight glow)", min_value=0.0, max_value=1.0, step=0.05,
                help="The red-orange glow film stock shows around bright lights. 0.2–0.35 looks like 35 mm.")
    with c3:
        setting(p, "chroma", "slider", "Chromatic aberration", min_value=0.0, max_value=1.0, step=0.05)
        setting(p, "sharpen", "slider", "Sharpen", min_value=0.0, max_value=1.0, step=0.05)
        setting(p, "letterbox", "toggle", "Cinematic letterbox (2.39:1 bars)")
        setting(p, "title_card", "toggle", "Opening title card")
        setting(p, "gate_weave", "toggle", "Film gate weave", help="Tiny organic frame drift like a film projector.")
        setting(p, "resample", "select", "Camera resampling", options=["bilinear", "bicubic"],
                help="Bicubic is slightly sharper on strong zooms but ~2.5× slower to render.")
    st.markdown("#### 💬 Subtitles & transitions")
    d1, d2, d3 = st.columns(3)
    with d1:
        setting(p, "subtitle_style", "select", "Burned-in subtitles", options=list(cinema.SUBTITLE_STYLES),
                help=" · ".join(f"{k}: {v}" for k, v in cinema.SUBTITLE_STYLES.items()))
    with d2:
        setting(p, "caption_words", "slider", "Words per caption", min_value=3, max_value=12, step=1)
    with d3:
        setting(p, "transition_override", "select", "Transitions", options=[""] + list(cinema.TRANSITIONS),
                format_func=lambda t: t or "Per scene (AI)")
    setting(p, "kinetic_captions", "toggle", "Burn in kinetic word-highlight captions",
            help="Uses word-level transcription, 2–4 word context lines, and the channel accent color.")
    if p.settings.get("kinetic_captions", False):
        setting(p, "caption_preset", "select", "Viral caption preset",
                options=["Hormozi Bounce", "Clean Documentary", "Karaoke Glow",
                         "Minimalist White", "Shorts Pop"])
        color = st.color_picker("Kinetic caption highlight color", p.settings.get("caption_color", "#22D3EE"),
                                key=f"tf_{p.id}_caption_color")
        if color != p.settings.get("caption_color"):
            p.settings["caption_color"] = color
            p.save()


def page_audio() -> None:
    p = _project()
    if not p:
        st.info("Create a project first.")
        return
    running = _running(p)
    s = p.settings
    st.markdown("#### 🎙️ Narrator")
    c1, c2 = st.columns(2)
    with c1:
        setting(p, "language", "select", "Language", options=list(tts.EDGE_VOICES))
        if s["language"] == "English":
            engine_options = ["kokoro", "groq_orpheus", "sarvam", "edge"]
        elif s["language"] == "Hindi":
            engine_options = ["sarvam", "edge"]
        else:
            engine_options = ["edge"]
        engine_key = f"tf_{p.id}_voice_engine"
        engine = s.get("voice_engine", "kokoro")
        if engine not in engine_options:
            engine = engine_options[0]
            st.session_state[engine_key] = engine
        eng = st.selectbox(
            "Production voice engine",
            engine_options,
            index=engine_options.index(engine),
            format_func=lambda key: tts.TTS_ENGINES[key],
            key=engine_key,
            disabled=running,
        )
        if eng != s.get("voice_engine"):
            p.settings["voice_engine"] = eng
            p.save()
        st.caption("Kokoro runs offline and is the default English production voice. Edge is labeled as a fast draft preview.")
        opts = tts.voice_options(s["language"], eng)
        labels = dict(opts)
        if opts:
            voice_ids = [v for v, _ in opts]
            voice = s.get("voice") if s.get("voice") in voice_ids else voice_ids[0]
            selected_voice = st.selectbox(
                "Voice",
                voice_ids,
                index=voice_ids.index(voice),
                format_func=lambda v: labels.get(v, v),
                key=f"tf_{p.id}_voice_{eng}",
                disabled=running,
            )
            if selected_voice != s.get("voice"):
                p.settings["voice"] = selected_voice
                p.save()
        else:
            st.warning("No production voices are available for this language. Select Edge TTS (Fast Draft Preview).")
    with c2:
        setting(p, "voice_speed", "slider", "Speed", min_value=0.7, max_value=1.4, step=0.05)
        if eng == "edge":
            setting(p, "voice_pitch", "slider", "Pitch (Hz)", min_value=-30, max_value=30, step=1)
        setting(p, "dialogue_voices", "toggle", "Distinct voices for character dialogue")
        if st.button("▶ Preview narrator", disabled=running):
            with st.spinner("Synthesising…"):
                try:
                    st.session_state["tf_prev_narrator"] = tts.sample(
                        s.get("voice", "af_bella"), s["language"], eng, s["voice_speed"]
                    )
                except Exception as e:  # noqa: BLE001
                    st.session_state.pop("tf_prev_narrator", None)
                    st.error(f"Voice preview failed: {e}")
        if st.session_state.get("tf_prev_narrator"):
            st.audio(st.session_state["tf_prev_narrator"], format="audio/mp3")
        if st.button("▶ Fast draft preview (Edge TTS)", disabled=running):
            try:
                draft_voice = tts.voice_options(s["language"], "edge")[0][0]
                st.session_state["tf_prev_draft"] = tts.sample(
                    draft_voice, s["language"], "edge", s["voice_speed"]
                )
            except Exception as e:  # noqa: BLE001
                st.session_state.pop("tf_prev_draft", None)
                st.error(f"Draft preview failed: {e}")
        if st.session_state.get("tf_prev_draft"):
            st.audio(st.session_state["tf_prev_draft"], format="audio/mp3")
        if eng == "sarvam":
            st.caption("Sarvam uses the SARVAM_API_KEY server environment variable and supports Hindi/Indian English code-switching.")
            if not config.sarvam_key():
                st.warning("SARVAM_API_KEY is not configured in the server environment.")
        if eng == "groq_orpheus" and not config.groq_key():
            st.warning("GROQ_API_KEY is not configured; Groq Orpheus cannot be used until it is set.")
    st.markdown("#### 🎙️ Import External Voiceover (WAV/MP3)")
    voiceover_upload = st.file_uploader(
        "Import External Voiceover (WAV/MP3)",
        type=["wav", "mp3"],
        key=f"tf_{p.id}_voiceover_upload",
        help="Uses your imported narration in the final mix instead of synthesizing scene voices.",
    )
    if voiceover_upload is not None and st.button("Use imported voiceover", disabled=running):
        try:
            p.settings["external_voiceover_path"] = _save_upload(
                p, voiceover_upload, "audio", "external_voiceover"
            )
            p.save()
            try:
                aligned = alignment.align_project_audio(
                    Path(p.settings["external_voiceover_path"]), p.dir,
                    language={"English": "en", "Hindi": "hi"}.get(s.get("language", "English")),
                    log=p.log, force=True, model_size=str(s.get("alignment_model", "tiny")),
                )
                _flash(f"Word alignment saved ({len(aligned)} words)")
            except Exception as align_error:
                _flash(f"Voiceover imported, but alignment failed: {align_error}", "error")
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            st.error(f"Could not import voiceover: {exc}")
    setting(p, "alignment_model", "select", "Local Whisper fallback model",
            options=["tiny", "base", "small"],
            help="Runs on CPU with int8 quantization; larger models improve accuracy but take more time and memory.")
    external_path = s.get("external_voiceover_path", "")
    if external_path:
        st.caption("Imported narration replaces generated voices. Extend the project timeline if needed; longer audio is not silently truncated.")
        if Path(external_path).exists():
            st.caption(f"Imported narration: {Path(external_path).name}")
            st.audio(external_path)
            if st.button("Rebuild word alignment", disabled=running):
                try:
                    aligned = alignment.align_project_audio(
                        Path(external_path), p.dir,
                        language={"English": "en", "Hindi": "hi"}.get(s.get("language", "English")),
                        log=p.log, force=True, model_size=str(s.get("alignment_model", "tiny")),
                    )
                    st.success(f"Saved word timestamps for {len(aligned)} words.")
                except Exception as exc:
                    st.error(f"Could not align imported voiceover: {exc}")
        else:
            st.error(f"Imported voiceover file is missing: {external_path}")
        if st.button("Remove imported voiceover", disabled=running):
            p.settings["external_voiceover_path"] = ""
            p.save()
            st.rerun()
    plan = p.plan
    script = "\n\n".join(
        line
        for scene in (plan.scenes if plan else [])
        for line in ([scene.narration.strip()] + [dialogue.line.strip() for dialogue in scene.dialogue])
        if line
    )
    if script:
        words = script.split()
        script_blocks = "\n\n".join(
            f"Chunk {index + 1}\n" + " ".join(words[start:start + 200])
            for index, start in enumerate(range(0, len(words), 200))
        )
        with st.expander("📋 Copy Chunked Script (200-word blocks)"):
            st.caption("Use the code block's copy button, then generate each block in your free external voice tool.")
            st.code(script_blocks, language=None, height=260)
    st.markdown("#### 🎼 Soundtrack")
    m1, m2 = st.columns(2)
    with m1:
        setting(p, "music_mood", "select", "Music mood", options=["auto"] + cinema.MUSIC_MOODS,
                format_func=lambda m: f"Auto ({plan.music_mood if plan else 'from story'})" if m == "auto" else m.title())
        up = st.file_uploader("…or upload your own music (mp3/wav/m4a, looped and ducked)", type=["mp3", "wav", "m4a", "ogg"],
                              key=f"tf_{p.id}_music_up")
        if up is not None and st.button("Use uploaded music", disabled=running):
            p.settings["music_path"] = _save_upload(p, up, "audio", "music")
            p.save()
            st.rerun()
        if s.get("music_path"):
            st.caption(f"Using uploaded track: {Path(s['music_path']).name}")
            st.audio(s["music_path"])
            if st.button("Remove uploaded music", disabled=running):
                p.settings["music_path"] = ""
                p.save()
                st.rerun()
        if st.button("🎼 Generate a different score", disabled=running):
            with p.lock:
                p.settings["music_variation"] = int(p.settings.get("music_variation", 0) or 0) + 1
            p.save()
            st.session_state.pop("tf_prev_score", None)
            st.rerun()
        if st.button("▶ Preview generated score", disabled=running):
            mood = s["music_mood"] if s["music_mood"] not in ("auto", "none") else (plan.music_mood if plan else "epic")
            seed = jobs._music_seed(p.id, int(s.get("music_variation", 0)))
            with st.spinner(f"Composing a {mood} score…"):
                out = config.CACHE_DIR / "audio" / f"preview_{mood}_{seed}.wav"
                audio.build_music(out, 24.0, mood, [(0, 8, "calm"), (8, 16, "build"), (16, 24, "climax")],
                                  seed=seed)
                st.session_state["tf_prev_score"] = str(out)
        if st.session_state.get("tf_prev_score") and Path(st.session_state["tf_prev_score"]).exists():
            st.audio(st.session_state["tf_prev_score"])
        st.caption("The score is composed locally: layered pads, chord voicings, arpeggios and mood-specific percussion "
                   "follow each scene's cue. Each project gets its own variation; use “Generate a different score” to "
                   "change key, tempo and chord order. For orchestral or higher-fidelity music, upload a licensed track.")
    with m2:
        setting(p, "voice_vol", "slider", "Voice volume", min_value=0.2, max_value=2.0, step=0.05)
        setting(p, "music_vol", "slider", "Music volume", min_value=0.0, max_value=1.0, step=0.05)
        setting(p, "fx_vol", "slider", "Ambience & SFX volume", min_value=0.0, max_value=1.0, step=0.05)
        setting(p, "duck", "slider", "Duck music under voice", min_value=0.0, max_value=1.0, step=0.05)
        setting(p, "duck_db", "slider", "Music below narration (dB)", min_value=-26.0, max_value=-22.0, step=0.5)
        setting(p, "loudness", "slider", "Loudness target (LUFS)", min_value=-23.0, max_value=-9.0, step=0.5)
        setting(p, "ambience_on", "toggle", "Scene ambience (rain, city, wind…)")
        setting(p, "sfx_on", "toggle", "Scene sound effects (whoosh, impact…)")
    st.markdown("#### 🔈 Sound library preview")
    a1, a2 = st.columns(2)
    amb = a1.selectbox("Ambience", [a for a in cinema.AMBIENCES if a != "none"], key="tf_amb_prev")
    if a1.button("▶ Play ambience"):
        st.session_state["tf_prev_amb"] = str(audio.ambience_loop(amb))
    if st.session_state.get("tf_prev_amb"):
        a1.audio(st.session_state["tf_prev_amb"])
    fx = a2.selectbox("Sound effect", [x for x in cinema.SFX if x != "none"], key="tf_fx_prev")
    if a2.button("▶ Play effect"):
        st.session_state["tf_prev_fx"] = str(audio.sfx(fx))
    if st.session_state.get("tf_prev_fx"):
        a2.audio(st.session_state["tf_prev_fx"])


def page_render() -> None:
    p = _project()
    plan = p.plan if p else None
    if not p:
        st.info("Create a project first.")
        return
    running = _running(p)
    s = p.settings
    out = p.data.get("outputs") or {}
    _visual_qa_panel(p)
    audit = jobs.monetization_audit(p)
    with st.expander("✅ Pre-render Monetization & Quality Audit", expanded=not audit["passed"]):
        st.caption("Non-blocking readiness checks. AI disclosure helper: " + audit["ai_disclosure"])
        st.dataframe([{"Check": row["name"], "Status": "✅ ready" if row["passed"] else "⚠ review",
                       "Details": row["detail"]} for row in audit["checks"]],
                     hide_index=True, width="stretch")
        st.caption("Audio measurements are configuration targets here; verify the final file with loudness analysis "
                   "before publishing.")
    if out.get("video") and Path(out["video"]).exists():
        st.markdown(f"### 🎉 {_esc(plan.title if plan else 'Your film')}")
        v1, v2 = st.columns([3, 1])
        v1.video(out["video"])
        with v2:
            st.metric("Duration", _fmt_time(out.get("duration", 0)))
            st.caption(f"{out.get('width')}×{out.get('height')} · {s['fps']} fps · {s['format']}")
            if out.get("thumbnail") and Path(out["thumbnail"]).exists():
                st.image(out["thumbnail"], caption="Thumbnail")
        if out.get("short") and Path(out["short"]).exists():
            with st.expander("📱 Shorts / Reels teaser (9:16)", expanded=False):
                sc1, sc2 = st.columns([1, 2])
                sc1.video(out["short"])
                with open(out["short"], "rb") as fh:
                    sc2.download_button("⬇️ Download teaser", fh, file_name=Path(out["short"]).name, mime="video/mp4",
                                        key="tf_dl_short")
                sc2.caption("Post it as a Short/Reel/TikTok and link the full film.")
        extra = [t for t in (out.get("thumbnails") or [])[1:] if Path(t).exists()]
        if extra:
            with st.expander(f"🖼️ Thumbnail variants ({len(extra) + 1})", expanded=False):
                tc = st.columns(len(extra) + 1)
                for ti, tpath in enumerate([out["thumbnail"]] + extra):
                    tc[ti].image(tpath)
                    with open(tpath, "rb") as fh:
                        tc[ti].download_button("⬇️", fh, file_name=Path(tpath).name, mime="image/jpeg",
                                               key=f"tf_dl_thumb_{ti}", width="stretch")
        png_thumbnails = [path for path in out.get("thumbnails_png", []) if Path(path).is_file()]
        if png_thumbnails:
            with st.expander(f"🧩 A/B hook thumbnails (PNG · {len(png_thumbnails)} variants)", expanded=False):
                thumb_cols = st.columns(len(png_thumbnails))
                for index, path in enumerate(png_thumbnails):
                    thumb_cols[index].image(path)
                    with open(path, "rb") as png_file:
                        thumb_cols[index].download_button(
                            "Download PNG", png_file, file_name=Path(path).name, mime="image/png",
                            key=f"tf_dl_thumb_png_{index}",
                        )
        d = st.columns(6)
        for col, key, lab, mime in ((d[0], "video", "⬇️ Video", "video/mp4"), (d[1], "srt", "⬇️ Subtitles (SRT)", "text/plain"),
                                    (d[2], "vtt", "⬇️ Subtitles (VTT)", "text/vtt"),
                                    (d[3], "thumbnail", "⬇️ Thumbnail", "image/jpeg"),
                                    (d[4], "metadata", "⬇️ YouTube metadata", "text/plain")):
            f = out.get(key)
            if f and Path(f).exists():
                with open(f, "rb") as fh:
                    col.download_button(lab, fh, file_name=Path(f).name, mime=mime, width="stretch",
                                        key=f"tf_dl_{key}")
        if d[5].button("📦 Build project package", width="stretch", disabled=running):
            with st.spinner("Zipping film, subtitles, plan and every scene asset…"):
                st.session_state[f"tf_zip_{p.id}"] = str(jobs.package_zip(p))
        zp = st.session_state.get(f"tf_zip_{p.id}")
        if zp and Path(zp).exists():
            with open(zp, "rb") as fh:
                st.download_button("⬇️ Download project package (.zip)", fh, file_name=Path(zp).name,
                                   mime="application/zip", key="tf_dl_zip")
        if out.get("description") or (plan and (plan.youtube_title or plan.youtube_description)):
            with st.expander("📺 YouTube Video Title, Description & Tags (Ready to Copy)", expanded=True):
                if plan and (plan.youtube_title or plan.title):
                    st.markdown("**Suggested YouTube Title:**")
                    st.code(plan.youtube_title or plan.title, language=None)
                if out.get("description"):
                    st.markdown("**YouTube Description (with Chapters & Hashtags):**")
                    st.code(out["description"], language=None)
                elif plan and plan.youtube_description:
                    st.markdown("**YouTube Description:**")
                    st.code(plan.youtube_description, language=None)
                if plan and plan.tags:
                    st.markdown("**YouTube Tags:**")
                    st.code(", ".join(plan.tags), language=None)
                if out.get("pinned_comment"):
                    st.markdown("**Pinned Comment:**")
                    st.code(out["pinned_comment"], language=None)
        if out.get("video") and Path(out["video"]).is_file():
            with st.expander("YouTube upload metadata"):
                setting(p, "youtube_category_id", "text", "YouTube category ID", placeholder="27")
                setting(p, "youtube_default_language", "text", "Default language (ISO 639-1)",
                        placeholder="en")
                setting(p, "youtube_contains_synthetic_media", "toggle",
                        "Contains altered or synthetic media (YouTube disclosure)")
                setting(p, "youtube_publish_at", "text",
                        "Optional schedule time (ISO 8601; private until then)", placeholder="2027-01-01T00:00:00Z")
                setting(p, "youtube_disclosure_acknowledged", "toggle",
                        "I reviewed YouTube's synthetic-media disclosure requirements")
                setting(p, "youtube_playlist_title", "text", "Optional YouTube playlist title")
            if st.button("Upload as PRIVATE YouTube draft", disabled=running,
                         key=f"tf_{p.id}_youtube_upload"):
                if s.get("youtube_contains_synthetic_media") and not s.get("youtube_disclosure_acknowledged"):
                    st.error("Synthetic-media disclosure must be acknowledged before YouTube upload.")
                    return
                client = Path(config.youtube_client_secret_path())
                token = config.CACHE_DIR / "youtube" / "token.json"
                try:
                    video_id = youtube_upload.upload_private_draft(
                        Path(out["video"]), client, token,
                        title=(plan.youtube_title or plan.title) if plan else "TubeForge video",
                        description=out.get("description", ""),
                        tags=plan.tags if plan else [],
                        thumbnail=Path(out["thumbnail"]) if out.get("thumbnail") else None,
                        captions_srt=Path(out["srt"]) if out.get("srt") else None,
                        caption_language={"Hindi": "hi", "English": "en"}.get(s.get("language", "English"), "en"),
                        category_id=s.get("youtube_category_id", "27"),
                        default_language=s.get("youtube_default_language", ""),
                        contains_synthetic_media=bool(s.get("youtube_contains_synthetic_media", False)),
                        publish_at=s.get("youtube_publish_at", ""),
                    )
                    st.success(f"Uploaded as private draft: https://www.youtube.com/watch?v={video_id}")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Private YouTube upload failed: {exc}")
        st.divider()
    _autoclip_panel(p, running)
    st.markdown("#### 📐 Export settings")
    c1, c2, c3 = st.columns(3)
    with c1:
        setting(p, "aspect", "select", "Format / aspect", options=list(cinema.ASPECTS))
        setting(p, "resolution", "select", "Resolution", options=list(cinema.RESOLUTIONS))
    with c2:
        setting(p, "fps", "select", "Frame rate", options=cinema.FPS_OPTIONS)
        setting(p, "format", "select", "File format", options=list(cinema.FORMATS))
    with c3:
        setting(p, "quality", "select", "Quality", options=list(cinema.QUALITY))
        setting(p, "workers", "slider", "Parallel scenes", min_value=1, max_value=6, step=1,
                help="Scenes generated/rendered at the same time. Higher is faster on strong CPUs.")
    f1, f2 = st.columns(2)
    with f1:
        setting(p, "fade_in", "slider", "Fade in from black (s)", min_value=0.0, max_value=3.0, step=0.1)
    with f2:
        setting(p, "fade_out", "slider", "Fade out to black (s)", min_value=0.0, max_value=4.0, step=0.1)
    st.markdown("#### 🚀 Engagement extras")
    e1, e2, e3 = st.columns(3)
    with e1:
        setting(p, "end_card", "toggle", "Animated end card (title + subscribe)",
                help="A closing card with the film title, your channel badge, a Subscribe button and a call to action.")
        if s.get("end_card", True):
            setting(p, "end_card_len", "slider", "End card length (s)", min_value=3.0, max_value=10.0, step=0.5)
            setting(p, "end_card_cta", "text", "End card call to action",
                    placeholder="Like • Share • Comment what happens next")
    with e2:
        setting(p, "shorts_teaser", "toggle", "Also export a 9:16 Shorts / Reels teaser",
                help="Landscape films only: a vertical teaser with a hook title and a 'Full film' button.")
        if s.get("shorts_teaser", True):
            setting(p, "teaser_len", "slider", "Teaser length (s)", min_value=10.0, max_value=59.0, step=1.0)
    with e3:
        setting(p, "transition_sfx", "toggle", "Whoosh on fast transitions",
                help="Adds a whoosh under whip pans, slides, zooms and flashes.")
        setting(p, "thumb_variants", "toggle", "Extra thumbnail variants (A/B test)")
    W, H, (FW, FH) = jobs.render_size(s)
    st.caption(f"Renders at {W}×{H}" + (f", upscaled to {FW}×{FH} with Lanczos + sharpening at export" if (W, H) != (FW, FH)
                                        else "") + ". 4K is an upscale from 1440p; 60 fps changes the output cadence but "
                                                "does not invent new source motion.")
    st.markdown("#### 🖼️ Visual generation")
    e1, e2 = st.columns(2)
    with e1:
        engines = ["auto"] + images.AUTO_ORDER
        if images.engine_available("cf_klein4b")[0]:
            engines.append("cf_klein4b")
        if config.pollinations_key():
            engines.append("pollinations_key")

        def elabel(k):
            if k == "auto":
                return "Auto (best available free engine, with fallbacks)"
            ok, why = images.engine_available(k)
            e = images.ENGINES[k]
            return f"{e.label} · {e.tier}" + ("" if ok else " · unavailable")
        setting(p, "image_engine", "select", "Image engine", options=engines, format_func=elabel)
        if s.get("image_engine") == "cf_klein4b":
            st.warning("FLUX.2 Klein is explicit opt-in for bulk scenes (~1,364 estimated neurons per 1 MP image); "
                       "daily neuron usage is locally budget-guarded.")
        if config.pollinations_key():
            st.caption("Authenticated Pollinations is available as an explicit engine; API-key usage may consume account balance. "
                       "It is not used automatically.")
        setting(p, "allow_stock", "toggle", "Allow stock photos as last-resort fallback (Pexels / Pixabay)")
        setting(p, "negative_global", "text", "Global negative prompt")
    with e2:
        st.markdown("**🎥 Free original motion**")
        st.caption("Each scene gets an original generated frame, then local camera movement, simulated parallax and "
                   "atmosphere. Pexels and Pixabay footage are optional.")
    st.markdown("#### 🎬 Paid / hosted video controls")
    setting(p, "allow_paid_video", "toggle",
            "Enable OpenRouter paid AI video (explicitly opt in; hero scenes only)")
    setting(p, "paid_video_provider", "select", "Paid video provider",
            options=["openrouter"], format_func=lambda k: "OpenRouter (hosted video)")
    setting(p, "ai_video_model", "text", "OpenRouter video model ID")
    setting(p, "ai_video_res", "select", "Estimated resolution",
            options=["360p", "480p", "720p", "768p", "1080p", "2K", "4K"])
    setting(p, "paid_video_budget_usd", "number", "Per-project paid-video budget (USD)",
            min_value=0.0, max_value=10000.0, step=0.25)
    eligible_scenes = [sc for sc in (plan.scenes if plan else [])
                       if sc.is_hero_scene and sc.source == "ai_video"]
    estimate_count = len(eligible_scenes)
    estimated_costs = []
    if s.get("allow_paid_video") and s.get("ai_video_model"):
        model = video.model_info(s["ai_video_model"])
        if model:
            aspect = "9:16" if "9:16" in str(s.get("aspect", "")) else "16:9"
            for scene in eligible_scenes:
                params = video.pick_params(model, scene.duration, s.get("ai_video_res", "720p"), aspect)
                cost = video.estimate_cost(model, params["duration"], params["resolution"])
                if cost is not None:
                    estimated_costs.append(cost)
    estimate_usd = round(sum(estimated_costs), 3) if len(estimated_costs) == estimate_count else None
    m1, m2, m3 = st.columns(3)
    m1.metric("Eligible hero clips", estimate_count)
    m2.metric("Visible estimate", f"${estimate_usd:.3f}" if estimate_usd is not None else "Unknown")
    m3.metric("Budget remaining",
              f"${max(0.0, float(s.get('paid_video_budget_usd', 0.0)) - float(s.get('paid_video_spent_usd', 0.0))):.3f}")
    if estimate_count and estimate_usd is None:
        st.warning("No request will run until pricing can be estimated from the selected model.")
    if estimate_usd is not None and estimate_usd > float(s.get("paid_video_budget_usd", 0.0)):
        st.error("Visible estimate exceeds the per-project budget; all paid calls are blocked.")
    st.markdown("**ComfyUI & manual Flow**")
    setting(p, "comfyui_url", "text", "ComfyUI base URL", placeholder="http://127.0.0.1:8188")
    setting(p, "comfyui_workflow_json", "text", "ComfyUI exported API workflow JSON path")
    setting(p, "allow_remote_comfy", "toggle",
            "Enable remote ComfyUI tunnel (explicit opt-in; no cost check is invented)")
    setting(p, "strict_loudness_gate", "toggle",
            "Block export unless post-render ebur128 loudness is -15 to -13 LUFS")
    setting(p, "license_unknown_acknowledged", "toggle",
            "Acknowledge and permit export of assets without a complete license record")
    if st.button("Run production preflight", disabled=running, key=f"tf_{p.id}_preflight"):
        st.session_state[f"tf_{p.id}_preflight_result"] = render.preflight_check(
            str(s.get("comfyui_url", "")), str(s.get("comfyui_workflow_json", "")))
    preflight = st.session_state.get(f"tf_{p.id}_preflight_result")
    if preflight:
        if preflight["passed"]:
            st.success("Required local production checks passed.")
        else:
            st.warning("One or more required production checks failed; local motion fallback remains available.")
        st.dataframe(
            [{"Check": name, "Passed": passed} for name, passed in preflight["checks"].items()],
            hide_index=True, width="stretch",
        )
        st.caption(
            f"GPU: {preflight['gpu']['name'] or 'not detected'} "
            f"({preflight['gpu']['vram_mb'] or 'unknown'} MiB VRAM); "
            f"OpenGL: {'available' if preflight['checks']['opengl'] else 'unavailable'}; "
            f"ComfyUI: {preflight['comfyui_detail']}."
        )
    st.markdown("#### 🎞️ Local scene motion engines")
    st.caption("Procedural graphics run as isolated Remotion scenes, then join the existing Timeline assembly and receive "
               "the usual captions, transitions, audio and export treatment.")
    st.markdown("**Procedural motion-graphic brand palette**")
    brand_cols = st.columns(3)
    brand_values = [
        brand_cols[0].color_picker("Primary", s.get("brand_primary_color", "#8B5CF6"),
                                   key=f"tf_{p.id}_proc_brand_primary"),
        brand_cols[1].color_picker("Secondary", s.get("brand_secondary_color", "#22D3EE"),
                                   key=f"tf_{p.id}_proc_brand_secondary"),
        brand_cols[2].color_picker("Accent", s.get("brand_accent_color", "#F472B6"),
                                   key=f"tf_{p.id}_proc_brand_accent"),
    ]
    brand_keys = ("brand_primary_color", "brand_secondary_color", "brand_accent_color")
    if any(s.get(key) != value for key, value in zip(brand_keys, brand_values)):
        for key, value in zip(brand_keys, brand_values):
            s[key] = value
        p.save()
    st.caption("These project colors and the Channel Name are passed dynamically to every procedural template.")
    engine_col, apply_col = st.columns([3, 1])
    with engine_col:
        default_video_source = setting(
            p, "default_video_source", "select", "Default videoSource for new scenes",
            options=list(VIDEO_SOURCES), format_func=lambda key: VIDEO_SOURCES[key],
        )
        setting(
            p, "ai_video_fallback_source", "select", "Fallback when an AI-video scene cannot run",
            options=["original_motion", "ai_image"],
            format_func=lambda key: {"original_motion": "Local FFmpeg motion", "ai_image": "AI still + motion"}[key],
        )
    with apply_col:
        if st.button("Apply to all scenes", disabled=running or not plan, width="stretch"):
            for scene in plan.scenes:
                scene.video_source = default_video_source
            p.set_plan(plan)
            p.save()
            _flash(f"Applied {VIDEO_SOURCES[default_video_source]} to all scenes")
            st.rerun()
    st.caption("DepthFlow, cutout and Remotion dependencies are optional. If a selected engine is unavailable, the built-in "
               "FFmpeg motion renderer is used and the warning is recorded in Providers.")
    st.markdown("#### 🚀 Produce")
    if not plan:
        st.info("Generate a plan first (✨ Create).")
        return
    b1, b2, b3 = st.columns(3)
    if b1.button("🖼️ Prepare visuals + voices only", width="stretch", disabled=running):
        jobs.MANAGER.start_render(p, assets_only=True)
        st.session_state[f"tf_was_running_{p.id}"] = True
        st.rerun()
    if b2.button("🎬 Render final film", type="primary", width="stretch", disabled=running):
        jobs.MANAGER.start_render(p)
        st.session_state[f"tf_was_running_{p.id}"] = True
        st.rerun()
    if b3.button("⏹ Cancel job", width="stretch", disabled=not running):
        jobs.MANAGER.cancel(p.id)
        st.toast("Cancelling…")
    st.caption("Rendering is incremental: finished scenes are reused, and only scenes whose prompts, voice or look "
               "changed are regenerated. Jobs keep running if you switch pages; after an app restart, press Resume.")
    rows = []
    for i, sc in enumerate(plan.scenes):
        x = p.state(sc.id)
        rows.append({"#": i + 1, "Scene": sc.title, "Status": f"{STATUS_ICON.get(x.get('status', 'pending'), '')} "
                     f"{x.get('status', 'pending')}", "Visual": x.get("image_engine") or ("stock" if x.get("stock") else ""),
                     "Length": f"{x['clip_len']:.1f}s" if x.get("clip_len") else "", "Error": (x.get("error") or "")[:120]})
    st.dataframe(rows, hide_index=True, width="stretch")
    with st.expander("Full project log"):
        st.markdown("<div class='tf-log'>" + "<br>".join(_esc(x) for x in reversed(p.data.get("logs", []))) + "</div>",
                    unsafe_allow_html=True)


def page_providers() -> None:
    conf = config.configured_providers()
    breakers = http.breaker_status()
    usage = cache.usage_today()
    st.markdown("#### 🔌 Providers")
    rows = []
    roles = {"groq": "Groq LLM + Orpheus TTS (TTS app cap: 10 RPM / 100 requests per UTC day)",
             "sarvam": "Optional Hindi / Hinglish Bulbul v4 Flash TTS",
             "openrouter": "Free :free LLM models with rate-limit rotation",
             "cloudflare": "FLUX.1 Schnell bulk stills; FLUX.2 Klein reserved for Visual Bible / opt-in images",
             "pixabay": "Free stock video & photos", "pexels": "Stock video & photos (200 req/hr, 20k/month)",
             "gemini": "Gemini 3.5 Flash-Lite (local cap 500/day) + 3.8 Flash (20/day)",
             "pollinations": "Authenticated image endpoint; explicit API-key use may consume account balance"}
    for k, ok in conf.items():
        blocked = [f"{b}: {r[:90]}" for b, r in breakers.items() if b.split(":")[0].startswith(k.split(" ")[0])]
        rows.append({"Provider": k, "Key": "✅ configured" if ok else "— missing", "Role": roles.get(k, ""),
                     "Status": ("⏸ " + " | ".join(blocked)) if blocked else ("ready" if ok else "")})
    st.dataframe(rows, hide_index=True, width="stretch")
    st.caption("Keys are read from `.env` / environment on the server and never sent to the browser. Free-provider "
               "limits are tracked locally; rate limits are not bypassed.")
    st.caption("Groq's current model catalog lists per-token prices. Groq calls may be metered depending on your account plan; "
               "the local TTS request cap is not a billing guarantee.")
    st.markdown("[Photos and videos provided by Pexels](https://www.pexels.com)")
    c1, c2, c3 = st.columns(3)
    c1.metric("Estimated Neurons (local calculation; resets 00:00 UTC)",
              f"{usage.get('cloudflare_neurons', 0):.0f}",
              f"of {cache.CLOUDFLARE_FREE_NEURONS} free", delta_color="off")
    c2.metric("OpenRouter spend today", f"${usage.get('openrouter_usd', 0):.2f}")
    c3.metric("API calls today", sum((usage.get("calls") or {}).values()))
    st.caption(f"Groq Orpheus TTS requests today: {usage.get('calls', {}).get('groq_tts', 0)} / 100. "
               "The local request limiter allows at most 10 per minute.")
    gemini_calls = usage.get("calls", {})
    st.caption("Gemini local daily caps: "
               f"Flash-Lite {gemini_calls.get('gemini:gemini-3.5-flash-lite', 0)} / 500; "
               f"Flash creative {gemini_calls.get('gemini:gemini-3.8-flash', 0)} / 20.")
    pexels_quota = cache.provider_quota("pexels")
    remote_remaining = pexels_quota.get("remote_month_remaining")
    pexels_month = (f"{remote_remaining} requests remain this month (provider-reported)" if remote_remaining is not None
                    else f"{pexels_quota['monthly_used']} locally tracked requests this month")
    st.caption(f"Pexels: {pexels_quota['hourly_used']} / 200 requests in the rolling hour; {pexels_month}. "
               f"Today: {gemini_calls.get('pexels', 0)} requests.")
    if usage.get("calls"):
        st.caption(" · ".join(f"{k}: {v}" for k, v in sorted(usage["calls"].items())))
    if config.pollinations_key_kind() == "publishable":
        st.warning("POLLINATIONS_API_KEY has a pk_ publishable prefix. Image requests require a secret sk_ key and will return HTTP 401.")
    if config.pollinations_key_kind() == "unknown" and config.pollinations_key():
        st.warning("Pollinations key prefix is not recognized. Authenticated image requests require an sk_ secret key.")
    comfy_url = config.comfyui_url()
    workflow_path = config.comfyui_workflow_path()
    st.markdown("#### 🧩 Optional local ComfyUI")
    st.caption(f"Configured endpoint: {comfy_url or 'invalid URL'} · workflow: "
               f"{workflow_path or 'not configured'}. When ComfyUI is not used or unavailable, use the built-in "
               "DepthFlow CPU/GPU and FFmpeg motion fallback chain.")
    if st.button("Check ComfyUI connection", key="tf_comfy_check"):
        try:
            response = http.request("comfyui", "GET", f"{comfy_url}/system_stats", timeout=4, retries=0)
            st.success(f"ComfyUI responded (HTTP {response.status_code}).")
        except Exception as exc:  # noqa: BLE001
            st.warning(f"ComfyUI unavailable; local DepthFlow / FFmpeg remains available: {exc}")
    st.markdown("#### 🎞️ Local motion engines")
    motion_status = render.motion_engine_status()
    engine_labels = {
        "depthflow_3d": "DepthFlow 3D",
        "cutout_2d": "2D layered cutout",
        "procedural_3d_2d": "Remotion procedural 3D/2D",
    }
    st.dataframe([
        {"Engine": engine_labels[key], "Dependencies installed": value["installed"],
         "Last result": value["state"]}
        for key, value in motion_status.items()
    ], hide_index=True, width="stretch")
    for key, value in motion_status.items():
        if value.get("warning"):
            st.warning(f"{engine_labels[key]} fallback: {value['warning']}")
    st.caption("Optional installs: `pip install -r requirements-visual-engines.txt` for DepthFlow/cutout; install Node.js "
               "20+ and run `npm install` in `remotion-engine` for procedural graphics. DepthFlow is distributed under "
               "AGPL-3.0; review its license before enabling it in a redistributed deployment.")
    st.markdown("#### 🧪 Connection tests")
    t1, t2, t3 = st.columns(3)
    if t1.button("Test LLM router", width="stretch"):
        with st.spinner("Asking the AI…"):
            try:
                data, model = llm.complete_json("Reply with JSON only.", 'Return {"ok": true, "film": "<a 3 word film title>"}',
                                                task="fast", max_tokens=200, use_cache=False)
                st.success(f"{model}: {data}")
            except Exception as e:  # noqa: BLE001
                st.error(str(e))
    if t2.button("Test OpenRouter credits", width="stretch"):
        try:
            st.json(video.openrouter_credits() or {"error": "no key"})
        except Exception as e:  # noqa: BLE001
            st.error(str(e))
    if t3.button("Clear paused providers", width="stretch"):
        with http._breaker_lock:
            http._breaker.clear()
        _flash("Providers will be retried on the next request")
        st.rerun()
    st.markdown("#### 🧪 Minimal-quota live provider verification")
    st.caption("Runs one small real request for each configured provider. Requests may consume the provider's quota; "
               "unset providers are reported as SKIPPED. Raw keys are never printed.")
    if st.button("Run Live Key Check (Minimal Quota)", key="tf_live_provider_check"):
        script = Path(__file__).resolve().parents[1] / "scripts" / "test_live_providers.py"
        with st.spinner("Running minimal live checks; this may take a few minutes…"):
            try:
                proc = subprocess.run([sys.executable, str(script), "--json"], capture_output=True,
                                      text=True, timeout=600, check=False)
                report = json.loads(proc.stdout)
                st.dataframe(report.get("results", []), hide_index=True, width="stretch")
                st.json(report.get("summary", {}))
                if proc.stderr.strip():
                    st.caption(proc.stderr[-1200:])
            except subprocess.TimeoutExpired:
                st.error("Live provider checks timed out. No secret values were collected.")
            except (json.JSONDecodeError, OSError) as exc:
                st.error(f"Could not read live provider results: {exc}")
    st.markdown("#### 🔐 YouTube OAuth (private drafts only)")
    client_path = Path(config.youtube_client_secret_path())
    client_upload = st.file_uploader("Add OAuth client_secret.json", type=["json"], key="tf_youtube_client_secret")
    if client_upload is not None and st.button("Save OAuth client file", key="tf_save_youtube_client"):
        try:
            payload = json.loads(client_upload.getvalue().decode("utf-8"))
            if not isinstance(payload, dict) or not any(key in payload for key in ("installed", "web")):
                raise ValueError("This does not look like a Google OAuth desktop/web client file.")
            client_path.parent.mkdir(parents=True, exist_ok=True)
            client_path.write_text(json.dumps(payload), encoding="utf-8")
            st.success(f"OAuth client stored locally at {client_path}.")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Could not save OAuth client file: {exc}")
    if client_path.is_file():
        st.caption("OAuth client file is present on this machine; authorization is local and never uploaded to TubeForge.")
        if st.button("Connect / verify YouTube channel (read-only)", key="tf_youtube_connect"):
            try:
                channel = youtube_upload.channel_title(client_path, config.CACHE_DIR / "youtube" / "readonly_token.json",
                                                      allow_browser=True)
                st.success(f"Connected channel: {channel}")
            except Exception as exc:  # noqa: BLE001
                st.error(f"YouTube connection failed: {exc}")
        if st.button("Authorize private draft uploads", key="tf_youtube_upload_auth"):
            try:
                youtube_upload.connect(client_path, config.CACHE_DIR / "youtube" / "token.json",
                                       allow_browser=True)
                st.success("Private draft upload permissions were authorized and saved locally.")
            except Exception as exc:  # noqa: BLE001
                st.error(f"YouTube upload authorization failed: {exc}")
    st.markdown("#### 🖥️ Local engines")
    if st.button("Check Local Engines", width="stretch"):
        script = Path(__file__).resolve().parents[1] / "scripts" / "check_media_env.py"
        try:
            proc = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if proc.stdout.strip():
                report = json.loads(proc.stdout)
                st.json(report)
                if proc.returncode != 0:
                    st.warning("One or more required local engines are missing or below the supported version.")
            else:
                st.error(proc.stderr.strip() or "Local engine check returned no result.")
        except subprocess.TimeoutExpired:
            st.error("Local engine check timed out.")
        except json.JSONDecodeError:
            st.error(proc.stdout.strip() or proc.stderr.strip() or "Local engine check returned invalid output.")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Could not run the local engine check: {exc}")
    st.markdown("#### Available image engines")
    st.dataframe([{"Engine": e.label, "Tier": e.tier, "Refs": "✅" if e.refs else "", "Ready": "✅" if images.engine_available(k)[0]
                   else "— " + images.engine_available(k)[1][:80], "Note": e.note} for k, e in images.ENGINES.items()
                  if k in images.FREE_ENGINE_KEYS],
                 hide_index=True, width="stretch")


# ------------------------------------------------------------------------------------------------ main
def render_studio(public_mode: bool = False) -> None:
    config.ensure_env_loaded()
    st.markdown(CSS, unsafe_allow_html=True)
    p = _project()
    with st.sidebar:
        st.markdown("### 🎬 Cinematic Studio")
        projects = jobs.list_projects()
        ids = [""] + [x["id"] for x in projects]
        names = {x["id"]: x["title"][:40] or x["id"] for x in projects}
        cur = _pid() if _pid() in ids else ""
        sel = st.selectbox("Project", ids, index=ids.index(cur), format_func=lambda k: names.get(k, "➕ New project"))
        if sel != cur:
            st.session_state["tf_pid"] = sel or None
            st.rerun()
        if p:
            plan = p.plan
            st.caption(f"{len(plan.scenes) if plan else 0} scenes · {p.settings['style']} · {p.settings['resolution']}")
            busy = [x["title"] for x in projects if jobs.MANAGER.is_running(x["id"])]
            if busy:
                st.caption("⚙️ Running: " + ", ".join(b[:24] for b in busy))
        if st.button("↻ Refresh projects and page", width="stretch"):
            st.rerun()
        st.caption("Generation uses configured providers and local tools. Groq and explicit authenticated Pollinations usage may be metered by your account.")
    st.markdown("<div class='tf-hero'><h1>TubeForge Cinematic AI Studio</h1><p>Idea → story → characters → scenes → "
                "visuals → voice → music → edit → render → export. Hollywood-style camera moves, colour grading, "
                "atmosphere, sound design and subtitles.</p></div>", unsafe_allow_html=True)
    _stepper(p)
    _show_flash()
    if p:
        _live_panel(p.id)
    if "tf_nav_next" in st.session_state:
        st.session_state["tf_nav"] = st.session_state.pop("tf_nav_next")
    if st.session_state.get("tf_nav") not in PAGES:
        st.session_state["tf_nav"] = PAGES[1] if not p else PAGES[3]
    nav = st.segmented_control("Navigate", PAGES, key="tf_nav", label_visibility="collapsed")
    nav = nav or (PAGES[1] if not p else PAGES[3])
    {
        PAGES[0]: page_dashboard, PAGES[1]: page_create, PAGES[2]: page_story, PAGES[3]: page_storyboard,
        PAGES[4]: page_timeline, PAGES[5]: page_audio, PAGES[6]: page_render, PAGES[7]: page_providers,
    }[nav]()
