"""
Supabase 認証ヘルパー
マルチユーザー対応のログイン / 会員登録 / セッション管理
"""
import os


def _get_supabase_config() -> tuple[str, str] | None:
    """Supabase URL と anon_key を取得。未設定なら None を返す"""
    try:
        import streamlit as st
        url = st.secrets["supabase"]["url"]
        key = st.secrets["supabase"]["anon_key"]
        if url and key:
            return url, key
    except Exception:
        pass
    # 環境変数フォールバック
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_ANON_KEY", "")
    if url and key:
        return url, key
    return None


def _get_service_role_key() -> str | None:
    """service_role_key を取得。未設定なら None を返す"""
    try:
        import streamlit as st
        key = st.secrets["supabase"].get("service_role_key", "")
        if key:
            return key
    except Exception:
        pass
    return os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "") or None


def is_supabase_configured() -> bool:
    """Supabase が設定されているか確認（マルチユーザーモード判定に使用）"""
    return _get_supabase_config() is not None


def get_supabase():
    """Supabase クライアントを返す（anon key 使用 / auth 操作用）"""
    from supabase import create_client
    cfg = _get_supabase_config()
    if not cfg:
        raise RuntimeError(
            "Supabase が設定されていません。"
            "Streamlit Secrets に [supabase] url と anon_key を設定してください。"
        )
    return create_client(cfg[0], cfg[1])


def get_supabase_admin():
    """
    サービスロールキーを使った Supabase クライアントを返す。
    RLS をバイパスするため、サーバーサイドの DB 操作（トークン保存・使用量更新等）に使用。
    service_role_key 未設定の場合は anon クライアントで代用（RLS エラーの可能性あり）。
    """
    from supabase import create_client
    cfg = _get_supabase_config()
    if not cfg:
        raise RuntimeError(
            "Supabase が設定されていません。"
            "Streamlit Secrets に [supabase] url と anon_key を設定してください。"
        )
    url = cfg[0]
    service_key = _get_service_role_key()
    if service_key:
        return create_client(url, service_key)
    # Fallback: anon key（RLS 設定によっては失敗する場合あり）
    return create_client(url, cfg[1])


def sign_up(email: str, password: str):
    """メール＋パスワードで会員登録"""
    sb = get_supabase()
    return sb.auth.sign_up({"email": email, "password": password})


def sign_in(email: str, password: str):
    """メール＋パスワードでログイン"""
    sb = get_supabase()
    return sb.auth.sign_in_with_password({"email": email, "password": password})


def sign_out():
    """ログアウト"""
    try:
        sb = get_supabase()
        sb.auth.sign_out()
    except Exception:
        pass


def get_user_by_token(access_token: str):
    """アクセストークンからユーザー情報を取得"""
    try:
        sb = get_supabase()
        return sb.auth.get_user(access_token)
    except Exception:
        return None


def _pkce_pair() -> tuple[str, str]:
    """PKCE code_verifier と code_challenge のペアを生成"""
    import hashlib, secrets, base64 as _b64
    verifier  = secrets.token_urlsafe(96)
    challenge = _b64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return verifier, challenge


def get_google_oauth_url(redirect_url: str) -> tuple[str, str]:
    """
    Supabase Google OAuth の認証 URL と code_verifier を返す。
    PKCE を自前で生成し、code_verifier を state パラメータに埋め込む。
    → リダイレクト後に Python が state から verifier を読み出せるので
      ブラウザストレージ不要（iOS Safari でも動作）。
    戻り値: (url, code_verifier) — 失敗時は ("", "")
    """
    try:
        cfg = _get_supabase_config()
        if not cfg:
            return "", ""
        import base64 as _b64, json as _json
        from urllib.parse import urlencode
        verifier, challenge = _pkce_pair()
        # verifier を state に JSON エンコードして埋め込む
        state_payload = _b64.urlsafe_b64encode(
            _json.dumps({"cv": verifier}).encode()
        ).rstrip(b"=").decode()
        params = {
            "provider": "google",
            "redirect_to": redirect_url,
            "code_challenge": challenge,
            "code_challenge_method": "s256",
            "state": state_payload,
        }
        base = cfg[0].rstrip("/")
        url  = f"{base}/auth/v1/authorize?{urlencode(params)}"
        return url, verifier
    except Exception:
        return "", ""


def refresh_session(refresh_token: str) -> dict | None:
    """refresh_token でセッションを復元し、ユーザー情報と新 refresh_token を返す"""
    try:
        sb = get_supabase()
        resp = sb.auth.refresh_session(refresh_token)
        if resp and resp.session and resp.user:
            return {
                "user_id":       resp.user.id,
                "user_email":    resp.user.email,
                "refresh_token": resp.session.refresh_token,
            }
    except Exception:
        pass
    return None
