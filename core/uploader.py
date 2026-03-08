"""YouTube Data API v3 — 予約投稿アップローダー（マルチユーザー対応）"""
import json
from pathlib import Path
from datetime import datetime, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow, Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",  # playlistItems.insert に必要
]

_BASE = Path(__file__).parent.parent
TOKEN_PATH = _BASE / "credentials" / "token.json"
CLIENT_SECRET_PATH = _BASE / "credentials" / "client_secret.json"


# ─────────────────────────────────────────────
# シングルユーザー（ファイルベース）— 後方互換
# ─────────────────────────────────────────────

def get_youtube_service():
    """認証済み YouTube サービスを返す（シングルユーザー・ファイルベース）"""
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


def check_auth() -> bool:
    """シングルユーザー認証確認（スコープ検証込み）"""
    if not TOKEN_PATH.exists():
        return False
    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        stored = set(creds.scopes or [])
        if stored:
            required = set(SCOPES)
            if not required.issubset(stored):
                return False
        return creds.valid or bool(creds.refresh_token)
    except Exception:
        return False


# ─────────────────────────────────────────────
# マルチユーザー Web OAuth
# ─────────────────────────────────────────────

def get_auth_url(redirect_uri: str) -> tuple[str, str]:
    """
    Web OAuth 認証 URL を生成。
    Returns: (auth_url, state)
    """
    if not CLIENT_SECRET_PATH.exists():
        raise FileNotFoundError(
            "credentials/client_secret.json が見つかりません。"
            "ステップ4の認証設定から client_secret.json をアップロードしてください。"
        )
    # web / installed どちらの形式も Flow が読み込める
    flow = Flow.from_client_secrets_file(
        str(CLIENT_SECRET_PATH),
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return auth_url, state


def exchange_code(code: str, redirect_uri: str) -> str:
    """
    認証コードをトークンに交換。
    Returns: token_json 文字列
    """
    if not CLIENT_SECRET_PATH.exists():
        raise FileNotFoundError("credentials/client_secret.json が見つかりません。")
    flow = Flow.from_client_secrets_file(
        str(CLIENT_SECRET_PATH),
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )
    flow.fetch_token(code=code)
    return flow.credentials.to_json()


def get_youtube_service_from_token(token_json: dict):
    """
    トークン辞書から YouTube サービスを構築（マルチユーザー用）。
    Returns: (youtube_service, updated_token_dict)
    """
    creds = Credentials.from_authorized_user_info(token_json, SCOPES)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:
            raise RuntimeError(
                f"YouTube認証トークンのリフレッシュに失敗しました。再接続してください。({e})"
            ) from e
    if not creds.valid:
        raise RuntimeError("YouTube認証が無効です。再接続してください。")
    service = build("youtube", "v3", credentials=creds)
    return service, json.loads(creds.to_json())


def refresh_token_if_needed(token_json: dict) -> dict:
    """
    必要であればトークンをリフレッシュして返す。
    変更なければ同じ dict を返す。
    """
    creds = Credentials.from_authorized_user_info(token_json, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        return json.loads(creds.to_json())
    return token_json


def check_token_valid(token_json: dict) -> bool:
    """トークンが有効か確認（リフレッシュは行わない）"""
    try:
        creds = Credentials.from_authorized_user_info(token_json, SCOPES)
        return creds.valid or bool(creds.refresh_token)
    except Exception:
        return False


# ─────────────────────────────────────────────
# アップロード
# ─────────────────────────────────────────────

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
    token_json: dict = None,   # マルチユーザー用（指定時は file-based auth をスキップ）
) -> str:
    """
    Shorts をアップロードして予約投稿に設定する。

    token_json が指定された場合はそのトークンを使用（マルチユーザー対応）。
    publishAt は UTC datetime で渡すこと。

    Returns: YouTube video_id (str)
    """
    if token_json:
        youtube, _ = get_youtube_service_from_token(token_json)
    else:
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
            pass

    return video_id
