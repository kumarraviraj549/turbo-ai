"""Content-addressed optional thumbnail cutout and mobile-feed previews."""
import hashlib
import io
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image, ImageChops, ImageFilter


class ThumbnailLabError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def process_thumbnail(source: Path, cache_dir: Path, cutout_glow: bool = False) -> Tuple[Path, Optional[str]]:
    source, cache_dir = Path(source), Path(cache_dir)
    if not source.is_file():
        raise FileNotFoundError(f"Thumbnail not found: {source}")
    key = hashlib.sha256(f"thumbnail-lab-v1:{_sha256(source)}:{int(cutout_glow)}".encode()).hexdigest()
    cache_dir.mkdir(parents=True, exist_ok=True)
    output = cache_dir / f"{key[:24]}.jpg"
    warning_path = output.with_suffix(".warning")
    if output.is_file() and output.stat().st_size:
        warning = warning_path.read_text(encoding="utf-8") if warning_path.is_file() else None
        return output, warning or None
    image = Image.open(source).convert("RGB")
    warning = None
    if cutout_glow:
        try:
            from rembg import remove
        except ImportError:
            warning = "Optional rembg package is unavailable; showing the original thumbnail."
        else:
            try:
                encoded = io.BytesIO()
                image.save(encoded, format="PNG")
                foreground = Image.open(io.BytesIO(remove(encoded.getvalue()))).convert("RGBA")
                if foreground.size != image.size:
                    foreground = foreground.resize(image.size, Image.Resampling.LANCZOS)
                alpha = foreground.getchannel("A")
                if alpha.getextrema() == (0, 0) or alpha.getextrema() == (255, 255):
                    raise ThumbnailLabError("Foreground cutout returned an empty or full-frame mask.")
                backdrop = image.filter(ImageFilter.GaussianBlur(max(10, image.width // 36)))
                glow_mask = alpha.filter(ImageFilter.GaussianBlur(max(8, image.width // 48)))
                glow_mask = ImageChops.subtract(glow_mask, alpha.filter(ImageFilter.GaussianBlur(2)))
                glow = Image.new("RGBA", image.size, (34, 211, 238, 0))
                glow.putalpha(glow_mask.point(lambda value: min(185, int(value * 0.8))))
                composite = backdrop.convert("RGBA")
                composite.alpha_composite(glow)
                composite.alpha_composite(foreground)
                image = composite.convert("RGB")
            except ThumbnailLabError:
                raise
            except Exception as exc:
                raise ThumbnailLabError(f"Foreground cutout failed: {exc}") from exc
    temporary = output.with_name(output.stem + ".part.jpg")
    image.save(temporary, format="JPEG", quality=92, optimize=True)
    temporary.replace(output)
    if warning:
        warning_path.write_text(warning, encoding="utf-8")
    else:
        warning_path.unlink(missing_ok=True)
    return output, warning


def mobile_preview(source: Path, mode: str = "dark") -> Image.Image:
    """Returns a 168x94 thumbnail card preview with a local-only feed-card background."""
    source = Path(source)
    if mode not in ("dark", "light"):
        raise ValueError("Feed preview mode must be 'dark' or 'light'.")
    background = (24, 24, 27) if mode == "dark" else (250, 250, 250)
    thumb = Image.open(source).convert("RGB").resize((168, 94), Image.Resampling.LANCZOS)
    card = Image.new("RGB", (184, 142), background)
    card.paste(thumb, (8, 8))
    return card
