# TubeForge Studio Pro — Windows Quick-Start Guide

TubeForge is a local-first Streamlit workspace for taking a video from an idea or script through story planning,
scene design, visuals, narration, soundtrack, editing, rendering, and export. It combines hosted AI services with
local media processing; using it does not require every optional provider or engine.

## 1. How to Start the Application (Daily Use)
Whenever you restart your PC or open a new terminal window, run these 3 commands in **PowerShell** or **Command Prompt (CMD)**:

```powershell
cd "C:\Users\Ravi Ranjan\Downloads\turbo"
.\venv\Scripts\activate
streamlit run app.py
```

For a new environment or after dependency updates, install requirements once with
`.\venv\Scripts\python.exe -m pip install -r requirements.txt`.

When exposing the app on a network (including the Docker image, which binds to `0.0.0.0`), set a strong
`APP_PASSWORD` environment variable. Startup is blocked if a public/network binding has no password; loopback
development runs do not require one.

For a one-time Windows setup, install Python 3.11 or newer, FFmpeg (including `ffprobe`) on `PATH`, create the
virtual environment, and install the app dependencies:

```powershell
py -3.11 -m venv venv
.\venv\Scripts\python.exe -m pip install --upgrade pip
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

FFmpeg and `ffprobe` are required for video/audio assembly. Node.js 20+ is only required for the optional Remotion
graphics engine. The **Providers → Check Local Engines** action reports local engine availability. Alternatively,
run `.\venv\Scripts\python.exe scripts\check_media_env.py` from the repository root.

## Free-tier production mode

The app opens the Cinematic AI Studio, which routes text through Gemini, Groq, and explicitly free OpenRouter
models; images use Cloudflare FLUX.1 Schnell / FLUX.2 Klein 4B (with local 10,000-neuron daily reservations),
anonymous Pollinations, and optional Pexels/Pixabay stock. English production narration defaults to offline Kokoro;
Microsoft Edge is available as a fast draft preview and can also be explicitly selected as a voice engine, while
Sarvam/Groq TTS are optional. Music, sound effects, motion, and rendering are produced locally. Groq and keyed
Pollinations requests may be metered, depending on the provider account.
Paid hosted video is **off by default** and is available only as an explicit opt-in for eligible hero scenes; it can
incur provider charges. Original motion uses a generated still with local camera/parallax animation; it is not
semantic AI-generated character motion. The active application is the Cinematic AI Studio.

Free-service quotas, availability, and terms still apply. The Cloudflare reservation covers this app's usage only;
other workloads using the same account can consume its daily allowance, so use a separate free-tier account with
billing disabled if you need to avoid unexpected charges.

English production narration defaults to Kokoro-82M through `kokoro-onnx` (the model is Apache 2.0; the runtime
package is MIT), runs locally after setup, and caches completed text chunks under the operating system's user
cache directory for resume. The ONNX model and voices are downloaded there on first use. Microsoft Edge TTS is
available as a fast draft preview and can also be explicitly selected as a voice engine; the readiness audit marks
Edge as draft rather than production narration.
Optional Groq Orpheus narration uses `GROQ_API_KEY` and falls back to local Kokoro when rate-limited. Groq's
current model catalog lists per-token prices, so verify your account's billing terms before enabling Groq calls;
the app's TTS request cap is not a billing guarantee. Hindi and
Hinglish production narration can use Sarvam Bulbul v4 Flash with `SARVAM_API_KEY`. Keys remain server-side in
the environment or `.env`; imported WAV/MP3 voiceovers are stored with their project.

Final audio uses voice cleanup, voice-responsive music ducking, and EBU R128 loudness normalization targeting
-14 LUFS / -1.5 dBTP.

## Word alignment, pacing and captions

After voice audio is assembled (or an external WAV/MP3 is imported), TubeForge writes word-level timestamps to
`output/cinematic/<project-id>/alignment.json`. Matching audio is reused by SHA-256; large Groq transcription inputs
are split into overlapping files below 20 MB. With `GROQ_API_KEY`, alignment uses Groq Whisper
`whisper-large-v3-turbo`; otherwise, or when Groq is unavailable or rate-limited, CPU `faster-whisper` runs
tiny/base/small with int8 quantization. The selected local model may be downloaded to the Hugging Face cache.

Aligned words drive retention-paced visual cuts (up to 4.5 seconds in the opening 30 seconds, then up to 7 seconds)
and transition-effect placement. SRT and VTT files are included in exports. Kinetic word-highlight captions are an
optional Render & Export setting; they use the selected accent color and safe positioning for both landscape and
vertical Shorts output.

Hindi/Devanagari captions prefer Nirmala UI on Windows and Noto Sans Devanagari elsewhere. If no suitable system
font is installed, TubeForge fetches the Noto Sans Devanagari variable font from Google Fonts and verifies its
SHA-256 before use; that font is licensed under the SIL Open Font License. Pillow must be built with libraqm for
direct complex-script shaping. Without RAQM, Hindi caption rasterization falls back to libass with the selected
Devanagari font. The Docker image builds Pillow with libraqm and verifies that capability during image creation;
standard Windows Pillow wheels may not include RAQM.

## Visual consistency and free-provider routing

New projects use Cloudflare FLUX.1 Schnell (4 steps) first in automatic image generation, followed by anonymous
Pollinations and optional Pexels/Pixabay stock fallbacks. FLUX.2 Klein 4B is reserved for Visual Bible references
or an explicit image-engine selection; its neuron use is estimated at approximately 1,364 neurons per megapixel.
The Cloudflare API does not expose per-generation neuron usage in the image response, so the Providers page labels
this counter **Estimated Neurons (local calculation; resets 00:00 UTC)** and warns before an estimated reservation
crosses 30% of the local daily budget. The local estimate cannot see other workloads on the same Cloudflare account.

The Storyboard's **Generate & Lock Visual Bible** step saves a `visualBible` object into `project.json`, alongside
the master style reference and character sheets in that project's `visual_bible` folder. New projects require
approval before batch image generation; existing projects are migrated without changing their prior workflow.
Once approved, scene prompts include the locked style prefix, use deterministic per-scene seeds derived from the
project seed, and send the master/character references to reference-capable engines. The fingerprint changes when
the locked visual inputs change, so only affected frames are regenerated. Project duplication and project bundles
preserve the Visual Bible references.

Gemini uses `gemini-3.5-flash-lite` for routine generation (local cap 500 requests/day) and
`gemini-3.8-flash` for creative story passes (local cap 20/day). Groq rotates through the configured models on
rate limits; Groq model calls may be metered by the account. OpenRouter continues to use `:free` models. These
local caps do not override the provider's actual account limits or billing terms.
`PEXELS_API_KEY` enables landscape/portrait-filtered photo and video search. TubeForge tracks Pexels request
windows and provider-reported monthly remaining quota, and includes Pexels attribution in generated metadata.
`POLLINATIONS_API_KEY` enables authenticated Pollinations as an explicitly selected engine; because keyed usage
may consume account balance, it is not used by automatic fallback. Authenticated image requests require an `sk_`
secret key; the Providers page warns when a `pk_` publishable key is configured.

## Channel reuse, imports and Hero clips

Projects with a Channel Name can save/load a reusable Visual Bible, character references, locations, brand palette,
caption preset, and narrator voice under `output/channels/<channel-id>/library.json`. Completed project titles,
topics, and opening hooks are recorded in `series_history.json`; the most recent 25 are passed to planning as
anti-repetition context. These local files are reusable across projects on this machine.

Create can import an article URL or pasted transcript and extract source-bounded facts/statistics into Research
Notes. Storyboard can derive a new Visual Bible from 1–3 screenshots using broad style traits only; the vision prompt
instructs the model not to reproduce protected characters, logos, or distinctive compositions. The Hero Clips Drawer
shows prompts for hook/climax scenes, accepts external MP4 clips, and extracts their final frame for continuity.
Attached hero clips are normalized and substituted during assembly. Optional ComfyUI settings are checked on
Providers; unavailable ComfyUI does not block the existing local motion path.

## Shorts, publishing pack and provider checks

Render & Export ranks word-aligned 30–55 second AutoClip windows with a heuristic score, then exports selected
clips as subject-aware 1080×1920 videos with one of five kinetic caption presets. Face tracking uses OpenCV Haar
when available and spectral saliency otherwise; the score is a ranking aid, not a view/virality guarantee.
Alignment-derived chapters, a pinned-comment template, SEO description, tags, and three hook-copy thumbnail PNGs
are included in the export package. A non-blocking readiness panel checks project settings/assets; it is not a
YouTube Partner Program approval or monetization guarantee.

Providers includes **Run Live Key Check (Minimal Quota)**, which makes one small request per configured provider and
reports PASS, FAIL, RATE_LIMITED, or SKIPPED without printing keys. Live checks can still consume account quota and
provider behavior may change. Optional YouTube OAuth credentials remain in the local cache; uploads use resumable
chunks and are forced to **private**. The app never publishes a video publicly automatically.

## Local scene motion engines

Storyboard scenes can keep the built-in FFmpeg parallax renderer or opt into **DepthFlow 3D** and **2D layered
cutout**. Choose an engine per scene, edit the scene's motion preset in Storyboard or Timeline, or bulk-apply an
engine in Storyboard, Timeline, or Render. The one-scene preview renders only that scene. Auto-Choreograph assigns
different motion presets to adjacent scenes.

The optional workers are installed separately from the main app dependencies:
`.\venv\Scripts\python.exe -m pip install -r requirements-visual-engines.txt`. DepthFlow estimates and caches
`scene_XX_depth.png`, then caches the raw `scene_XX_3d.mp4`; cutout caches `foreground.png`,
`background_clean.png`, and its raw scene clip in the scene folder. Cache metadata keys use image content hashes,
motion settings, duration, resolution, and frame rate. Final color, captions, and overlays are applied afterward.

DepthFlow requires a working OpenGL context and its optional dependency stack. If it is unavailable or fails to
initialize headlessly, TubeForge records a warning under **Providers** and renders an overscanned, smooth-eased
FFmpeg parallax zoom instead. Cutout segmentation uses rembg (BiRefNet, with a u2net retry) and OpenCV inpainting;
if those dependencies or segmentation are unavailable, it also falls back to the built-in renderer. The cutout
models download on first use and are cached locally. DepthFlow is currently licensed under AGPL-3.0; review that
license before enabling it in a redistributed deployment. Neither optional engine is installed by the normal
requirements file.

## Procedural motion graphics

The optional `remotion-engine/` workspace adds the `procedural_3d_2d` scene source. Install Node.js 20+ and the
workspace dependencies once:

```powershell
cd remotion-engine
npm install
npm run typecheck
```

Scene templates are Zod-validated and include 3D bar charts, 3D stat callouts, comparison splits, timeline journeys,
process flows, quote/evidence cards, 2D character explainers, and kinetic hook titles. Story/Storyboard can assign
a template and edit its JSON props; channel name and the project's primary, secondary, and accent colors are
inherited from Render settings. Evidence-card citations must match a source recorded in the Story. Storyboard's
**Motion still** button invokes the Remotion still renderer.

The renderer runs only the selected scene, producing `scene_XX_proc.mp4` with a cache key based on the source image,
template props, duration, dimensions, and frame rate. The resulting clip then passes through the usual scene
post-processing and final Timeline assembly; existing projects continue to default to the original FFmpeg renderer.
To render a standalone template from the workspace, use:

```powershell
npx remotion render src/index.tsx ProceduralScene out/test.mp4 --props path\to\scene_props.json
npx remotion still src/index.tsx ProceduralScene out/test.png --props path\to\scene_props.json --frame=60
```

All template animation is driven by Remotion frame values; the Three.js canvas uses Remotion's synchronized
`ThreeCanvas` and `Sequence layout="none"`.

Template names and props are validated before the renderer runs; arbitrary props are rejected. The evidence card's
source must be listed among the Story's research sources. Constraints include up to eight chart bars or timeline
milestones, two to eight process steps, up to five metrics per comparison side, and bounded text fields. Character
explainer props support five expressions and an optional frame-based mouth-state timeline; this is a graphic
animation aid, not automatic audio-driven lip-sync.

## Feature-by-feature guide

The app's navigation is **Dashboard**, **Create**, **Story**, **Storyboard**, **Timeline & Look**, **Audio**,
**Render & Export**, and **Providers**. All project data and generated assets are stored under `output/` unless
otherwise noted.

### Dashboard and project lifecycle

- View project count, completed-film count, available LLM backends, and today's locally estimated Cloudflare neuron
  use.
- Open projects, duplicate a project, or delete its project files. Duplicating is useful for trying alternate edits
  without overwriting the original.
- The sidebar project selector and progress panel make it possible to move between pages while a background job
  runs. Jobs expose stage, per-scene status, progress, recent logs, cancellation, and resume controls.
- Rendering is incremental: cached scene work is reused when its inputs remain unchanged. After a process restart,
  use **Resume** for an interrupted render.

### Create: idea, script, research, and project setup

- Start with a free-form idea or one of the quick inspiration prompts. Pick a 30-second, 1-, 2-, 3-, or 5-minute
  target, or enter a custom duration. Set 1–50 scenes and a 2–30-second minimum scene duration.
- Optionally provide a narration script, director notes, research notes, and links. Supplying a script asks the
  planner to preserve the wording while generating visual direction.
- Import an article URL or paste article/transcript text to extract facts and statistics into Research Notes.
  Extraction is an AI-assisted research aid, not independent fact-checking; verify each statement and citation.
- Choose visual style, output aspect ratio, narration language, default visual source, and Auto Cinematic Mode.
  Auto Cinematic Mode proposes creative settings; review its output rather than treating it as factual or final.
- Set a channel name, watermark position, logo, voice-branding toggle, and watermark toggle. Branding preferences
  can be reused on this machine; public mode does not save one visitor's channel preferences as another visitor's
  defaults.
- Generate a story/scene plan or request a one-click full-film job. The latter is convenient, but does not remove
  the value of reviewing the story and scenes before publishing.

### Story: treatment, metadata, characters, and environments

- Edit the title, genre, logline, first-five-second hook, storyline, visual identity, ending, soundtrack mood,
  YouTube title/description, and comma-separated tags.
- Create and edit characters (role, appearance, personality, and dialogue voice), upload or generate reference
  images, and keep character names synchronized with scene references when renamed.
- Edit named environments and their visual descriptions.
- Assign a procedural motion-graphic template to selected scenes from the Story page.
- Generated titles, tags, facts, stories, and dialogue can be inaccurate or generic. Treat them as editable drafts.

### Storyboard: lock the visual direction and edit every scene

- Generate a Visual Bible with a visual mode (stylized 2D, stylized 3D, or cinematic documentary), shared style
  prefix, global negative prompt, project seed, master style reference, and character references. Review and approve
  it before bulk scene-image generation on new projects.
- Reuse a channel library containing Visual Bible, character/location references, palette, caption preset, and
  narrator voice. The series history records completed titles, topics, and opening hooks; the latest 25 entries are
  supplied as anti-repetition context.
- Optionally extract an original style direction from 1–3 screenshots. This derives broad traits, not a license to
  copy an image, character, logo, or distinctive composition.
- Each scene supports editable title, source, duration, narration, dialogue, image and negative prompts, stock
  keywords, motion direction, engine, camera, lighting, atmosphere, grade, animation, transition, music cue, SFX,
  ambience, overlay text, and a user-uploaded still or video.
- Supported visual sources include generated stills with local motion, AI stills, stock video, explicitly enabled
  hosted AI video, local/remote ComfyUI, a Google Flow prompt pack with manual clip import, and user-uploaded media.
- Generate or refresh a scene image, make three image variations, lock/unlock a chosen variation, rewrite or
  enhance the prompt, preview its voice, or render a one-scene preview. Arrange scenes by moving, duplicating,
  inserting, or deleting them (at least one scene remains).
- Auto-Choreograph assigns non-repeating camera presets to adjacent scenes. Bulk engine selection and retry-failed
  controls help with larger storyboards.
- The Hero Clips Drawer provides clean prompts for hook/climax scenes, accepts externally created clips, and can
  extract the final frame for continuity review. Imported clips are normalized during assembly.
- Scene consistency is aided by repeated character descriptions, locked references, and deterministic seeds; image
  models can still vary faces, hands, props, and fine details between shots.

### Timeline & Look: pacing, per-scene edits, and global finish

- Review total target and estimated runtime, scene count, spoken-word estimate, rendered status, thumbnails, and a
  colored timeline bar. Auto-scale proportionally adjusts scene durations toward the target, subject to the
  renderer's 2–30-second scene bounds.
- Edit scene title/source/engine/motion preset/duration/camera/transition/atmosphere/music cue/SFX/ambience in a
  table, then apply edits. Per-scene Storyboard controls expose more detailed prompt and visual controls.
- Run local visual QA for missing, low-detail, undersized, near-duplicate frames, and recurring characters without
  reference images. QA is a warning tool, not a semantic review or a guarantee of visual quality.
- Set channel watermark/position, global grade and atmosphere, camera intensity, grain, bloom, vignette, depth of
  field, halation, chromatic aberration, sharpening, letterbox, title card, film gate weave, and camera resampling.
- Choose burned-in subtitle style, caption word grouping, per-scene or global transitions, and optional kinetic
  word-highlight captions with one of five caption presets and a custom highlight color.
- The renderer supports landscape, vertical, square, ultra-wide, and portrait-feed aspect ratios; 720p, 1080p,
  1440p, and 2160p export targets; 24/30/60 fps; H.264, H.265, and VP9 output options; and Draft, High, or Cinema
  quality. A 4K target is upscaled from 1440p; 60 fps changes output cadence and does not create new motion detail.
- Available visual styles are Hollywood Blockbuster, Hyper-Realistic, Pixar-Style 3D, Anime, Sci-Fi Cyberpunk,
  Epic Fantasy, Dark Fantasy, Documentary, Nature Documentary, Film Noir, Horror, Historical Epic, Retro Synthwave,
  Watercolor Storybook, and Luxury Commercial. Per-scene camera moves include static, dolly/push/pull, pans,
  tilts, tracking, crane, drone, orbit, handheld, close-up/establishing reveals, Ken Burns, Dutch tilt, slow drift,
  and vertigo/dolly zoom.
- Other per-scene camera controls include nine angles (eye/low/high, bird's-eye, worm's-eye, over-the-shoulder,
  Dutch, POV, aerial), nine shot sizes (extreme wide through extreme close-up, full/medium, two-shot, insert), and
  14 lighting choices (golden/blue hour, volumetric, neon, moonlight, candle, rim, chiaroscuro, soft/high-key,
  overcast, midday, fire, bioluminescent, and studio three-point).
- Film finish options include Natural, Teal & Orange, Golden Hour, Neon Noir, Bleach Bypass, Cold Thriller,
  Vintage Film, Noir B&W, Vibrant, Soft Pastel, Clean Commercial, Desert Heat, and Matrix Green grades; fog, rain,
  snow, embers, dust, light leaks, fireflies, or no atmosphere; and None, breathing zoom, parallax, lens flare,
  pulse glow, chromatic aberration, or motion blur animation. Transitions include cut, crossfade, dissolve, fade
  through black, white flash, whip pan, slide, wipe, circle, radial, zoom, motion blur, pixelize, diagonal, and
  smooth-up.

### Audio: narration, alignment, music, ambience, and effects

- Select language, production voice engine/voice, speed, and (for Edge) pitch. Preview the selected narrator or a
  fast Edge draft voice. Character dialogue can use distinct voices.
- English production voices include local Kokoro and optional Groq Orpheus; Sarvam is optional for Hindi and
  supported Indian-English/code-switching narration. Edge supports additional languages, but the readiness audit
  treats Edge as a draft engine. Provider availability and supported voices can change.
- Import a WAV/MP3 voiceover to replace generated narration. Rebuild or remove its alignment. Longer imported audio
  is not silently truncated; extend the project timeline as needed.
- Copy the script as 200-word blocks if using an external voice service. This does not synthesize or upload those
  blocks for you.
- Generate and preview a local mood-based score, vary its deterministic seed, or upload licensed MP3/WAV/M4A/OGG
  music. Set voice/music/effects levels, music ducking, loudness target, and toggles for generated ambience and SFX.
- Score moods are epic, dark, uplifting, mysterious, emotional, tense, ambient, or none. Scene ambience choices are
  rain, wind, ocean, city, fire, forest, space, crowd, storm, or none; sound effects are whoosh, impact, riser,
  boom, shimmer, heartbeat, or none. Scene music cues can be calm, build, tension, climax, resolve, silence, or
  automatic from the story arc.
- Alignment writes word timestamps, drives pacing and chapter timing, and supplies SRT/VTT plus AutoClip timing.
  Groq alignment can be metered; the local faster-whisper fallback can download a model and use significant CPU,
  memory, and disk.

### Render & Export: render controls and deliverables

- Run pre-render checks, free local visual QA, and an optional post-render render inspection. The monetization and
  quality audit is non-blocking and does not grant monetization approval.
- Configure image engine/fallbacks, stock fallback, negative prompt, optional paid-video controls, ComfyUI
  endpoint/workflow, strict loudness gate, and explicit handling for assets whose license is unknown.
- Configure opening/end cards, subscribe CTA, Shorts/Reels teaser, transition whooshes, thumbnail variants, and
  output settings. Prepare visuals/voices separately or render the final film.
- Export the main video, thumbnail variants, optional 9:16 teaser, subtitles, YouTube metadata, alignment, and a
  downloadable project ZIP containing the film, available Shorts/AutoClips, subtitles, metadata, thumbnails,
  alignment, plan, Visual Bible references, generated/stock scene images and hosted-video assets, voice parts, and
  license registry. It does not include every uploaded source or intermediate render/cache file.
- AutoClip ranks up to five eligible word-aligned moments and renders selected portrait Shorts to the project
  folder. Its score is heuristic, not a prediction of engagement. Subject framing uses face detection when
  available and saliency as a fallback.
- The YouTube publishing pack can include a title/description/tags, chapters, pinned-comment template, and
  alternative hook thumbnails. Review generated text and all rights/disclosure requirements before use.

### Providers: diagnostics, usage, OAuth, and optional services

- See which provider keys are configured, provider breaker/cooldown status, local request counts, Cloudflare neuron
  estimate, Pexels quotas, and tracked OpenRouter spend where available.
- Run the LLM router test, OpenRouter credit check, ComfyUI connection check, local-engine check, or minimal-quota
  live key check. A live check makes real requests and can consume quota; it never prints raw keys.
- Upload a Google OAuth client file, verify the connected channel read-only, and authorize private-draft upload.
  Uploads are resumable and private. An optional playlist title creates a private playlist and adds the uploaded
  draft to it. TubeForge does not automatically make uploads public.
- Keys are read from server-side environment variables or `.env`; do not commit `.env`, share it, or expose a
  network-bound app without `APP_PASSWORD`. The shared password is an access gate, not per-user accounts or
  per-user project isolation.

## Provider and environment variables

Set only the providers you intend to use. Optional keys are not required to run the local renderer.

| Variable | Used for | Notes |
| --- | --- | --- |
| `APP_PASSWORD` | App access gate | Required when public mode is enabled or Streamlit binds to a non-loopback address. Use a strong secret. |
| `TUBEFORGE_PUBLIC_MODE` | Public deployment behavior | Set to `1`/`true` for public mode. Avoid storing visitor-provided keys as server defaults. |
| `GEMINI_API_KEY` | Gemini text and supported image/style tasks | Local request caps are app-side guards, not a provider quota or billing guarantee. |
| `GROQ_API_KEY` | Text routing, Orpheus TTS, and Whisper alignment | Provider pricing and account terms may apply. |
| `OPENROUTER_API_KEY` | Free `:free` text models; optional paid video | Text routing prefers free models. Paid video is a separate explicit opt-in. |
| `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` | FLUX image generation | The local neuron ledger is an estimate and cannot observe other workloads on the account. |
| `POLLINATIONS_API_KEY` | Explicit authenticated image requests | Use an `sk_` secret key; keyed use may consume account balance. It is not an automatic fallback. |
| `PEXELS_API_KEY` | Pexels stock photos/video | Provider limits apply; generated metadata includes Pexels attribution. |
| `PIXABAY_API_KEY` | Pixabay stock photos/video | Optional stock-provider key. |
| `SARVAM_API_KEY` | Sarvam Bulbul narration | Optional Hindi/Hinglish and supported Indian-English voices. |
| `COMFYUI_URL` | ComfyUI endpoint | Defaults to `http://127.0.0.1:8188` when unset. |
| `COMFYUI_WORKFLOW_JSON` | ComfyUI API workflow | Path to an exported API workflow JSON file. |
| `YOUTUBE_CLIENT_SECRET` | Google OAuth client file | Optional path; otherwise the app uses its local YouTube cache directory. |

For local development, create a `.env` file in the repository root with `NAME=value` lines. Keep secrets out of
screenshots, logs, source control, and shared workspaces. The app's local quota counters are not a billing cap.

## Project files, caching, and recovery

- Project state, scene assets, render outputs, and logs live under `output/cinematic/<project-id>/`.
- Shared local caches and provider usage ledgers live under `output/_cache/` and `output/_usage.json`.
- Reusable per-channel assets and series history live under `output/channels/`.
- Kokoro model files and completed narration chunks use the operating system's user cache directory.
- Project state saves use rolling `project.json` backups (five retained by default).
- Keep backups of `output/` if projects matter. Deleting a project in the Dashboard deletes that project's files;
  clearing caches may require regenerating derived media.
- A render can be cancelled, retried per failed scene, or resumed after interruption. Do not edit/delete project
  files while a job is actively writing them.

## What is good, and what are the trade-offs?

### Strengths

- **End-to-end workflow:** story, scene direction, art, voices, soundtrack, timeline, captions, and export metadata
  share one editable project.
- **Flexible cost profile:** the core path can use local narration/motion/rendering and free-provider tiers; optional
  paid services are separate and user-configured.
- **Useful iteration tools:** scene-level retries/previews, prompt edits, image variations, locks, caches, project
  duplication, and background-job resume reduce the cost of revising a film.
- **Continuity and pacing aids:** Visual Bible references, channel libraries, series history, word alignment, and
  Auto-Choreograph provide repeatable structure.
- **Manual control:** generated plans remain editable, and users can bring their own scripts, voiceovers, music,
  visuals, clips, and ComfyUI workflows.
- **Transparency tools:** local QA, provider diagnostics, usage counters, preflight, and export audits surface
  common issues before delivery.

### Limitations and risks

- **Not a one-click factual authority:** LLMs can invent or misstate facts, citations, scene details, titles, and
  metadata. Verify claims against original sources and review every frame and caption.
- **Generated stills are not full AI video:** the default motion path animates still images with camera/parallax
  effects. It cannot reliably create coherent new character actions or dialogue lip-sync.
- **Continuity is probabilistic:** a Visual Bible and references improve consistency but cannot guarantee identical
  faces, clothing, hands, props, or locations from scene to scene.
- **Provider dependency:** network access, service availability, model changes, rate limits, geography, terms, and
  account billing affect hosted generation. Free tiers can change or disappear.
- **No universal zero-cost guarantee:** Groq, authenticated Pollinations, OpenRouter paid video, and other
  provider-account usage may be billed. Local neuron/request estimates do not see other account workloads and are
  not provider-enforced hard caps.
- **Rendering can be resource-intensive:** long/high-resolution films need disk, memory, CPU/GPU time, and may
  download large models. Optional DepthFlow/segmentation can be especially heavy; CPU fallback is slower.
- **Resolution claims need context:** selecting 4K upscales 1440p output rather than recovering native detail.
  Increasing fps does not invent source motion.
- **QA/audit is advisory:** local visual checks use simple image/video heuristics; the readiness audit is neither a
  legal review, a YouTube policy decision, a copyright check, nor a monetization guarantee.
- **Rights remain the user's responsibility:** confirm licenses/attribution for stock, music, uploaded assets,
  generated outputs, and optional engine dependencies. Acknowledging unknown licenses does not grant rights.
- **Shared deployment is not multi-tenant:** `APP_PASSWORD` is a shared gate, not separate accounts, access roles,
  or isolated project storage. Deploy only for users who may share the same server-side projects and keys.
- **Optional engine caveats:** DepthFlow's AGPL-3.0 license may matter for redistribution. Check licenses and terms
  for all providers, models, packages, and assets before commercial use.

## Troubleshooting checklist

1. **App will not start:** use the repository's virtual environment and confirm dependencies are installed. Run
   `.\venv\Scripts\python.exe -m pip install -r requirements.txt`.
2. **Media render fails:** confirm both `ffmpeg` and `ffprobe` work in a new terminal (`ffmpeg -version`,
   `ffprobe -version`) and run the local engine check.
3. **Images/LLM unavailable:** open **Providers**, inspect configured keys and status, then run a provider test.
   Provider live tests use real quota.
4. **No scene generation before batch render:** review the Visual Bible references and approve/lock the Bible for
   new projects.
5. **DepthFlow/cutout falls back:** install `requirements-visual-engines.txt`, check GPU/OpenGL status, and inspect
   the warning under **Providers**. The built-in FFmpeg renderer remains the fallback.
6. **Remotion template fails:** install Node.js 20+, run `npm install` and `npm run typecheck` from
   `remotion-engine`, then review the template's strict JSON props.
7. **YouTube upload fails:** check the OAuth client file, YouTube Data API access, upload authorization, token
   cache, and video requirements. A failed playlist insertion does not undo an already uploaded private draft.

Feature availability and provider behavior are version- and account-dependent. For commercial releases, independently
review the current platform policies, provider prices, asset licenses, and disclosure requirements.