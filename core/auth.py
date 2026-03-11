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


def get_google_oauth_url(redirect_url: str) -> str:
    """
    Supabase 経由で Google OAuth URL を取得する。
    PKCE を使わない implicit flow クライアントで URL を生成することで、
    Supabase が #access_token=SUPABASE_JWT でコールバックするよう強制する。
    ※ response_type=token を手動で付与してはいけない（Google に直接渡って
      Supabase をスキップしてしまう）。
    """
    try:
        cfg = _get_supabase_config()
        if not cfg:
            return ""
        from supabase import create_client
        # implicit flow クライアント（code_challenge を生成しない）
        # → URL に code_challenge が含まれない → Supabase が implicit mode で動作
        # → Supabase が #access_token=SUPABASE_JWT をアプリに返す
        try:
            from supabase.lib.client_options import ClientOptions
            from gotrue.types import AuthFlowType
            sb = create_client(cfg[0], cfg[1],
                               options=ClientOptions(flow_type=AuthFlowType.implicit))
        except Exception:
            # フォールバック: デフォルトクライアント（PKCE になるが後続で処理）
            sb = create_client(cfg[0], cfg[1])
        res = sb.auth.sign_in_with_oauth({
            "provider": "google",
            "options": {
                "redirect_to": redirect_url,
                "skip_browser_redirect": True,
            },
        })
        return res.url or ""
    except Exception:
        return ""


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
