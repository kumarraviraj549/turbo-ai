"""Persistent, sequential overnight render queue."""
import json
import re
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from urllib.parse import urlparse

import requests

from . import config

QUEUE_PATH = config.OUTPUT_ROOT / "_queue.json"
_LOCK = threading.RLock()
_STOP = threading.Event()
_RUNNER: Optional[threading.Thread] = None


def _read() -> dict:
    if not QUEUE_PATH.is_file():
        return {"version": 1, "items": []}
    try:
        payload = json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Batch queue file is unreadable: {QUEUE_PATH}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise RuntimeError(f"Batch queue file has an invalid schema: {QUEUE_PATH}")
    return payload


def _write(payload: dict) -> None:
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = QUEUE_PATH.with_name(QUEUE_PATH.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(QUEUE_PATH)


def snapshot() -> List[dict]:
    global _RUNNER
    with _LOCK:
        payload = _read()
        if not (_RUNNER and _RUNNER.is_alive()):
            recovered = False
            for item in payload["items"]:
                if item.get("status") == "running":
                    item.update(status="pending", message="Recovered after an interrupted render.")
                    recovered = True
            if recovered:
                _write(payload)
        return [dict(item) for item in payload["items"]]


def add_projects(project_ids: Iterable[str], upload_ids: Optional[Iterable[str]] = None) -> List[dict]:
    from . import jobs

    upload_ids = set(upload_ids or [])
    added = []
    with _LOCK:
        payload = _read()
        existing = {item.get("project_id") for item in payload["items"]}
        for project_id in dict.fromkeys(str(value) for value in project_ids):
            if not re.fullmatch(r"[A-Za-z0-9_-]+", project_id):
                raise ValueError(f"Invalid project ID: {project_id}")
            project = jobs.load_project(project_id)
            if not project or not project.plan:
                raise ValueError(f"Project {project_id} has no saved plan.")
            bible = jobs.ensure_visual_bible(project)
            if bible.get("required") and not bible.get("approved"):
                raise ValueError(f"Project {project_id} needs an approved Visual Bible before queueing.")
            if project_id in existing:
                continue
            row = {
                "project_id": project_id,
                "title": (project.plan.title or project.data.get("idea") or project_id)[:120],
                "status": "pending",
                "upload_private": project_id in upload_ids,
                "message": "",
                "added_at": time.time(),
                "finished_at": 0.0,
            }
            payload["items"].append(row)
            added.append(dict(row))
            existing.add(project_id)
        _write(payload)
    return added


def clear_finished() -> int:
    with _LOCK:
        payload = _read()
        before = len(payload["items"])
        payload["items"] = [item for item in payload["items"] if item.get("status") in ("pending", "running")]
        _write(payload)
        return before - len(payload["items"])


def _update(project_id: str, **fields) -> None:
    with _LOCK:
        payload = _read()
        row = next((item for item in payload["items"] if item.get("project_id") == project_id), None)
        if row is not None:
            row.update(fields)
            _write(payload)


def _notify(title: str, status: str) -> Optional[str]:
    url = config.secret("TUBEFORGE_BATCH_WEBHOOK_URL")
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return "Completion webhook must be an HTTPS URL."
    message = f"TubeForge batch render {status}: {title}"
    payload = {"text": message}
    if parsed.hostname.lower() == "api.telegram.org":
        chat_id = config.secret("TUBEFORGE_TELEGRAM_CHAT_ID")
        if not chat_id:
            return "Telegram webhook requires TUBEFORGE_TELEGRAM_CHAT_ID."
        payload = {"chat_id": chat_id, "text": message}
    try:
        response = requests.post(url, json=payload, timeout=10, allow_redirects=False)
    except requests.RequestException:
        return "Completion webhook request failed."
    if not response.ok:
        return f"Completion webhook returned HTTP {response.status_code}."
    return None


def _upload_private(project) -> Optional[str]:
    from . import youtube_upload

    outputs = project.data.get("outputs") or {}
    video_path = Path(str(outputs.get("video") or ""))
    if not video_path.is_file():
        return "Private draft upload skipped: rendered video is missing."
    client = Path(config.youtube_client_secret_path())
    token = config.CACHE_DIR / "youtube" / "token.json"
    if not client.is_file() or not token.is_file():
        return "Private draft upload skipped: YouTube OAuth is not authorized."
    if (project.settings.get("youtube_contains_synthetic_media")
            and not project.settings.get("youtube_disclosure_acknowledged")):
        return "Private draft upload skipped: synthetic-media disclosure has not been acknowledged."
    plan = project.plan
    try:
        youtube_upload.upload_private_draft(
            video_path, client, token,
            title=(plan.youtube_title or plan.title) if plan else "TubeForge video",
            description=outputs.get("description", ""),
            tags=plan.tags if plan else [],
            thumbnail=Path(outputs["thumbnail"]) if outputs.get("thumbnail") else None,
            captions_srt=Path(outputs["srt"]) if outputs.get("srt") else None,
            caption_language=project.settings.get("youtube_default_language") or "en",
            category_id=project.settings.get("youtube_category_id", "27"),
            default_language=project.settings.get("youtube_default_language", ""),
            contains_synthetic_media=bool(project.settings.get("youtube_contains_synthetic_media", False)),
            publish_at=project.settings.get("youtube_publish_at", ""),
        )
    except Exception as exc:
        return f"Private draft upload failed: {str(exc)[:240]}"
    return None


def _run_items() -> None:
    from . import jobs

    while not _STOP.is_set():
        pending = next((item for item in snapshot() if item.get("status") == "pending"), None)
        if pending is None:
            return
        project_id = pending["project_id"]
        project = jobs.load_project(project_id)
        if project is None:
            _update(project_id, status="error", message="Project file is missing.", finished_at=time.time())
            _notify(pending.get("title", project_id), "failed")
            continue
        if jobs.MANAGER.is_running(project_id):
            time.sleep(1)
            continue
        _update(project_id, status="running", message="Rendering.", started_at=time.time())
        if not jobs.MANAGER.start_render(project):
            _update(project_id, status="pending", message="Waiting for render worker.")
            time.sleep(1)
            continue
        while jobs.MANAGER.is_running(project_id):
            time.sleep(0.5)
        job = project.data.get("job") or {}
        status = job.get("status", "error")
        if status != "done":
            message = str(job.get("error") or job.get("stage") or "Render did not finish.")[:300]
            _update(project_id, status="error", message=message, finished_at=time.time())
            _notify(pending.get("title", project_id), "failed")
            continue
        warning = None
        if pending.get("upload_private"):
            warning = _upload_private(project)
        _update(project_id, status="done", message=warning or "Render complete.", finished_at=time.time())
        webhook_error = _notify(pending.get("title", project_id), "complete")
        if webhook_error:
            _update(project_id, webhook_error=webhook_error)


def start() -> bool:
    """Starts the queue explicitly; queued work is never run automatically on application startup."""
    global _RUNNER
    with _LOCK:
        if _RUNNER and _RUNNER.is_alive():
            return False
        if not any(item.get("status") == "pending" for item in snapshot()):
            return False
        _STOP.clear()
        _RUNNER = threading.Thread(target=_run_items, name="tubeforge-batch-queue", daemon=True)
        _RUNNER.start()
        return True


def pause() -> None:
    _STOP.set()
