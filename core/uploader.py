"""YouTube Data API v3 — 予約投稿アップローダー"""
from pathlib import Path
from datetime import datetime, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",  # playlistItems.insert に必要
]

_BASE = Path(__file__).parent.parent
TOKEN_PATH = _BASE / "credentials" / "token.json"
CLIENT_SECRET_PATH = _BASE / "credentials" / "client_secret.json"


def get_youtube_service():
    """認証済み YouTube サービスを返す（初回はブラウザ認証）"""
    creds = None

    if TOKEN_PATH.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        except Exception:
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                # スコープ変更などでリフレッシュ失敗 → 旧トークンを削除して再認証
                try:
                    TOKEN_PATH.unlink()
                except Exception:
                    pass
                creds = None

        if not creds or not creds.valid:
            if not CLIENT_SECRET_PATH.exists():
                raise FileNotFoundError(
                    "credentials/client_secret.json が見つかりません。"
                    "Google Cloud Console からダウンロードしてください。"
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CLIENT_SECRET_PATH), SCOPES
            )
            creds = flow.run_local_server(port=0)

        TOKEN_PATH.parent.mkdir(exist_ok=True)
        TOKEN_PATH.write_text(creds.to_json())

    return build("youtube", "v3", credentials=creds)


def upload_shorts(
    video_path: Path,
    title: str,
    description: str,
    tags: list,
    publish_at: datetime,
    category_id: str = "22",
    playlist_id: str = None,
    made_for_kids: bool = False,
    age_restricted: bool = False,
) -> str:
    """
    Shorts をアップロードして予約投稿に設定する。
    publishAt は UTC datetime で渡すこと。

    playlist_id   : 追加先の再生リスト ID（任意）
    made_for_kids : True = 子ども向けコンテンツ
    age_restricted: True = 年齢制限（18歳以上）
    Returns: YouTube video_id
    """
    youtube = get_youtube_service()

    # publishAt は ISO 8601 / UTC
    if publish_at.tzinfo is None:
        publish_at = publish_at.replace(tzinfo=timezone.utc)
    publish_str = publish_at.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": tags[:30],
            "categoryId": category_id,
            "defaultLanguage": "ja",
        },
        "status": {
            "privacyStatus": "private",   # scheduled は private + publishAt
            "publishAt": publish_str,
            "selfDeclaredMadeForKids": made_for_kids,
            "madeForKids": made_for_kids,
        },
    }

    # 年齢制限
    parts = "snippet,status"
    if age_restricted:
        body["contentRating"] = {"ytRating": "ytAgeRestricted"}
        parts = "snippet,status,contentRating"

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        resumable=True,
        chunksize=5 * 1024 * 1024,  # 5 MB
    )

    request = youtube.videos().insert(
        part=parts,
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        _, response = request.next_chunk()

    video_id = response["id"]

    # 再生リストへ追加
    if playlist_id:
        try:
            youtube.playlistItems().insert(
                part="snippet",
                body={
                    "snippet": {
                        "playlistId": playlist_id,
                        "resourceId": {
                            "kind": "youtube#video",
                            "videoId": video_id,
                        },
                    }
                },
            ).execute()
        except Exception:
            pass  # プレイリスト追加失敗は無視（アップロード自体は成功）

    return video_id


def check_auth() -> bool:
    """認証済みかつ必要スコープを持つか確認（API 呼び出しなし）"""
    if not TOKEN_PATH.exists():
        return False
    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        # 保存済みスコープが SCOPES をすべて含むか検証
        stored = set(creds.scopes or [])
        if stored:  # スコープ情報が保存されている場合のみチェック
            required = set(SCOPES)
            if not required.issubset(stored):
                return False  # スコープ不足 → 再認証が必要
        return creds.valid or bool(creds.refresh_token)
    except Exception:
        return False
