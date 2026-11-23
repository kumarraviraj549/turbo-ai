# TubeForge Studio Pro — Windows Quick-Start Guide

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

## Free-tier production mode

The app opens the Cinematic AI Studio, which routes text through Gemini, Groq, and explicitly free OpenRouter
models; images use Cloudflare FLUX.1 Schnell / FLUX.2 Klein 4B (with local 10,000-neuron daily reservations),
anonymous Pollinations, and optional Pexels/Pixabay stock. English production narration defaults to offline Kokoro;
Microsoft Edge is available only as a fast draft preview, and Sarvam/Groq TTS are optional. Music, sound effects,
motion, and rendering are produced locally. Groq and keyed Pollinations requests may be metered, depending on the
provider account.
Paid hosted video generation and the legacy Gemini-based Classic Studio are disabled. Original motion uses a
generated still with local camera/parallax animation; it is not semantic AI-generated character motion.

Free-service quotas, availability, and terms still apply. The Cloudflare reservation covers this app's usage only;
other workloads using the same account can consume its daily allowance, so use a separate free-tier account with
billing disabled if you need to avoid unexpected charges.

English production narration defaults to Kokoro-82M through `kokoro-onnx` (the model is Apache 2.0; the runtime
package is MIT), runs locally after setup, and caches completed text chunks under the operating system's user
cache directory for resume. The ONNX model and voices are downloaded there on first use. Microsoft Edge TTS is
available separately as **Edge TTS (Fast Draft Preview)**.
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

Scene templates are Zod-validated and include 3D bar charts, stat callouts, comparison cards, timelines, process
flows, and evidence cards. Story/Storyboard can assign a template and edit its JSON props; channel name and the
project's primary, secondary, and accent colors are inherited from Render settings. Evidence-card citations must
match a source recorded in the Story. Storyboard's **Motion still** button invokes the Remotion still renderer.

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