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


def is_supabase_configured() -> bool:
    """Supabase が設定されているか確認（マルチユーザーモード判定に使用）"""
    return _get_supabase_config() is not None


def get_supabase():
    """Supabase クライアントを返す"""
    from supabase import create_client
    cfg = _get_supabase_config()
    if not cfg:
        raise RuntimeError(
            "Supabase が設定されていません。"
            "Streamlit Secrets に [supabase] url と anon_key を設定してください。"
        )
    return create_client(cfg[0], cfg[1])


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
