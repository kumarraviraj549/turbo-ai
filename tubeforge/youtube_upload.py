"""Optional private-draft upload helpers for the official YouTube Data API v3."""
import http.client
import random
import socket
import time
from pathlib import Path
from typing import Optional

from PIL import Image

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.force-ssl"]
READONLY_SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]


def _google_modules():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        raise RuntimeError(
            "YouTube upload dependencies are missing; install google-api-python-client and google-auth-oauthlib."
        ) from exc
    return Request, Credentials, InstalledAppFlow, build, MediaFileUpload


def connect(client_secret: Path, token_path: Path, allow_browser: bool = False):
    """Load a saved OAuth token or explicitly open the local consent flow."""
    Request, Credentials, InstalledAppFlow, _, _ = _google_modules()
    client_secret, token_path = Path(client_secret), Path(token_path)
    if not client_secret.is_file():
        raise FileNotFoundError(f"YouTube OAuth client file not found: {client_secret}")
    credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES) if token_path.exists() else None
    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    if not credentials or not credentials.valid:
        if not allow_browser:
            raise RuntimeError("YouTube is not connected yet. Use Connect YouTube to authorize this local app.")
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
        credentials = flow.run_local_server(port=0, open_browser=True)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json(), encoding="utf-8")
    return credentials


def channel_title(client_secret: Path, token_path: Path, allow_browser: bool = False) -> str:
    Request, Credentials, InstalledAppFlow, build, _ = _google_modules()
    client_secret, token_path = Path(client_secret), Path(token_path)
    if not client_secret.is_file():
        raise FileNotFoundError(f"YouTube OAuth client file not found: {client_secret}")
    credentials = Credentials.from_authorized_user_file(str(token_path), READONLY_SCOPES) \
        if token_path.exists() else None
    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    if not credentials or not credentials.valid:
        if not allow_browser:
            raise RuntimeError("YouTube read-only verification is not authorized yet.")
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), READONLY_SCOPES)
        credentials = flow.run_local_server(port=0, open_browser=True)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json(), encoding="utf-8")
    response = build("youtube", "v3", credentials=credentials).channels().list(
        part="snippet", mine=True, maxResults=1
    ).execute()
    items = response.get("items") or []
    if not items:
        raise RuntimeError("The authorized Google account has no YouTube channel.")
    return str(items[0].get("snippet", {}).get("title") or "Connected YouTube channel")


def _thumbnail_mime_type(path: Path) -> str:
    try:
        with Image.open(path) as image:
            image_format = image.format
    except (OSError, ValueError) as exc:
        raise ValueError(f"Could not identify thumbnail image format: {path}") from exc
    mime_types = {"JPEG": "image/jpeg", "PNG": "image/png"}
    if image_format not in mime_types:
        raise ValueError("YouTube thumbnails must be JPEG or PNG images.")
    return mime_types[image_format]


def _next_chunk_with_retry(upload_request, max_retries: int = 10):
    from googleapiclient.errors import HttpError
    import httplib2

    retryable_network_errors = (
        httplib2.HttpLib2Error, OSError, http.client.NotConnected, http.client.IncompleteRead,
        http.client.ImproperConnectionState, http.client.CannotSendRequest, http.client.CannotSendHeader,
        http.client.ResponseNotReady, http.client.BadStatusLine, socket.timeout,
    )
    retryable_statuses = (500, 502, 503, 504)
    for retry in range(max_retries):
        try:
            return upload_request.next_chunk()
        except HttpError as exc:
            if getattr(exc.resp, "status", None) not in retryable_statuses or retry == max_retries - 1:
                raise
        except retryable_network_errors:
            if retry == max_retries - 1:
                raise
        time.sleep(2 ** retry + random.random())
    raise RuntimeError("YouTube resumable upload retry loop exited unexpectedly.")


def upload_private_draft(video_path: Path, client_secret: Path, token_path: Path, title: str,
                         description: str, tags: list, thumbnail: Optional[Path] = None,
                         captions_srt: Optional[Path] = None, caption_language: str = "en",
                         category_id: str = "27", default_language: str = "",
                         contains_synthetic_media: bool = False,
                         publish_at: str = "") -> str:
    """Upload resumably and strictly as private; attach optional thumbnail and SRT captions."""
    Request, Credentials, InstalledAppFlow, build, MediaFileUpload = _google_modules()
    credentials = connect(client_secret, token_path, allow_browser=False)
    video_path = Path(video_path)
    if not video_path.is_file():
        raise FileNotFoundError(f"Video file not found: {video_path}")
    service = build("youtube", "v3", credentials=credentials)
    snippet = {"title": str(title)[:100], "description": str(description)[:5000],
               "tags": [str(tag)[:100] for tag in (tags or [])[:500]],
               "categoryId": str(category_id)}
    if default_language.strip():
        snippet["defaultLanguage"] = default_language.strip()[:16]
    status = {"privacyStatus": "private", "selfDeclaredMadeForKids": False,
              "containsSyntheticMedia": bool(contains_synthetic_media)}
    if publish_at.strip():
        status["publishAt"] = publish_at.strip()
        status["privacyStatus"] = "private"
    request = service.videos().insert(
        part="snippet,status",
        body={"snippet": snippet, "status": status},
        media_body=MediaFileUpload(str(video_path), chunksize=8 * 1024 * 1024,
                                   resumable=True, mimetype="video/*"),
    )
    response = None
    while response is None:
        _, response = _next_chunk_with_retry(request)
    video_id = str(response.get("id") or "")
    if not video_id:
        raise RuntimeError("YouTube completed the upload without returning a video ID.")
    if thumbnail and Path(thumbnail).is_file():
        service.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(
            str(thumbnail), mimetype=_thumbnail_mime_type(Path(thumbnail))
        )).execute()
    if captions_srt and Path(captions_srt).is_file():
        service.captions().insert(
            part="snippet",
            body={"snippet": {"videoId": video_id, "language": caption_language[:16],
                              "name": caption_language[:16], "isDraft": False}},
            media_body=MediaFileUpload(str(captions_srt), mimetype="application/x-subrip"),
        ).execute()
    return video_id


def insert_playlist(video_ids: list, client_secret: Path, token_path: Path, title: str,
                    description: str = "") -> str:
    """Create a playlist and append videos; playlists.insert requires a title."""
    title = str(title or "").strip()
    if not title:
        raise ValueError("Playlist title is required.")
    _, _, _, build, _ = _google_modules()
    credentials = connect(Path(client_secret), Path(token_path), allow_browser=False)
    service = build("youtube", "v3", credentials=credentials)
    playlist = service.playlists().insert(
        part="snippet,status",
        body={"snippet": {"title": title[:150], "description": str(description)[:5000]},
              "status": {"privacyStatus": "private"}},
    ).execute()
    playlist_id = str(playlist.get("id") or "")
    if not playlist_id:
        raise RuntimeError("YouTube returned no playlist ID.")
    for video_id in video_ids:
        if not str(video_id).strip():
            continue
        service.playlistItems().insert(
            part="snippet",
            body={"snippet": {"playlistId": playlist_id,
                              "resourceId": {"kind": "youtube#video", "videoId": str(video_id)}}},
        ).execute()
    return playlist_id
