"""Server-side configuration. Secrets are read from the environment / .env only and never sent to the browser."""
import json
import os
import re
import urllib.parse
from pathlib import Path

ENV_FILE = Path(".env")
UI_PREFS_FILE = Path(".saved_keys.json")
OUTPUT_ROOT = Path("output")
CACHE_DIR = OUTPUT_ROOT / "_cache"
STUDIO_ROOT = OUTPUT_ROOT / "cinematic"

_loaded = False


def ensure_env_loaded() -> None:
    """Loads .env into os.environ. Populates or overrides empty environment variables."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    if not ENV_FILE.exists():
        return
    try:
        for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            k, v = line.split("=", 1)
            v = v.strip()
            if " #" in v and not v.startswith(('"', "'")):
                v = v.split(" #", 1)[0].strip()
            clean_k = k.strip()
            clean_v = v.strip('"').strip("'").strip()
            # If not in os.environ or existing value is empty, update it
            if not os.getenv(clean_k, ""):
                os.environ[clean_k] = clean_v
    except Exception:
        pass


def secret(name: str) -> str:
    ensure_env_loaded()
    return os.getenv(name, "").strip()


def groq_key() -> str:
    return secret("GROQ_API_KEY")


def sarvam_key() -> str:
    return secret("SARVAM_API_KEY")


def openrouter_key() -> str:
    return secret("OPENROUTER_API_KEY")


def pixabay_key() -> str:
    return secret("PIXABAY_API_KEY")


def pexels_key() -> str:
    return secret("PEXELS_API_KEY")


def gemini_key() -> str:
    return secret("GEMINI_API_KEY")


def pollinations_key() -> str:
    return secret("POLLINATIONS_API_KEY")


def pollinations_key_kind() -> str:
    key = pollinations_key()
    if key.startswith("pk_"):
        return "publishable"
    if key.startswith("sk_"):
        return "secret"
    return "unknown" if key else "missing"


def comfyui_url() -> str:
    value = secret("COMFYUI_URL") or "http://127.0.0.1:8188"
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return value.rstrip("/")


def comfyui_workflow_path() -> str:
    return secret("COMFYUI_WORKFLOW_JSON")


def youtube_client_secret_path() -> str:
    return secret("YOUTUBE_CLIENT_SECRET") or str(CACHE_DIR / "youtube" / "client_secret.json")


def network_binding_requires_password(public_mode: bool, bind_address: object) -> bool:
    """Require authentication for public mode and explicit non-loopback Streamlit binds."""
    if public_mode:
        return True
    address = str(bind_address or "").strip().lower()
    return address not in ("", "localhost", "127.0.0.1", "::1")


def cloudflare_token() -> str:
    return secret("CLOUDFLARE_API_TOKEN")


def cloudflare_account() -> str:
    acc = secret("CLOUDFLARE_ACCOUNT_ID").lower()
    return acc if re.fullmatch(r"[0-9a-f]{32}", acc) else ""


def configured_providers() -> dict:
    return {
        "groq": bool(groq_key()),
        "openrouter": bool(openrouter_key()),
        "cloudflare": bool(cloudflare_token()),
        "pixabay": bool(pixabay_key()),
        "pexels": bool(pexels_key()),
        "gemini": bool(gemini_key()),
        "pollinations": bool(pollinations_key()),
        "sarvam": bool(sarvam_key()),
    }


def load_brand_prefs() -> dict:
    """Loads default Channel Name, Brand Position, and Logo from saved UI preferences."""
    saved = {}
    if UI_PREFS_FILE.exists():
        try:
            saved = json.loads(UI_PREFS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "channel_name": saved.get("channel_name", ""),
        "brand_position": saved.get("brand_position", "Top-Right"),
        "channel_logo_path": saved.get("channel_logo_path", ""),
        "voice_branding": saved.get("voice_branding", True),
    }


def save_brand_prefs(channel_name: str, brand_position: str, logo_path: str = "", voice_branding: bool = True) -> None:
    """Saves brand preferences for convenient reuse across projects."""
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
    if logo_path:
        existing["channel_logo_path"] = logo_path
    existing["voice_branding"] = bool(voice_branding)
    try:
        UI_PREFS_FILE.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    except Exception:
        pass
