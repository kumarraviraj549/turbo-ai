"""Optional private-draft upload helpers for the official YouTube Data API v3."""
from pathlib import Path
from typing import Optional

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


def upload_private_draft(video_path: Path, client_secret: Path, token_path: Path, title: str,
                         description: str, tags: list, thumbnail: Optional[Path] = None,
                         captions_srt: Optional[Path] = None, caption_language: str = "en") -> str:
    """Upload resumably and strictly as private; attach optional thumbnail and SRT captions."""
    Request, Credentials, InstalledAppFlow, build, MediaFileUpload = _google_modules()
    credentials = connect(client_secret, token_path, allow_browser=False)
    video_path = Path(video_path)
    if not video_path.is_file():
        raise FileNotFoundError(f"Video file not found: {video_path}")
    service = build("youtube", "v3", credentials=credentials)
    request = service.videos().insert(
        part="snippet,status",
        body={"snippet": {"title": str(title)[:100], "description": str(description)[:5000],
                          "tags": [str(tag)[:100] for tag in (tags or [])[:500]],
                          "categoryId": "27"},
              "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False}},
        media_body=MediaFileUpload(str(video_path), chunksize=8 * 1024 * 1024,
                                   resumable=True, mimetype="video/*"),
    )
    response = None
    while response is None:
        _, response = request.next_chunk()
    video_id = str(response.get("id") or "")
    if not video_id:
        raise RuntimeError("YouTube completed the upload without returning a video ID.")
    if thumbnail and Path(thumbnail).is_file():
        service.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(
            str(thumbnail), mimetype="image/jpeg"
        )).execute()
    if captions_srt and Path(captions_srt).is_file():
        service.captions().insert(
            part="snippet",
            body={"snippet": {"videoId": video_id, "language": caption_language[:16],
                              "name": caption_language[:16], "isDraft": False}},
            media_body=MediaFileUpload(str(captions_srt), mimetype="application/x-subrip"),
        ).execute()
    return video_id
