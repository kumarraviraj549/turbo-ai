"""Local, content-hashed B-roll index with a dependency-free lexical search fallback."""
import hashlib
import json
import re
import shutil
import subprocess
import threading
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import numpy as np

from . import config
from .config import OUTPUT_ROOT

VAULT_ROOT = OUTPUT_ROOT / "vault"
INDEX_PATH = VAULT_ROOT / "index.json"
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".png", ".jpg", ".jpeg", ".webp"}
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
_LOCK = threading.RLock()
_WORD_RE = re.compile(r"[a-z0-9]{2,}")
_CLIP_MODEL = None
_CLIP_ERROR = ""


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_index() -> List[dict]:
    if not INDEX_PATH.is_file():
        return []
    try:
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"B-roll vault index is unreadable: {INDEX_PATH}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("assets"), list):
        raise RuntimeError(f"B-roll vault index has an invalid schema: {INDEX_PATH}")
    return [row for row in data["assets"] if isinstance(row, dict)]


def _write_index(rows: List[dict]) -> None:
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = INDEX_PATH.with_name(INDEX_PATH.name + ".tmp")
    temporary.write_text(json.dumps({"version": 1, "assets": rows}, indent=2), encoding="utf-8")
    temporary.replace(INDEX_PATH)


def _tags_for(path: Path, tags: Optional[Iterable[str]] = None) -> List[str]:
    values = list(tags or []) + [path.stem, *path.parent.parts[-2:]]
    tokens = []
    for value in values:
        tokens.extend(_WORD_RE.findall(str(value).lower().replace("_", " ").replace("-", " ")))
    return sorted(set(tokens))


def index_files(paths: Iterable[Path], tags: Optional[Iterable[str]] = None) -> List[dict]:
    added = []
    with _LOCK:
        rows = _read_index()
        by_hash = {row.get("sha256"): row for row in rows}
        for raw_path in paths:
            path = Path(raw_path).expanduser().resolve()
            if not path.is_file() or path.suffix.lower() not in ALLOWED_EXTENSIONS:
                continue
            digest = _hash_file(path)
            row = {
                "sha256": digest, "path": str(path), "name": path.name,
                "kind": "video" if path.suffix.lower() in {".mp4", ".mov", ".m4v", ".mkv", ".webm"} else "image",
                "tags": _tags_for(path, tags), "size": path.stat().st_size,
            }
            if digest in by_hash:
                old = by_hash[digest]
                row["tags"] = sorted(set(old.get("tags", [])) | set(row["tags"]))
                rows[rows.index(old)] = row
            else:
                rows.append(row)
            by_hash[digest] = row
            added.append(row)
        _write_index(rows)
    return added


def store_upload(name: str, contents: bytes, tags: Optional[Iterable[str]] = None) -> Path:
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError(f"Unsupported vault file type: {suffix or '(none)'}")
    if not contents or len(contents) > MAX_UPLOAD_BYTES:
        raise ValueError("Vault uploads must be non-empty and no larger than 512 MiB.")
    digest = hashlib.sha256(contents).hexdigest()
    target = VAULT_ROOT / "media" / f"{digest[:16]}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.is_file():
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_bytes(contents)
        temporary.replace(target)
    index_files([target], tags)
    return target


def index_directory(directory: Path, tags: Optional[Iterable[str]] = None) -> List[dict]:
    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"B-roll vault source directory does not exist: {root}")
    paths = (
        path for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in ALLOWED_EXTENSIONS
    )
    return index_files(paths, tags)


def search(query: str, kind: Optional[str] = None, limit: int = 20) -> List[dict]:
    terms = set(_WORD_RE.findall(str(query or "").lower()))
    if not terms:
        return []
    rows = [row for row in _read_index() if Path(str(row.get("path", ""))).is_file()
            and (kind is None or row.get("kind") == kind)]
    documents = [set(row.get("tags") or _tags_for(Path(row["path"]))) for row in rows]
    doc_freq = Counter(term for doc in documents for term in set(doc))
    ranked = []
    total = max(1, len(documents))
    for row, tokens in zip(rows, documents):
        overlap = terms & tokens
        if not overlap:
            continue
        score = sum(1.0 + (total / (1 + doc_freq[token])) for token in overlap)
        ranked.append((score, row))
    ranked.sort(key=lambda pair: (-pair[0], pair[1].get("name", "").casefold()))
    lexical_rows = [row for _, row in ranked[:100]]
    candidate_hashes = {str(row.get("sha256")) for row in lexical_rows}
    semantic_rows = list(lexical_rows)
    semantic_rows.extend(
        row for row in rows if str(row.get("sha256")) not in candidate_hashes
    )
    semantic_rows = semantic_rows[:100]
    semantic = _semantic_scores(query, semantic_rows)
    if semantic:
        max_lexical = max((score for score, _ in ranked), default=1.0)
        lexical_scores = {str(row.get("sha256")): score for score, row in ranked}
        ranked = [
            (
                0.65 * lexical_scores.get(str(row.get("sha256")), 0.0) / max_lexical
                + 0.35 * semantic.get(str(row.get("sha256")), 0.0),
                row,
            )
            for row in semantic_rows
        ]
    return [dict(row, score=score) for score, row in
            sorted(ranked, key=lambda pair: (-pair[0], pair[1].get("name", "").casefold()))
            [:max(1, min(int(limit), 100))]]


def find_video(query: str) -> Optional[dict]:
    hits = search(query, kind="video", limit=10)
    return hits[0] if hits else None


def find_image(query: str) -> Optional[dict]:
    hits = search(query, kind="image", limit=10)
    return hits[0] if hits else None


def _clip_setup():
    global _CLIP_MODEL, _CLIP_ERROR
    if _CLIP_MODEL is not None:
        return _CLIP_MODEL
    checkpoint = Path(config.secret("TUBEFORGE_VAULT_CLIP_CHECKPOINT")).expanduser()
    if not checkpoint.is_file():
        return None
    try:
        import open_clip
        import torch

        architecture = config.secret("TUBEFORGE_VAULT_CLIP_ARCH") or "ViT-B-32"
        model, _, preprocess = open_clip.create_model_and_transforms(architecture, pretrained=None)
        weights = torch.load(checkpoint, map_location="cpu", weights_only=True)
        if isinstance(weights, dict) and isinstance(weights.get("state_dict"), dict):
            weights = weights["state_dict"]
        model.load_state_dict(weights, strict=False)
        model.eval()
        _CLIP_MODEL = (model, preprocess, open_clip.get_tokenizer(architecture), torch)
        _CLIP_ERROR = ""
    except Exception as exc:
        _CLIP_ERROR = f"Local CLIP checkpoint could not be loaded: {type(exc).__name__}."
        return None
    return _CLIP_MODEL


def semantic_status() -> str:
    if not config.secret("TUBEFORGE_VAULT_CLIP_CHECKPOINT"):
        return "TF-IDF/tag search (local CLIP checkpoint not configured)"
    if _CLIP_ERROR:
        return _CLIP_ERROR
    if _CLIP_MODEL:
        return "Local CLIP embeddings enabled"
    if not Path(config.secret("TUBEFORGE_VAULT_CLIP_CHECKPOINT")).expanduser().is_file():
        return "TF-IDF/tag search (configured CLIP checkpoint is missing)"
    return "Local CLIP checkpoint is ready to load"


def _image_for_asset(row: dict) -> Optional[Path]:
    path = Path(str(row.get("path", "")))
    if row.get("kind") == "image":
        return path
    digest = str(row.get("sha256") or _hash_file(path))
    cache_dir = VAULT_ROOT / "embeddings"
    cache_dir.mkdir(parents=True, exist_ok=True)
    frame = cache_dir / f"{digest[:24]}.jpg"
    if not frame.is_file():
        probe = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-sseof", "-1", "-i", str(path),
             "-frames:v", "1", str(frame)],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if probe.returncode or not frame.is_file():
            frame.unlink(missing_ok=True)
            global _CLIP_ERROR
            _CLIP_ERROR = "Local CLIP could not extract a video frame; tag search remains active."
            return None
    return frame


def _encode_image(model_tuple, path: Path) -> Optional[np.ndarray]:
    model, preprocess, _, torch = model_tuple
    vector_path = VAULT_ROOT / "embeddings" / f"{_hash_file(path)[:24]}.json"
    if vector_path.is_file():
        try:
            return np.asarray(json.loads(vector_path.read_text(encoding="utf-8")), dtype=np.float32)
        except (OSError, ValueError, TypeError):
            vector_path.unlink(missing_ok=True)
    try:
        from PIL import Image

        image = preprocess(Image.open(path).convert("RGB")).unsqueeze(0)
        with torch.no_grad():
            vector = model.encode_image(image).float()
            vector /= vector.norm(dim=-1, keepdim=True).clamp(min=1e-8)
        values = vector[0].cpu().numpy().astype(np.float32)
        vector_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = vector_path.with_name(vector_path.name + ".tmp")
        temporary.write_text(json.dumps(values.tolist()), encoding="utf-8")
        temporary.replace(vector_path)
        return values
    except Exception as exc:
        global _CLIP_ERROR
        _CLIP_ERROR = f"Local CLIP image embedding failed; tag search remains active ({type(exc).__name__})."
        return None


def _semantic_scores(query: str, rows: List[dict]) -> Dict[str, float]:
    model_tuple = _clip_setup()
    if not model_tuple:
        return {}
    model, _, tokenizer, torch = model_tuple
    try:
        with torch.no_grad():
            text = model.encode_text(tokenizer([query])).float()
            text /= text.norm(dim=-1, keepdim=True).clamp(min=1e-8)
        query_vector = text[0].cpu().numpy().astype(np.float32)
        scores = {}
        for row in rows:
            image = _image_for_asset(row)
            if image is None:
                continue
            vector = _encode_image(model_tuple, image)
            if vector is not None and vector.shape == query_vector.shape:
                scores[str(row.get("sha256"))] = max(0.0, float(np.dot(query_vector, vector)))
        return scores
    except Exception as exc:
        global _CLIP_ERROR
        _CLIP_ERROR = f"Local CLIP search failed; TF-IDF remains active ({type(exc).__name__})."
        return {}


def copy_to_project(path: Path, project_dir: Path) -> Path:
    source = Path(path).resolve()
    if not source.is_file() or source.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ValueError("Selected vault asset is missing or has an unsupported media type.")
    digest = _hash_file(source)
    target = Path(project_dir) / "vault_assets" / f"{digest[:20]}{source.suffix.lower()}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.is_file():
        temporary = target.with_name(target.name + ".tmp")
        shutil.copy2(source, temporary)
        temporary.replace(target)
    return target
