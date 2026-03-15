#!/usr/bin/env python3
"""
YouTubeショート 自動切り抜き・投稿予約アプリ
1本の動画URL → 10本のショートを自動選定 → 予約投稿
"""
import os
import sys
import base64
import random
import json
import time
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st

BASE_DIR   = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"
CREDS_DIR  = BASE_DIR / "credentials"
sys.path.insert(0, str(BASE_DIR))

# ── サーバー起動時: Streamlit Secrets / 環境変数から認証情報を復元 ──
def _restore_credentials():
    """
    Streamlit Community Cloud の Secrets または環境変数から
    credentials/ 以下のファイルを復元する。
    ローカル開発時はファイルが既にあるのでスキップ。
    """
    CREDS_DIR.mkdir(parents=True, exist_ok=True)

    # client_secret.json — Secrets が設定されていれば常に上書き（更新反映のため）
    cs_path = CREDS_DIR / "client_secret.json"
    raw = None
    try:
        raw = st.secrets["youtube"]["client_secret_json"]
    except Exception:
        raw = os.environ.get("YOUTUBE_CLIENT_SECRET")
    if raw:
        raw = raw.strip()
        if raw.startswith("{"):
            # 生のJSON文字列
            cs_path.write_text(raw, encoding="utf-8")
        else:
            # Base64エンコード済み
            try:
                cs_path.write_bytes(base64.b64decode(raw))
            except Exception:
                cs_path.write_text(raw, encoding="utf-8")

    # token.json
    tk_path = CREDS_DIR / "token.json"
    if not tk_path.exists():
        raw = None
        try:
            raw = st.secrets["youtube"]["token_json"]
        except Exception:
            raw = os.environ.get("YOUTUBE_TOKEN")
        if raw:
            try:
                tk_path.write_bytes(base64.b64decode(raw))
            except Exception:
                tk_path.write_text(raw)

    # cookies.txt — YouTube ダウンロード用（Netscape 形式）
    ck_path = CREDS_DIR / "cookies.txt"
    raw_ck = None

    # 優先順位1: Supabase site_settings（管理パネルから再起動なしで更新可能）
    try:
        from core.db import get_site_setting as _gss
        raw_ck = _gss("youtube_cookies")
    except Exception:
        pass

    # 優先順位2: Streamlit Secrets（初期設定 / フォールバック）
    if not raw_ck:
        try:
            raw_ck = st.secrets["youtube"]["cookies"]
        except Exception:
            raw_ck = os.environ.get("YOUTUBE_COOKIES")
    if raw_ck:
        raw_ck = raw_ck.strip()
        # JSON形式（ブラウザ開発者ツールからのエクスポート等）→ Netscape形式に変換
        if raw_ck.startswith("["):
            try:
                import json as _json
                cookies_list = _json.loads(raw_ck)
                lines = ["# Netscape HTTP Cookie File"]
                for c in cookies_list:
                    domain = c.get("domain", "")
                    flag = "TRUE" if domain.startswith(".") else "FALSE"
                    path = c.get("path", "/")
                    secure = "TRUE" if c.get("secure", False) else "FALSE"
                    expiry = str(int(c.get("expirationDate", 0)))
                    name = c.get("name", "")
                    value = c.get("value", "")
                    lines.append(f"{domain}\t{flag}\t{path}\t{secure}\t{expiry}\t{name}\t{value}")
                raw_ck = "\n".join(lines)
            except Exception:
                pass  # 変換失敗時はそのまま書き込む
        ck_path.write_text(raw_ck, encoding="utf-8")

    # OAuth2 トークン — Supabase から復元（cookies よりも優先度が高い）
    try:
        from core.db import get_site_setting as _gss2
        from core.downloader import restore_oauth2_token as _rot
        _oauth2_json = _gss2("youtube_oauth2_token")
        if _oauth2_json:
            _rot(_oauth2_json)
    except Exception:
        pass


_restore_credentials()


# ══════════════════════════════════════════════════════════
# マルチユーザー認証ヘルパー
# ══════════════════════════════════════════════════════════

def _is_multi_user_mode() -> bool:
    """Supabase が設定されていればマルチユーザーモード"""
    try:
        from core.auth import is_supabase_configured
        return is_supabase_configured()
    except Exception:
        return False


def _get_app_url() -> str:
    """リダイレクト URI 用アプリ URL を取得"""
    try:
        return st.secrets["app"]["url"]
    except Exception:
        return os.environ.get("APP_URL", "http://localhost:8501")


def _is_admin() -> bool:
    """現在のログインユーザーが管理者かチェック"""
    try:
        admin_emails = [e.strip() for e in st.secrets["admin"]["emails"].split(",")]
    except Exception:
        env_val = os.environ.get("ADMIN_EMAILS", "")
        admin_emails = [e.strip() for e in env_val.split(",") if e.strip()]
    return bool(s.get("user_email")) and s.get("user_email") in admin_emails


def _make_oauth_state(user_id: str, code_verifier: str = None) -> str:
    """OAuth state にユーザーID・タイムスタンプ（・code_verifier）を埋め込む（Base64 JSON）"""
    data = {"uid": user_id, "ts": int(time.time())}
    if code_verifier:
        data["cv"] = code_verifier
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode()


def _parse_oauth_state(state: str) -> tuple[str | None, str | None]:
    """OAuth state を解析して (user_id, code_verifier) を返す。10分超またはエラーなら (None, None)"""
    try:
        data = json.loads(base64.urlsafe_b64decode(state.encode()))
        if time.time() - data.get("ts", 0) > 600:
            return None, None
        return data.get("uid"), data.get("cv")
    except Exception:
        return None, None


def _handle_oauth_callback() -> bool:
    """
    URL に ?code=xxx&state=yyy が含まれていれば YouTube OAuth コールバックを処理。
    処理した場合は True を返す。
    ※ Supabase Google OAuth は implicit flow (#access_token=...) を使用するため
       ?code= として来ることは通常ないが、万一来ても無視してリセットする。
    """
    params = st.query_params
    code = params.get("code")
    if not code:
        return False

    state = params.get("state", "")
    redirect_uri = _get_app_url()

    # state からユーザーIDと PKCE code_verifier を事前に取得
    user_id, code_verifier = _parse_oauth_state(state)

    # state が YouTube OAuth 形式でない場合は未処理として返す
    # → Supabase Google OAuth の ?code= は _handle_supabase_pkce_callback() で処理
    if not user_id:
        return False

    try:
        from core.uploader import exchange_code
        token_json_str = exchange_code(code, redirect_uri, code_verifier=code_verifier)
        token_data = json.loads(token_json_str)
        if user_id and _is_multi_user_mode():
            from core.db import save_youtube_token, set_youtube_approved
            save_youtube_token(user_id, token_json_str)
            try:
                set_youtube_approved(user_id, True)
            except Exception:
                pass
            st.session_state["user_id"]  = user_id
            st.session_state["yt_token"] = token_data
            # user_email を Supabase から復元
            try:
                from core.auth import get_supabase_admin
                u = get_supabase_admin().auth.admin.get_user_by_id(user_id)
                if u and u.user:
                    st.session_state["user_email"] = u.user.email
            except Exception:
                pass
            # OAuth完了後は step4 に戻す
            st.session_state["step"] = 4
        else:
            # シングルユーザーモード: 旧来の token.json に書き戻す
            st.session_state["yt_token"] = token_data
            token_path = CREDS_DIR / "token.json"
            CREDS_DIR.mkdir(exist_ok=True)
            token_path.write_text(token_json_str)

        # チャンネル情報を取得して session_state に保存
        try:
            from core.uploader import get_channel_info
            ch = get_channel_info(token_data)
            if ch:
                st.session_state["yt_channel_name"]      = ch["title"]
                st.session_state["yt_channel_id"]        = ch["id"]
                st.session_state["yt_channel_thumbnail"] = ch.get("thumbnail", "")
        except Exception:
            pass

        st.query_params.clear()
        st.session_state["_oauth_success"] = True
        st.rerun()

    except Exception as e:
        st.query_params.clear()
        st.session_state["_oauth_error"] = str(e)
        st.rerun()

    return True


def _handle_supabase_confirmation():
    """
    Supabase メール確認 / Google OAuth コールバックのアクセストークンを処理。
    URL fragment (#access_token=...) は JS で ?sb_access_token= に変換済み。
    """
    token         = st.query_params.get("sb_access_token", "")
    refresh_token = st.query_params.get("sb_refresh_token", "") or ""

    if not token:
        st.query_params.clear()
        return

    try:
        from core.auth import get_supabase
        sb = get_supabase()

        user         = None
        session_resp = None
        _dbg: list[str] = []

        # ① set_session（refresh_token が存在する場合のみ）
        if refresh_token:
            try:
                session_resp = sb.auth.set_session(token, refresh_token)
                if session_resp and session_resp.user:
                    user = session_resp.user
            except Exception as _e1:
                _dbg.append(f"set_session失敗: {_e1}")
                session_resp = None
        else:
            _dbg.append("refresh_token なし → set_session スキップ")

        # ② get_user(jwt) でユーザー検証
        if not user:
            try:
                user_resp = sb.auth.get_user(token)
                if user_resp and user_resp.user:
                    user = user_resp.user
                    if refresh_token and not session_resp:
                        try:
                            session_resp = sb.auth.set_session(token, refresh_token)
                        except Exception:
                            session_resp = None
                else:
                    _dbg.append(f"get_user: user=None (user_resp={user_resp})")
            except Exception as _e2:
                _dbg.append(f"get_user失敗: {_e2}")

        # ③ JWT 直接パース（最終手段）
        if not user:
            try:
                import base64 as _b64, json as _json
                _parts = token.split(".")
                if len(_parts) == 3:
                    _pad = _parts[1] + "=" * ((4 - len(_parts[1]) % 4) % 4)
                    _data = _json.loads(_b64.urlsafe_b64decode(_pad))
                    _uid   = _data.get("sub", "")
                    _email = _data.get("email", "")
                    if _uid:
                        from types import SimpleNamespace
                        user = SimpleNamespace(id=_uid, email=_email)
                        _dbg.append(f"JWT parse成功: uid={_uid}")
                    else:
                        _dbg.append(f"JWT parse: subなし keys={list(_data.keys())}")
                else:
                    _dbg.append(f"JWTでない (parts={len(_parts)}) token先頭={token[:30]!r}")
            except Exception as _e3:
                _dbg.append(f"JWT parse失敗: {_e3}")

        if user:
            st.session_state["user_id"]    = user.id
            st.session_state["user_email"] = user.email
            # refresh_token を保存（ログイン保持用）
            try:
                if session_resp and session_resp.session:
                    st.session_state["_supabase_rt"] = session_resp.session.refresh_token
                elif refresh_token:
                    st.session_state["_supabase_rt"] = refresh_token
            except Exception:
                pass
            # 保存済み YouTube トークンがあれば復元
            try:
                from core.db import get_youtube_token
                from core.uploader import get_channel_info
                yt = get_youtube_token(user.id)
                if yt:
                    st.session_state["yt_token"] = yt
                    ch = get_channel_info(yt)
                    if ch:
                        st.session_state["yt_channel_name"]      = ch["title"]
                        st.session_state["yt_channel_id"]        = ch["id"]
                        st.session_state["yt_channel_thumbnail"] = ch.get("thumbnail", "")
            except Exception:
                pass
            st.query_params.clear()
            st.session_state["_email_confirmed"] = True
            st.rerun()
        else:
            st.query_params.clear()
            st.warning("⚠️ ログインに失敗しました。もう一度お試しください。")
            with st.expander("🔍 詳細（開発者向け）", expanded=True):
                for _d in _dbg:
                    st.code(_d)

    except Exception as e:
        st.query_params.clear()
        st.error(f"認証エラー: {e}")


def _handle_supabase_pkce_callback() -> bool:
    """
    Supabase Google OAuth の PKCE コールバック処理。
    ?code=SUPABASE_CODE が来たとき、保存済み code_verifier で Supabase JWT に交換する。

    code_verifier の取得順:
      ① JS リダイレクト経由の ?_cv= パラメータ（localStorage/sessionStorage から JS が付加）
      ② ?state= パラメータ（Supabase が state を callback URL に返す場合）
      ③ Cookie _sb_pkce_cv（onclick で設定）
      ④ session_state（同一セッションのみ）
      ⑤ 全て失敗 → JS が localStorage/sessionStorage を読んで ?_cv= 付きでリダイレクト
    """
    code = st.query_params.get("code", "")
    if not code:
        return False

    code_verifier = ""

    # ① JS リダイレクトで付加された ?_cv= パラメータ（最も確実）
    code_verifier = st.query_params.get("_cv", "")

    # ② state パラメータから取得（Supabase が state を callback URL に返す場合）
    if not code_verifier:
        _state = st.query_params.get("state", "")
        if _state:
            try:
                import base64 as _b64s, json as _js2
                _pad = (4 - len(_state) % 4) % 4
                _decoded = _js2.loads(
                    _b64s.urlsafe_b64decode(_state + "=" * _pad).decode()
                )
                code_verifier = _decoded.get("cv", "")
            except Exception:
                pass

    # ③ Cookie から取得
    if not code_verifier:
        try:
            import urllib.parse as _up
            _cv_raw = st.context.cookies.get("_sb_pkce_cv", "")
            if _cv_raw:
                code_verifier = _up.unquote(_cv_raw)
        except Exception:
            pass

    # ④ フォールバック: session_state（同一セッション内の場合のみ有効）
    if not code_verifier:
        code_verifier = st.session_state.get("_google_oauth_cv", "")

    # ⑤ 全て失敗 → JS で localStorage/sessionStorage を読んで ?_cv= 付きでリダイレクト
    #    st.stop() は使わない（早期 stop は components.html を正しくフラッシュしない）
    #    → render_login_page() まで描画を続け、ページがフル描画された後に JS が動く
    if not code_verifier:
        import streamlit.components.v1 as _comp_cv
        _comp_cv.html("""<script>
(function() {
  try {
    var cv = sessionStorage.getItem('_sb_pkce_cv') || localStorage.getItem('_sb_pkce_cv');
    if (cv) {
      var u = window.top.location.href;
      u = u.replace(/[&?]_cv=[^&]*/g, '');
      u += (u.indexOf('?') >= 0 ? '&' : '?') + '_cv=' + encodeURIComponent(cv);
      window.top.location.href = u;
    } else {
      window.top.location.href = window.top.location.origin + window.top.location.pathname
        + '?sb_auth_error=' + encodeURIComponent('認証情報が見つかりませんでした。もう一度ログインしてください。');
    }
  } catch(e) {
    window.top.location.href = window.top.location.origin + window.top.location.pathname
      + '?sb_auth_error=' + encodeURIComponent('認証エラーが発生しました。再度お試しください。');
  }
})();
</script>""", height=0)
        return True  # ← st.stop() しない。routing を継続させ render_login_page() まで描画する

    try:
        from core.auth import get_supabase
        sb = get_supabase()

        session = sb.auth.exchange_code_for_session(
            {"auth_code": code, "code_verifier": code_verifier}
        )

        if session and session.user:
            st.session_state["user_id"]    = session.user.id
            st.session_state["user_email"] = session.user.email
            try:
                if session.session:
                    st.session_state["_supabase_rt"] = session.session.refresh_token
            except Exception:
                pass
            # 保存済み YouTube トークンがあれば復元
            try:
                from core.db import get_youtube_token
                from core.uploader import get_channel_info
                yt = get_youtube_token(session.user.id)
                if yt:
                    st.session_state["yt_token"] = yt
                    ch = get_channel_info(yt)
                    if ch:
                        st.session_state["yt_channel_name"]      = ch["title"]
                        st.session_state["yt_channel_id"]        = ch["id"]
                        st.session_state["yt_channel_thumbnail"] = ch.get("thumbnail", "")
            except Exception:
                pass
            st.query_params.clear()
            st.session_state["_email_confirmed"] = True
            st.rerun()
            return True
        else:
            st.query_params.clear()
            st.error("Googleログインに失敗しました。もう一度お試しください。")
            return True

    except Exception as e:
        st.query_params.clear()
        st.error(f"Googleログインエラー: {e}")
        return True


def _redirect_to_url(url: str):
    """JavaScript でトップレベルウィンドウを指定 URL にリダイレクト"""
    import streamlit.components.v1 as _components
    # XSS 対策: url はサーバー生成値のみ渡す
    _components.html(
        f'<script>window.top.location.href = {json.dumps(url)};</script>',
        height=0,
    )


# ── Cookie ヘルパー（ログイン保持用） ────────────────────
_COOKIE_NAME    = "kirinuki_sb_rt"
_COOKIE_MAX_AGE = 30 * 24 * 3600  # 30日


def _emit_cookie_writer(rt: str):
    """refresh_token を Cookie に書き込む JS を発行（毎レンダリング・ローテーション対応）"""
    import streamlit.components.v1 as _c
    _c.html(
        f'<script>document.cookie={json.dumps(_COOKIE_NAME)}'
        f'+"="+encodeURIComponent({json.dumps(rt)})'
        f'+"; path=/; max-age={_COOKIE_MAX_AGE}; SameSite=Lax";</script>',
        height=1,
    )


def _emit_cookie_clear():
    """Cookie の refresh_token を削除する JS を発行"""
    import streamlit.components.v1 as _c
    _c.html(
        f'<script>document.cookie={json.dumps(_COOKIE_NAME)}'
        f'+"=; path=/; max-age=0; SameSite=Lax";</script>',
        height=1,
    )


# ── タイトルデザインテーマ ──────────────────────────────────
TITLE_THEMES = {
    "purple": {
        "label": "🟣 パープル",
        "bg":      "linear-gradient(135deg,#7c3aed 0%,#4338ca 55%,#2563eb 100%)",
        "accent":  "linear-gradient(90deg,#f59e0b,#ef4444,#ec4899,#8b5cf6)",
        "text":    "#ffffff",
        "sub":     "rgba(255,255,255,0.72)",
    },
    "red_hot": {
        "label": "🔴 レッドホット",
        "bg":      "linear-gradient(135deg,#7f1d1d 0%,#dc2626 50%,#ea580c 100%)",
        "accent":  "linear-gradient(90deg,#fef9c3,#fde68a,#f97316,#dc2626)",
        "text":    "#ffffff",
        "sub":     "rgba(255,245,200,0.80)",
    },
    "midnight": {
        "label": "⚫ ミッドナイト",
        "bg":      "linear-gradient(135deg,#020617 0%,#0f172a 60%,#1e293b 100%)",
        "accent":  "linear-gradient(90deg,#38bdf8,#818cf8,#c084fc,#38bdf8)",
        "text":    "#f1f5f9",
        "sub":     "rgba(186,230,253,0.70)",
    },
    "emerald": {
        "label": "🟢 エメラルド",
        "bg":      "linear-gradient(135deg,#064e3b 0%,#059669 60%,#0891b2 100%)",
        "accent":  "linear-gradient(90deg,#ecfdf5,#6ee7b7,#34d399,#059669)",
        "text":    "#ffffff",
        "sub":     "rgba(209,250,229,0.78)",
    },
    "gold": {
        "label": "🟡 ゴールド",
        "bg":      "linear-gradient(135deg,#78350f 0%,#b45309 45%,#d97706 100%)",
        "accent":  "linear-gradient(90deg,#fef3c7,#fde68a,#f59e0b,#d97706)",
        "text":    "#ffffff",
        "sub":     "rgba(254,249,195,0.82)",
    },
    "pink": {
        "label": "🩷 ピンク",
        "bg":      "linear-gradient(135deg,#831843 0%,#be185d 50%,#9333ea 100%)",
        "accent":  "linear-gradient(90deg,#fce7f3,#f9a8d4,#f472b6,#c026d3)",
        "text":    "#ffffff",
        "sub":     "rgba(253,220,234,0.78)",
    },
}

TITLE_SIZES = {
    "large":  {"label": "大",   "font": "18px", "weight": "900", "lh": "1.5", "pad": "16px 16px 22px"},
    "xlarge": {"label": "特大", "font": "22px", "weight": "900", "lh": "1.5", "pad": "18px 16px 26px"},
}
# タイトルバー最小高さ — 実出力の最小18%(345/1920)をプレビュー幅224pxに換算
TITLE_BAR_H = {"large": 88, "xlarge": 96}

# タイトル背景の柄パターン
_NOISE_CSS = (
    "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'"
    " width='4' height='4'%3E%3Crect width='1' height='1'"
    " fill='rgba(255,255,255,0.04)'/%3E%3C/svg%3E\")"
)
TITLE_PATTERNS = {
    "none":          {"label": "✨ なし",       "css": _NOISE_CSS},
    "dots_sm":       {"label": "⚫ ドット小",   "css": "radial-gradient(circle,rgba(255,255,255,0.22) 1px,transparent 1px) center/8px 8px"},
    "dots":          {"label": "⚫ ドット中",   "css": "radial-gradient(circle,rgba(255,255,255,0.18) 1.5px,transparent 1.5px) center/14px 14px"},
    "dots_lg":       {"label": "⚫ ドット大",   "css": "radial-gradient(circle,rgba(255,255,255,0.20) 3px,transparent 3px) center/22px 22px"},
    "stripes_thin":  {"label": "↗ ライン細",   "css": "repeating-linear-gradient(45deg,rgba(255,255,255,0.09) 0,rgba(255,255,255,0.09) 1px,transparent 1px,transparent 8px)"},
    "stripes":       {"label": "↗ ライン中",   "css": "repeating-linear-gradient(45deg,rgba(255,255,255,0.10) 0,rgba(255,255,255,0.10) 2px,transparent 2px,transparent 12px)"},
    "stripes_thick": {"label": "↗ ライン太",   "css": "repeating-linear-gradient(45deg,rgba(255,255,255,0.13) 0,rgba(255,255,255,0.13) 5px,transparent 5px,transparent 14px)"},
    "grid":          {"label": "⊞ グリッド",   "css": "linear-gradient(rgba(255,255,255,0.12) 1px,transparent 1px) top/18px 18px,linear-gradient(90deg,rgba(255,255,255,0.12) 1px,transparent 1px) left/18px 18px"},
    "diamond":       {"label": "◇ ダイヤ",     "css": "repeating-linear-gradient(45deg,rgba(255,255,255,0.10) 0,rgba(255,255,255,0.10) 1.5px,transparent 1.5px,transparent 9px),repeating-linear-gradient(-45deg,rgba(255,255,255,0.10) 0,rgba(255,255,255,0.10) 1.5px,transparent 1.5px,transparent 9px)"},
    "wave":          {"label": "〜 ウェーブ",  "css": "radial-gradient(ellipse 100% 3px at 50% 50%,rgba(255,255,255,0.15) 0%,transparent 100%) center/18px 9px repeat"},
}

# ── デザインプロンプト用キーワードマップ ──────────────────
_THEME_KEYWORDS = [
    ("purple",   ["紫", "パープル", "purple", "青紫"]),
    ("red_hot",  ["赤", "レッド", "red", "情熱"]),
    ("midnight", ["黒", "ミッドナイト", "dark", "ダーク", "夜", "ブラック"]),
    ("emerald",  ["緑", "エメラルド", "green", "グリーン"]),
    ("gold",     ["金", "ゴールド", "gold", "黄", "オレンジ"]),
    ("pink",     ["ピンク", "pink", "桃", "ローズ"]),
]
_SIZE_KEYWORDS = [
    ("large",  ["文字大", "大きめ", "大文字", "大"]),
    ("xlarge", ["文字特大", "特大", "超大きめ", "最大文字"]),
]
_PATTERN_KEYWORDS = [
    ("none",          ["なし", "シンプル", "無地", "パターンなし"]),
    ("dots_sm",       ["ドット小", "小ドット", "細かいドット"]),
    ("dots_lg",       ["ドット大", "大ドット", "大きいドット"]),
    ("dots",          ["ドット", "水玉"]),
    ("stripes_thin",  ["ライン細", "細ライン", "細い線"]),
    ("stripes_thick", ["ライン太", "太ライン", "太い線"]),
    ("stripes",       ["ライン", "ストライプ", "斜線"]),
    ("grid",          ["グリッド", "格子", "チェック"]),
    ("diamond",       ["ダイヤ", "菱形", "ひし形"]),
    ("wave",          ["ウェーブ", "波"]),
]

def _parse_design_prompt(text: str):
    """テキストからデザイン設定（テーマ・サイズ・柄・ランダムモード）を推定する"""
    is_rand = any(kw in text for kw in ["ランダム", "バラバラ", "random"])
    theme   = next((k for k, kws in _THEME_KEYWORDS   if any(kw in text for kw in kws)), None)
    size    = next((k for k, kws in _SIZE_KEYWORDS    if any(kw in text for kw in kws)), None)
    pattern = next((k for k, kws in _PATTERN_KEYWORDS if any(kw in text for kw in kws)), None)
    return theme, size, pattern, is_rand

# venv の bin を PATH に追加（yt-dlp / ffmpeg が確実に見つかるように）
_VENV_BIN = str(BASE_DIR / ".venv" / "bin")
if _VENV_BIN not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _VENV_BIN + ":" + os.environ.get("PATH", "")

# ── ページ設定 ─────────────────────────────────────────────
st.set_page_config(
    page_title="切り抜きくん",
    page_icon="✂️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── グローバル CSS ─────────────────────────────────────────
st.markdown("""
<style>
/* ベース */
[data-testid="stAppViewContainer"] > .main { background:#f8f9fb; }
[data-testid="stHeader"] { background:transparent; }
section[data-testid="stSidebar"] { display:none; }
.block-container { padding:0 0 60px !important; max-width:1100px; }
/* 横スクロール防止（グローバル） */
html, body { overflow-x: hidden; }
[data-testid="stAppViewContainer"],
[data-testid="stMain"] { overflow-x: hidden; max-width: 100vw; }

/* ── アプリヘッダー ── */
.app-header {
  background:#ffffff;
  border-bottom:2px solid #f1f5f9;
  padding:14px 40px 16px;
  margin-bottom:0;
  display:flex;
  align-items:center;
  gap:14px;
  margin-left:-40px;
  margin-right:-40px;
  box-shadow:0 1px 8px rgba(0,0,0,0.05);
}
.brand-logo {
  height:68px; width:auto; object-fit:contain; flex-shrink:0;
}
.brand-logo-fallback {
  width:68px; height:68px; font-size:40px;
  display:flex; align-items:center; justify-content:center;
  background:linear-gradient(135deg,#fff4ed,#ffe4d4);
  border-radius:14px; flex-shrink:0;
}
.brand-text { display:flex; flex-direction:column; gap:2px; }
.brand-catchcopy {
  font-size:10.5px; color:#f97316; font-weight:700;
  letter-spacing:.06em; text-transform:none;
}
.brand-name {
  font-size:24px; font-weight:900; letter-spacing:-.02em; line-height:1.1;
  background:linear-gradient(135deg,#ea580c 0%,#dc2626 50%,#b91c1c 100%);
  -webkit-background-clip:text; -webkit-text-fill-color:transparent;
  background-clip:text;
}
.brand-tagline {
  font-size:11.5px; color:#94a3b8; font-weight:500; letter-spacing:.04em;
}
.header-divider { flex:1; }
.header-user {
  display:flex; align-items:center; gap:8px; margin-right:8px;
}
.header-user-email {
  font-size:11.5px; color:#64748b; font-weight:500;
  background:#f1f5f9; padding:3px 10px; border-radius:20px;
  border:1px solid #e2e8f0; white-space:nowrap;
}
.header-badge {
  background:linear-gradient(135deg,#fef3c7,#fed7aa);
  color:#92400e; font-size:11px; font-weight:700;
  padding:4px 12px; border-radius:20px; letter-spacing:.04em;
  border:1px solid #fde68a;
}
/* ログアウトボタン */
div[data-testid="stButton"] button[title="ログアウトします"] {
  font-size:12px !important; padding:4px 14px !important;
  height:32px !important; border-radius:8px !important;
  background:transparent !important;
  color:#dc2626 !important;
  border:1.5px solid #fca5a5 !important;
  font-weight:500 !important;
}
div[data-testid="stButton"] button[title="ログアウトします"]:hover {
  background:#fee2e2 !important;
  border-color:#dc2626 !important;
}

/* ── ステップエリア ── */
.step-area {
  background:#ffffff;
  border-bottom:1px solid #e5e7eb;
  padding:20px 40px 0;
  margin-left:-40px; margin-right:-40px; margin-bottom:32px;
}

/* ── ステップバー ── */
.stepbar { display:flex; align-items:center; margin-bottom:0; gap:0; padding-bottom:20px; }
.st-step { display:flex; flex-direction:column; align-items:center; gap:5px; position:relative; }
.st-circle {
  width:36px; height:36px; border-radius:50%;
  display:flex; align-items:center; justify-content:center;
  font-size:14px; font-weight:700; transition:.3s;
}
.st-line { flex:1; height:2px; margin-bottom:22px; min-width:40px; }
.done  .st-circle { background:#10b981; color:#fff; cursor:pointer; transition:.15s; }
.done  .st-circle:hover { background:#059669; transform:scale(1.1); }
.done  .st-line   { background:#10b981; }
.active .st-circle { background:#ea580c; color:#fff; box-shadow:0 0 14px #ea580c88; }
.active .st-line   { background:linear-gradient(90deg,#ea580c50,#e5e7eb); }
.wait  .st-circle { background:#f3f4f6; color:#9ca3af; border:1px solid #d1d5db; }
.wait  .st-line   { background:#e5e7eb; }
.st-label { font-size:11px; font-weight:500; white-space:nowrap; }
.done  .st-label  { color:#059669; }
.active .st-label { color:#ea580c; font-weight:700; }
.wait  .st-label  { color:#9ca3af; }

/* ── デザイン設定ページ ── */
.design-sec {
  background:#fff; border:1px solid #e0e7ff;
  border-radius:18px; padding:22px 24px 18px;
  margin:16px 0; box-shadow:0 2px 12px rgba(79,70,229,0.07);
}
.design-sec-hd {
  display:flex; align-items:center; gap:14px; margin-bottom:16px;
  padding-bottom:14px; border-bottom:1px solid #f0f0ff;
}
.design-sec-icon {
  width:44px; height:44px; border-radius:12px; flex-shrink:0;
  display:flex; align-items:center; justify-content:center;
  font-size:22px;
}
.design-sec-icon.purple { background:linear-gradient(135deg,#ede9fe,#ddd6fe); }
.design-sec-icon.orange { background:linear-gradient(135deg,#fff7ed,#fed7aa); }
.design-sec-title { font-size:17px; font-weight:800; color:#1e293b; }
.design-sec-desc  { font-size:12px; color:#64748b; margin-top:3px; }
.design-diagram {
  background:linear-gradient(145deg,#f8faff,#f0f4ff);
  border:1px solid #e0e7ff; border-radius:14px;
  padding:14px 16px; margin-bottom:16px;
  display:flex; align-items:flex-start; gap:16px;
}
.shorts-thumb {
  width:56px; height:98px; border-radius:8px; overflow:hidden;
  border:2px solid #c4b5fd; flex-shrink:0; position:relative;
  background:#000;
}
.shorts-thumb-title {
  /* 出力比率 タイトル ~35% → 98px × 0.35 ≈ 34px */
  position:absolute; top:0; left:0; right:0; height:34px;
  background:linear-gradient(135deg,#4f46e5,#7c3aed);
  display:flex; align-items:center; justify-content:center;
  font-size:5px; color:#fff; font-weight:700; text-align:center;
  padding:1px;
}
.shorts-thumb-video {
  /* 出力比率 動画 ~32% → 98px × 0.32 ≈ 31px */
  position:absolute; top:34px; left:0; right:0; height:31px;
  background:#1a1a2e; display:flex; align-items:center; justify-content:center;
  font-size:10px; color:#666;
}
.shorts-thumb-bottom {
  /* 出力比率 底部 ~33% → 98px × 0.33 ≈ 33px (1.7:1 横長) */
  position:absolute; top:65px; left:0; right:0; height:33px;
  background:#e2e8f0; display:flex; align-items:center; justify-content:center;
  font-size:5px; color:#64748b;
}
.shorts-thumb-hl-title .shorts-thumb-title  { outline:2.5px solid #f59e0b; }
.shorts-thumb-hl-bottom .shorts-thumb-bottom { outline:2.5px solid #10b981; }
.diagram-note { font-size:12px; color:#475569; line-height:1.7; }
.diagram-note strong { color:#4f46e5; }

/* ── コンテンツエリア ── */
.content-area { padding:0 40px; margin-left:-40px; margin-right:-40px; padding-top:0; }

/* ビデオ情報カード */
.video-card {
  background:linear-gradient(135deg,#f0f4ff 0%,#e8f0fe 100%);
  border:1px solid #c7d2fe; border-radius:16px;
  padding:20px 24px; margin-bottom:24px; display:flex; gap:20px; align-items:flex-start;
}
.video-meta h3 { margin:0 0 6px; font-size:17px; color:#1e293b; }
.video-meta p  { margin:0; font-size:12px; color:#64748b; }
.badge {
  display:inline-block; padding:2px 10px; border-radius:20px;
  font-size:11px; font-weight:600; margin-right:6px;
}
.badge-purple { background:#ede9fe; color:#5b21b6; }
.badge-green  { background:#d1fae5; color:#065f46; }

/* ── クリップセクション ──────────────────────── */
.clip-section-hd {
  background: linear-gradient(110deg, #1e1b4b 0%, #4338ca 45%, #7c3aed 100%);
  border-radius: 16px;
  padding: 15px 20px;
  display: flex; align-items: center; gap: 12px;
  position: relative; overflow: hidden;
  box-shadow: 0 4px 20px rgba(79,70,229,0.22), 0 1px 4px rgba(0,0,0,0.1);
}
.clip-section-hd::before {
  content: '';
  position: absolute; top: 0; right: 0;
  width: 160px; height: 100%;
  background: radial-gradient(ellipse at 80% 50%, rgba(255,255,255,0.07) 0%, transparent 70%);
  pointer-events: none;
}
.clip-section-num {
  width: 36px; height: 36px; border-radius: 50%;
  background: rgba(255,255,255,0.16); border: 2px solid rgba(255,255,255,0.38);
  color: #fff; display: flex; align-items: center; justify-content: center;
  font-size: 16px; font-weight: 800; flex-shrink: 0; letter-spacing: -0.5px;
}
.clip-section-title {
  font-size: 14px; color: rgba(255,255,255,0.95); font-weight: 600;
  flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.clip-section-badges {
  display: flex; gap: 7px; align-items: center; flex-shrink: 0; flex-wrap: wrap;
}
.clip-score-tag {
  border-radius: 8px; padding: 3px 10px; font-size: 12px; font-weight: 800;
  color: #fff; border: 1.5px solid rgba(255,255,255,0.22); white-space: nowrap;
  cursor: default; box-shadow: 0 1px 4px rgba(0,0,0,0.2);
}
.clip-time-tag {
  background: rgba(255,255,255,0.13); color: rgba(255,255,255,0.88);
  border-radius: 7px; padding: 3px 10px; font-size: 11px;
  font-family: monospace; border: 1px solid rgba(255,255,255,0.2); white-space: nowrap;
}
/* 字幕ボックス */
.transcript-box {
  background: #fff; border-radius: 10px; padding: 10px 14px;
  font-size: 12px; color: #4b5563; line-height: 1.7;
  margin-bottom: 12px;
  border-left: 3px solid #818cf8;
  box-shadow: 0 1px 3px rgba(0,0,0,0.05), inset 0 0 0 1px #eef2ff;
}
.no-transcript { font-style: italic; color: #9ca3af; }
/* クリップ間セパレータ */
.clip-divider {
  display: flex; align-items: center; gap: 12px;
  margin: 28px 0 16px; color: #a5b4fc;
  font-size: 10px; font-weight: 700; letter-spacing: 0.14em; user-select: none;
}
.clip-divider::before, .clip-divider::after {
  content: ''; flex: 1; height: 1.5px;
  background: linear-gradient(90deg, transparent, #c4b5fd 35%, transparent);
}

/* スケジュールリスト */
.sched-row {
  background:#f9fafb; border:1px solid #e5e7eb; border-radius:8px;
  padding:11px 16px; margin:5px 0;
  display:flex; justify-content:space-between; align-items:center;
}
.sched-num { color:#4f46e5; font-weight:700; font-size:13px; min-width:32px; }
.sched-time { color:#059669; font-family:monospace; font-size:13px; }
.sched-title { color:#374151; font-size:13px; flex:1; margin:0 16px; }
.sched-skip { color:#d1d5db; text-decoration:line-through; }

/* セクション見出し */
.sec-label {
  font-size:11px; font-weight:700; letter-spacing:.08em;
  color:#6366f1; text-transform:uppercase; margin-bottom:6px; margin-top:2px;
}

/* フッター */
.footer { text-align:center; font-size:11px; color:#9ca3af; margin-top:48px; padding-top:16px; border-top:1px solid #e5e7eb; }

/* ボタン上書き */
div[data-testid="stButton"] > button[kind="primary"] {
  background:linear-gradient(135deg,#7c3aed,#4f46e5) !important;
  border:none !important; font-weight:700 !important;
  font-size:15px !important; border-radius:10px !important;
  letter-spacing:.02em !important;
}
div[data-testid="stButton"] > button[kind="secondary"] {
  background:#1f2937 !important; color:#9ca3af !important;
  border:1px solid #374151 !important; border-radius:8px !important;
}
/* 採点根拠ポップオーバー：テキスト改行なし */
div[data-testid="stPopover"] button,
div[data-testid="stPopover"] button p,
div[data-testid="stPopover"] button span {
  white-space: nowrap !important;
  overflow: hidden !important;
  font-size: 12px !important;
}
div[data-testid="stPopover"] {
  min-width: 0 !important;
}

/* 採点根拠バッジボタン */
div[data-testid="stButton"] > button[key^="score_"],
div[data-testid="column"] div[data-testid="stButton"]:has(button[kind="secondary"]) > button {
  /* fallback: 最初の小カラムの secondary ボタン全般には適用しない */
}
.score-badge-wrap div[data-testid="stButton"] > button {
  background: linear-gradient(135deg, #1e1b4b 0%, #4338ca 100%) !important;
  color: #c7d2fe !important;
  border: 1.5px solid rgba(139,92,246,0.5) !important;
  border-radius: 100px !important;
  padding: 3px 14px !important;
  font-size: 11.5px !important;
  font-weight: 700 !important;
  letter-spacing: 0.04em !important;
  height: auto !important;
  min-height: 0 !important;
  line-height: 1.6 !important;
  box-shadow: 0 2px 8px rgba(67,56,202,0.35) !important;
  transition: box-shadow 0.15s, transform 0.1s !important;
  width: auto !important;
}
.score-badge-wrap div[data-testid="stButton"] > button:hover {
  background: linear-gradient(135deg, #312e81 0%, #6d28d9 100%) !important;
  color: #ede9fe !important;
  box-shadow: 0 4px 14px rgba(109,40,217,0.5) !important;
  transform: translateY(-1px) !important;
}
.score-badge-wrap {
  margin-top: 10px;
  margin-bottom: 2px;
}

/* 入力フィールドをライトに */
div[data-testid="stTextInput"] input,
div[data-testid="stTextArea"] textarea {
  background:#ffffff !important; color:#1e293b !important;
  border-color:#d1d5db !important;
}
div[data-testid="stNumberInput"] input { background:#ffffff !important; color:#1e293b !important; }

</style>
""", unsafe_allow_html=True)

# ── レスポンシブCSS（別ブロックで注入） ──────────────────────
st.markdown("""
<style>
@media (max-width: 640px) {
  /* === 水平余白（+12px → 合計28px/側） === */
  .block-container { padding: 0 12px 40px !important; overflow-x: hidden !important; }
  /* 全幅バー: -(16+12)=-28px */
  .app-header { padding: 12px 20px !important; margin-left: -28px !important; margin-right: -28px !important; }
  .step-area { padding: 14px 20px 0 !important; margin-left: -28px !important; margin-right: -28px !important; }
  /* カラム縦積み */
  [data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; overflow-x: hidden !important; }
  [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] { min-width: 100% !important; flex: 1 1 100% !important; }
  /* フォントサイズ（iOS zoom防止: 16px以上） */
  div[data-testid="stTextInput"] input,
  div[data-testid="stTextArea"] textarea,
  div[data-testid="stNumberInput"] input { font-size: 16px !important; }
  /* ラベル */
  div[data-testid="stTextInput"] label, div[data-testid="stTextArea"] label,
  div[data-testid="stNumberInput"] label, div[data-testid="stCheckbox"] label { font-size: 13px !important; font-weight: 600 !important; }
  /* 本文 */
  div[data-testid="stMarkdown"] p, div[data-testid="stMarkdown"] li { font-size: 13px !important; line-height: 1.65 !important; }
  /* ボタン */
  div[data-testid="stButton"] > button { font-size: 15px !important; min-height: 44px !important; }
  /* 見出し階層 */
  h1 { font-size: 22px !important; font-weight: 800 !important; line-height: 1.25 !important; }
  h2 { font-size: 19px !important; font-weight: 700 !important; line-height: 1.3 !important; }
  h3 { font-size: 16px !important; font-weight: 700 !important; line-height: 1.4 !important; }
  /* クリップセクションヘッダー */
  .clip-section-hd { padding: 12px 16px !important; border-radius: 12px !important; gap: 10px !important; }
  .clip-section-num { width: 30px !important; height: 30px !important; font-size: 13px !important; }
  .clip-section-title { font-size: 13px !important; }
  .clip-section-badges { gap: 5px !important; }
  .clip-score-tag { padding: 2px 8px !important; font-size: 11px !important; }
  .clip-time-tag { font-size: 10px !important; padding: 2px 8px !important; }
  .transcript-box { font-size: 12px !important; margin-bottom: 10px !important; }
  .clip-divider { margin: 20px 0 12px !important; }
  /* ブランド */
  .brand-logo { height: 48px !important; }
  .brand-logo-fallback { width: 48px !important; height: 48px !important; font-size: 28px !important; }
  .brand-name { font-size: 19px !important; }
  /* ステップバー */
  .st-circle { width: 28px !important; height: 28px !important; font-size: 12px !important; }
  .st-line { min-width: 12px !important; }
  .st-label { font-size: 10px !important; }
  .stepbar { padding-bottom: 14px !important; }
  /* ビデオ・クリップカード */
  .video-card { flex-direction: column !important; gap: 10px !important; padding: 14px !important; }
  .video-meta h3 { font-size: 15px !important; }
  .video-meta p { font-size: 13px !important; }
  /* スケジュール */
  .sched-row { flex-direction: column !important; align-items: flex-start !important; gap: 4px !important; }
  .sched-title { margin: 0 !important; font-size: 14px !important; }
  .sched-num { font-size: 14px !important; }
  .sched-time { font-size: 13px !important; }
  /* ポップオーバー */
  div[data-testid="stPopover"] button, div[data-testid="stPopover"] button p,
  div[data-testid="stPopover"] button span { white-space: nowrap !important; overflow: hidden !important; font-size: 13px !important; }
  div[data-testid="stPopover"] { min-width: 0 !important; }
}
</style>
""", unsafe_allow_html=True)

# ── セッション状態の保存・復元 ────────────────────────────
SESSION_FILE = OUTPUT_DIR / "session_state.json"

def _save_session(video_info: dict, clips: list):
    """解析結果をファイルに保存"""
    OUTPUT_DIR.mkdir(exist_ok=True)
    SESSION_FILE.write_text(
        __import__("json").dumps({
            "video_info": video_info,
            "clips": clips,
            "ai_status": st.session_state.get("ai_status"),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

def _load_session() -> tuple[dict | None, list, dict | None]:
    """保存済み解析結果を読み込む"""
    try:
        if SESSION_FILE.exists():
            data = __import__("json").loads(SESSION_FILE.read_text(encoding="utf-8"))
            video_info = data.get("video_info")
            # 旧バージョンで保存されたURLが破損している場合に備えてクリーニング
            if video_info and video_info.get("url"):
                from core.downloader import _clean_url
                video_info["url"] = _clean_url(video_info["url"])
            return video_info, data.get("clips", []), data.get("ai_status")
    except Exception:
        pass
    return None, [], None


def _run_claude_api_on_clips(user_prompt: str = "") -> dict:
    """
    既存クリップに対して Claude API でタイトル等を再生成し、
    ai_status を session_state とファイルに保存して rerun する。
    user_prompt: UI から渡される追加要望テキスト（任意）
    """
    clips = list(st.session_state.get("clips", []))
    video_title = (st.session_state.get("video_info") or {}).get("title", "")
    n_clips = len(clips)
    if n_clips == 0:
        return

    # ai_writer カウンターを手動リセット（clip_index=1 のリセットに依存せず確実に）
    try:
        import core.ai_writer as _aw
        _aw._ai_errors        = []
        _aw._ai_success_count = 0
        _aw._ai_total_count   = 0
    except Exception:
        pass

    progress = st.progress(0, text="Claude API を呼び出し中…")
    for i, clip in enumerate(clips):
        progress.progress((i + 1) / n_clips, text=f"クリップ {i + 1}/{n_clips} を生成中…")
        try:
            from core.ai_writer import generate_clip_metadata
            ai_meta = generate_clip_metadata(
                clip_text=clip.get("transcript", ""),
                video_title=video_title,
                clip_index=i + 1,
                total_clips=n_clips,
                clip_start=clip.get("start", 0.0),
                clip_end=clip.get("end", 60.0),
                user_prompt=user_prompt,
            )
        except Exception as _e:
            ai_meta = None
            try:
                import core.ai_writer as _aw2
                _aw2._ai_errors.append(
                    f"clip {i + 1}: 予期しないエラー: {type(_e).__name__}: {_e}"
                )
            except Exception:
                pass

        if ai_meta:
            if ai_meta.get("title"):
                clip["title"]        = ai_meta["title"]
                # ウィジェットキーも同期: text_input が st.session_state["title_i"] を
                # value より優先するため、ここで書き換えないとフォームと不一致になる
                st.session_state[f"title_{i}"] = ai_meta["title"]
            if ai_meta.get("catchphrase"):
                clip["catchphrase"]  = ai_meta["catchphrase"]
                st.session_state[f"catch_{i}"] = ai_meta["catchphrase"]
            if ai_meta.get("description"):
                clip["description"]  = ai_meta["description"]
                st.session_state[f"desc_{i}"]  = ai_meta["description"]
            if ai_meta.get("hashtags"):
                clip["hashtags"]     = ai_meta["hashtags"]
                st.session_state[f"tags_{i}"]  = ai_meta["hashtags"]

    progress.empty()

    # ステータスを session_state とファイルに保存
    try:
        from core.ai_writer import get_ai_status
        st.session_state["ai_status"] = get_ai_status()
    except Exception:
        pass

    st.session_state["clips"] = clips
    _save_session(st.session_state.get("video_info", {}), clips)
    st.rerun()


# ── セッション初期化 ───────────────────────────────────────
def _init():
    if "step" not in st.session_state:
        # 初回ロード時：保存済みセッションがあれば Step 2 から再開
        saved_info, saved_clips, saved_ai_status = _load_session()
        if saved_info and saved_clips:
            st.session_state["step"]       = 2
            st.session_state["video_info"] = saved_info
            st.session_state["clips"]      = saved_clips
            if saved_ai_status is not None:
                st.session_state["ai_status"] = saved_ai_status
        else:
            st.session_state["step"]       = 1
            st.session_state["video_info"] = None
            st.session_state["clips"]      = []

    defaults = {
        "schedule": {
            "start_date":  str((datetime.now() + timedelta(days=1)).date()),
            "daily_times": ["09:00", "15:00", "21:00"],
        },
        "results":         [],
        "running":         False,
        "tmp_dir":         None,
        "generated_clips":   [],    # _generate_pipeline が設定
        "raw_path":          None,  # 元動画パス（str）
        "sched_pending":     None,  # _upload_pipeline で使うsched
        "pipeline_error":    None,  # エラーメッセージ（rerun後も保持）
        "_pipeline_pending": False, # パイプライン実行待ちフラグ
        "_pipeline_ran":     None,  # パイプライン完走フラグ（デバッグ用）
        "_pipeline_want_dl": True,  # ダウンロードフラグ保存
        "_pipeline_clips":   [],    # 実行対象クリップ保存
        "_pipeline_sched":   {},    # sched保存
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init()
s = st.session_state   # 短縮

# ── URL ナビゲーション処理（ステップクリック・ホーム戻り） ──
_nav_param = st.query_params.get("nav")
if _nav_param:
    try:
        _nav_step = int(_nav_param)
        if 1 <= _nav_step <= 5:
            s.step = _nav_step
    except Exception:
        pass
    st.query_params.clear()
    st.rerun()

# ── YouTube OAuth コールバック処理 ──
# ※ ログインチェック・render_login_page は全関数定義後（ファイル末尾）に実行


# ── ブランドヘッダー ──────────────────────────────────────
def render_logo():
    """アプリ上部にブランドヘッダー（ロゴ + サービス名 + ユーザー情報）を表示
    ※ st.markdown の DOMPurify / React がクリックを遮断するため
      components.html で全体を描画し window.top.location.href で確実にナビゲート。
    """
    _top_url = "/?nav=1"
    logo_path = BASE_DIR / "assets" / "logo.png"
    logo_src = ""
    if logo_path.exists():
        logo_src = "data:image/png;base64," + base64.b64encode(logo_path.read_bytes()).decode()

    if logo_src:
        logo_inner = (
            f'<img src="{logo_src}" alt="切り抜きくん"'
            f' style="width:64px;height:64px;border-radius:13px;flex-shrink:0;">'
        )
    else:
        logo_inner = (
            '<div style="width:64px;height:64px;border-radius:13px;flex-shrink:0;'
            'background:linear-gradient(135deg,#fff4ed,#ffe4d4);'
            'display:flex;align-items:center;justify-content:center;font-size:36px;">✂️</div>'
        )

    user_html = ""
    if _is_multi_user_mode() and st.session_state.get("user_id"):
        email = st.session_state.get("user_email", "")[:28]
        user_html = (
            f'<span style="font-size:11.5px;color:#64748b;margin-right:8px;">{email}</span>'
        )

    import streamlit.components.v1 as _ch
    _ch.html(f"""<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{overflow:hidden;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#fff;}}
.hdr{{
  display:flex;align-items:center;gap:0;
  background:#fff;border-bottom:2px solid #f1f5f9;
  padding:11px 16px 12px;width:100%;
}}
.logo-btn{{
  display:flex;align-items:center;gap:13px;
  cursor:pointer;border:none;background:transparent;
  padding:0;outline:none;flex-shrink:0;border-radius:10px;
  -webkit-tap-highlight-color:transparent;
}}
.logo-btn:active{{opacity:.8;}}
.bt{{display:flex;flex-direction:column;gap:1px;text-align:left;}}
.cc{{font-size:10px;color:#f97316;font-weight:700;}}
.nm{{
  font-size:22px;font-weight:900;letter-spacing:-.02em;line-height:1.15;
  background:linear-gradient(135deg,#ea580c 0%,#dc2626 50%,#b91c1c 100%);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
}}
.tg{{font-size:10px;color:#64748b;}}
.sp{{flex:1;}}
.badge{{
  display:flex;align-items:center;gap:4px;flex-shrink:0;
  background:linear-gradient(135deg,#fef3c7,#fde68a);
  border:1px solid #f59e0b;border-radius:20px;
  padding:5px 12px;font-size:12px;font-weight:700;color:#92400e;
}}
</style>
<div class="hdr">
  <button class="logo-btn"
          onclick="window.top.location.href='{_top_url}'"
          title="切り抜きくん トップへ">
    {logo_inner}
    <div class="bt">
      <div class="cc">動画の&quot;おいしい瞬間&quot;を切り抜くヒーロー</div>
      <div class="nm">切り抜きくん</div>
      <div class="tg">YouTubeショート 自動作成ツール</div>
    </div>
  </button>
  <div class="sp"></div>
  {user_html}
  <div class="badge">✂️ Beta</div>
</div>
""", height=88)

    # ログアウト / 管理パネルボタン（マルチユーザーモード時）
    if _is_multi_user_mode() and st.session_state.get("user_id"):
        btn_cols = st.columns([8, 1, 1]) if _is_admin() else st.columns([10, 1])
        if _is_admin():
            with btn_cols[1]:
                if st.button("⚙️ 管理", key="_admin_btn", help="管理パネルを開く"):
                    st.query_params["page"] = "admin"
                    st.rerun()
            with btn_cols[2]:
                if st.button("ログアウト", key="_logout_btn", help="ログアウトします"):
                    from core.auth import sign_out, sign_out_global
                    _uid_logout = st.session_state.get("user_id", "")
                    sign_out_global(_uid_logout)
                    sign_out()
                    for k in [k for k in st.session_state.keys() if k != "_clearing_cookie"]:
                        del st.session_state[k]
                    st.session_state["_clearing_cookie"] = True
                    st.rerun()
        else:
            with btn_cols[1]:
                if st.button("ログアウト", key="_logout_btn", help="ログアウトします"):
                    from core.auth import sign_out, sign_out_global
                    _uid_logout = st.session_state.get("user_id", "")
                    sign_out_global(_uid_logout)
                    sign_out()
                    for k in [k for k in st.session_state.keys() if k != "_clearing_cookie"]:
                        del st.session_state[k]
                    st.session_state["_clearing_cookie"] = True
                    st.rerun()


# ── ステップバー ──────────────────────────────────────────
def render_stepbar(current: int):
    render_logo()
    steps = [
        (1, "URL入力"),
        (2, "デザイン設定"),
        (3, "クリップ確認"),
        (4, "スケジュール"),
        (5, "実行"),
    ]
    parts = []
    for i, (num, label) in enumerate(steps):
        cls  = "done" if num < current else ("active" if num == current else "wait")
        icon = "✓" if num < current else str(num)

        # 完了済みステップはクリックで戻れる
        if num < current:
            step_attrs = (
                f'onclick="window.top.location.href=\'/?nav={num}\'"'
                f' style="cursor:pointer;" title="ステップ{num}に戻る"'
            )
        else:
            step_attrs = ""

        parts.append(f"""
          <div class="st-step {cls}" {step_attrs}>
            <div class="st-circle">{icon}</div>
            <div class="st-label">{label}</div>
          </div>
        """)
        if i < len(steps) - 1:
            line_cls = "done" if num < current else ("active" if num == current else "wait")
            parts.append(f'<div class="st-line {line_cls}"></div>')

    st.markdown(
        f'<div class="step-area"><div class="stepbar">{"".join(parts)}</div></div>',
        unsafe_allow_html=True,
    )


# ── 管理者パネル ───────────────────────────────────────────
def render_admin_panel():
    """管理者用ダッシュボード"""
    render_logo()

    # ← 管理パネルから戻るボタン
    if st.button("← アプリに戻る", key="_admin_back"):
        st.query_params.clear()
        st.rerun()

    st.markdown(
        '<div style="font-size:22px;font-weight:800;color:#1e293b;margin:16px 0 24px;">⚙️ 管理パネル</div>',
        unsafe_allow_html=True,
    )

    with st.spinner("データを読み込み中..."):
        try:
            from core.db import get_all_users_with_stats, update_user_plan
            users = get_all_users_with_stats()
        except Exception as e:
            st.error(f"データ取得エラー: {e}")
            return

    if not users:
        st.info("まだユーザーがいません")
        return

    # ── サマリー metrics ──
    total      = len(users)
    paid       = sum(1 for u in users if u["plan"] != "free")
    yt_ok      = sum(1 for u in users if u["youtube_connected"])
    clips_total = sum(u["clips_used"] for u in users)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("総ユーザー数",   f"{total} 人")
    c2.metric("有料プラン",     f"{paid} 人")
    c3.metric("YT接続済み",     f"{yt_ok} 人")
    c4.metric("今月の総クリップ", f"{clips_total} 本")

    st.divider()

    # ── YouTube 接続申請（承認待ち） ──────────────────────────────
    yt_pending = [u for u in users
                  if u.get("youtube_request_email") and not u.get("youtube_approved")]

    st.markdown("### 🎬 YouTube接続申請")

    if not yt_pending:
        st.markdown(
            '<div style="background:#f0fdf4;border:1px solid #86efac;border-radius:8px;'
            'padding:10px 14px;font-size:13px;color:#166534;">✅ 承認待ちの申請はありません</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f'<div style="background:#fef2f2;border:1px solid #fca5a5;border-radius:8px;'
            f'padding:10px 14px;margin-bottom:16px;font-size:14px;font-weight:700;color:#991b1b;">'
            f'🔔 承認待ち {len(yt_pending)} 件</div>',
            unsafe_allow_html=True,
        )

        # 承認手順の説明
        st.markdown(
            '<div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:10px;'
            'padding:14px 18px;margin-bottom:16px;">'
            '<div style="font-weight:700;color:#1e40af;font-size:13px;margin-bottom:10px;">📋 承認手順（1件につき3ステップ）</div>'
            '<div style="font-size:13px;color:#1e3a8a;line-height:2;">'
            '<b>① 下の表で申請者の「Google メール」をコピー</b><br>'
            '<b>② <a href="https://console.cloud.google.com/apis/credentials/consent" target="_blank"'
            ' style="color:#1d4ed8;">Google Cloud Console → OAuth同意画面 → テストユーザー</a>'
            ' に追加して保存</b><br>'
            '<b>③ 下の「✅ 承認」ボタンをクリック → お客さんにメールで連絡</b>'
            '</div></div>',
            unsafe_allow_html=True,
        )

        # 申請一覧
        ph = st.columns([3, 3, 1.5])
        ph[0].markdown("**アカウント（アプリ）**")
        ph[1].markdown("**① コピーするGoogleメール**")
        ph[2].markdown("**③ 承認**")
        st.markdown('<hr style="margin:4px 0;">', unsafe_allow_html=True)

        for _req in yt_pending:
            rc = st.columns([3, 3, 1.5])
            rc[0].write(_req["email"])
            rc[1].code(_req["youtube_request_email"], language=None)
            if rc[2].button("✅ 承認", key=f"_yt_approve_{_req['id']}"):
                try:
                    from core.db import set_youtube_approved as _set_yt_ok
                    _set_yt_ok(_req["id"], True)
                    st.success(
                        f"✅ {_req['email']} を承認しました。"
                        f"「{_req['youtube_request_email']}」をGoogle Cloud Consoleに追加済みか確認し、"
                        f"お客さんに「YouTube接続できます」とメールしてください。"
                    )
                    st.rerun()
                except Exception as _e:
                    st.error(f"承認エラー: {_e}")

    st.divider()

    # ── ユーザー一覧テーブル ──
    st.markdown("### 👥 ユーザー一覧")

    _PLAN_LABELS = {
        "trial":  "🆓 無料トライアル（5本）",
        "basic":  "⭐ ベーシック（月105本）",
        "pro":    "🚀 プロ（月505本）",
        "agency": "🏢 エージェンシー（月1,000本）",
        "test":   "🔧 テストユーザー（無制限）",
        # 旧プラン（後方互換）
        "free":     "🆓 無料",
        "lite":     "💡 ライト",
        "standard": "⭐ スタンダード",
    }

    # ヘッダー行
    hcols = st.columns([3, 1.5, 1, 1, 1, 1.5, 1.5])
    for col, label in zip(hcols, ["メール", "プラン", "今月使用", "上限", "YT", "登録日", "最終ログイン"]):
        col.markdown(f"**{label}**")
    st.markdown('<hr style="margin:4px 0;">', unsafe_allow_html=True)

    for user in users:
        row = st.columns([3, 1.5, 1, 1, 1, 1.5, 1.5])
        row[0].write(user["email"])
        row[1].write(_PLAN_LABELS.get(user["plan"], user["plan"]))
        row[2].write(str(user["clips_used"]))
        row[3].write(str(user["clips_limit"]))
        _yt_status = (
            "🔗" if user["youtube_connected"]
            else "✅" if user.get("youtube_approved")
            else "📩" if user.get("youtube_request_email")
            else "—"
        )
        row[4].write(_yt_status)
        row[5].write(user["created_at"])
        row[6].write(user["last_sign_in"])
    st.caption("🔗 接続中　✅ 承認済（未接続）　📩 申請中　— 未申請")

    st.divider()

    # ── プラン変更 ──
    st.markdown("### ✏️ プラン変更")

    _PLAN_OPTIONS = ["trial", "basic", "pro", "agency", "test"]
    _PLAN_LIMITS  = {"trial": 5, "basic": 105, "pro": 505, "agency": 1005, "test": 999999}

    emails = [u["email"] for u in users]
    selected_email = st.selectbox("対象ユーザーを選択", emails, key="_admin_user_sel")
    selected_user  = next((u for u in users if u["email"] == selected_email), None)

    if selected_user:
        _current_plan = selected_user.get("plan", "trial")
        if _current_plan not in _PLAN_OPTIONS:
            _current_plan = "trial"
        col_plan, col_btn = st.columns([3, 1])
        new_plan = col_plan.selectbox(
            "新しいプラン",
            _PLAN_OPTIONS,
            index=_PLAN_OPTIONS.index(_current_plan),
            format_func=lambda x: _PLAN_LABELS.get(x, x),
            key="_admin_plan_sel",
        )
        if col_btn.button("💾 変更", type="primary", key="_admin_plan_save"):
            update_user_plan(selected_user["id"], new_plan, _PLAN_LIMITS[new_plan])
            st.success(f"✅ {selected_email} → {_PLAN_LABELS[new_plan]} に変更しました")
            st.rerun()

    st.divider()

    # ── 使用本数の手動設定 ──
    st.markdown("### 🔢 使用本数の手動設定")
    _usage_emails = [u["email"] for u in users]
    _usage_sel_email = st.selectbox("対象ユーザー", _usage_emails, key="_admin_usage_sel")
    _usage_sel_user  = next((u for u in users if u["email"] == _usage_sel_email), None)
    if _usage_sel_user:
        _cur_used = int(_usage_sel_user.get("clips_used", 0) or 0)
        col_used, col_used_btn = st.columns([2, 1])
        _new_used = col_used.number_input(
            "clips_used_this_month", min_value=0, max_value=99999,
            value=_cur_used, step=1, key="_admin_usage_val",
        )
        if col_used_btn.button("💾 更新", type="primary", key="_admin_usage_save"):
            try:
                from core.auth import get_supabase_admin as _gsa_u
                _sb_u = _gsa_u()
                _sb_u.table("subscriptions").update({
                    "clips_used_this_month": int(_new_used),
                    "updated_at": datetime.utcnow().isoformat(),
                }).eq("user_id", _usage_sel_user["id"]).execute()
                st.success(f"✅ {_usage_sel_email} の使用本数を {int(_new_used)} に更新しました")
                st.rerun()
            except Exception as _ue:
                st.error(f"更新エラー: {_ue}")

    st.divider()

    # ── ユーザー削除 ──
    st.markdown("### 🗑️ ユーザー削除")
    st.markdown(
        '<div style="background:#fef2f2;border:1px solid #fecaca;border-radius:8px;'
        'padding:10px 14px;margin-bottom:14px;font-size:13px;color:#b91c1c;">'
        '⚠️ この操作は取り消せません。Auth アカウント・サブスクリプション・'
        'YouTube トークンが完全に削除されます。</div>',
        unsafe_allow_html=True,
    )

    del_emails = [u["email"] for u in users]
    del_selected_email = st.selectbox(
        "削除するユーザーを選択",
        del_emails,
        key="_admin_del_user_sel",
    )
    del_selected_user = next((u for u in users if u["email"] == del_selected_email), None)

    if del_selected_user:
        confirmed = st.checkbox(
            f"「{del_selected_email}」を完全に削除することを確認しました",
            key="_admin_del_confirm",
        )
        if st.button(
            "🗑️ ユーザーを削除",
            type="primary",
            disabled=not confirmed,
            key="_admin_del_btn",
        ):
            try:
                # core.db のモジュールキャッシュ問題を回避するため直接実装
                from core.auth import get_supabase_admin as _get_sb_admin
                _sb = _get_sb_admin()
                _uid = del_selected_user["id"]
                _sb.table("youtube_tokens").delete().eq("user_id", _uid).execute()
                _sb.table("subscriptions").delete().eq("user_id", _uid).execute()
                _sb.auth.admin.delete_user(_uid)
                st.success(f"✅ {del_selected_email} を削除しました")
                st.rerun()
            except Exception as _del_err:
                st.error(f"削除エラー: {_del_err}")

    # ── メールアドレス指定で完全削除（再登録用） ──
    st.divider()
    st.markdown("### 🔁 ソフト削除済みユーザーの完全削除")
    st.caption("Supabase ダッシュボードで削除したユーザーが同じメールで再登録できない場合に使用")
    _purge_email = st.text_input("メールアドレス", placeholder="user@example.com", key="_admin_purge_email")
    if st.button("🗑️ 完全削除して再登録を許可", key="_admin_purge_btn", disabled=not _purge_email.strip()):
        try:
            from core.auth import get_supabase_admin as _gsa2
            _sb2 = _gsa2()
            # auth.users からメールで検索（ソフト削除済み含む）
            _res = _sb2.auth.admin.list_users()
            _target = next((u for u in _res if getattr(u, "email", "") == _purge_email.strip()), None)
            if _target:
                _sb2.auth.admin.delete_user(_target.id)
                st.success(f"✅ {_purge_email} を完全削除しました。再登録できるようになりました。")
            else:
                st.warning(f"⚠️ {_purge_email} が見つかりません（すでに完全削除済みの可能性）")
        except Exception as _purge_err:
            st.error(f"エラー: {_purge_err}")

    # ═══ 🍪 YouTube Cookies 管理 ══════════════════════════════
    st.divider()
    st.subheader("🍪 YouTube Cookies 管理")
    st.caption("cookies は YouTube の IP 制限を回避するために必要です。期限切れになると動画のダウンロードが失敗します（目安: 1〜4週間ごとに更新）。")

    _col_meta, _col_check = st.columns([3, 1])
    with _col_meta:
        try:
            from core.db import get_site_setting_meta
            _meta = get_site_setting_meta("youtube_cookies")
            if _meta:
                _upd_at = (_meta.get("updated_at") or "")[:16].replace("T", " ")
                _upd_by = _meta.get("updated_by") or "不明"
                st.caption(f"最終更新: {_upd_at}  by {_upd_by}  （Supabase）")
            else:
                st.caption("Supabase 未設定 — Streamlit Secrets の cookies を使用中")
        except Exception:
            st.caption("（取得エラー）")
    with _col_check:
        if st.button("🔍 有効性チェック", key="admin_check_cookies_btn"):
            with st.spinner("確認中（最大30秒）…"):
                from core.downloader import check_cookies_validity
                _ck_ok, _ck_msg = check_cookies_validity()
            if _ck_ok:
                st.success(_ck_msg)
            else:
                st.error(_ck_msg)

    with st.expander("🔄 Cookies を更新する", expanded=False):
        st.markdown(
            "**手順:**\n"
            "1. Chrome で **YouTube にログイン** した状態で\n"
            "2. 「[Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)」拡張をクリック → **Export** → `youtube.com` のみ保存\n"
            "3. 保存したファイルの**中身**を下記テキストエリアに貼り付けて保存\n\n"
            "💡 保存後はアプリ再起動なしで即時反映されます。"
        )
        _new_ck = st.text_area(
            "cookies.txt の中身を貼り付け",
            height=180,
            placeholder="# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t...",
            key="admin_cookies_textarea",
        )
        if st.button("💾 Supabase に保存して即時適用", key="admin_save_cookies_btn"):
            _ck_stripped = (_new_ck or "").strip()
            if not _ck_stripped:
                st.error("cookies の内容が空です")
            elif (
                "Netscape HTTP Cookie File" not in _ck_stripped
                and not _ck_stripped.startswith(".")
                and not _ck_stripped.startswith("[")
            ):
                st.warning("⚠️ Netscape 形式（`# Netscape HTTP Cookie File` から始まる）か JSON 形式を貼り付けてください。")
            else:
                try:
                    from core.db import set_site_setting
                    set_site_setting("youtube_cookies", _ck_stripped, updated_by=s.get("user_email", ""))
                    _restore_credentials()   # 即時適用
                    st.success("✅ 保存完了。次のパイプライン実行から新しい cookies が使われます。")
                    st.rerun()
                except Exception as _ck_err:
                    st.error(f"保存失敗: {_ck_err}")

    # ── OAuth2 認証セクション ─────────────────────────────────
    st.markdown("---")
    st.subheader("🔑 YouTube OAuth2 認証（cookies 不要・長期有効）")

    from core.downloader import (
        has_oauth2_token as _has_oauth2,
        get_oauth2_token_json as _get_oauth2_json,
        start_oauth2_flow as _start_oauth2_flow,
    )

    # 現在の OAuth2 状態を表示
    if _has_oauth2():
        st.success("✅ OAuth2 トークンあり（cookies なしでダウンロード可能）")
    else:
        st.info("OAuth2 トークンなし（cookies またはフォールバックで動作）")

    # OAuth2 フロー中のセッション状態管理
    # _oauth2_lines: バックグラウンドスレッドが蓄積する出力行（module-level list）
    import threading as _threading
    import re as _re

    if "_oauth2_lines" not in s:
        s["_oauth2_lines"] = []
    if "_oauth2_running" not in s:
        s["_oauth2_running"] = False
    if "_oauth2_done" not in s:
        s["_oauth2_done"] = False
    if "_oauth2_error" not in s:
        s["_oauth2_error"] = None

    # 認証開始ボタン
    _col_oauth_start, _col_oauth_reset = st.columns([2, 1])
    with _col_oauth_start:
        if not s["_oauth2_running"] and not s["_oauth2_done"]:
            if st.button("🚀 OAuth2 認証を開始する", key="admin_oauth2_start_btn"):
                s["_oauth2_lines"] = []
                s["_oauth2_running"] = True
                s["_oauth2_done"] = False
                s["_oauth2_error"] = None
                # バックグラウンドスレッドで yt-dlp OAuth2 フローを実行
                _result_holder = {"lines": s["_oauth2_lines"]}
                def _oauth2_worker(holder=_result_holder):
                    try:
                        proc = _start_oauth2_flow()
                        for _line in proc.stdout:
                            holder["lines"].append(_line.rstrip())
                        proc.wait()
                        holder["lines"].append(f"__DONE__:{proc.returncode}")
                    except Exception as _ex:
                        holder["lines"].append(f"__ERROR__:{_ex}")
                _t = _threading.Thread(target=_oauth2_worker, daemon=True)
                _t.start()
                s["_oauth2_thread_lines"] = _result_holder["lines"]
                st.rerun()

    with _col_oauth_reset:
        if s["_oauth2_running"] or s["_oauth2_done"]:
            if st.button("🔄 リセット", key="admin_oauth2_reset_btn"):
                s["_oauth2_running"] = False
                s["_oauth2_done"] = False
                s["_oauth2_lines"] = []
                s["_oauth2_error"] = None
                st.rerun()

    # フロー実行中の表示
    if s["_oauth2_running"]:
        _lines = s.get("_oauth2_thread_lines", [])

        # 終了判定
        _finished = any(l.startswith("__DONE__:") or l.startswith("__ERROR__:") for l in _lines)
        if _finished:
            _last = next(l for l in _lines if l.startswith("__DONE__:") or l.startswith("__ERROR__:"))
            if _last.startswith("__DONE__:0"):
                # 成功 → トークンを Supabase に保存
                s["_oauth2_running"] = False
                s["_oauth2_done"] = True
                try:
                    from core.db import set_site_setting as _sss
                    _tok_json = _get_oauth2_json()
                    if _tok_json:
                        _sss("youtube_oauth2_token", _tok_json, updated_by=s.get("user_email", ""))
                        st.success("✅ OAuth2 認証完了！トークンを Supabase に保存しました。")
                        st.balloons()
                    else:
                        st.warning("⚠️ トークンファイルが見つかりませんでした。")
                except Exception as _e:
                    st.error(f"Supabase 保存失敗: {_e}")
                st.rerun()
            else:
                s["_oauth2_running"] = False
                _ytdlp_output = "\n".join(
                    l for l in _lines if not l.startswith("__")
                ) or "（出力なし）"
                s["_oauth2_error"] = f"{_last}\n\nyt-dlp 出力:\n{_ytdlp_output}"
                st.rerun()
        else:
            # フロー継続中: URL とコードを探して表示
            _auth_url = None
            _auth_code = None
            for _ln in _lines:
                _m_url = _re.search(r'https://\S+google\S+', _ln)
                if _m_url:
                    _auth_url = _m_url.group(0).rstrip(".")
                _m_code = _re.search(r'\b([A-Z]{4}-[A-Z]{4})\b', _ln)
                if _m_code:
                    _auth_code = _m_code.group(1)

            if _auth_url and _auth_code:
                st.markdown("### 📱 ブラウザで以下の手順を実行してください")
                st.markdown(f"**① 下記の URL をブラウザで開く:**")
                st.code(_auth_url, language=None)
                st.markdown(f"**② コードを入力する:**")
                st.code(_auth_code, language=None)
                st.markdown("**③ Google アカウントでログインして承認する**")
                st.info("⏳ 承認待ち中... 承認が完了すると自動的に完了します。")
            else:
                st.info("⏳ yt-dlp を起動して認証 URL を取得しています...")

            import time as _time
            _time.sleep(1.5)
            st.rerun()

    if s.get("_oauth2_error"):
        st.error(f"OAuth2 エラー: {s['_oauth2_error']}")

    if s["_oauth2_done"]:
        st.success("✅ OAuth2 認証済み。次回からは cookies 不要でダウンロードできます。")

    with st.expander("OAuth2 とは？"):
        st.markdown("""
**OAuth2 認証** = Google アカウントで yt-dlp を承認する仕組みです。

| | cookies | OAuth2 |
|---|---|---|
| 有効期限 | 数日〜1週間 | **数週間〜数ヶ月** |
| 操作 | 拡張機能でエクスポート | **ブラウザで1クリック承認** |
| 安定性 | △ | **◎** |

承認後はトークンが Supabase に保存されるため、Railway が再起動しても自動復元されます。
        """)


# ── 認証エラーメッセージ日本語化 ────────────────────────────
def _auth_error_ja(e) -> str:
    """Supabase 認証エラーを日本語に変換"""
    msg = str(e).lower()
    if "invalid login credentials" in msg or "invalid credentials" in msg:
        return "メールアドレスまたはパスワードが正しくありません。"
    if "email not confirmed" in msg:
        return "メールアドレスの確認が完了していません。確認メールのリンクをクリックしてください。"
    if "already registered" in msg or "user already exists" in msg:
        return "このメールアドレスは既に登録されています。"
    if "password" in msg and ("short" in msg or "weak" in msg or "characters" in msg):
        return "パスワードが条件を満たしていません（8文字以上）。"
    if "rate limit" in msg or "too many" in msg:
        return "リクエストが多すぎます。しばらく待ってから再試行してください。"
    if "network" in msg or "connection" in msg:
        return "ネットワークエラーが発生しました。接続を確認してください。"
    if "database error" in msg:
        return "サーバーエラーが発生しました。しばらく待ってから再試行してください。"
    if "sending confirmation email" in msg or ("email" in msg and "send" in msg):
        return "確認メールの送信に失敗しました。しばらく待ってから再試行してください（1時間あたりの送信数制限に達した可能性があります）。"
    if "signup disabled" in msg:
        return "現在新規登録は受け付けていません。"
    if "only request this after" in msg or "security purposes" in msg:
        return "少し待ってから再度お試しください（連続操作の制限）。"
    return f"エラーが発生しました: {str(e)}"


# ── ログイン / 会員登録ページ ─────────────────────────────
def render_login_page():
    """マルチユーザーモード時のログイン・会員登録画面（リデザイン版）"""
    import streamlit.components.v1 as _comp

    # ── Supabase 認証コールバック処理 JS ──────────────────────────────
    # #access_token fragment (メール確認) を ?sb_access_token= に変換する
    # ?code= (Google PKCE) は Python (_handle_supabase_pkce_callback) が state パラメータで処理する
    _comp.html("""
<script>
(function() {
  try {
    var top = window.top;

    // #access_token fragment (メール確認) を ?sb_access_token= に変換する
    // ?code= (Google PKCE) は Python (_handle_supabase_pkce_callback) が処理するので不要
    var hash = top.location.hash;
    if (hash && hash.indexOf('access_token') !== -1) {
      var params  = new URLSearchParams(hash.substring(1));
      var token   = params.get('access_token');
      var refresh = params.get('refresh_token') || '';
      var type    = params.get('type') || 'signup';
      if (!token) return;
      var loc = top.location;
      var url = loc.origin + loc.pathname
              + '?sb_access_token='   + encodeURIComponent(token)
              + '&sb_refresh_token='  + encodeURIComponent(refresh)
              + '&sb_type='           + encodeURIComponent(type);
      var a = top.document.createElement('a');
      a.href = url;
      top.document.body.appendChild(a);
      a.click();
      top.document.body.removeChild(a);
    }
  } catch(e) { console.warn('sb-auth:', e); }
})();
</script>
""", height=0)

    # ── ページ全体 CSS ──────────────────────────────────────
    st.markdown("""
<style>
/* ヘッダー・ツールバー非表示 */
[data-testid="stHeader"],
[data-testid="stToolbar"],
.stDeployButton { display: none !important; }

/* コンテンツ中央寄せ・幅制限 */
section.main > div.block-container {
  max-width: 460px !important;
  padding: 28px 20px 48px !important;
  margin: 0 auto !important;
}

/* ── st.tabs スタイリング ── */
div[data-testid="stTabs"] > div:first-child {
  background: #f3f4f6 !important;
  border-radius: 14px !important;
  padding: 4px !important;
  border-bottom: none !important;
  gap: 4px !important;
}
div[data-testid="stTabs"] button[role="tab"] {
  border-radius: 10px !important;
  font-size: 14px !important;
  font-weight: 600 !important;
  padding: 10px 0 !important;
  flex: 1 !important;
  border: none !important;
  color: #6b7280 !important;
  background: transparent !important;
}
div[data-testid="stTabs"] button[role="tab"][aria-selected="true"] {
  background: white !important;
  box-shadow: 0 2px 10px rgba(0,0,0,0.10) !important;
  font-weight: 800 !important;
  color: #111827 !important;
}
div[data-testid="stTabs"] button[role="tab"]:focus:not(:focus-visible) {
  box-shadow: 0 2px 10px rgba(0,0,0,0.10) !important;
}
/* タブの下線を非表示 */
div[data-testid="stTabs"] > div:first-child::after,
div[data-testid="stTabs"] > div:first-child > div::after { display:none !important; }
div[data-testid="stTabs"] [data-baseweb="tab-highlight"] { display:none !important; }
div[data-testid="stTabs"] [data-baseweb="tab-border"] { display:none !important; }

/* ── 入力フィールド ── */
.stTextInput > div > div > input {
  border-radius: 12px !important;
  border: 1.5px solid #e5e7eb !important;
  padding: 13px 14px !important;
  font-size: 14.5px !important;
  transition: border-color 0.18s, box-shadow 0.18s !important;
}
.stTextInput > div > div > input:focus {
  border-color: #ea580c !important;
  box-shadow: 0 0 0 3px rgba(234,88,12,0.13) !important;
}

/* ── プライマリボタン（ログイン/登録） ── */
button[data-testid="baseButton-primary"],
.stButton > button[kind="primary"] {
  background: linear-gradient(145deg, #f97316 0%, #ea580c 60%, #dc2626 100%) !important;
  border: none !important;
  border-radius: 14px !important;
  font-size: 16px !important;
  font-weight: 800 !important;
  padding: 14px !important;
  box-shadow: 0 4px 16px rgba(234,88,12,0.34) !important;
  letter-spacing: 0.02em !important;
  transition: all 0.2s !important;
  color: white !important;
}
button[data-testid="baseButton-primary"]:hover:not(:disabled),
.stButton > button[kind="primary"]:hover:not(:disabled) {
  transform: translateY(-2px) !important;
  box-shadow: 0 8px 24px rgba(234,88,12,0.44) !important;
}
button[data-testid="baseButton-primary"]:disabled,
.stButton > button[kind="primary"]:disabled {
  opacity: 0.55 !important;
}

/* ── ラベル ── */
.stTextInput label {
  font-size: 13.5px !important;
  font-weight: 700 !important;
  color: #374151 !important;
}

/* ── 下余白の微調整 ── */
.stTextInput { margin-bottom: 4px !important; }
</style>
""", unsafe_allow_html=True)

    # ── アプリアイコン + ブランド名 ─────────────────────────
    logo_path = BASE_DIR / "assets" / "logo.png"
    if logo_path.exists():
        _logo_b64 = base64.b64encode(logo_path.read_bytes()).decode()
        _icon_html = (
            f'<img src="data:image/png;base64,{_logo_b64}" '
            f'style="width:72px;height:72px;object-fit:contain;border-radius:18px;">'
        )
    else:
        _icon_html = (
            '<div style="width:72px;height:72px;'
            'background:linear-gradient(135deg,#fff4ed 0%,#fed7aa 100%);'
            'border-radius:18px;display:flex;align-items:center;justify-content:center;'
            'font-size:38px;box-shadow:0 6px 20px rgba(234,88,12,0.22);">✂️</div>'
        )
    st.markdown(f"""
<div style="text-align:center;margin-bottom:24px;">
  <div style="display:inline-block;margin-bottom:12px;">{_icon_html}</div>
  <div style="font-size:22px;font-weight:900;letter-spacing:-.02em;
              background:linear-gradient(135deg,#ea580c 0%,#dc2626 60%,#b91c1c 100%);
              -webkit-background-clip:text;-webkit-text-fill-color:transparent;
              background-clip:text;margin-bottom:3px;">切り抜きくん</div>
  <div style="font-size:12.5px;color:#94a3b8;font-weight:500;">YouTube Shorts 自動生成ツール</div>
</div>
""", unsafe_allow_html=True)

    # ── Google OAuth URL を生成（毎回再生成して PKCE ペアを新鮮に保つ）──
    # code_verifier は state パラメータに埋め込まれるのでブラウザストレージ不要
    if not st.session_state.get("_google_oauth_url"):
        try:
            from core.auth import get_google_oauth_url
            _gurl, _gcv = get_google_oauth_url(_get_app_url())
            if _gurl:
                st.session_state["_google_oauth_url"] = _gurl
                st.session_state["_google_oauth_cv"]  = _gcv  # フォールバック用
        except Exception:
            pass
    _google_url = st.session_state.get("_google_oauth_url", "")
    _gcv        = st.session_state.get("_google_oauth_cv", "")

    # ── Google ボタン（components.html で描画）──────────────────────
    # onclick で localStorage 保存 + window.top 遷移を1アクションで実行する。
    # ・st.markdown は DOMPurify で onclick が削除されるため使用不可。
    # ・<a target="_top"> は iframe サンドボックス制限で動作しないため使用不可。
    # ・window.top.location.href は components.html の iframe JS から確実に動作する。
    # ・window._sbGUrl / window._sbGCv は <script> で事前設定→属性エスケープ不要。
    def _make_google_btn(label: str) -> str:
        if not _google_url:
            return ""
        _url_js = json.dumps(_google_url)
        _cv_js  = json.dumps(_gcv)
        _svg = (
            '<svg width="20" height="20" viewBox="0 0 24 24">'
            '<path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92'
            'c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57'
            'c2.08-1.92 3.28-4.74 3.28-8.09z" fill="#4285F4"/>'
            '<path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77'
            'c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18'
            'v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853"/>'
            '<path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09'
            'V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93'
            'l2.85-2.22.81-.62z" fill="#FBBC05"/>'
            '<path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15'
            'C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07'
            'l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335"/>'
            '</svg>'
        )
        return (
            # <script> で変数を設定 → onclick 属性内では引用符エスケープ不要
            "<script>"
            "window._sbGUrl=" + _url_js + ";"
            "window._sbGCv=" + _cv_js + ";"
            "</script>"
            "<style>"
            "*{margin:0;padding:0;box-sizing:border-box}"
            ".gb{display:flex;align-items:center;justify-content:center;gap:10px;"
            "width:100%;padding:12px 20px;"
            "border:1.5px solid #e5e7eb;border-radius:14px;"
            "background:white;font-size:14.5px;font-weight:700;color:#374151;"
            "font-family:-apple-system,'Hiragino Sans',sans-serif;"
            "cursor:pointer;transition:border-color .18s,background .18s}"
            ".gb:hover{border-color:#d1d5db;background:#fafafa}"
            "</style>"
            # onclick: localStorage に verifier を保存してから遷移
            "<button class='gb' onclick=\"(function(){"
            "try{var v=window._sbGCv;"
            "sessionStorage.setItem('_sb_pkce_cv',v);"
            "localStorage.setItem('_sb_pkce_cv',v);}catch(e){}"
            "window.top.location.href=window._sbGUrl;"
            "})();\">"
            + _svg + label
            + "</button>"
        )

    _divider_html = """
<div style="display:flex;align-items:center;gap:10px;margin:16px 0;">
  <div style="flex:1;height:1px;background:#e5e7eb;"></div>
  <span style="font-size:12px;color:#9ca3af;font-weight:500;">または</span>
  <div style="flex:1;height:1px;background:#e5e7eb;"></div>
</div>
"""

    # ── タブ ────────────────────────────────────────────────
    tab_login, tab_register = st.tabs(["　ログイン　", "　新規登録　"])

    # ══════════════════════════════════════════════════════
    # ログインパネル
    # ══════════════════════════════════════════════════════
    with tab_login:
        st.markdown("""
<div style="text-align:center;margin-bottom:22px;">
  <h2 style="font-size:24px;font-weight:800;color:#111827;margin:0 0 6px;letter-spacing:-.3px;">
    おかえりなさい
  </h2>
  <p style="font-size:13px;color:#6b7280;margin:0;line-height:1.6;">
    アカウント情報を入力してログインしてください。
  </p>
</div>
""", unsafe_allow_html=True)

        # Google ボタン（components.html で描画 → onclick で localStorage 保存+遷移）
        if _google_url:
            _comp.html(_make_google_btn("Googleでログイン"), height=56)
            st.markdown(_divider_html, unsafe_allow_html=True)

        # メール/パスワード
        email_l = st.text_input("メールアドレス", key="login_email",
                                placeholder="you@example.com")
        pass_l  = st.text_input("パスワード", type="password", key="login_pass",
                                placeholder="••••••••")

        if st.button("ログイン", type="primary", use_container_width=True,
                     key="do_login",
                     disabled=not (email_l.strip() and pass_l)):
            try:
                from core.auth import sign_in
                res = sign_in(email_l.strip(), pass_l)
                if res.session:
                    user = res.user
                    st.session_state["user_id"]      = user.id
                    st.session_state["user_email"]   = user.email
                    st.session_state["_supabase_rt"] = res.session.refresh_token
                    try:
                        from core.db import get_youtube_token
                        yt = get_youtube_token(user.id)
                        if yt:
                            st.session_state["yt_token"] = yt
                            try:
                                from core.uploader import get_channel_info
                                ch = get_channel_info(yt)
                                if ch:
                                    st.session_state["yt_channel_name"]      = ch["title"]
                                    st.session_state["yt_channel_id"]        = ch["id"]
                                    st.session_state["yt_channel_thumbnail"] = ch.get("thumbnail", "")
                            except Exception:
                                pass
                    except Exception:
                        pass
                    st.query_params.clear()
                    st.rerun()
                else:
                    st.error("ログインに失敗しました。メールアドレスとパスワードを確認してください。")
            except Exception as e:
                st.error(_auth_error_ja(e))

        # フッターリンク
        st.markdown("""
<div style="text-align:center;margin-top:20px;">
  <span style="font-size:13px;color:#6b7280;">
    アカウントをお持ちでない場合は
    <strong style="color:#ea580c;">↑ 新規登録タブ</strong>から登録できます
  </span>
</div>
""", unsafe_allow_html=True)

    # ══════════════════════════════════════════════════════
    # 新規登録パネル
    # ══════════════════════════════════════════════════════
    with tab_register:
        st.markdown("""
<div style="text-align:center;margin-bottom:22px;">
  <h2 style="font-size:24px;font-weight:800;color:#111827;margin:0 0 6px;letter-spacing:-.3px;">
    はじめましょう
  </h2>
  <p style="font-size:13px;color:#6b7280;margin:0;line-height:1.6;">
    無料で始められます。
  </p>
</div>
""", unsafe_allow_html=True)

        # Google ボタン（テキストを「Googleで登録」に変更・components.html で描画）
        if _google_url:
            _comp.html(_make_google_btn("Googleで登録"), height=56)
            st.markdown(_divider_html, unsafe_allow_html=True)

        # 入力フォーム
        email_s  = st.text_input("メールアドレス", key="signup_email",
                                 placeholder="you@example.com")
        pass_s1  = st.text_input("パスワード（8文字以上）", type="password",
                                 key="signup_pass1", placeholder="••••••••")
        pass_s2  = st.text_input("パスワード（確認）", type="password",
                                 key="signup_pass2", placeholder="••••••••")

        pass_ok = len(pass_s1) >= 8 and pass_s1 == pass_s2

        # バリデーションメッセージ
        if pass_s1 and pass_s2 and not pass_ok:
            if len(pass_s1) < 8:
                st.markdown(
                    '<div style="font-size:12px;color:#f97316;margin-top:-8px;margin-bottom:4px;">'
                    '⚠️ パスワードは8文字以上で設定してください</div>',
                    unsafe_allow_html=True,
                )
            elif pass_s1 != pass_s2:
                st.markdown(
                    '<div style="font-size:12px;color:#f97316;margin-top:-8px;margin-bottom:4px;">'
                    '⚠️ パスワードが一致しません</div>',
                    unsafe_allow_html=True,
                )

        if st.button("無料で登録", type="primary", use_container_width=True,
                     key="do_register",
                     disabled=not (email_s.strip() and pass_ok)):
            try:
                from core.auth import sign_up, sign_in
                res = sign_up(email_s.strip(), pass_s1)
                if res.user:
                    try:
                        from core.mailer import send_welcome_email
                        send_welcome_email(email_s.strip())
                    except Exception:
                        pass
                    # 確認メール不要設定の場合はそのままログイン
                    try:
                        login_res = sign_in(email_s.strip(), pass_s1)
                        if login_res.session:
                            st.session_state["user_id"]      = login_res.user.id
                            st.session_state["user_email"]   = login_res.user.email
                            st.session_state["_supabase_rt"] = login_res.session.refresh_token
                            st.query_params.clear()
                            st.rerun()
                            return
                    except Exception:
                        pass
                    st.success("✅ 登録完了！確認メールを送信しました。メールをご確認の上ログインしてください。")
                else:
                    st.error("登録に失敗しました。")
            except Exception as e:
                st.error(_auth_error_ja(e))

        st.markdown("""
<div style="text-align:center;margin-top:20px;">
  <span style="font-size:13px;color:#6b7280;">
    すでにアカウントをお持ちの方は
    <strong style="color:#ea580c;">↑ ログインタブ</strong>からどうぞ
  </span>
</div>
""", unsafe_allow_html=True)

    # ── フッター ────────────────────────────────────────────
    st.markdown("""
<div style="text-align:center;font-size:11px;color:#cbd5e1;margin-top:36px;line-height:2;">
  ✂️ 切り抜きくん Beta
</div>
""", unsafe_allow_html=True)


# ── 動画情報バナー（ステップ2以降で表示） ─────────────────
def render_video_banner():
    info = s.video_info
    if not info:
        return
    dur = info.get("duration", 0)
    h = int(dur) // 3600
    m = (int(dur) % 3600) // 60
    sec = int(dur) % 60
    dur_str = f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"
    views = f"{info.get('view_count', 0):,}" if info.get("view_count") else "—"

    st.markdown(f"""
    <div class="video-card">
      <div class="video-meta">
        <span class="badge badge-purple">📺 元動画</span>
        <h3>{info.get('title','')[:70]}</h3>
        <p>{info.get('uploader','')} &nbsp;·&nbsp; 尺: {dur_str} &nbsp;·&nbsp; 再生: {views}</p>
      </div>
    </div>
    """, unsafe_allow_html=True)


# ── ステージHTML表示ヘルパー（st.markdown はSVGをsanitizeするため components.html を使用）──
def _show_stage_html(ph, html: str, height: int = 370) -> None:
    """st.empty() プレースホルダーに components.html でHTMLを描画する。
    st.markdown は SVG/filter 要素をサニタイズして生テキストになるため使用不可。"""
    import streamlit.components.v1 as _cv1
    with ph:
        _cv1.html(html, height=height, scrolling=False)


# ── Step1 解析ステージ用ローディングカード ─────────────────
def _get_wait_note(elapsed: float) -> str:
    """経過時間に応じた気遣いメッセージを返す（汎用）"""
    if elapsed < 30:
        return ""
    elif elapsed < 90:
        return "⏳ 動画のサイズによっては数分かかる場合があります"
    elif elapsed < 180:
        return "☕ コーヒーでも飲みながらお待ちください。まだ処理中です"
    elif elapsed < 360:
        return "🙏 もう少しかかります。ページを閉じないでください"
    else:
        return "💪 頑張ってます！完了まで今しばらくお待ちください"


def _get_dl_context_note(elapsed: float, speed_mbps: float = 0.0) -> str:
    """ダウンロード中の気遣いメッセージ（通信速度・経過時間を考慮）"""
    if elapsed < 20:
        return ""
    if 0 < speed_mbps < 0.5:
        return f"📡 通信速度が遅めです（{speed_mbps:.1f} MB/s）。動画が長いほど待ち時間が増えます ☕"
    elif elapsed < 60:
        return "⏳ 動画の長さや通信環境によって待ち時間が変わります"
    elif elapsed < 180:
        return "☕ 長い動画ほど時間がかかります。コーヒーでも飲みながらお待ちください"
    elif elapsed < 360:
        return "🙏 まだ処理中です。ページを閉じないでください"
    else:
        return "💪 長い動画はどうしても時間がかかります。もう少しです！"


def _make_analysis_stage_html(title: str, detail: str = "", note: str = "") -> str:
    """3D 料理鍋アニメーション（蓋なし・食材が中で煮込まれている）+ ステータス表示のローディングカード。"""
    return f"""
<style>
@keyframes ck-shimmer{{0%{{background-position:-200% center}}100%{{background-position:200% center}}}}
@keyframes ck-bubble{{0%{{transform:translateY(0) scale(.7);opacity:.95}}70%{{opacity:.5}}100%{{transform:translateY(-55px) scale(2);opacity:0}}}}
@keyframes ck-steam{{0%{{transform:translateY(0) scaleX(1);opacity:.6}}50%{{transform:translateY(-35px) scaleX(1.3);opacity:.3}}100%{{transform:translateY(-75px) scaleX(.5);opacity:0}}}}
@keyframes ck-bob{{0%,100%{{transform:translateY(0) rotate(-5deg) scale(1)}}50%{{transform:translateY(-10px) rotate(5deg) scale(1.1)}}}}
@keyframes ck-sway{{0%,100%{{transform:translateX(0) rotate(-8deg)}}50%{{transform:translateX(6px) rotate(8deg)}}}}
@keyframes ck-spin{{to{{transform:rotate(360deg)}}}}
@keyframes ck-flicker{{0%,100%{{transform:scaleY(1) scaleX(1);opacity:.85}}33%{{transform:scaleY(1.18) scaleX(.9);opacity:1}}66%{{transform:scaleY(.92) scaleX(1.05);opacity:.9}}}}
@keyframes ck-ladle{{0%{{transform:rotate(-25deg) translateX(0)}}25%{{transform:rotate(15deg) translateX(10px)}}50%{{transform:rotate(35deg) translateX(14px)}}75%{{transform:rotate(15deg) translateX(10px)}}100%{{transform:rotate(-25deg) translateX(0)}}}}
@keyframes ck-drip{{0%{{transform:translateY(0) scale(1);opacity:.9}}100%{{transform:translateY(28px) scale(.3);opacity:0}}}}
@keyframes ck-float-in{{0%,100%{{transform:translateY(0) rotate(-5deg) scale(1)}}50%{{transform:translateY(-12px) rotate(5deg) scale(1.08)}}}}
@keyframes ck-status-pulse{{0%,100%{{opacity:.75}}50%{{opacity:1}}}}
@keyframes ck-wave{{0%{{transform:scaleX(1)}}50%{{transform:scaleX(.96)}}100%{{transform:scaleX(1)}}}}
@keyframes ck-boil-up{{
  0%,100%{{transform:translateY(0) rotate(0deg) scale(1);opacity:.85}}
  25%{{transform:translateY(-16px) rotate(-12deg) scale(1.22);opacity:1}}
  55%{{transform:translateY(-9px) rotate(7deg) scale(1.1);opacity:1}}
  80%{{transform:translateY(-3px) rotate(-2deg) scale(1.03);opacity:.9}}
}}
@keyframes ck-boil-up2{{
  0%,100%{{transform:translateY(0) rotate(3deg) scale(1);opacity:.8}}
  30%{{transform:translateY(-20px) rotate(-9deg) scale(1.28);opacity:1}}
  60%{{transform:translateY(-11px) rotate(11deg) scale(1.13);opacity:1}}
  85%{{transform:translateY(-2px) rotate(2deg) scale(1.02);opacity:.9}}
}}
@keyframes ck-splash{{0%,100%{{transform:translateY(0) scale(1);opacity:.6}}50%{{transform:translateY(-8px) scale(1.5);opacity:0}}}}
@keyframes ck-soup-glow{{0%,100%{{opacity:.7}}50%{{opacity:1}}}}
.ck-card{{
  background:radial-gradient(ellipse at 50% 0%,#3a1400 0%,#0d0400 65%);
  border:1.5px solid rgba(251,146,60,.45);border-radius:20px;
  padding:18px 14px 16px;
  font-family:-apple-system,'Hiragino Sans',sans-serif;
  position:relative;overflow:hidden;text-align:center;
}}
.ck-glow{{position:absolute;border-radius:50%;pointer-events:none;filter:blur(50px);}}
.ck-title{{font-size:16px;font-weight:800;margin-bottom:12px;
  background:linear-gradient(90deg,#fbbf24,#f97316,#ef4444,#fb923c,#fbbf24);background-size:300%;
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
  animation:ck-shimmer 2.2s linear infinite;}}
.ck-status{{
  margin:0 auto 12px;
  background:rgba(251,146,60,.12);
  border:1.5px solid rgba(251,146,60,.35);
  border-radius:14px;padding:9px 18px;
  font-size:13px;font-weight:700;color:#fed7aa;
  line-height:1.5;max-width:320px;
  animation:ck-status-pulse 2s ease-in-out infinite;
}}
.ck-status-icon{{font-size:15px;margin-right:5px;}}
.ck-spinner{{width:22px;height:22px;border-radius:50%;
  border:3px solid rgba(251,146,60,.2);border-top-color:#f97316;
  animation:ck-spin .75s linear infinite;margin:0 auto;}}
</style>
<div class="ck-card">
  <div class="ck-glow" style="width:260px;height:160px;background:rgba(234,88,12,.22);top:-60px;left:50%;transform:translateX(-50%);"></div>
  <div class="ck-glow" style="width:140px;height:140px;background:rgba(251,146,60,.15);bottom:-50px;right:-10px;"></div>
  <div class="ck-glow" style="width:110px;height:110px;background:rgba(239,68,68,.12);bottom:-40px;left:-10px;"></div>

  <svg viewBox="0 0 260 185" width="224" height="160" xmlns="http://www.w3.org/2000/svg" style="overflow:visible;display:block;margin:0 auto 6px;">
    <defs>
      <linearGradient id="pg-side" x1="0%" y1="0%" x2="100%" y2="0%">
        <stop offset="0%" stop-color="#1a0700"/>
        <stop offset="22%" stop-color="#5a2508"/>
        <stop offset="55%" stop-color="#7a3410"/>
        <stop offset="85%" stop-color="#4a1c06"/>
        <stop offset="100%" stop-color="#200900"/>
      </linearGradient>
      <linearGradient id="pg-rim" x1="0%" y1="0%" x2="0%" y2="100%">
        <stop offset="0%" stop-color="#c09050"/>
        <stop offset="45%" stop-color="#7a5228"/>
        <stop offset="100%" stop-color="#3e2408"/>
      </linearGradient>
      <radialGradient id="pg-liq" cx="40%" cy="35%">
        <stop offset="0%" stop-color="#ff9d50"/>
        <stop offset="30%" stop-color="#f06010"/>
        <stop offset="65%" stop-color="#c03a00"/>
        <stop offset="100%" stop-color="#6a1500"/>
      </radialGradient>
      <radialGradient id="pg-lhl" cx="35%" cy="28%">
        <stop offset="0%" stop-color="rgba(255,240,150,.9)"/>
        <stop offset="55%" stop-color="rgba(255,160,60,.35)"/>
        <stop offset="100%" stop-color="rgba(255,80,0,.0)"/>
      </radialGradient>
      <radialGradient id="pg-flame1" cx="50%" cy="80%">
        <stop offset="0%" stop-color="#fff9c4"/>
        <stop offset="25%" stop-color="#ffb300"/>
        <stop offset="60%" stop-color="#f44336"/>
        <stop offset="100%" stop-color="rgba(183,28,28,0)"/>
      </radialGradient>
      <radialGradient id="pg-flame2" cx="50%" cy="70%">
        <stop offset="0%" stop-color="#fff59d"/>
        <stop offset="40%" stop-color="#ff8f00"/>
        <stop offset="100%" stop-color="rgba(244,67,54,0)"/>
      </radialGradient>
      <linearGradient id="pg-ladle" x1="0%" y1="0%" x2="0%" y2="100%">
        <stop offset="0%" stop-color="#d8d8d8"/>
        <stop offset="100%" stop-color="#787878"/>
      </linearGradient>
      <filter id="pg-sh"><feDropShadow dx="0" dy="4" stdDeviation="6" flood-color="rgba(0,0,0,.7)"/></filter>
      <filter id="pg-glow"><feGaussianBlur stdDeviation="3" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
      <filter id="pg-soft"><feGaussianBlur stdDeviation="1.5"/></filter>
      <filter id="pg-food"><feDropShadow dx="0" dy="2" stdDeviation="2.5" flood-color="rgba(160,50,0,.8)"/></filter>
      <!-- リムを輪っか（リング）にするマスク：中央を透明にして鍋の内部を見えるようにする -->
      <mask id="pg-rim-mask">
        <rect x="0" y="0" width="260" height="185" fill="white"/>
        <ellipse cx="130" cy="88" rx="52" ry="12" fill="black"/>
      </mask>
    </defs>

    <!-- 地面の影 -->
    <ellipse cx="130" cy="178" rx="85" ry="10" fill="rgba(0,0,0,.45)"/>

    <!-- コンロの輝き -->
    <ellipse cx="130" cy="167" rx="55" ry="10" fill="rgba(255,120,0,.18)" filter="url(#pg-soft)" style="animation:ck-wave 1.5s ease-in-out infinite;"/>

    <!-- 炎 レイヤー3 (base) -->
    <g style="animation:ck-flicker .5s ease-in-out infinite alternate .2s;">
      <path d="M82,164 Q100,130 130,150 Q160,130 178,164" fill="url(#pg-flame1)" opacity=".9"/>
    </g>
    <!-- 炎 レイヤー2 -->
    <g style="animation:ck-flicker .35s ease-in-out infinite alternate;">
      <path d="M88,164 Q105,138 130,152 Q155,138 172,164" fill="url(#pg-flame2)" opacity=".85"/>
      <path d="M94,164 Q110,144 130,155 Q150,144 166,164" fill="rgba(255,245,100,.4)"/>
    </g>
    <!-- 炎 レイヤー1 (top) -->
    <g style="animation:ck-flicker .28s ease-in-out infinite alternate .1s;">
      <path d="M97,165 Q113,148 130,157 Q147,148 163,165" fill="rgba(255,255,180,.5)"/>
    </g>

    <!-- 鍋本体 -->
    <ellipse cx="130" cy="163" rx="70" ry="14" fill="#180700" filter="url(#pg-sh)"/>
    <path d="M60,163 Q60,98 74,90 L186,90 Q200,98 200,163 Z" fill="url(#pg-side)"/>
    <path d="M74,90 L86,163" stroke="rgba(255,200,80,.08)" stroke-width="12" stroke-linecap="round"/>
    <path d="M186,90 L174,163" stroke="rgba(0,0,0,.3)" stroke-width="12" stroke-linecap="round"/>

    <!-- スープ（グツグツと輝く煮込みスープ） -->
    <ellipse cx="130" cy="92" rx="55" ry="15" fill="url(#pg-liq)"/>
    <ellipse cx="118" cy="87" rx="36" ry="10" fill="url(#pg-lhl)" opacity=".95"/>
    <!-- スープ表面の輝きスポット -->
    <ellipse cx="108" cy="88" rx="14" ry="5" fill="rgba(255,240,180,.18)" style="animation:ck-soup-glow 1.8s ease-in-out infinite;"/>
    <!-- 液面波紋 -->
    <ellipse cx="130" cy="92" rx="55" ry="15" fill="none" stroke="rgba(255,180,60,.3)" stroke-width="2.5">
      <animate attributeName="rx" values="55;50;55" dur="1.2s" repeatCount="indefinite"/>
      <animate attributeName="ry" values="15;12;15" dur="1.2s" repeatCount="indefinite"/>
    </ellipse>
    <ellipse cx="130" cy="92" rx="38" ry="10" fill="none" stroke="rgba(255,220,100,.18)" stroke-width="1.5">
      <animate attributeName="rx" values="38;34;38" dur="1.6s" repeatCount="indefinite" begin="0.25s"/>
      <animate attributeName="ry" values="10;8;10" dur="1.6s" repeatCount="indefinite" begin="0.25s"/>
    </ellipse>

    <!-- ★ 鍋の中で煮込まれている食材（蓋なし・リムより先に描いてフレーム効果）★ -->
    <!-- 肉 🥩 -->
    <text x="97" y="88" font-size="18" filter="url(#pg-food)" style="animation:ck-boil-up 2.1s ease-in-out infinite;">🥩</text>
    <!-- にんじん 🥕 -->
    <text x="127" y="85" font-size="16" filter="url(#pg-food)" style="animation:ck-boil-up2 2.8s ease-in-out infinite .55s;">🥕</text>
    <!-- ブロッコリー 🥦 -->
    <text x="149" y="87" font-size="16" filter="url(#pg-food)" style="animation:ck-boil-up 2.0s ease-in-out infinite 1.1s;">🥦</text>
    <!-- たまねぎ 🧅 -->
    <text x="112" y="90" font-size="15" filter="url(#pg-food)" style="animation:ck-boil-up2 3.2s ease-in-out infinite .3s;">🧅</text>
    <!-- きのこ 🍄 -->
    <text x="141" y="89" font-size="14" filter="url(#pg-food)" style="animation:ck-boil-up 2.6s ease-in-out infinite 1.8s;">🍄</text>
    <!-- とうもろこし 🌽 -->
    <text x="109" y="83" font-size="14" filter="url(#pg-food)" style="animation:ck-boil-up2 3.5s ease-in-out infinite .85s;">🌽</text>
    <!-- スープの飛び跳ね粒 -->
    <circle cx="110" cy="79" r="3.5" fill="rgba(255,140,30,.75)" style="animation:ck-splash 1.1s ease-out infinite .35s;"/>
    <circle cx="150" cy="77" r="3" fill="rgba(255,100,20,.7)" style="animation:ck-splash 1.5s ease-out infinite .9s;"/>
    <circle cx="130" cy="75" r="2.5" fill="rgba(255,180,60,.65)" style="animation:ck-splash .95s ease-out infinite .65s;"/>
    <circle cx="120" cy="80" r="2" fill="rgba(255,220,100,.6)" style="animation:ck-splash 1.3s ease-out infinite 1.2s;"/>
    <circle cx="142" cy="76" r="2" fill="rgba(255,160,50,.6)" style="animation:ck-splash 1.7s ease-out infinite .15s;"/>

    <!-- 鍋の縁（リム）← 食材の上に描画してフレーム効果 -->
    <ellipse cx="130" cy="90" rx="62" ry="17" fill="url(#pg-rim)" mask="url(#pg-rim-mask)" filter="url(#pg-sh)"/>
    <ellipse cx="130" cy="86" rx="57" ry="12" fill="none" stroke="rgba(255,220,130,.35)" stroke-width="2"/>
    <ellipse cx="121" cy="84" rx="28" ry="6" fill="rgba(255,255,200,.1)"/>

    <!-- 取っ手（左） -->
    <path d="M66,108 Q32,100 30,116 Q28,132 66,128" stroke="url(#pg-rim)" stroke-width="12" fill="none" stroke-linecap="round" filter="url(#pg-sh)"/>
    <path d="M66,108 Q32,100 30,116 Q28,132 66,128" stroke="rgba(255,210,120,.25)" stroke-width="4" fill="none" stroke-linecap="round"/>
    <!-- 取っ手（右） -->
    <path d="M194,108 Q228,100 230,116 Q232,132 194,128" stroke="url(#pg-rim)" stroke-width="12" fill="none" stroke-linecap="round" filter="url(#pg-sh)"/>
    <path d="M194,108 Q228,100 230,116 Q232,132 194,128" stroke="rgba(255,210,120,.25)" stroke-width="4" fill="none" stroke-linecap="round"/>

    <!-- しゃもじ（お玉）— 蓋なしなのでスープに深くかき混ぜる -->
    <g style="transform-origin:146px 84px;animation:ck-ladle 2.4s ease-in-out infinite;">
      <line x1="146" y1="84" x2="168" y2="30" stroke="url(#pg-ladle)" stroke-width="4.5" stroke-linecap="round"/>
      <ellipse cx="168" cy="27" rx="9" ry="6.5" fill="url(#pg-ladle)" filter="url(#pg-glow)"/>
      <ellipse cx="166" cy="25" rx="4.5" ry="2.5" fill="rgba(255,255,255,.45)"/>
      <!-- お玉に付いたスープの滴 -->
      <circle cx="152" cy="70" r="2.5" fill="rgba(255,130,40,.8)" style="animation:ck-drip 1.3s ease-in infinite .2s;"/>
    </g>

    <!-- 泡（活発・大量に） -->
    <circle cx="110" cy="88" r="5.5" fill="rgba(255,220,120,.82)" style="animation:ck-bubble 1.1s ease-out infinite;"/>
    <circle cx="133" cy="86" r="4.5" fill="rgba(255,170,60,.9)" style="animation:ck-bubble 1.5s ease-out infinite .35s;"/>
    <circle cx="119" cy="93" r="5" fill="rgba(255,240,140,.72)" style="animation:ck-bubble .95s ease-out infinite .12s;"/>
    <circle cx="148" cy="89" r="5.5" fill="rgba(255,130,40,.78)" style="animation:ck-bubble 1.7s ease-out infinite .65s;"/>
    <circle cx="101" cy="91" r="4" fill="rgba(255,200,80,.86)" style="animation:ck-bubble 1.3s ease-out infinite .95s;"/>
    <circle cx="157" cy="85" r="3.5" fill="rgba(255,180,60,.82)" style="animation:ck-bubble 1.0s ease-out infinite .5s;"/>
    <circle cx="125" cy="95" r="3" fill="rgba(255,230,100,.72)" style="animation:ck-bubble 1.45s ease-out infinite 1.25s;"/>
    <circle cx="143" cy="93" r="4" fill="rgba(255,150,50,.82)" style="animation:ck-bubble 1.9s ease-out infinite .22s;"/>
    <circle cx="117" cy="84" r="2.5" fill="rgba(255,240,160,.78)" style="animation:ck-bubble .85s ease-out infinite .75s;"/>
    <circle cx="138" cy="87" r="3" fill="rgba(255,200,80,.72)" style="animation:ck-bubble 1.25s ease-out infinite 1.55s;"/>
    <circle cx="105" cy="86" r="2" fill="rgba(255,220,120,.68)" style="animation:ck-bubble 1.6s ease-out infinite 1.8s;"/>

    <!-- 蒸気（4本、開口部から立ち上る） -->
    <path d="M100,72 Q92,55 100,38 Q108,22 100,7" stroke="rgba(255,255,255,.32)" stroke-width="6.5" fill="none" stroke-linecap="round" style="animation:ck-steam 1.7s ease-out infinite;"/>
    <path d="M116,68 Q108,50 116,31 Q124,14 116,0" stroke="rgba(255,240,200,.26)" stroke-width="5.5" fill="none" stroke-linecap="round" style="animation:ck-steam 1.95s ease-out infinite .45s;"/>
    <path d="M144,68 Q136,50 144,31 Q152,14 144,0" stroke="rgba(255,255,255,.30)" stroke-width="5.5" fill="none" stroke-linecap="round" style="animation:ck-steam 1.65s ease-out infinite .95s;"/>
    <path d="M160,72 Q152,55 160,37 Q168,21 160,6" stroke="rgba(255,200,150,.24)" stroke-width="5" fill="none" stroke-linecap="round" style="animation:ck-steam 2.1s ease-out infinite 1.35s;"/>

    <!-- 外側の浮遊食材デコレーション -->
    <text x="14" y="78" font-size="22" style="animation:ck-float-in 2.4s ease-in-out infinite;filter:drop-shadow(0 3px 5px rgba(0,0,0,.6));">🥕</text>
    <text x="202" y="72" font-size="20" style="animation:ck-float-in 3s ease-in-out infinite .4s;filter:drop-shadow(0 3px 5px rgba(0,0,0,.6));">🧅</text>
    <text x="4" y="128" font-size="18" style="animation:ck-sway 3.2s ease-in-out infinite .9s;filter:drop-shadow(0 2px 4px rgba(0,0,0,.6));">🌿</text>
    <text x="208" y="128" font-size="19" style="animation:ck-bob 2.6s ease-in-out infinite 1.1s;filter:drop-shadow(0 3px 5px rgba(0,0,0,.6));">🍖</text>
    <text x="26" y="48" font-size="17" style="animation:ck-float-in 3.8s ease-in-out infinite .6s;filter:drop-shadow(0 2px 4px rgba(0,0,0,.5));">🌶️</text>
    <text x="192" y="44" font-size="15" style="animation:ck-sway 3.5s ease-in-out infinite .3s;filter:drop-shadow(0 2px 4px rgba(0,0,0,.5));">🧄</text>
    <!-- 輝き・炎エフェクト -->
    <text x="54" y="32" font-size="14" style="animation:ck-bob 3s ease-in-out infinite .2s;opacity:.85;">✨</text>
    <text x="186" y="26" font-size="12" style="animation:ck-sway 2.8s ease-in-out infinite .7s;opacity:.75;">⭐</text>
    <text x="18" y="98" font-size="11" style="animation:ck-bob 5s ease-in-out infinite 2s;opacity:.55;">🔥</text>
    <text x="220" y="94" font-size="11" style="animation:ck-bob 4.5s ease-in-out infinite 1.5s;opacity:.5;">🔥</text>
  </svg>

  <div class="ck-title">{title}</div>
  {f'<div class="ck-status"><span class="ck-status-icon">📡</span>{detail}</div>' if detail else ''}
  <div class="ck-spinner"></div>
</div>
"""


def _make_complete_html() -> str:
    """解析完了時のお皿盛り付け演出HTML。"""
    return """
<style>
@keyframes dm-shimmer{0%{background-position:-200% center}100%{background-position:200% center}}
@keyframes dm-plate-in{0%{transform:scale(0) rotate(-20deg);opacity:0}60%{transform:scale(1.15) rotate(5deg);opacity:1}80%{transform:scale(.95) rotate(-2deg)}100%{transform:scale(1) rotate(0deg);opacity:1}}
@keyframes dm-food-in{0%{transform:translateY(-80px) rotate(-45deg) scale(0);opacity:0}70%{transform:translateY(5px) rotate(5deg) scale(1.2);opacity:1}100%{transform:translateY(0) rotate(0deg) scale(1);opacity:1}}
@keyframes dm-steam{0%{transform:translateY(0) scaleX(1);opacity:.5}50%{transform:translateY(-28px) scaleX(1.3);opacity:.25}100%{transform:translateY(-60px) scaleX(.4);opacity:0}}
@keyframes dm-sparkle{0%,100%{transform:scale(0);opacity:0}50%{transform:scale(1);opacity:1}}
@keyframes dm-title-in{0%{opacity:0;transform:translateY(14px) scale(.8)}100%{opacity:1;transform:translateY(0) scale(1)}}
@keyframes dm-glow-pulse{0%,100%{opacity:.5;transform:scale(.9)}50%{opacity:1;transform:scale(1.15)}}
.dm-card{
  background:radial-gradient(ellipse at 50% 30%,#1c0c00 0%,#050505 70%);
  border:1.5px solid rgba(251,191,36,.45);border-radius:20px;
  padding:20px 14px 18px;
  font-family:-apple-system,'Hiragino Sans',sans-serif;
  position:relative;overflow:hidden;text-align:center;
}
.dm-glow{position:absolute;border-radius:50%;pointer-events:none;filter:blur(55px);}
.dm-title{font-size:22px;font-weight:900;margin-bottom:5px;
  background:linear-gradient(90deg,#fbbf24,#f59e0b,#fcd34d,#f97316,#fbbf24);background-size:300%;
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
  animation:dm-shimmer 1.8s linear infinite,dm-title-in .6s ease-out .8s both;
  letter-spacing:.05em;}
.dm-sub{font-size:12.5px;color:#d97706;font-weight:700;opacity:0;animation:dm-title-in .5s ease-out 1.2s forwards;}
</style>
<div class="dm-card">
  <div class="dm-glow" style="width:280px;height:180px;background:rgba(251,191,36,.2);top:-70px;left:50%;transform:translateX(-50%);"></div>
  <div class="dm-glow" style="width:160px;height:160px;background:rgba(234,88,12,.16);bottom:-60px;right:-20px;"></div>
  <div class="dm-glow" style="width:130px;height:130px;background:rgba(234,88,12,.13);bottom:-50px;left:-15px;"></div>
  <svg viewBox="0 0 260 170" width="224" height="148" xmlns="http://www.w3.org/2000/svg" style="overflow:visible;display:block;margin:0 auto 4px;">
    <defs>
      <radialGradient id="dm-plate" cx="40%" cy="35%">
        <stop offset="0%" stop-color="#fffbf0"/><stop offset="30%" stop-color="#f8f0dc"/>
        <stop offset="70%" stop-color="#e8d8b0"/><stop offset="100%" stop-color="#c8b890"/>
      </radialGradient>
      <radialGradient id="dm-rim" cx="50%" cy="50%">
        <stop offset="0%" stop-color="#f0e0b8"/><stop offset="80%" stop-color="#d4b870"/>
        <stop offset="100%" stop-color="#b89840"/>
      </radialGradient>
      <radialGradient id="dm-shadow" cx="50%" cy="50%">
        <stop offset="0%" stop-color="rgba(0,0,0,.55)"/><stop offset="100%" stop-color="rgba(0,0,0,0)"/>
      </radialGradient>
      <filter id="dm-gf"><feGaussianBlur stdDeviation="2.5" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
      <filter id="dm-sh"><feDropShadow dx="0" dy="6" stdDeviation="10" flood-color="rgba(0,0,0,.75)"/></filter>
    </defs>
    <ellipse cx="130" cy="162" rx="90" ry="10" fill="url(#dm-shadow)"/>
    <!-- お皿 登場 -->
    <g style="transform-origin:130px 118px;animation:dm-plate-in .7s cubic-bezier(.34,1.56,.64,1) .1s both;">
      <ellipse cx="132" cy="150" rx="72" ry="15" fill="rgba(0,0,0,.38)"/>
      <ellipse cx="130" cy="136" rx="72" ry="18" fill="url(#dm-rim)" filter="url(#dm-sh)"/>
      <ellipse cx="130" cy="131" rx="60" ry="13" fill="url(#dm-plate)"/>
      <ellipse cx="112" cy="126" rx="24" ry="7" fill="rgba(255,255,255,.38)" transform="rotate(-12 112 126)"/>
      <ellipse cx="130" cy="131" rx="60" ry="13" fill="none" stroke="rgba(255,230,160,.3)" stroke-width="1.5"/>
    </g>
    <!-- 食材が次々と盛り付けられる -->
    <text x="102" y="129" font-size="28" filter="url(#dm-gf)" style="transform-origin:116px 115px;animation:dm-food-in .5s cubic-bezier(.34,1.56,.64,1) .35s both;">🥩</text>
    <text x="128" y="124" font-size="22" filter="url(#dm-gf)" style="transform-origin:139px 113px;animation:dm-food-in .5s cubic-bezier(.34,1.56,.64,1) .52s both;">🥕</text>
    <text x="145" y="130" font-size="22" filter="url(#dm-gf)" style="transform-origin:156px 119px;animation:dm-food-in .5s cubic-bezier(.34,1.56,.64,1) .68s both;">🥦</text>
    <text x="115" y="138" font-size="20" filter="url(#dm-gf)" style="transform-origin:125px 128px;animation:dm-food-in .5s cubic-bezier(.34,1.56,.64,1) .82s both;">🍄</text>
    <text x="137" y="136" font-size="18" filter="url(#dm-gf)" style="transform-origin:146px 127px;animation:dm-food-in .5s cubic-bezier(.34,1.56,.64,1) .95s both;">🧅</text>
    <!-- 蒸気（熱々） -->
    <path d="M114,110 Q107,95 114,80 Q121,66 114,52" stroke="rgba(255,255,255,.32)" stroke-width="5.5" fill="none" stroke-linecap="round" style="animation:dm-steam 1.8s ease-out infinite 1.2s;"/>
    <path d="M130,106 Q123,89 130,72 Q137,57 130,43" stroke="rgba(255,240,200,.26)" stroke-width="5" fill="none" stroke-linecap="round" style="animation:dm-steam 2.1s ease-out infinite 1.5s;"/>
    <path d="M146,110 Q139,93 146,76 Q153,61 146,47" stroke="rgba(255,255,255,.29)" stroke-width="5" fill="none" stroke-linecap="round" style="animation:dm-steam 1.95s ease-out infinite 1.7s;"/>
    <!-- キラキラ -->
    <text x="66" y="92" font-size="18" style="transform-origin:75px 82px;animation:dm-sparkle .6s ease-in-out .9s both,dm-sparkle 2s ease-in-out 2s infinite;">✨</text>
    <text x="178" y="88" font-size="16" style="transform-origin:186px 79px;animation:dm-sparkle .6s ease-in-out 1.1s both,dm-sparkle 2.2s ease-in-out 2.2s infinite;">⭐</text>
    <text x="70" y="144" font-size="15" style="transform-origin:78px 136px;animation:dm-sparkle .5s ease-in-out 1.3s both,dm-sparkle 2.5s ease-in-out 2.4s infinite;">✨</text>
    <text x="176" y="146" font-size="14" style="transform-origin:183px 138px;animation:dm-sparkle .5s ease-in-out 1.05s both,dm-sparkle 2.3s ease-in-out 2.1s infinite;">🌟</text>
    <text x="40" y="116" font-size="16" style="animation:dm-sparkle .5s ease-in-out 1.45s both,dm-glow-pulse 2s ease-in-out 2s infinite;">💛</text>
    <text x="196" y="114" font-size="14" style="animation:dm-sparkle .5s ease-in-out 1.65s both,dm-glow-pulse 2.3s ease-in-out 2.3s infinite;">💫</text>
  </svg>
  <div class="dm-title">🍽️ できあがり！</div>
  <div class="dm-sub">クリップの選定が完了しました 🎉</div>
</div>
"""


# ══════════════════════════════════════════════════════════
# STEP 1 — URL 入力 & 解析
# ══════════════════════════════════════════════════════════
def step1():
    render_stepbar(1)

    # ── サービス紹介セクション ──────────────────────────────────
    # components.html を使い Markdown パーサーを完全バイパス（HTML コメントによる誤動作回避）
    import streamlit.components.v1 as _comp_s1
    _comp_s1.html("""
<style>
  body{margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;}
</style>
<div style="margin:4px 0 14px;">

  <div style="
    background:linear-gradient(145deg,#0f172a 0%,#1e1b4b 50%,#0f172a 100%);
    border-radius:22px;padding:28px 22px 24px;text-align:center;
    margin-bottom:14px;position:relative;overflow:hidden;">

    <div style="position:absolute;top:-30px;left:50%;transform:translateX(-50%);
                width:240px;height:160px;
                background:radial-gradient(ellipse,rgba(249,115,22,.25) 0%,transparent 70%);
                pointer-events:none;"></div>

    <div style="position:relative;display:inline-flex;align-items:center;gap:5px;
                background:rgba(249,115,22,.12);border:1px solid rgba(249,115,22,.25);
                border-radius:100px;padding:4px 12px;margin-bottom:16px;">
      <span style="width:5px;height:5px;border-radius:50%;background:#f97316;
                   box-shadow:0 0 6px #f97316;display:inline-block;"></span>
      <span style="font-size:10.5px;font-weight:800;color:#fb923c;letter-spacing:.1em;">AI POWERED</span>
    </div>

    <div style="position:relative;font-size:22px;font-weight:900;color:#f8fafc;
                line-height:1.35;letter-spacing:-.02em;margin-bottom:12px;">
      YouTube動画の<br>
      <span style="background:linear-gradient(90deg,#f97316 0%,#ef4444 100%);
                   -webkit-background-clip:text;-webkit-text-fill-color:transparent;
                   background-clip:text;">おいしい瞬間</span>を<br>
      ショートに自動で切り抜き
    </div>

    <div style="position:relative;font-size:12.5px;color:#94a3b8;font-weight:500;line-height:1.7;">
      URLを貼るだけ。<br>AIが解析・編集・投稿まで全自動でやります。
    </div>

  </div>

  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;">

    <!-- ① AI文字起こし＆自動選定 -->
    <div style="background:#fff;border-radius:16px;padding:14px 8px 14px;
                text-align:center;border:1px solid #f1f5f9;
                box-shadow:0 2px 12px rgba(15,23,42,.07);">
      <svg viewBox="0 0 54 70" fill="none" style="width:100%;max-width:60px;height:auto;display:block;margin:0 auto 10px;">
        <defs>
          <clipPath id="ai-clip"><rect width="54" height="70" rx="11"/></clipPath>
          <linearGradient id="ai-grad" x1="0" y1="0" x2="54" y2="27" gradientUnits="userSpaceOnUse">
            <stop offset="0%" stop-color="#fb923c"/><stop offset="100%" stop-color="#ea580c"/>
          </linearGradient>
        </defs>
        <g clip-path="url(#ai-clip)">
          <rect width="54" height="27" fill="url(#ai-grad)"/>
          <rect y="27" width="54" height="28" fill="#111827"/>
          <rect y="55" width="54" height="15" fill="#fff7ed"/>
        </g>
        <rect width="54" height="70" rx="11" fill="none" stroke="#fed7aa" stroke-width="1.5"/>
        <!-- waveform bars -->
        <rect x="9"  y="10" width="3" height="12" rx="1.5" fill="rgba(255,255,255,.35)"/>
        <rect x="14" y="7"  width="3" height="18" rx="1.5" fill="rgba(255,255,255,.55)"/>
        <rect x="19" y="4"  width="3" height="24" rx="1.5" fill="rgba(255,255,255,.8)"/>
        <rect x="24" y="2"  width="3" height="26" rx="1.5" fill="white"/>
        <rect x="29" y="5"  width="3" height="20" rx="1.5" fill="rgba(255,255,255,.65)"/>
        <rect x="34" y="8"  width="3" height="14" rx="1.5" fill="rgba(255,255,255,.45)"/>
        <rect x="39" y="12" width="3" height="8"  rx="1.5" fill="rgba(255,255,255,.3)"/>
        <!-- highlight around peak -->
        <rect x="22" y="1" width="9" height="26" rx="3" fill="rgba(255,255,255,.15)" stroke="rgba(255,255,255,.65)" stroke-width="1"/>
        <!-- transcript lines -->
        <rect x="8" y="33"   width="38" height="2.5" rx="1.25" fill="rgba(255,255,255,.18)"/>
        <rect x="8" y="38.5" width="30" height="2.5" rx="1.25" fill="rgba(255,255,255,.18)"/>
        <!-- highlighted selected line -->
        <rect x="8" y="44" width="24" height="2.5" rx="1.25" fill="#fb923c" opacity=".9"/>
        <!-- star badge -->
        <circle cx="45" cy="44" r="5" fill="#f97316"/>
        <text x="42.3" y="46.8" font-family="system-ui,sans-serif" font-size="6.5" fill="white">★</text>
        <!-- score dots -->
        <circle cx="15" cy="62" r="2.5" fill="#fed7aa"/>
        <circle cx="22" cy="62" r="2.5" fill="#fdba74"/>
        <circle cx="29" cy="62" r="3.5" fill="#f97316"/>
        <circle cx="36" cy="62" r="2.5" fill="#fdba74"/>
        <circle cx="43" cy="62" r="2.5" fill="#fed7aa"/>
      </svg>
      <div style="font-size:11.5px;font-weight:800;color:#1e293b;line-height:1.4;margin-bottom:5px;">
        AI文字起こし<br>&amp;自動選定
      </div>
      <div style="font-size:10.5px;color:#94a3b8;line-height:1.5;">
        スコアで「バズる瞬間」を自動抽出
      </div>
    </div>

    <!-- ② デザインカスタマイズ -->
    <div style="background:#fff;border-radius:16px;padding:14px 8px 14px;
                text-align:center;border:1px solid #f1f5f9;
                box-shadow:0 2px 12px rgba(15,23,42,.07);">
      <svg viewBox="0 0 54 70" fill="none" style="width:100%;max-width:60px;height:auto;display:block;margin:0 auto 10px;">
        <defs>
          <clipPath id="ds-clip"><rect width="54" height="70" rx="11"/></clipPath>
          <linearGradient id="ds-grad" x1="0" y1="0" x2="0" y2="27" gradientUnits="userSpaceOnUse">
            <stop offset="0%" stop-color="#8b5cf6"/><stop offset="100%" stop-color="#5b21b6"/>
          </linearGradient>
        </defs>
        <g clip-path="url(#ds-clip)">
          <rect width="54" height="27" fill="url(#ds-grad)"/>
          <rect y="27" width="54" height="28" fill="#111827"/>
          <rect y="55" width="54" height="15" fill="#f5f3ff"/>
        </g>
        <rect width="54" height="70" rx="11" fill="none" stroke="#ddd6fe" stroke-width="1.5"/>
        <!-- TITLE label block -->
        <rect x="10" y="8" width="34" height="8" rx="4" fill="rgba(255,255,255,.9)"/>
        <!-- TEXT label smaller -->
        <rect x="14" y="18" width="26" height="5" rx="2.5" fill="rgba(255,255,255,.5)"/>
        <!-- play button circle -->
        <circle cx="27" cy="41" r="9.5" fill="rgba(255,255,255,.08)" stroke="rgba(255,255,255,.18)" stroke-width="1"/>
        <polygon points="24,37 24,45 33.5,41" fill="rgba(255,255,255,.8)"/>
        <!-- image thumbnail in bottom -->
        <rect x="17" y="57.5" width="20" height="11" rx="3.5" fill="#c4b5fd" opacity=".65"/>
        <!-- landscape in thumbnail -->
        <path d="M17 68.5 L21.5 62 L26 65.5 L29.5 61 L37 68.5 Z" clip-path="url(#ds-clip)" fill="#7c3aed" opacity=".45"/>
        <circle cx="21.5" cy="60.5" r="2" fill="#fbbf24" opacity=".85"/>
      </svg>
      <div style="font-size:11.5px;font-weight:800;color:#1e293b;line-height:1.4;margin-bottom:5px;">
        デザイン<br>カスタマイズ
      </div>
      <div style="font-size:10.5px;color:#94a3b8;line-height:1.5;">
        フォント・色・テロップを自由編集
      </div>
    </div>

    <!-- ③ YouTube予約投稿 -->
    <div style="background:#fff;border-radius:16px;padding:14px 8px 14px;
                text-align:center;border:1px solid #f1f5f9;
                box-shadow:0 2px 12px rgba(15,23,42,.07);">
      <svg viewBox="0 0 54 70" fill="none" style="width:100%;max-width:60px;height:auto;display:block;margin:0 auto 10px;">
        <defs>
          <clipPath id="sc-clip"><rect width="54" height="70" rx="11"/></clipPath>
          <linearGradient id="sc-grad" x1="0" y1="0" x2="0" y2="27" gradientUnits="userSpaceOnUse">
            <stop offset="0%" stop-color="#f43f5e"/><stop offset="100%" stop-color="#be123c"/>
          </linearGradient>
        </defs>
        <g clip-path="url(#sc-clip)">
          <rect width="54" height="27" fill="url(#sc-grad)"/>
          <rect y="27" width="54" height="28" fill="#111827"/>
          <rect y="55" width="54" height="15" fill="#f0fdf4"/>
        </g>
        <rect width="54" height="70" rx="11" fill="none" stroke="#bbf7d0" stroke-width="1.5"/>
        <!-- calendar date circle -->
        <circle cx="19" cy="15" r="8" fill="rgba(255,255,255,.9)"/>
        <text x="15.5" y="19.5" font-family="system-ui,sans-serif" font-size="8" fill="#be123c" font-weight="800">25</text>
        <!-- day labels -->
        <rect x="30" y="9"  width="16" height="2.5" rx="1.25" fill="rgba(255,255,255,.55)"/>
        <rect x="30" y="14" width="12" height="2.5" rx="1.25" fill="rgba(255,255,255,.4)"/>
        <rect x="30" y="19" width="14" height="2.5" rx="1.25" fill="rgba(255,255,255,.4)"/>
        <!-- video thumbnails in dark section -->
        <rect x="6"  y="30" width="18" height="13" rx="2.5" fill="#1f2937"/>
        <rect x="6"  y="30" width="18" height="13" rx="2.5" fill="#ef4444" opacity=".12"/>
        <polygon points="11,35 11,40.5 17.5,37.5" fill="rgba(255,255,255,.75)"/>
        <rect x="28" y="30" width="18" height="13" rx="2.5" fill="#1f2937"/>
        <polygon points="33,35 33,40.5 39.5,37.5" fill="rgba(255,255,255,.35)"/>
        <!-- clock badge -->
        <circle cx="43" cy="49" r="6.5" fill="#16a34a"/>
        <circle cx="43" cy="49" r="4.5"  fill="white"/>
        <line x1="43" y1="46" x2="43"  y2="49"  stroke="#16a34a" stroke-width="1.4" stroke-linecap="round"/>
        <line x1="43" y1="49" x2="45.5" y2="51"  stroke="#16a34a" stroke-width="1.4" stroke-linecap="round"/>
        <!-- check marks in bottom -->
        <circle cx="16" cy="62" r="5" fill="#dcfce7"/>
        <path d="M13 62 L15.5 64.5 L19.5 59.5" stroke="#16a34a" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
        <circle cx="29" cy="62" r="5" fill="#dcfce7"/>
        <path d="M26 62 L28.5 64.5 L32.5 59.5" stroke="#16a34a" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
        <circle cx="42" cy="62" r="5" fill="#bbf7d0"/>
        <path d="M39 62 L41.5 64.5 L45.5 59.5" stroke="#16a34a" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" fill="none" opacity=".5"/>
      </svg>
      <div style="font-size:11.5px;font-weight:800;color:#1e293b;line-height:1.4;margin-bottom:5px;">
        YouTube<br>予約投稿
      </div>
      <div style="font-size:10.5px;color:#94a3b8;line-height:1.5;">
        複数本を日時指定で一括スケジュール
      </div>
    </div>

  </div>

</div>
""", height=440, scrolling=False)

    # ── 共通設定（タブの外に置く）──────────────────────────
    # プランによる本数上限を取得
    _s1_max_clips = 50
    _s1_remaining = None
    _s1_is_test   = False
    if _is_multi_user_mode() and s.get("user_id"):
        try:
            from core.usage_tracker import get_plan_info
            _pi_s1 = get_plan_info(s["user_id"])
            _s1_is_test   = _pi_s1.get("is_test", False)
            _s1_remaining = _pi_s1["remaining"] if not _s1_is_test else None
            _s1_max_clips = 50 if _s1_is_test else max(1, _pi_s1["remaining"])
            # 残り0本 → アップグレード画面を表示してブロック
            if not _s1_is_test and _s1_remaining == 0:
                _show_upgrade_ui(s["user_id"])
                st.stop()
        except Exception:
            pass

    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        clip_sec = st.slider("クリップの長さ（秒）", 30, 120, 60, key="clip_sec_s1")
        st.markdown(
            '<p style="font-size:11px;color:#94a3b8;margin:-6px 0 0;line-height:1.5;">'
            '💡 <strong style="color:#6366f1;">30〜120秒</strong>が最も効果的。'
            '30秒台→最後まで見てもらいやすく再生完了率↑、'
            '60〜120秒→内容が深く視聴維持率・保存率↑'
            '</p>',
            unsafe_allow_html=True,
        )
    with col2:
        _s1_default = min(10, _s1_max_clips)
        n_clips = st.number_input("本数", 1, _s1_max_clips, _s1_default, key="n_clips_s1")
        if _s1_is_test:
            st.caption("🔧 無制限")
        elif _s1_remaining is not None:
            _cap_color = "color:#ef4444;" if _s1_remaining <= 3 else ("color:#f59e0b;" if _s1_remaining <= 10 else "")
            st.markdown(
                f'<p style="font-size:12px;{_cap_color}margin:2px 0;">あと <b>{_s1_remaining} 本</b></p>',
                unsafe_allow_html=True,
            )
    with col3:
        st.markdown("")

    st.markdown("")

    # ── 入力方法タブ ────────────────────────────────────────
    _tab_file, _tab_yt = st.tabs(["📁 ファイルアップロード（推奨）", "🔗 YouTube URL（不安定）"])

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # タブ①: YouTube URL
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    with _tab_yt:
        # ── 成功/失敗条件の案内 ──
        import streamlit.components.v1 as _cv1_yt_info
        _cv1_yt_info.html("""
<style>
  body{margin:0;padding:0;font-family:-apple-system,'Hiragino Sans',sans-serif;}
  .yt-info{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:4px;}
  .yt-box{border-radius:12px;padding:12px 14px;}
  .yt-ok{background:#f0fdf4;border:1.5px solid #86efac;}
  .yt-ng{background:#fff7ed;border:1.5px solid #fdba74;}
  .yt-box-title{font-size:12px;font-weight:800;margin-bottom:8px;}
  .yt-ok .yt-box-title{color:#15803d;}
  .yt-ng .yt-box-title{color:#c2410c;}
  .yt-item{font-size:11.5px;color:#374151;line-height:1.5;margin-bottom:4px;display:flex;align-items:center;gap:5px;}
  .yt-warn{
    margin-top:8px;background:#fffbeb;border:1.5px solid #fcd34d;
    border-radius:10px;padding:9px 14px;
    font-size:11.5px;color:#92400e;line-height:1.6;
  }
</style>
<div class="yt-info">
  <div class="yt-box yt-ok">
    <div class="yt-box-title">✅ 成功しやすい条件</div>
    <div class="yt-item">▸ 一般公開されている動画</div>
    <div class="yt-item">▸ 30分以内の短い動画</div>
    <div class="yt-item">▸ 通信環境が安定している</div>
    <div class="yt-item">▸ 日本語・英語の字幕あり</div>
  </div>
  <div class="yt-box yt-ng">
    <div class="yt-box-title">⚠️ 失敗しやすい条件</div>
    <div class="yt-item">▸ 年齢制限・メンバー限定動画</div>
    <div class="yt-item">▸ 非公開・プレミアム限定動画</div>
    <div class="yt-item">▸ 1時間を超える長い動画</div>
    <div class="yt-item">▸ 地域制限のある動画</div>
  </div>
</div>
<div class="yt-warn">
  💡 失敗した場合は「ファイルアップロード（推奨）」タブから動画ファイルを直接アップロードしてください。
</div>
""", height=250)

        _last_url = (s.video_info or {}).get("url", "")
        url = st.text_input(
            "YouTube URL",
            value=_last_url,
            placeholder="https://www.youtube.com/watch?v=xxxxxxxx",
            label_visibility="collapsed",
        )

        st.markdown("")
        _analyze_error = None
        if st.button("🔍 解析開始", type="primary", use_container_width=True,
                     disabled=not url.strip(), key="analyze_yt_btn"):
            with st.status("動画を解析中...", expanded=True) as status:
                try:
                    from core.analyzer import get_video_info, get_transcript, auto_select_clips, get_transcript_debug

                    # ステージ①: 動画情報取得
                    _anim1 = st.empty()
                    _show_stage_html(_anim1, _make_analysis_stage_html(
                        "動画のおいしいところ分析中", "YouTube から動画のメタ情報を取得しています"
                    ))
                    info = get_video_info(url.strip())
                    _anim1.empty()
                    s.video_info = info
                    s["_file_upload_mode"] = False
                    st.write(f"✅ 動画タイトル: **{info['title'][:60]}**")
                    st.write(f"⏱ 尺: {int(info['duration']//60)}分{int(info['duration']%60)}秒")

                    # ステージ②: 字幕取得
                    _anim2 = st.empty()
                    _show_stage_html(_anim2, _make_analysis_stage_html(
                        "動画のおいしいところ分析中", "複数のクライアントで自動字幕を取得しています"
                    ))
                    tmp = OUTPUT_DIR / "transcript"
                    tmp.mkdir(parents=True, exist_ok=True)
                    # 今回の動画と無関係な古いjson3を削除（別動画の字幕が混入しないよう）
                    current_id = info.get("id", "")
                    for old_f in tmp.glob("*.json3"):
                        if current_id and not old_f.name.startswith(current_id):
                            old_f.unlink(missing_ok=True)
                    transcript = get_transcript(url.strip(), tmp)
                    _anim2.empty()
                    if transcript:
                        st.write(f"✅ 字幕取得完了（{len(transcript)} セグメント）")
                    else:
                        st.write("⚠️ 字幕を取得できませんでした → 概要欄テキストで代替します")
                        _dbg = get_transcript_debug()
                        if _dbg:
                            st.session_state["transcript_debug"] = _dbg  # Step2でも見えるよう保存
                            with st.expander("🔍 字幕取得ログ（デバッグ用）"):
                                for _d in _dbg:
                                    st.code(_d)

                    # ── AI選定ローディングアニメーション（クッキング演出）──
                    _seg_count = len(transcript) if transcript else 0
                    _nc = int(n_clips)
                    # A: フィルムコマ（色違いのシーンブロック）
                    _scene_colors = [
                        "#1e3a5f","#1a3a1a","#3a1a1a","#2d1f00","#1a1a3a",
                        "#2a1a2a","#1e3a2a","#3a2a1a","#1a2a3a","#2a3a1a",
                        "#3a1a2a","#1a3a3a","#2a2a1a","#1f1a3a","#3a2a2a",
                    ]
                    _scenes = "".join(
                        f'<div class="scene" style="background:{c}"></div>'
                        for c in _scene_colors
                    )
                    # C: 鍋バブル（大きさ・位置・タイミングをばらす）
                    _bubbles = "".join(
                        f'<div class="bub" style="'
                        f'width:{4+i%3}px;height:{4+i%3}px;'
                        f'left:{15+i*14}%;'
                        f'animation-delay:{round(i*0.28,2)}s"></div>'
                        for i in range(6)
                    )
                    # クリップチップ（最大10個表示、超過分は +N本 バッジ）
                    _show = min(_nc, 10)
                    _citems = "".join(
                        f'<div class="ci" style="animation-delay:{round(0.6+j*0.2,2)}s">'
                        f'✂ {j}本目</div>'
                        for j in range(1, _show + 1)
                    )
                    if _nc > 10:
                        _citems += (
                            f'<div class="ci" style="animation-delay:{round(0.6+_show*0.2,2)}s;'
                            f'background:rgba(251,191,36,.25);border-color:rgba(251,191,36,.7);">'
                            f'+ {_nc - 10}本</div>'
                        )
                    import streamlit.components.v1 as _cai
                    _cai.html(f"""
    <style>
    *{{box-sizing:border-box;margin:0;padding:0;
       font-family:-apple-system,'Hiragino Sans',sans-serif;}}
    body{{background:transparent;overflow:hidden;}}
    
    /* ── カード ── */
    .card{{
      background:linear-gradient(135deg,#1c0a00 0%,#2d1500 50%,#1c0a00 100%);
      border-radius:14px;padding:16px 16px 14px;
      border:1px solid rgba(251,191,36,.28);
    }}
    
    /* ── ヘッダー ── */
    .hd{{display:flex;align-items:center;gap:8px;margin-bottom:13px;}}
    .badge{{
      background:linear-gradient(135deg,#f97316,#dc2626);
      border-radius:5px;padding:3px 8px;font-size:10px;
      font-weight:800;color:#fff;letter-spacing:.05em;flex-shrink:0;
    }}
    .htitle{{font-size:13px;font-weight:700;color:#fef3c7;}}
    .dots span{{animation:blink 1.2s infinite;color:#f97316;}}
    .dots span:nth-child(2){{animation-delay:.25s;}}
    .dots span:nth-child(3){{animation-delay:.5s;}}
    @keyframes blink{{0%,100%{{opacity:.1;}}50%{{opacity:1;}}}}
    
    /* ══ A: フィルムリール ══ */
    .film-wrap{{position:relative;margin-bottom:11px;}}
    .film{{
      height:34px;border-radius:6px;overflow:hidden;
      background:#0c0c18;border:1px solid rgba(255,255,255,.08);
      position:relative;display:flex;align-items:center;
    }}
    /* スプロケット穴（上下） */
    .film::before,.film::after{{
      content:'';position:absolute;left:0;right:0;height:6px;
      background:repeating-linear-gradient(
        90deg,transparent 0,transparent 16px,
        rgba(255,255,255,.13) 16px,rgba(255,255,255,.13) 24px);
      z-index:2;
    }}
    .film::before{{top:2px;}}
    .film::after{{bottom:2px;}}
    /* シーンブロック */
    .scenes{{
      display:flex;align-items:center;gap:1px;
      padding:8px 2px;z-index:1;width:100%;height:100%;
    }}
    .scene{{flex:1;height:18px;border-radius:1px;opacity:.7;}}
    /* ✂️ ハサミ */
    .scissors{{
      position:absolute;top:50%;left:-32px;
      transform:translateY(-50%) rotate(90deg);
      font-size:20px;z-index:6;
      animation:snip 2.6s linear infinite;
      filter:drop-shadow(0 0 7px rgba(249,115,22,.9));
    }}
    @keyframes snip{{0%{{left:-32px;}}100%{{left:108%;}}}}
    /* カット光跡 */
    .cutline{{
      position:absolute;top:0;left:-32px;
      width:2px;height:100%;z-index:5;
      background:linear-gradient(180deg,transparent,#f97316 40%,#fbbf24 60%,transparent);
      animation:snip 2.6s linear infinite;opacity:.8;
    }}
    /* スパーク */
    .spark{{
      position:absolute;border-radius:50%;
      background:#fbbf24;z-index:7;
      animation:spark 2.6s linear infinite;
      pointer-events:none;
    }}
    @keyframes spark{{
      0%,60%{{opacity:0;transform:translate(0,0) scale(0);}}
      65%{{opacity:1;transform:translate(var(--dx),var(--dy)) scale(1);}}
      80%{{opacity:.5;}}
      100%,61%{{opacity:0;transform:translate(calc(var(--dx)*1.8),calc(var(--dy)*1.8)) scale(.2);}}
    }}
    
    /* ══ B+C 横並び ══ */
    .cook-row{{display:flex;gap:8px;margin-bottom:11px;}}
    
    /* ── B: フライパン ── */
    .pan-box{{
      flex:1;background:rgba(249,115,22,.08);
      border:1px solid rgba(249,115,22,.25);
      border-radius:10px;padding:8px 8px 6px;
      text-align:center;position:relative;overflow:hidden;
    }}
    .box-lbl{{font-size:9px;font-weight:800;letter-spacing:.05em;margin-bottom:3px;}}
    .pan-lbl{{color:#c2410c;}}
    .pan-emoji{{font-size:26px;display:block;line-height:1;
      animation:sizzle .35s ease-in-out infinite alternate;
      transform-origin:center bottom;}}
    @keyframes sizzle{{
      0%{{transform:rotate(-3deg) scale(1);}}
      100%{{transform:rotate(3deg) scale(1.07);}}
    }}
    /* 油はね */
    .drop{{
      position:absolute;border-radius:50%;
      background:rgba(251,191,36,.65);
      animation:dropsplash .7s ease-out infinite;
    }}
    @keyframes dropsplash{{
      0%{{transform:scale(0);opacity:.8;}}
      100%{{transform:scale(2.2);opacity:0;}}
    }}
    /* 炎 */
    .flame{{
      position:absolute;bottom:4px;right:8px;
      font-size:13px;
      animation:flicker .4s ease-in-out infinite alternate;
    }}
    @keyframes flicker{{0%{{transform:scale(1) rotate(-4deg);}}100%{{transform:scale(1.15) rotate(3deg);}}}}
    
    /* ── C: 鍋 ── */
    .pot-box{{
      flex:1;background:rgba(99,102,241,.08);
      border:1px solid rgba(99,102,241,.25);
      border-radius:10px;padding:8px 8px 6px;
      text-align:center;position:relative;overflow:hidden;
    }}
    .pot-lbl{{color:#4f46e5;}}
    .pot-emoji{{font-size:26px;display:block;line-height:1;}}
    /* バブル */
    .bub{{
      position:absolute;border-radius:50%;
      background:rgba(99,102,241,.55);
      animation:bubup 1.4s ease-in infinite;bottom:8px;
    }}
    @keyframes bubup{{
      0%{{transform:translateY(0) scale(1);opacity:.7;}}
      100%{{transform:translateY(-32px) scale(.2);opacity:0;}}
    }}
    /* 湯気 */
    .steam{{
      position:absolute;width:3px;height:3px;border-radius:50%;
      background:rgba(255,255,255,.35);
      animation:steamup 1.3s ease-out infinite;
    }}
    @keyframes steamup{{
      0%{{opacity:.6;transform:translateY(0) scale(1);}}
      100%{{opacity:0;transform:translateY(-22px) scale(.2);}}
    }}
    
    /* ══ クリップチップ ══ */
    .chips{{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:10px;}}
    .ci{{
      background:rgba(251,191,36,.12);
      border:1px solid rgba(251,191,36,.4);
      border-radius:5px;padding:3px 8px;
      font-size:11px;color:#fde68a;
      opacity:0;animation:ci-in .4s ease forwards;
    }}
    @keyframes ci-in{{
      from{{opacity:0;transform:translateY(5px);}}
      to{{opacity:1;transform:translateY(0);}}
    }}
    
    /* ══ ステータス ══ */
    .st{{display:flex;align-items:center;gap:7px;}}
    .sp{{
      width:12px;height:12px;flex-shrink:0;border-radius:50%;
      border:2px solid rgba(249,115,22,.3);border-top-color:#f97316;
      animation:spin .75s linear infinite;
    }}
    @keyframes spin{{to{{transform:rotate(360deg);}}}}
    .stxt{{font-size:11px;color:#d97706;}}
    </style>
    
    <div class="card">
      <!-- ヘッダー -->
      <div class="hd">
        <div class="badge">✂️ AI</div>
        <div class="htitle">おいしい瞬間を仕込み中
          <span class="dots"><span>.</span><span>.</span><span>.</span></span>
        </div>
      </div>
    
      <!-- A: フィルムリールをハサミでカット -->
      <div class="film-wrap">
        <div class="film">
          <div class="scenes">{_scenes}</div>
        </div>
        <div class="scissors">✂️</div>
        <div class="cutline"></div>
        <div class="spark" style="width:4px;height:4px;top:30%;--dx:-6px;--dy:-8px;animation-delay:.1s"></div>
        <div class="spark" style="width:3px;height:3px;top:60%;--dx:7px;--dy:-6px;animation-delay:.15s"></div>
        <div class="spark" style="width:5px;height:5px;top:45%;--dx:-8px;--dy:5px;animation-delay:.08s"></div>
        <div class="spark" style="width:3px;height:3px;top:25%;--dx:5px;--dy:7px;animation-delay:.2s"></div>
      </div>
    
      <!-- B+C: フライパン & 鍋 -->
      <div class="cook-row">
        <!-- B: フライパンで炒める -->
        <div class="pan-box">
          <div class="box-lbl pan-lbl">🔥 おいしいシーンを炒める</div>
          <span class="pan-emoji">🍳</span>
          <div class="drop" style="width:5px;height:5px;bottom:12px;left:28%;animation-delay:0s"></div>
          <div class="drop" style="width:4px;height:4px;bottom:12px;left:52%;animation-delay:.25s"></div>
          <div class="drop" style="width:6px;height:6px;bottom:12px;left:70%;animation-delay:.5s"></div>
          <div class="flame">🔥</div>
        </div>
        <!-- C: 鍋でエキスを抽出 -->
        <div class="pot-box">
          <div class="box-lbl pot-lbl">♨️ おいしさを抽出中</div>
          <span class="pot-emoji">🫕</span>
          {_bubbles}
          <div class="steam" style="left:35%;bottom:34px;animation-delay:.1s"></div>
          <div class="steam" style="left:55%;bottom:34px;animation-delay:.55s"></div>
          <div class="steam" style="left:70%;bottom:34px;animation-delay:.9s"></div>
        </div>
      </div>
    
      <!-- クリップチップ -->
      <div class="chips">{_citems}</div>
    
      <!-- ステータス -->
      <div class="st">
        <div class="sp"></div>
        <div class="stxt">{_seg_count} セグメントの字幕から {_nc} 本のおいしい瞬間を仕込み中...</div>
      </div>
    </div>
    """, height=310, scrolling=False)
    
                    clips = auto_select_clips(
                        info["duration"], transcript,
                        n_clips=int(n_clips), clip_sec=clip_sec,
                        video_title=info.get("title", ""),
                        description=info.get("description", ""),
                    )
                    s.clips = clips
                    st.write(f"✅ {len(clips)} 本のクリップを選定しました")
    
                    # Claude API のステータスを session_state に保存（Step2で表示）
                    try:
                        from core.ai_writer import get_ai_status
                        st.session_state["ai_status"] = get_ai_status()
                    except Exception:
                        pass
    
                    _save_session(info, clips)
                    status.update(label="解析完了！", state="complete")
                    _done_ph = st.empty()
                    _show_stage_html(_done_ph, _make_complete_html(), height=330)
                    import time as _tc; _tc.sleep(2.2)
                    _done_ph.empty()
                    s.step = 2
                    st.rerun()
    
                except Exception as e:
                    status.update(label="エラーが発生しました", state="error")
                    _analyze_error = e

        # st.status が折りたたまれてもエラーが見えるよう外に出す
        if _analyze_error is not None:
            _emsg = str(_analyze_error)
            if "n-challenge" in _emsg or "EJS" in _emsg or "javascript runtime" in _emsg.lower():
                _friendly = "🤖 YouTubeの認証チェック（n-challenge）に対応できませんでした。\nサーバー環境に Node.js が必要なため、このサービスでは対応が難しい動画です。"
            elif "sign in" in _emsg.lower() or "cookies" in _emsg.lower() and "期限切れ" in _emsg:
                _friendly = "🍪 YouTubeへのログイン認証が必要な動画、または認証の有効期限が切れています。"
            elif "SABR" in _emsg:
                _friendly = "📡 YouTubeの新しい配信方式（SABR）に対応できませんでした。認証なしでは取得が難しい動画です。"
            elif "403" in _emsg:
                _friendly = "🚫 YouTubeにアクセスを拒否されました（403エラー）。しばらく時間をおいてお試しください。"
            elif "タイムアウト" in _emsg or "timeout" in _emsg.lower() or "2時間" in _emsg:
                _friendly = "⏱ ダウンロードに時間がかかりすぎて自動的に中断しました。通信環境が不安定か、動画が非常に長い可能性があります。"
            elif "private" in _emsg.lower():
                _friendly = "🔒 非公開動画はダウンロードできません。"
            elif "age" in _emsg.lower() or "年齢" in _emsg:
                _friendly = "🔞 年齢制限のある動画はダウンロードできません。"
            elif "not a playlist" in _emsg.lower() or "playlist" in _emsg.lower():
                _friendly = "📋 プレイリストURLは対応していません。個別の動画URLを入力してください。"
            elif "urlが" in _emsg.lower() or "invalid url" in _emsg.lower() or "unsupported url" in _emsg.lower():
                _friendly = "🔗 URLの形式が正しくありません。YouTubeの動画URLを確認してください。"
            else:
                _friendly = f"❌ ダウンロードに失敗しました。\n\n（詳細: {_emsg[:300]}）"
            st.error(f"{_friendly}\n\n💡 **「ファイルアップロード（推奨）」タブ**から動画ファイルを直接アップロードしてください。")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # タブ②: ファイルアップロード
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    with _tab_file:
        # ── 推奨元動画の条件 ──
        import streamlit.components.v1 as _cv1_file_info
        _cv1_file_info.html("""
<style>
  body{margin:0;padding:0;font-family:-apple-system,'Hiragino Sans',sans-serif;}
  .fi-wrap{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:6px;}
  .fi-box{border-radius:12px;padding:12px 14px;}
  .fi-ok{background:#f0fdf4;border:1.5px solid #86efac;}
  .fi-ng{background:#fff7ed;border:1.5px solid #fdba74;}
  .fi-title{font-size:12px;font-weight:800;margin-bottom:8px;}
  .fi-ok .fi-title{color:#15803d;}
  .fi-ng .fi-title{color:#c2410c;}
  .fi-item{font-size:11.5px;color:#374151;line-height:1.5;margin-bottom:4px;display:flex;align-items:flex-start;gap:5px;}
  .fi-tip{margin-top:8px;background:#eff6ff;border:1.5px solid #93c5fd;border-radius:10px;padding:9px 14px;font-size:11.5px;color:#1e40af;line-height:1.6;}
</style>
<div class="fi-wrap">
  <div class="fi-box fi-ok">
    <div class="fi-title">✅ ショートに向いている動画</div>
    <div class="fi-item">▸ トーク・解説・Vlog系</div>
    <div class="fi-item">▸ 5〜60分程度の長さ</div>
    <div class="fi-item">▸ 音声がはっきりしている</div>
    <div class="fi-item">▸ 日本語 / 英語の内容</div>
    <div class="fi-item">▸ 横型（16:9）動画</div>
  </div>
  <div class="fi-box fi-ng">
    <div class="fi-title">⚠️ 精度が下がりやすい動画</div>
    <div class="fi-item">▸ BGMや効果音が大きい</div>
    <div class="fi-item">▸ 無音・字幕なしの動画</div>
    <div class="fi-item">▸ 3分未満の短い動画</div>
    <div class="fi-item">▸ 複数人が同時に話す</div>
    <div class="fi-item">▸ 縦型・正方形動画</div>
  </div>
</div>
<div class="fi-tip">
  💡 AIはトランスクリプト（音声の文字起こし）をもとに「おいしいシーン」を選定します。しゃべりが多い動画ほど精度が上がります。
</div>
""", height=230)

        with st.expander("📖 使い方（Google Drive の場合）", expanded=False):
            st.markdown("""
**① Google Drive に動画をアップロード**

**② 共有設定を変更**
1. ファイルを右クリック →「共有」
2. 「リンクを知っている全員」に変更
3. 「リンクをコピー」

**③ コピーしたURLをそのまま貼り付けてください**

---
**対応URL形式**
- `https://drive.google.com/file/d/xxxx/view` — Google Drive
- `https://dl.dropboxusercontent.com/...` — Dropbox 直リンク
- `https://example.com/video.mp4` — mp4 直接URL
""")

        # 入力方法の選択
        _f_input_method = st.radio(
            "入力方法",
            ["🔗 URLを入力", "💻 ファイルをアップロード"],
            horizontal=True,
            key="s1_file_input_method",
            label_visibility="collapsed",
        )

        _f_video_url = ""
        _f_uploaded_file = None

        if _f_input_method == "🔗 URLを入力":
            _f_video_url = st.text_input(
                "動画ファイルのURL",
                placeholder="https://drive.google.com/file/d/xxxxxxxx/view?usp=sharing",
                key="s1_file_url",
                help="Google Drive の共有リンク、または動画ファイルへの直接URLに対応しています",
            )
        else:
            _f_uploaded_file = st.file_uploader(
                "動画ファイルを選択",
                type=["mp4", "mov", "avi", "mkv", "webm", "m4v"],
                key="s1_file_uploader",
                help="mp4, mov, avi, mkv, webm に対応しています",
            )

        _f_video_title = ""
        _f_description = ""

        st.markdown("")

        # 前回エラーを表示
        if s.get("_file_upload_error"):
            st.error(s["_file_upload_error"])
            if st.button("✕ エラーを閉じる", key="close_file_err"):
                s["_file_upload_error"] = None
                st.rerun()

        _f_btn_disabled = (
            (not _f_video_url.strip() and _f_input_method == "🔗 URLを入力") or
            (_f_uploaded_file is None and _f_input_method == "💻 ファイルをアップロード")
        )
        if st.button("📁 解析開始", type="primary", use_container_width=True,
                     key="analyze_file_btn", disabled=_f_btn_disabled):
            s["_file_upload_error"] = None  # 前回エラーをクリア
            if _f_input_method == "🔗 URLを入力" and not _f_video_url.strip():
                st.error("URLを入力してください")
            else:
                with st.status("ファイルを解析中...", expanded=True) as _fstatus:
                    try:
                        import subprocess as _sp
                        import json as _fjson
                        import re as _re

                        # ① URL または ローカルファイルから取得
                        _upload_dir = OUTPUT_DIR / "uploads"
                        _upload_dir.mkdir(parents=True, exist_ok=True)
                        _furl = _f_video_url.strip()

                        # フォルダリンクは非対応
                        if "drive.google.com/drive/folders" in _furl or "/folders/" in _furl:
                            raise RuntimeError(
                                "フォルダのリンクは使用できません。\n"
                                "フォルダ内の動画ファイルを右クリック → 共有 → 「リンクをコピー」で\n"
                                "ファイル単体のリンク（drive.google.com/file/d/...）を貼り付けてください。"
                            )

                        # ローカルファイルアップロードの場合
                        _fanim1 = st.empty()
                        if _f_input_method == "💻 ファイルをアップロード" and _f_uploaded_file is not None:
                            _show_stage_html(_fanim1, _make_analysis_stage_html(
                                "動画のおいしいところ分析中", _f_uploaded_file.name
                            ))
                            _fpath = _upload_dir / _f_uploaded_file.name
                            with open(_fpath, "wb") as _fp:
                                _fp.write(_f_uploaded_file.getbuffer())
                            if not _fpath.exists() or _fpath.stat().st_size == 0:
                                raise RuntimeError("ファイルの保存に失敗しました")
                            _fanim1.empty()
                            # 以降の処理用に _furl をダミーセット（ファイルパスで上書き）
                            _furl = ""
                            # ② 以降の処理はファイルパスを直接使うためスキップ
                        else:
                            # URL モード: Google Drive / 直接URL からダウンロード
                            _gdrive_match = _re.search(
                                r"drive\.google\.com/(?:file/d/|open\?id=)([\w-]+)", _furl
                            )
                            if _gdrive_match:
                                _file_id = _gdrive_match.group(1)
                                import gdown as _gdown
                                import threading as _gdt
                                import time as _gdt_time
                                _fpath = _upload_dir / f"{_file_id}.mp4"
                                _gd_res: list = [None, None]  # [path, error]
                                _gd_done = _gdt.Event()

                                def _gdown_worker(_fid=_file_id, _fp=_fpath, _res=_gd_res, _ev=_gd_done):
                                    try:
                                        _gdown.download(id=_fid, output=str(_fp), quiet=True, fuzzy=True)
                                        if not _fp.exists() or _fp.stat().st_size == 0:
                                            _res[1] = RuntimeError(
                                                "ご不便をおかけして申し訳ありません🙇\n\n"
                                                "Google Drive からダウンロードできませんでした。\n\n"
                                                "確認してほしいこと：\n"
                                                "① 共有設定が「リンクを知っている全員」になっているか\n"
                                                "② フォルダではなくファイル単体のリンクか\n"
                                                "   （drive.google.com/file/d/... の形式）\n"
                                                "③ ファイルサイズが 2GB 以内か\n\n"
                                                "設定変更後にもう一度お試しください。"
                                            )
                                        else:
                                            _res[0] = _fp
                                    except Exception as _ge:
                                        _res[1] = RuntimeError(
                                            f"ご不便をおかけして申し訳ありません🙇\n\n"
                                            f"Google Drive ダウンロード中にエラーが発生しました。\n"
                                            f"原因: {_ge}\n\n"
                                            f"共有設定を確認して、もう一度お試しください。"
                                        )
                                    finally:
                                        _ev.set()

                                _gdt.Thread(target=_gdown_worker, daemon=True).start()
                                _gd_t0 = _gdt_time.time()
                                _GD_TIMEOUT = 600  # 10分

                                while not _gd_done.wait(timeout=10.0):
                                    _el = _gdt_time.time() - _gd_t0
                                    if _el > _GD_TIMEOUT:
                                        raise RuntimeError(
                                            "ご不便をおかけして申し訳ありません🙇\n\n"
                                            "Google Drive からのダウンロードが 10 分を超えたため中断しました。\n\n"
                                            "考えられる原因：\n"
                                            "・ファイルが非常に大きい（2GB 以上）\n"
                                            "・インターネット接続が不安定\n\n"
                                            "ファイルを圧縮・分割するか、直接アップロードをお試しください。"
                                        )
                                    _show_stage_html(_fanim1, _make_analysis_stage_html(
                                        "動画のおいしいところ分析中",
                                        f"Google Drive からダウンロード中... {int(_el//60)}分{int(_el%60):02d}秒",
                                        note=_get_wait_note(_el),
                                    ))

                                if _gd_res[1]:
                                    raise _gd_res[1]
                            else:
                                import requests as _req
                                import time as _url_time
                                _fname = _furl.split("?")[0].split("/")[-1] or "video.mp4"
                                _fpath = _upload_dir / _fname
                                _show_stage_html(_fanim1, _make_analysis_stage_html(
                                    "動画のおいしいところ分析中",
                                    f"接続中... {_furl[:40]}...",
                                ))
                                try:
                                    with _req.get(_furl, stream=True, timeout=300) as _r:
                                        _r.raise_for_status()
                                        _total_bytes = int(_r.headers.get("Content-Length", 0))
                                        _dl_bytes = 0
                                        _url_t0 = _url_time.time()
                                        _last_ui = 0.0
                                        with open(_fpath, "wb") as _fp:
                                            for _chunk in _r.iter_content(chunk_size=65536):
                                                _fp.write(_chunk)
                                                _dl_bytes += len(_chunk)
                                                _now = _url_time.time()
                                                if _now - _last_ui >= 3.0:
                                                    _last_ui = _now
                                                    _el = _now - _url_t0
                                                    if _total_bytes > 0:
                                                        _pct = int(_dl_bytes / _total_bytes * 100)
                                                        _detail = f"ダウンロード中... {_pct}%（{_dl_bytes//1048576}MB / {_total_bytes//1048576}MB）"
                                                    else:
                                                        _detail = f"ダウンロード中... {_dl_bytes//1048576}MB 取得済み"
                                                    _show_stage_html(_fanim1, _make_analysis_stage_html(
                                                        "動画のおいしいところ分析中",
                                                        _detail,
                                                        note=_get_wait_note(_el),
                                                    ))
                                except _req.exceptions.Timeout:
                                    raise RuntimeError(
                                        "ご不便をおかけして申し訳ありません🙇\n\n"
                                        "URL からのダウンロードがタイムアウトしました（5分）。\n"
                                        "接続が不安定か、ファイルが大きすぎます。\n"
                                        "しばらく待ってからもう一度お試しください。"
                                    )
                                except _req.exceptions.HTTPError as _he:
                                    raise RuntimeError(
                                        f"ご不便をおかけして申し訳ありません🙇\n\n"
                                        f"URL へのアクセスが拒否されました（{_he}）。\n"
                                        f"URL が正しいか、ファイルが公開されているかを確認してください。"
                                    )
                            _fanim1.empty()
                        st.write(f"✅ ファイル準備完了: `{_fpath.name}`")
                        _fstem = _fpath.stem[:50]

                        # ② ffprobe で尺を取得
                        _probe = _sp.run(
                            ["ffprobe", "-v", "quiet", "-print_format", "json",
                             "-show_format", str(_fpath)],
                            capture_output=True, text=True,
                        )
                        _dur = 0.0
                        if _probe.returncode == 0:
                            _pdata = _fjson.loads(_probe.stdout)
                            _dur = float(_pdata.get("format", {}).get("duration", 0))
                        if _dur <= 0:
                            raise RuntimeError("動画の尺を取得できませんでした（ffprobe 失敗）")
                        st.write(f"⏱ 尺: {int(_dur//60)}分{int(_dur%60)}秒")

                        # ③ 文字起こし（ステージ専用 empty を作成）
                        _est_min = max(1, int(_dur / 60))
                        _fanim2 = st.empty()
                        _show_stage_html(_fanim2, _make_analysis_stage_html(
                            "動画のおいしいところ分析中",
                            f"動画 {int(_dur//60)}分{int(_dur%60)}秒 → 約 {_est_min}〜{_est_min*2} 分かかります"
                        ))
                        from core.transcriber import transcribe_file as _transcribe
                        _ftranscript = _transcribe(_fpath)
                        _fanim2.empty()
                        if _ftranscript:
                            st.write(f"✅ 文字起こし完了（{len(_ftranscript)} セグメント）")
                        else:
                            st.write("⚠️ 文字起こしを取得できませんでした → タイトル・説明文で代替します")

                        # ④ video_info を組み立て（タイトルは入力値優先、なければファイル名）
                        _title = _f_video_title.strip() or _fstem
                        _desc  = _f_description.strip()
                        _finfo = {
                            "url":         "",
                            "id":          _fstem[:11],
                            "title":       _title,
                            "duration":    _dur,
                            "thumbnail":   "",
                            "uploader":    "",
                            "view_count":  0,
                            "chapters":    [],
                            "description": _desc,
                        }
                        s.video_info          = _finfo
                        s["raw_path"]         = str(_fpath)
                        s["_file_upload_mode"] = True

                        # ⑤ クリップ自動選定（ステージ専用 empty を作成）
                        _fanim3 = st.empty()
                        _show_stage_html(_fanim3, _make_analysis_stage_html(
                            "動画のおいしいところ分析中",
                            f"文字起こし {len(_ftranscript)} セグメントから {int(n_clips)} 本を抽出"
                        ))
                        from core.analyzer import auto_select_clips as _asc
                        _fclips = _asc(
                            _dur, _ftranscript,
                            n_clips=int(n_clips), clip_sec=clip_sec,
                            video_title=_title,
                            description=_desc,
                        )
                        _fanim3.empty()
                        s.clips = _fclips
                        st.write(f"✅ {len(_fclips)} 本のクリップを選定しました")

                        _save_session(_finfo, _fclips)
                        _fstatus.update(label="解析完了！", state="complete")
                        _fdone_ph = st.empty()
                        _show_stage_html(_fdone_ph, _make_complete_html(), height=330)
                        import time as _ftc; _ftc.sleep(2.2)
                        _fdone_ph.empty()
                        s.step = 2
                        st.rerun()

                    except Exception as _fe:
                        _fstatus.update(label="エラーが発生しました", state="error")
                        s["_file_upload_error"] = f"❌ {_fe}"
                        st.rerun()


# ══════════════════════════════════════════════════════════
# STEP 2 — クリップ確認・編集
# ══════════════════════════════════════════════════════════

# ── 採点根拠ダイアログ（st.dialog: Streamlit 1.35+） ──────
@st.dialog("★ 採点根拠", width="small")
def _score_dialog(score: int, s_density: int, s_engage: int, s_complete: int):
    st.markdown(f"### 合計 **{score}** / 100点")
    c1, c2, c3 = st.columns(3)
    c1.metric("📝 文字密度",   f"{s_density} / 40")
    c2.metric("🔥 盛り上がり", f"{s_engage} / 40")
    c3.metric("✅ 完成度",     f"{s_complete} / 20")
    st.caption("📝 **文字密度** — 発話量（文字数/秒）")
    st.caption("🔥 **盛り上がり** — ？！・すごい・秘密 等のキーワード数")
    st.caption("✅ **完成度** — 字幕セグメントの充実度")
    if st.button("✕ 閉じる", use_container_width=True):
        st.rerun()


# ── プレビューカードレイアウト（Aバランス型：タイトル自動高さ）──────────
# タイトル 自動 / 動画 126px (16:9固定) / 底部 132px (1.7:1横長)
_PREVIEW_W = 224   # カード幅(px)
_VIDEO_H   = 126   # 動画エリア固定高さ (16:9 = 224×9/16)
_BOTTOM_H  = 132   # 底部エリア固定高さ (224:132 ≈ 1.7:1 横長)


def _render_clip_preview(clip: dict, idx: int, video_id: str):
    """
    9:16 ショートプレビューカード（縦型・テーマ対応版）
    ┌──────────────┐
    │ ⚡ キャッチ  │
    │ TITLE TEXT   │  ← テーマグラデーション
    ├──────────────┤
    │              │
    │   [video]    │  ← 9:16 縦型エリア（16:9動画がレターボックス）
    │              │
    └──────────────┘
    """
    import base64
    import streamlit.components.v1 as components

    # デザイン設定を session_state から取得（クリップごとランダムモード対応）
    _rand_mode = st.session_state.get("rand_mode", False)
    if _rand_mode:
        _designs = st.session_state.setdefault("clip_designs", {})
        if idx not in _designs:
            _designs[idx] = {
                "theme":   random.choice(list(TITLE_THEMES.keys())),
                "size":    "large",
                "pattern": random.choice(list(TITLE_PATTERNS.keys())),
            }
        _d = _designs[idx]
        theme_key   = _d["theme"]
        size_key    = _d["size"]
        pattern_key = _d["pattern"]
    else:
        theme_key   = st.session_state.get("title_theme",   "purple")
        size_key    = st.session_state.get("title_size",    "large")
        pattern_key = st.session_state.get("title_pattern", "none")
    theme = TITLE_THEMES.get(theme_key, TITLE_THEMES["purple"])
    size  = TITLE_SIZES.get(size_key,   TITLE_SIZES["large"])
    _pat_css = TITLE_PATTERNS.get(pattern_key, TITLE_PATTERNS["none"])["css"]
    # ::before CSS ブロックを文字列として構築（f-string に直接埋め込む）
    before_css_block = (
        ".title-bar::before{"
        "content:'';position:absolute;inset:0;"
        "background:" + _pat_css + ";"
        "pointer-events:none;"
        "}"
    )

    title       = clip.get("title", "")       or f"クリップ {clip['index']}"
    catchphrase = clip.get("catchphrase", "") or ""
    start_sec   = int(clip.get("start", 0))
    embed_url = (
        f"https://www.youtube.com/embed/{video_id}"
        f"?start={start_sec}&autoplay=0&rel=0&modestbranding=1&controls=1"
    )

    # キャッチコピー HTML
    catchphrase_html = ""
    if catchphrase:
        catchphrase_html = f"""
        <div class="catchphrase">{catchphrase}</div>
        """

    # 底部画像 HTML（未設定時プレースホルダー）
    bottom_img_html = (
        '<div style="width:100%;height:100%;background:#f1f5f9;'
        'display:flex;align-items:center;justify-content:center;'
        'flex-direction:column;gap:8px;color:#94a3b8;">'
        '<span style="font-size:32px;">📷</span>'
        '<span style="font-size:11px;font-weight:600;">底部画像を設定</span>'
        '<span style="font-size:10px;color:#cbd5e1;">ロゴ・顔写真など</span>'
        '</div>'
    )
    if clip.get("bottom_image"):
        p = Path(clip["bottom_image"])
        if p.exists():
            ext = p.suffix.lstrip(".")
            img_b64 = base64.b64encode(p.read_bytes()).decode()
            bottom_img_html = (
                f'<img src="data:image/{ext};base64,{img_b64}" '
                f'style="width:100%;height:100%;object-fit:cover;">'
            )

    # タイトル高さをテキスト量で推定（JS が実サイズに再調整）
    _pad_v  = {"large": 38, "xlarge": 44}.get(size_key, 38)   # 上下パディング合計
    _lh_px  = {"large": 27, "xlarge": 33}.get(size_key, 27)   # 1行の実高さ(px)
    _cpl    = {"large": 12, "xlarge": 10}.get(size_key, 12)   # 1行あたり文字数目安
    _cp_add = 28 if catchphrase else 0                         # キャッチコピー分
    _lines  = max(1, (len(title) + _cpl - 1) // _cpl)
    _title_h_est = _pad_v + _lines * _lh_px + _cp_add
    card_h = _title_h_est + _VIDEO_H + _BOTTOM_H

    # タイトル/キャッチコピー/底部画像が変わったら強制再レンダリング
    _render_key = hash((title, catchphrase, theme_key, size_key, clip.get("bottom_image", "")))

    card_html = f"""<!DOCTYPE html>
<html><head><style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ background:transparent; font-family:-apple-system,'Hiragino Sans',sans-serif; }}
  /* カード全体: タイトル自動高さ・flexbox 縦並び */
  .card {{
    width:{_PREVIEW_W}px;
    background:#fff; border-radius:16px; overflow:hidden;
    border:1px solid #cbd5e1; box-shadow:0 6px 28px rgba(0,0,0,0.16);
    display:flex; flex-direction:column;
  }}
  /* タイトルバー: 自動高さ（テキスト量に応じて伸縮） */
  .title-bar {{
    background:{theme["bg"]};
    padding:{size["pad"]};
    flex:0 0 auto;
    overflow:visible;
    display:flex; flex-direction:column; justify-content:center;
    position:relative;
  }}
  /* レインボーアクセントライン（下） */
  .title-bar::after {{
    content:''; position:absolute; bottom:0; left:0; right:0; height:4px;
    background:{theme["accent"]};
  }}
  /* 背景パターンオーバーレイ */
  {before_css_block}
  .catchphrase {{
    display:inline-flex; align-items:center; gap:3px;
    color:{theme["sub"]};
    font-size:10px; font-weight:700; letter-spacing:0.06em;
    background:rgba(0,0,0,0.18);
    border:1px solid rgba(255,255,255,0.22);
    padding:2px 10px; border-radius:20px;
    margin-bottom:7px; width:fit-content;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:92%;
  }}
  .title-text {{
    color:{theme["text"]};
    font-size:{size["font"]}; font-weight:{size["weight"]};
    line-height:{size["lh"]}; letter-spacing:-0.01em;
    text-shadow:0 2px 8px rgba(0,0,0,0.40);
    word-break:break-all;
  }}
  /* 動画セクション: 固定(16:9) */
  .video-area {{
    width:{_PREVIEW_W}px; height:{_VIDEO_H}px;
    flex:0 0 {_VIDEO_H}px;
    background:#000; overflow:hidden; position:relative;
  }}
  .video-area iframe {{ width:{_PREVIEW_W}px; height:{_VIDEO_H}px; border:none; display:block; }}
  /* 底部画像エリア: 132px 固定 (1.7:1 横長) */
  .bottom-area {{
    flex:0 0 {_BOTTOM_H}px;
    height:{_BOTTOM_H}px;
    overflow:hidden; border-top:1px solid #e2e8f0;
  }}
</style></head>
<body>
  <div class="card">
    <div class="title-bar">
      {catchphrase_html}
      <div class="title-text">{title}</div>
    </div>
    <div class="video-area">
      <iframe src="{embed_url}"
        allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
        allowfullscreen></iframe>
    </div>
    <div class="bottom-area">{bottom_img_html}</div>
  </div>
<script>
window.addEventListener('load',function(){{
  try{{
    var h=document.querySelector('.card').offsetHeight;
    if(window.frameElement){{
      window.frameElement.style.height=(h+10)+'px';
    }}
  }}catch(e){{}}
}});
</script>
<!-- rk:{_render_key} -->
</body></html>"""
    components.html(card_html, height=card_h + 30, scrolling=False)

    # 底部画像 — 削除ボタン（画像はカード内で表示済み）
    _bimg = clip.get("bottom_image")
    if _bimg and Path(_bimg).exists():
        if st.button("🗑 底部画像を削除", key=f"del_img_{idx}", use_container_width=True):
            Path(_bimg).unlink(missing_ok=True)
            clip["bottom_image"] = None
            _save_session(
                st.session_state.get("video_info"),
                st.session_state.get("clips", []),
            )
            st.rerun()
    else:
        st.markdown(
            '<div style="font-size:10px;color:#94a3b8;margin-bottom:2px;">'
            '📷 顔写真・ロゴ等を追加（このクリップのみ）</div>',
            unsafe_allow_html=True,
        )

    uploaded = st.file_uploader(
        "底部画像", key=f"img_{idx}",
        type=["png", "jpg", "jpeg"],
        label_visibility="collapsed",
    )
    # アップロード直後プレビュー
    if uploaded:
        st.image(uploaded, width=120,
                 caption=f"{uploaded.name}  ({uploaded.size // 1024} KB)")
        img_dir = OUTPUT_DIR / "images"
        img_dir.mkdir(exist_ok=True)
        ext = uploaded.name.rsplit(".", 1)[-1].lower()
        img_path = img_dir / f"clip_{clip['index']:02d}_bottom.{ext}"
        img_path.write_bytes(uploaded.read())
        clip["bottom_image"] = str(img_path)
        # rerun 前に session を保存（rerun 後に clips が巻き戻らないよう）
        _save_session(
            st.session_state.get("video_info"),
            st.session_state.get("clips", []),
        )
        st.rerun()


# ══════════════════════════════════════════════════════════
# STEP 2 — タイトルデザイン設定 & 底部画像設定
# ══════════════════════════════════════════════════════════
def step2():
    render_stepbar(2)
    render_video_banner()

    # ── ページ上部ナビゲーション ──
    if st.button("🔄 新しい動画", key="back2_top"):
        SESSION_FILE.unlink(missing_ok=True)
        for k in ["step", "video_info", "clips", "results"]:
            del st.session_state[k]
        st.rerun()
    st.markdown("<hr style='margin:6px 0 14px;border:none;border-top:1px solid #f1f5f9;'>",
                unsafe_allow_html=True)

    st.markdown("""
    <div style="padding:24px 40px 0;margin-left:-40px;margin-right:-40px;">
      <div style="font-size:20px;font-weight:800;color:#1e293b;margin-bottom:4px;">
        🎨 ショートのデザインを設定しよう
      </div>
      <div style="font-size:13px;color:#64748b;margin-bottom:8px;">
        全クリップを<strong style="color:#1e293b;">同じデザインで統一</strong>するか、
        <strong style="color:#1e293b;">クリップごとにバラバラ</strong>にするかを選べます。<br>
        統一→チャンネルのブランドが視聴者に伝わりやすくなる。
        バラバラ→どのデザインが伸びるか自動でA/Bテストできる。
      </div>
    </div>
    """, unsafe_allow_html=True)

    clips = s.clips
    video_id = (s.video_info or {}).get("id", "")

    # ── ペンディング処理（rerun後の適用） ────────────────────
    if "_pending_bulk_img" in st.session_state:
        _bpath = st.session_state.pop("_pending_bulk_img")
        if Path(_bpath).exists():
            for c in clips:
                c["bottom_image"] = _bpath
            s.clips = clips
            _save_session(s.video_info, clips)

    if "_design_pending" in st.session_state:
        _pd = st.session_state.pop("_design_pending")
        for _pk, _pv in _pd.items():
            st.session_state[_pk] = _pv
        if _pd.get("_clear_designs"):
            st.session_state.pop("clip_designs", None)

    # ══════════════════════════════════════════════════
    # SECTION 1: タイトルデザイン設定
    # ══════════════════════════════════════════════════
    st.markdown("""
    <div class="design-sec">
      <div class="design-sec-hd">
        <div class="design-sec-icon purple">🎨</div>
        <div>
          <div class="design-sec-title">タイトルカードのデザイン</div>
          <div class="design-sec-desc">動画の冒頭に表示される「タイトルエリア」のカラー・文字サイズ・背景パターンを設定</div>
        </div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    # どこに表示されるか図解
    _t_preview = TITLE_THEMES.get(st.session_state.get("title_theme", "purple"), TITLE_THEMES["purple"])
    _s_preview = TITLE_SIZES.get(st.session_state.get("title_size", "large"), TITLE_SIZES["large"])
    _pc_preview = TITLE_PATTERNS.get(st.session_state.get("title_pattern", "none"), TITLE_PATTERNS["none"])["css"]
    st.markdown(f"""
    <div class="design-diagram">
      <div class="shorts-thumb shorts-thumb-hl-title">
        <div class="shorts-thumb-title" style="background:{_t_preview['bg']};">
          TITLE<br>TEXT
        </div>
        <div class="shorts-thumb-video">▶</div>
        <div class="shorts-thumb-bottom">🖼</div>
      </div>
      <div class="diagram-note">
        <strong>← タイトルカード</strong>（ここを設定）<br><br>
        動画の一番上に表示される帯エリアです。<br>
        カラー・文字サイズ・背景の柄を自由にカスタマイズできます。<br><br>
        <span style="color:#94a3b8;font-size:11px;">
          💡 下のプレビューでリアルタイムに確認できます
        </span>
      </div>
    </div>
    """, unsafe_allow_html=True)

    # ── コントロール ─────────────────────────────────────
    _ctrl_col, _prev_col = st.columns([3, 2])
    with _ctrl_col:
        # ランダムモード
        st.session_state.setdefault("rand_mode_widget", False)
        rand_mode_on = st.toggle(
            "🎲 クリップごとにバラバラなデザイン",
            key="rand_mode_widget",
            help="ONにすると各クリップに異なるランダムデザインが自動割り当てされます",
            on_change=lambda: st.session_state.pop("clip_designs", None),
        )
        st.session_state["rand_mode"] = rand_mode_on
        if rand_mode_on:
            if st.button("🔀 シャッフル（再抽選）", key="shuffle_designs2",
                         use_container_width=True):
                st.session_state.pop("clip_designs", None)
                st.rerun()

        # プロンプト入力
        _pr_col2, _pb_col2 = st.columns([5, 1])
        with _pr_col2:
            design_prompt = st.text_input(
                "🖊 テキストでデザインを指定",
                key="design_prompt",
                placeholder="例: ゴールドでドット大・文字大きめ ／ 赤でグリッド ／ ランダム",
            )
        with _pb_col2:
            st.markdown('<div style="height:28px"></div>', unsafe_allow_html=True)
            if st.button("🎯 適用", key="apply_design_prompt2", use_container_width=True):
                _th, _sz, _pt, _rd = _parse_design_prompt(design_prompt)
                _pending = {}
                if _rd:
                    _pending["rand_mode_widget"] = True
                    _pending["_clear_designs"]   = True
                else:
                    _pending["rand_mode_widget"] = False
                    if _th: _pending["title_theme"] = _th; _pending["title_theme_sel"] = _th
                    if _sz: _pending["title_size"]  = _sz; _pending["title_size_sel"]  = _sz
                    if _pt: _pending["title_pattern"] = _pt; _pending["title_pattern_sel"] = _pt
                st.session_state["_design_pending"] = _pending
                st.rerun()

        # テーマ / サイズ / 柄
        st.session_state.setdefault("title_theme_sel",   "purple")
        st.session_state.setdefault("title_size_sel",    "large")
        st.session_state.setdefault("title_pattern_sel", "none")
        if st.session_state.get("title_theme_sel")   not in TITLE_THEMES:   st.session_state["title_theme_sel"]   = "purple"
        if st.session_state.get("title_size_sel")    not in TITLE_SIZES:    st.session_state["title_size_sel"]    = "large"
        if st.session_state.get("title_pattern_sel") not in TITLE_PATTERNS: st.session_state["title_pattern_sel"] = "none"

        sel_theme = st.radio(
            "🎨 テーマカラー",
            options=list(TITLE_THEMES.keys()),
            format_func=lambda k: TITLE_THEMES[k]["label"],
            horizontal=True, key="title_theme_sel", disabled=rand_mode_on,
        )
        st.session_state["title_theme"] = sel_theme

        sel_size = st.radio(
            "🔠 文字サイズ",
            options=list(TITLE_SIZES.keys()),
            format_func=lambda k: TITLE_SIZES[k]["label"],
            horizontal=True, key="title_size_sel", disabled=rand_mode_on,
        )
        st.session_state["title_size"] = sel_size

        sel_pattern = st.radio(
            "🗺 背景の柄",
            options=list(TITLE_PATTERNS.keys()),
            format_func=lambda k: TITLE_PATTERNS[k]["label"],
            horizontal=True, key="title_pattern_sel", disabled=rand_mode_on,
        )
        st.session_state["title_pattern"] = sel_pattern

    with _prev_col:
        st.markdown('<div style="font-size:12px;color:#64748b;font-weight:600;margin-bottom:6px;">リアルタイムプレビュー</div>', unsafe_allow_html=True)
        _t  = TITLE_THEMES[st.session_state["title_theme"]]
        _s  = TITLE_SIZES[st.session_state["title_size"]]
        _h  = TITLE_BAR_H[st.session_state["title_size"]]
        _pc = TITLE_PATTERNS[st.session_state.get("title_pattern", "none")]["css"]
        st.markdown(
            f"""<div style="background:{_t['bg']};border-radius:14px;min-height:{_h}px;
                    position:relative;overflow:hidden;
                    box-shadow:0 4px 20px rgba(0,0,0,0.18);">
              <div style="position:absolute;inset:0;background:{_pc};pointer-events:none;"></div>
              <div style="position:relative;z-index:1;padding:{_s['pad']};
                          display:flex;flex-direction:column;justify-content:center;">
                <div style="color:{_t['sub']};font-size:10px;font-weight:700;
                            background:rgba(0,0,0,0.18);border:1px solid rgba(255,255,255,0.22);
                            display:inline-block;padding:2px 10px;border-radius:20px;
                            margin-bottom:7px;letter-spacing:0.06em;">サンプルキャッチ✨</div>
                <div style="color:{_t['text']};font-size:{_s['font']};font-weight:{_s['weight']};
                            line-height:{_s['lh']};text-shadow:0 2px 8px rgba(0,0,0,0.4);">
                  実はこれが本当のコツ！
                </div>
              </div>
              <div style="position:absolute;bottom:0;left:0;right:0;height:4px;
                          background:{_t['accent']};"></div>
            </div>""",
            unsafe_allow_html=True,
        )

    st.markdown("<br>", unsafe_allow_html=True)

    # ══════════════════════════════════════════════════
    # SECTION 2: 底部画像設定
    # ══════════════════════════════════════════════════
    st.markdown("""
    <div class="design-sec">
      <div class="design-sec-hd">
        <div class="design-sec-icon orange">🖼</div>
        <div>
          <div class="design-sec-title">底部画像（全クリップ共通）</div>
          <div class="design-sec-desc">動画の一番下に表示するロゴ・顔写真などを全クリップに一括設定（各クリップごとに設定も可能です）</div>
        </div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    # 底部画像の位置図解
    _bulk_path_cur = clips[0].get("bottom_image") if clips else None
    _all_same = (
        _bulk_path_cur and
        all(c.get("bottom_image") == _bulk_path_cur for c in clips) and
        Path(_bulk_path_cur).exists()
    )
    st.markdown(f"""
    <div class="design-diagram">
      <div class="shorts-thumb shorts-thumb-hl-bottom">
        <div class="shorts-thumb-title" style="background:{_t_preview['bg']};">TITLE</div>
        <div class="shorts-thumb-video">▶</div>
        <div class="shorts-thumb-bottom" style="background:#d1fae5;color:#065f46;font-weight:700;">
          ← ここ
        </div>
      </div>
      <div class="diagram-note">
        動画の一番下に表示される画像エリアです。<br>
        チャンネルのロゴ・顔写真・SNS情報などを設定すると<br>
        ブランディングや宣伝に効果的です。<br><br>
        <strong style="color:#059669;">← 底部画像エリア</strong>（ここを設定）<br>
        <span style="color:#94a3b8;font-size:11px;">💡 スキップして後から設定することもできます</span>
      </div>
    </div>
    <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:12px;padding:14px 16px;margin-top:10px;">
      <div style="font-size:12px;font-weight:700;color:#15803d;margin-bottom:8px;">📐 推奨画像サイズ</div>
      <div style="font-size:12px;color:#166534;line-height:1.9;">
        <span style="display:inline-block;background:#dcfce7;border:1px solid #86efac;border-radius:6px;padding:2px 10px;font-weight:700;font-size:13px;margin-bottom:6px;">1080 × 640 px 推奨</span><br>
        縦横比 <strong>横長（約 1.7:1）</strong> が最適です。<br>
        <span style="color:#86efac;font-weight:700;">▸</span> 幅は1080pxに自動リサイズされます<br>
        <span style="color:#86efac;font-weight:700;">▸</span> 底部エリアは固定の横長エリアに収まります<br>
        <span style="color:#86efac;font-weight:700;">▸</span> 余白部分はテーマ色で自動補完されます<br>
        <span style="color:#94a3b8;font-size:11px;">※ 正方形・縦長でも表示されますが上下に余白が出ます</span>
      </div>
    </div>
    """, unsafe_allow_html=True)

    if _all_same:
        cur_l, cur_r = st.columns([1, 3])
        with cur_l:
            st.image(str(_bulk_path_cur), use_container_width=True)
        with cur_r:
            st.markdown(
                f'<div style="font-size:12px;color:#059669;font-weight:700;margin-top:6px;">'
                f'✅ 現在の一括設定画像</div>'
                f'<div style="font-size:11px;color:#64748b;margin-top:3px;">'
                f'{Path(_bulk_path_cur).name}</div>',
                unsafe_allow_html=True,
            )
        st.markdown("---")

    bulk_up = st.file_uploader(
        "全クリップ共通の底部画像をアップロード（顔写真・ロゴなど）",
        key="bulk_bottom_img2",
        type=["png", "jpg", "jpeg"],
        help="アップロード後「✅ 全クリップに適用」を押してください",
    )
    if bulk_up is not None:
        up_l, up_r = st.columns([1, 3])
        with up_l:
            st.image(bulk_up, use_container_width=True)
        with up_r:
            st.markdown(
                f'<div style="font-size:12px;color:#3b82f6;font-weight:700;margin-top:6px;">'
                f'📷 アップロード済み</div>'
                f'<div style="font-size:11px;color:#64748b;margin-top:3px;">'
                f'{bulk_up.name} &nbsp;({bulk_up.size // 1024} KB)</div>',
                unsafe_allow_html=True,
            )
        st.markdown("""
<div style="background:#fff7ed;border:2px solid #f97316;border-radius:10px;
            padding:11px 15px;margin:8px 0 2px;display:flex;align-items:center;gap:10px;">
  <div style="font-size:26px;line-height:1;">⚠️</div>
  <div>
    <div style="font-size:13px;font-weight:800;color:#c2410c;">あと1ステップ！</div>
    <div style="font-size:12px;color:#9a3412;margin-top:2px;line-height:1.5;">
      👇 下の「✅ 全クリップに適用」を押すと画像が確定されます
    </div>
  </div>
</div>
""", unsafe_allow_html=True)

    ap_col, cl_col = st.columns(2)
    with ap_col:
        if st.button("✅ 全クリップに適用", key="bulk_apply_img2",
                     use_container_width=True, disabled=(bulk_up is None),
                     type="primary" if bulk_up is not None else "secondary"):
            img_dir = OUTPUT_DIR / "images"
            img_dir.mkdir(exist_ok=True)
            ext_b = bulk_up.name.rsplit(".", 1)[-1].lower()
            bulk_path = img_dir / f"bulk_bottom.{ext_b}"
            bulk_path.write_bytes(bulk_up.read())
            st.session_state["_pending_bulk_img"] = str(bulk_path)
            st.rerun()
    with cl_col:
        if st.button("🗑 底部画像を削除", key="bulk_clear_img2", use_container_width=True):
            for c in clips:
                c["bottom_image"] = None
            s.clips = clips
            _save_session(s.video_info, clips)
            st.rerun()

    # ── ナビゲーション ────────────────────────────────────
    st.markdown("<br>", unsafe_allow_html=True)
    col_back, col_next = st.columns([1, 3])
    with col_back:
        if st.button("🔄 新しい動画", key="back2"):
            SESSION_FILE.unlink(missing_ok=True)
            for k in ["step", "video_info", "clips", "results"]:
                del st.session_state[k]
            st.rerun()
    with col_next:
        if st.button(
            "クリップ確認へ →",
            type="primary", use_container_width=True,
        ):
            s.step = 3
            st.rerun()


def step3():
    render_stepbar(3)
    render_video_banner()

    import streamlit.components.v1 as _cv1_s3hdr
    _cv1_s3hdr.html("""
<style>
  body{margin:0;padding:0;font-family:-apple-system,'Hiragino Sans',sans-serif;}
  .s3-wrap{padding:4px 0 0;}
  .s3-heading{font-size:20px;font-weight:800;color:#1e293b;margin-bottom:4px;}
  .s3-sub{font-size:14px;color:#475569;margin-bottom:18px;}
  .ai-banner{
    background:linear-gradient(135deg,#f5f3ff 0%,#ede9fe 55%,#faf5ff 100%);
    border:2px solid rgba(139,92,246,.3);
    border-radius:16px;padding:18px 20px 16px;position:relative;overflow:hidden;
    margin-bottom:4px;
  }
  .ai-banner-glow{
    position:absolute;border-radius:50%;filter:blur(50px);pointer-events:none;
  }
  .ai-banner-title{
    font-size:13px;font-weight:800;letter-spacing:.08em;
    color:#6d28d9;margin-bottom:12px;display:flex;align-items:center;gap:6px;
  }
  .ai-dot{width:8px;height:8px;border-radius:50%;background:#7c3aed;
    box-shadow:0 0 6px #7c3aed;display:inline-block;}
  .ai-fields{display:grid;grid-template-columns:1fr 1fr;gap:8px;}
  .ai-field{
    background:#fff;border:1.5px solid rgba(139,92,246,.2);
    border-radius:12px;padding:10px 12px;
    display:flex;align-items:center;gap:12px;
  }
  .ai-field-icon{font-size:22px;flex-shrink:0;}
  .ai-field-body{flex:1;min-width:0;}
  .ai-field-name{font-size:14px;font-weight:800;color:#1e1b4b;margin-bottom:2px;}
  .ai-field-dest{font-size:11px;color:#4b5563;margin-bottom:4px;line-height:1.4;}
  .ai-field-impact{display:flex;align-items:center;gap:4px;margin-bottom:4px;}
  .ai-stars{font-size:12px;}
  .stars-high{color:#f97316;}
  .stars-mid{color:#d1d5db;}
  .ai-field-impact-label{font-size:11px;color:#374151;font-weight:600;}
  .ai-badge{
    display:inline-flex;align-items:center;gap:4px;
    background:rgba(139,92,246,.12);border:1px solid rgba(139,92,246,.35);
    border-radius:100px;padding:2px 8px;font-size:10px;
    color:#6d28d9;font-weight:700;letter-spacing:.04em;
  }
</style>
<div class="s3-wrap">
  <div class="s3-heading">✂️ クリップを確認・編集</div>
  <div class="s3-sub">AIが自動生成した内容を確認・編集できます。タイトルとキャッチコピーが再生数に最も影響します。</div>

  <div class="ai-banner">
    <div class="ai-banner-glow" style="width:300px;height:300px;background:rgba(139,92,246,.08);top:-100px;right:-60px;"></div>
    <div class="ai-banner-glow" style="width:200px;height:200px;background:rgba(249,115,22,.05);bottom:-80px;left:-40px;"></div>

    <div class="ai-banner-title">
      <span class="ai-dot"></span> AI が自動生成したフィールドと効果
    </div>

    <div class="ai-fields">
      <div class="ai-field">
        <div class="ai-field-icon">📝</div>
        <div class="ai-field-body">
          <div class="ai-field-name">タイトル</div>
          <div class="ai-field-dest">YouTubeタイトル・検索・おすすめに表示</div>
          <div class="ai-field-impact">
            <span class="ai-stars stars-high">★★★</span>
            <span class="ai-field-impact-label">クリック率に直結</span>
          </div>
          <div class="ai-badge">🤖 AI生成</div>
        </div>
      </div>
      <div class="ai-field">
        <div class="ai-field-icon">⚡</div>
        <div class="ai-field-body">
          <div class="ai-field-name">キャッチコピー</div>
          <div class="ai-field-dest">動画画面にテキストとして焼き込まれる</div>
          <div class="ai-field-impact">
            <span class="ai-stars stars-high">★★★</span>
            <span class="ai-field-impact-label">視聴継続率に直結</span>
          </div>
          <div class="ai-badge">🤖 AI生成</div>
        </div>
      </div>
      <div class="ai-field">
        <div class="ai-field-icon">📄</div>
        <div class="ai-field-body">
          <div class="ai-field-name">説明文</div>
          <div class="ai-field-dest">YouTubeの概要欄に表示されるテキスト</div>
          <div class="ai-field-impact">
            <span class="ai-stars stars-high">★★</span><span class="ai-stars stars-mid">★</span>
            <span class="ai-field-impact-label">SEO・概要欄</span>
          </div>
          <div class="ai-badge">🤖 AI生成</div>
        </div>
      </div>
      <div class="ai-field">
        <div class="ai-field-icon">＃</div>
        <div class="ai-field-body">
          <div class="ai-field-name">ハッシュタグ</div>
          <div class="ai-field-dest">YouTubeの概要欄に追加されるタグ</div>
          <div class="ai-field-impact">
            <span class="ai-stars stars-high">★★</span><span class="ai-stars stars-mid">★</span>
            <span class="ai-field-impact-label">SEO・タグ検索</span>
          </div>
          <div class="ai-badge">🤖 AI生成</div>
        </div>
      </div>
    </div>
  </div>
</div>
""", height=400)

    # ── ページ上部ナビゲーション ──
    if st.button("← デザイン設定に戻る", key="back3_top"):
        s.step = 2
        st.rerun()

    st.markdown("<hr style='margin:10px 0 14px;border:none;border-top:1px solid #f1f5f9;'>",
                unsafe_allow_html=True)

    # ── 解析ステータスバッジ ───────────────────────────────────
    _status_cols = st.columns(2)

    # 字幕取得ステータス
    with _status_cols[0]:
        _dbg = st.session_state.get("transcript_debug")
        if _dbg:
            st.warning("⚠️ 字幕なし（概要欄テキストで代替）")
            with st.expander("詳細ログ", expanded=False):
                for _d in _dbg:
                    st.code(_d)
        else:
            st.success("✅ 字幕取得: 成功")

    # Claude API ステータス
    with _status_cols[1]:
        _ai_st = st.session_state.get("ai_status")
        _show_regen_btn = False
        _is_not_run = False  # 未実行フラグ（ボタンを目立たせる）

        if _ai_st is None:
            _is_not_run = True
            _show_regen_btn = True
            import streamlit.components.v1 as _cv1_ai
            _cv1_ai.html("""
<div style="background:linear-gradient(135deg,#7c2d12 0%,#92400e 100%);
            border:1px solid rgba(251,191,36,.4);border-radius:12px;
            padding:14px 16px;font-family:-apple-system,'Hiragino Sans',sans-serif;">
  <div style="color:#fef3c7;font-weight:800;font-size:13px;margin-bottom:8px;">
    🤖 AI 未実行 — 精度が低い状態です
  </div>
  <div style="color:rgba(254,243,199,.85);font-size:11.5px;line-height:1.8;">
    <strong style="color:#fbbf24;">タイトル・キャッチコピー・説明文・ハッシュタグ</strong>が基本ロジックで自動生成されています。<br>
    AI で再生成すると：<br>
    <span style="color:#fbbf24;">✦</span> タイトル — バイラルになりやすい表現に改善<br>
    <span style="color:#fbbf24;">✦</span> キャッチコピー — 視聴者の心に刺さる一言に改善<br>
    <span style="color:#fbbf24;">✦</span> 説明文 — 検索に強い構成に最適化<br>
    <span style="color:#fbbf24;">✦</span> ハッシュタグ — 伸びるタグをAIが自動選定
  </div>
</div>
""", height=195)

        elif _ai_st.get("errors") and _ai_st.get("errors")[0].startswith("Anthropic API キー未設定"):
            st.warning("⚠️ Claude API キー未設定\nRailway の Variables に ANTHROPIC_API_KEY を設定してください")
        elif _ai_st.get("total", 0) == 0:
            st.info("🤖 Claude API: 字幕なし（スキップ）")
            _show_regen_btn = True
        elif _ai_st.get("errors"):
            _ok  = _ai_st["success"]
            _tot = _ai_st["total"]
            with st.expander(f"🔴 Claude API: {_ok}/{_tot} 成功（エラーあり）", expanded=True):
                for _d in _ai_st["errors"]:
                    st.code(_d)
            _show_regen_btn = True
        else:
            _ok  = _ai_st["success"]
            _tot = _ai_st["total"]
            st.success(f"✅ Claude API: {_ok}/{_tot} クリップ成功")
            _show_regen_btn = True

        if _show_regen_btn:
            st.markdown(
                "**💡 AIへの追加指示**（任意）"
                if _is_not_run else
                "**✏️ 追加指示を変えて再生成**（任意）"
            )
            _user_prompt = st.text_area(
                "AIへの追加指示",
                placeholder=(
                    "指示を書くと精度がさらに上がります。例：\n"
                    "・ターゲット: 副業を始めたい20〜30代会社員\n"
                    "・「実は〜」「知らないと損」などのフックを多用して\n"
                    "・驚き・緊迫感のある強いコピーにして\n"
                    "・競合と差別化できるタイトルにして"
                ),
                key="claude_user_prompt",
                height=110,
                label_visibility="collapsed",
            )
            _btn_label = (
                "✨ AIでタイトルを高精度に生成する（推奨）"
                if _is_not_run else
                "🔄 AIで再生成"
            )
            if st.button(
                _btn_label,
                key="btn_regen_claude",
                type="primary" if _is_not_run else "secondary",
                use_container_width=True,
            ):
                with st.spinner("AI を呼び出し中…"):
                    _run_claude_api_on_clips(user_prompt=_user_prompt)

    clips = s.clips
    from core.analyzer import fmt_time
    video_id = (s.video_info or {}).get("id", "")

    # ── 一括底部画像：前回 rerun で保存されたパスを適用 ─────
    # （file_uploader は rerun でリセットされるため、ボタン押下時に
    #   _pending_bulk_img にパスを格納し、次の rerun 冒頭で一括適用する）
    if "_pending_bulk_img" in st.session_state:
        _bpath = st.session_state.pop("_pending_bulk_img")
        if Path(_bpath).exists():
            for c in clips:
                c["bottom_image"] = _bpath
            s.clips = clips
            _save_session(s.video_info, clips)

    # ── タイトルデザイン設定 UI（step2 で設定済み。ここでは省略）────────────────
    with st.expander("🎨 タイトルデザイン設定", expanded=False):
        _head_l, _head_r = st.columns([4, 3])
        with _head_l:
            st.markdown(
                '<p style="font-size:13px;color:#475569;margin:4px 0 10px;">切り抜きプレビューのタイトルエリアのデザインを設定します。</p>',
                unsafe_allow_html=True,
            )
        with _head_r:
            st.session_state.setdefault("rand_mode_widget", False)
            rand_mode_on = st.toggle(
                "🎲 クリップごとにバラバラ",
                key="rand_mode_widget",
                help="ONにすると各クリップに異なるランダムデザインが自動割り当てされます",
                on_change=lambda: st.session_state.pop("clip_designs", None),
            )
            # _render_clip_preview が読む "rand_mode" キーに同期
            st.session_state["rand_mode"] = rand_mode_on
            if rand_mode_on:
                if st.button("🔀 シャッフル（再抽選）", key="shuffle_designs",
                             use_container_width=True,
                             help="全クリップのデザインを再抽選します"):
                    st.session_state.pop("clip_designs", None)
                    st.rerun()

        # ── プロンプト入力 ──────────────────────────────────
        _pr_col, _pb_col = st.columns([5, 1])
        with _pr_col:
            design_prompt = st.text_input(
                "🖊 プロンプトでデザインを指定",
                key="design_prompt",
                placeholder="例: ゴールドでドット大・文字大きめ ／ 赤でグリッド ／ ランダムでバラバラに",
            )
        with _pb_col:
            st.markdown('<div style="height:28px"></div>', unsafe_allow_html=True)
            if st.button("🎯 適用", key="apply_design_prompt", use_container_width=True):
                _th, _sz, _pt, _rd = _parse_design_prompt(design_prompt)
                _pending = {}
                if _rd:
                    _pending["rand_mode_widget"] = True
                    _pending["_clear_designs"]   = True
                else:
                    _pending["rand_mode_widget"] = False
                    if _th:
                        _pending["title_theme"]     = _th
                        _pending["title_theme_sel"] = _th
                    if _sz:
                        _pending["title_size"]     = _sz
                        _pending["title_size_sel"] = _sz
                    if _pt:
                        _pending["title_pattern"]     = _pt
                        _pending["title_pattern_sel"] = _pt
                st.session_state["_design_pending"] = _pending
                st.rerun()

        d_left, d_right = st.columns([3, 2])

        with d_left:
            # setdefault でデフォルト設定（index= と session_state の二重設定警告を回避）
            st.session_state.setdefault("title_theme_sel",   "purple")
            st.session_state.setdefault("title_size_sel",    "large")
            st.session_state.setdefault("title_pattern_sel", "none")
            # 無効なキー値をリセット（パターン追加後の互換性確保）
            if st.session_state.get("title_theme_sel")   not in TITLE_THEMES:
                st.session_state["title_theme_sel"]   = "purple"
            if st.session_state.get("title_size_sel")    not in TITLE_SIZES:
                st.session_state["title_size_sel"]    = "large"
            if st.session_state.get("title_pattern_sel") not in TITLE_PATTERNS:
                st.session_state["title_pattern_sel"] = "none"

            sel_theme = st.radio(
                "🎨 テーマカラー",
                options=list(TITLE_THEMES.keys()),
                format_func=lambda k: TITLE_THEMES[k]["label"],
                horizontal=True,
                key="title_theme_sel",
                disabled=rand_mode_on,
            )
            st.session_state["title_theme"] = sel_theme

            sel_size = st.radio(
                "🔠 文字サイズ",
                options=list(TITLE_SIZES.keys()),
                format_func=lambda k: TITLE_SIZES[k]["label"],
                horizontal=True,
                key="title_size_sel",
                disabled=rand_mode_on,
            )
            st.session_state["title_size"] = sel_size

            sel_pattern = st.radio(
                "🗺 背景の柄",
                options=list(TITLE_PATTERNS.keys()),
                format_func=lambda k: TITLE_PATTERNS[k]["label"],
                horizontal=True,
                key="title_pattern_sel",
                disabled=rand_mode_on,
            )
            st.session_state["title_pattern"] = sel_pattern

        with d_right:
            # リアルタイムプレビュー（テーマ + サイズ + 柄パターン）
            _t  = TITLE_THEMES[st.session_state["title_theme"]]
            _s  = TITLE_SIZES[st.session_state["title_size"]]
            _h  = TITLE_BAR_H[st.session_state["title_size"]]
            _pc = TITLE_PATTERNS[st.session_state.get("title_pattern", "none")]["css"]
            st.markdown(
                f"""<div style="
                    background:{_t['bg']};
                    border-radius:12px;
                    min-height:{_h}px;
                    position:relative;overflow:hidden;margin-top:4px;
                    box-shadow:0 4px 16px rgba(0,0,0,0.18);">
                  <!-- 背景パターンオーバーレイ -->
                  <div style="position:absolute;inset:0;background:{_pc};pointer-events:none;"></div>
                  <!-- コンテンツ -->
                  <div style="position:relative;z-index:1;padding:{_s['pad']};
                              display:flex;flex-direction:column;justify-content:center;">
                    <div style="
                      color:{_t['sub']};font-size:10px;font-weight:700;
                      background:rgba(0,0,0,0.18);border:1px solid rgba(255,255,255,0.22);
                      display:inline-block;padding:2px 10px;border-radius:20px;
                      margin-bottom:7px;letter-spacing:0.06em;">
                      サンプルキャッチ✨
                    </div>
                    <div style="
                      color:{_t['text']};
                      font-size:{_s['font']};font-weight:{_s['weight']};
                      line-height:{_s['lh']};
                      text-shadow:0 2px 8px rgba(0,0,0,0.4);">
                      実はこれが本当のコツ！
                    </div>
                  </div>
                  <!-- アクセントライン -->
                  <div style="position:absolute;bottom:0;left:0;right:0;height:4px;
                              background:{_t['accent']};"></div>
                </div>""",
                unsafe_allow_html=True,
            )

    # ── 一括底部画像設定 UI ───────────────────────────────
    with st.expander("🖼 底部画像を全クリップに一括設定", expanded=False):
        # 現在の一括画像（全クリップ共通の場合）を表示
        _bulk_path_cur = clips[0].get("bottom_image") if clips else None
        _all_same_img  = (
            _bulk_path_cur and
            all(c.get("bottom_image") == _bulk_path_cur for c in clips) and
            Path(_bulk_path_cur).exists()
        )
        if _all_same_img:
            cur_l, cur_r = st.columns([1, 3])
            with cur_l:
                st.image(str(_bulk_path_cur), use_container_width=True)
            with cur_r:
                st.markdown(
                    f'<div style="font-size:12px;color:#059669;font-weight:700;margin-top:6px;">'
                    f'✅ 現在の一括設定画像</div>'
                    f'<div style="font-size:11px;color:#64748b;margin-top:3px;">'
                    f'{Path(_bulk_path_cur).name}</div>',
                    unsafe_allow_html=True,
                )
            st.markdown("---")

        bulk_up = st.file_uploader(
            "全クリップ共通の底部画像（顔写真・ロゴ等）をアップロード",
            key="bulk_bottom_img",
            type=["png", "jpg", "jpeg"],
            help="アップロード後「✅ 全クリップに適用」を押してください",
        )

        # アップロード直後のプレビュー
        if bulk_up is not None:
            up_l, up_r = st.columns([1, 3])
            with up_l:
                st.image(bulk_up, use_container_width=True)
            with up_r:
                st.markdown(
                    f'<div style="font-size:12px;color:#3b82f6;font-weight:700;margin-top:6px;">'
                    f'📷 アップロード済み</div>'
                    f'<div style="font-size:11px;color:#64748b;margin-top:3px;">'
                    f'{bulk_up.name} &nbsp;({bulk_up.size // 1024} KB)</div>',
                    unsafe_allow_html=True,
                )
            st.markdown("""
<div style="background:#fff7ed;border:2px solid #f97316;border-radius:10px;
            padding:11px 15px;margin:8px 0 2px;display:flex;align-items:center;gap:10px;">
  <div style="font-size:26px;line-height:1;">⚠️</div>
  <div>
    <div style="font-size:13px;font-weight:800;color:#c2410c;">あと1ステップ！</div>
    <div style="font-size:12px;color:#9a3412;margin-top:2px;line-height:1.5;">
      👇 下の「✅ 全クリップに適用」を押すと画像が確定されます
    </div>
  </div>
</div>
""", unsafe_allow_html=True)

        ap_col, cl_col = st.columns(2)
        with ap_col:
            if st.button(
                "✅ 全クリップに適用",
                key="bulk_apply_img",
                use_container_width=True,
                disabled=(bulk_up is None),
                type="primary" if bulk_up is not None else "secondary",
            ):
                img_dir = OUTPUT_DIR / "images"
                img_dir.mkdir(exist_ok=True)
                ext_b = bulk_up.name.rsplit(".", 1)[-1].lower()
                bulk_path = img_dir / f"bulk_bottom.{ext_b}"
                bulk_path.write_bytes(bulk_up.read())
                st.session_state["_pending_bulk_img"] = str(bulk_path)
                st.rerun()
        with cl_col:
            if st.button("🗑 全クリップの画像を削除", key="bulk_clear_img", use_container_width=True):
                for c in clips:
                    c["bottom_image"] = None
                s.clips = clips
                _save_session(s.video_info, clips)
                st.rerun()
    # ─────────────────────────────────────────────────────

    for i, clip in enumerate(clips):
        time_str = f"{fmt_time(clip['start'])} → {fmt_time(clip['end'])}"
        enabled  = clip.get("enabled", True)

        # スコア情報
        score      = clip.get("score", 0)
        s_density  = clip.get("score_density", 0)
        s_engage   = clip.get("score_engagement", 0)
        s_complete = clip.get("score_completeness", 0)
        score_color = (
            "#10b981" if score >= 70 else
            "#f59e0b" if score >= 40 else
            "#ef4444"
        )

        # ── クリップ間セパレータ（2枚目以降） ──
        if i > 0:
            st.markdown(
                '<div class="clip-divider"><span>NEXT CLIP</span></div>',
                unsafe_allow_html=True,
            )

        # ── フルワイドセクションヘッダー ──
        _clip_title_short = (clip.get("title") or "（タイトル未設定）")[:42]
        st.markdown(f"""
        <div class="clip-section-hd">
          <div class="clip-section-num">{clip['index']}</div>
          <div class="clip-section-title">{_clip_title_short}</div>
          <div class="clip-section-badges">
            <span class="clip-score-tag"
                  title="スコア内訳&#10;📝文字密度:{s_density}/40&#10;🔥盛り上がり:{s_engage}/40&#10;✅文章完成度:{s_complete}/20"
                  style="background:{score_color};">★ {score}点</span>
            <span class="clip-time-tag">{time_str}</span>
          </div>
        </div>
        """, unsafe_allow_html=True)

        # ── 左: 編集フォーム ／ 右: プレビュー ──
        edit_col, prev_col = st.columns([3, 2])

        with edit_col:
            # ℹ️ 採点根拠バッジボタン
            st.markdown('<div class="score-badge-wrap">', unsafe_allow_html=True)
            if st.button(f"ℹ️ 採点根拠を見る　★{score}点の理由", key=f"score_{i}"):
                _score_dialog(score, s_density, s_engage, s_complete)
            st.markdown('</div>', unsafe_allow_html=True)

            # この区間の内容
            if clip.get("transcript"):
                st.markdown(
                    f'<div style="font-size:11px;font-weight:700;color:#6366f1;margin-bottom:4px;">📄 この区間の内容</div>'
                    f'<div class="transcript-box">{clip["transcript"]}</div>',
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    '<div style="font-size:11px;font-weight:700;color:#6366f1;margin-bottom:4px;">📄 この区間の内容</div>'
                    '<div class="transcript-box no-transcript">（字幕なし）</div>',
                    unsafe_allow_html=True,
                )

            # 編集フォーム
            r1, r2, r3, r4 = st.columns([1, 1, 1, 0.5])
            with r1:
                clip["title"] = st.text_input(
                    "📝 タイトル",
                    value=clip.get("title", ""),
                    key=f"title_{i}", placeholder="ショートタイトル（〜40文字）",
                    help="YouTubeの動画タイトルに使われます。検索・おすすめ表示に直結するため再生数への影響が最も大きいフィールドです。",
                )
            with r2:
                clip["hashtags"] = st.text_input(
                    "＃ ハッシュタグ",
                    value=clip.get("hashtags", "#Shorts"),
                    key=f"tags_{i}", placeholder="#AI活用 #Shorts",
                    help="YouTubeの概要欄に追加されるハッシュタグです。検索SEOの補助として機能します。",
                )
            with r3:
                st.markdown('<div class="sec-label">開始 / 終了（秒）</div>', unsafe_allow_html=True)
                tc1, tc2 = st.columns(2)
                with tc1:
                    clip["start"] = st.number_input(
                        "開始", value=float(clip["start"]), min_value=0.0,
                        step=1.0, key=f"start_{i}", label_visibility="collapsed", format="%.0f",
                    )
                with tc2:
                    clip["end"] = st.number_input(
                        "終了", value=float(clip["end"]), min_value=1.0,
                        step=1.0, key=f"end_{i}", label_visibility="collapsed", format="%.0f",
                    )
            with r4:
                st.markdown('<div class="sec-label">スキップ</div>', unsafe_allow_html=True)
                clip["enabled"] = not st.checkbox(
                    "除外", value=not enabled, key=f"skip_{i}",
                )

            # キャッチコピー ＋ 説明文
            cp_col, desc_col = st.columns([1, 2])
            with cp_col:
                clip["catchphrase"] = st.text_input(
                    "⚡ キャッチコピー",
                    value=clip.get("catchphrase", ""),
                    key=f"catch_{i}",
                    placeholder="知らないと損！👀",
                    max_chars=25,
                    help="動画の画面上部に直接焼き込まれるテキストです。視聴者が最初に目にするため、視聴継続率に大きく影響します。15文字以内推奨。",
                )
            with desc_col:
                clip["description"] = st.text_area(
                    "📄 説明文",
                    value=clip.get("description", ""),
                    key=f"desc_{i}", height=120, placeholder="説明文（省略可）",
                    help="YouTubeの概要欄に表示されるテキストです。SEO補助として機能します。",
                )

        with prev_col:
            _render_clip_preview(clip, i, video_id)

    s.clips = clips
    _save_session(s.video_info, clips)

    # ナビゲーション
    enabled_count = sum(1 for c in clips if c.get("enabled", True))
    if st.button("← デザイン設定に戻る", key="back3"):
        s.step = 2
        st.rerun()
    st.markdown(
        '<p style="font-size:12px;color:#64748b;margin:10px 0 4px;font-weight:600;">'
        '▼ クリップの使い方を選んでください</p>',
        unsafe_allow_html=True,
    )
    col_dl_b, col_yt_b = st.columns(2)
    with col_dl_b:
        if st.button(
            f"⬇️ ダウンロードして手動で使う（{enabled_count}本）",
            key="dl_only3",
            type="primary", use_container_width=True,
            disabled=enabled_count == 0,
            help="動画ファイルをダウンロードして、自分で好きなタイミングに投稿できます",
        ):
            _now = datetime.now()
            s.schedule = {
                "start_date":  _now.strftime("%Y-%m-%d"),
                "daily_times": ["09:00", "15:00", "21:00"],
                "category_id": "22",
            }
            s["_download_only_mode"] = True
            s.step = 5
            st.rerun()
        st.caption("動画ファイルを保存 → 好きなタイミングで投稿")
    with col_yt_b:
        if st.button(
            f"📅 YouTubeに自動投稿する（{enabled_count}本）",
            key="next3_bottom",
            type="primary", use_container_width=True,
            disabled=enabled_count == 0,
            help="投稿日時・タイトルを設定してYouTubeへ自動アップロードします",
        ):
            s.step = 4
            st.rerun()
        st.caption("投稿日時を設定 → YouTube に自動アップ")


# ══════════════════════════════════════════════════════════
# STEP 4 — スケジュール設定
# ══════════════════════════════════════════════════════════
def step4():
    render_stepbar(4)
    render_video_banner()

    st.markdown("""
    <div style="padding:28px 40px 0;margin-left:-40px;margin-right:-40px;">
      <div style="font-size:20px;font-weight:800;color:#1e293b;margin-bottom:4px;">
        ⏰ 投稿スケジュールを設定
      </div>
    </div>
    """, unsafe_allow_html=True)

    # ── ページ上部ナビゲーション ──
    _tnc4 = st.columns([1, 3])
    with _tnc4[0]:
        if st.button("← 戻る", key="back4_top"):
            s.step = 3
            st.rerun()
    with _tnc4[1]:
        if st.button("実行画面へ →", key="next4_top",
                     type="primary", use_container_width=True):
            s.step = 5
            st.rerun()
    st.markdown("<hr style='margin:6px 0 14px;border:none;border-top:1px solid #f1f5f9;'>",
                unsafe_allow_html=True)

    sched = s.schedule
    from datetime import date as dt_date, time as dt_time

    col1, col2 = st.columns(2)
    with col1:
        try:
            init_date = datetime.strptime(sched["start_date"], "%Y-%m-%d").date()
        except Exception:
            init_date = (datetime.now() + timedelta(days=1)).date()
        start_date = st.date_input("開始日（JST）", value=init_date)
        sched["start_date"] = str(start_date)

        category = st.selectbox(
            "カテゴリー",
            options=["22 - 人・ブログ", "27 - 教育", "28 - 科学と技術",
                     "24 - エンターテインメント", "26 - ハウツー・スタイル"],
            index=0,
        )
        sched["category_id"] = category.split(" ")[0]

    with col2:
        _saved_times = sched.get("daily_times") or ["09:00", "15:00", "21:00"]
        _n_times = st.number_input(
            "1日の投稿本数", min_value=1, max_value=5,
            value=len(_saved_times), step=1, key="s4_n_times",
        )
        _new_times = []
        _t_cols = st.columns(min(int(_n_times), 3))
        for _ti in range(int(_n_times)):
            try:
                _tv = datetime.strptime(_saved_times[_ti], "%H:%M").time()
            except Exception:
                _tv = dt_time([9, 15, 21, 12, 18][_ti % 5], 0)
            _c = _t_cols[_ti % 3]
            _t_val = _c.time_input(
                f"{_ti+1}本目", value=_tv, key=f"s4_time_{_ti}",
            )
            _new_times.append(_t_val.strftime("%H:%M"))
        sched["daily_times"] = _new_times

    # ── YouTube 一括設定 ───────────────────────────────────────
    st.markdown("")
    st.markdown("### 📺 YouTube 一括設定")
    yt_col1, yt_col2 = st.columns(2)

    with yt_col1:
        playlist_id_input = st.text_input(
            "🎵 再生リスト ID（任意）",
            value=sched.get("playlist_id", "") or "",
            placeholder="PLxxxxxxxxxxxxxxxxxxxxxxxx",
            help="YouTube Studio の再生リスト URL から PL... の部分をコピーしてください",
        )
        sched["playlist_id"] = playlist_id_input.strip() or None

    with yt_col2:
        made_for_kids = st.checkbox(
            "👦 子ども向けコンテンツ（Made for Kids）",
            value=bool(sched.get("made_for_kids", False)),
            help="ONにするとYouTube Kidsに表示。通常はOFFのまま",
        )
        sched["made_for_kids"] = made_for_kids

        age_restricted = st.checkbox(
            "🔞 年齢制限（18歳以上のみ）",
            value=bool(sched.get("age_restricted", False)),
            help="ONにすると未成年は視聴不可",
        )
        sched["age_restricted"] = age_restricted

    # 再生リストを使うには youtube スコープが必要 → 既存tokenを削除して再認証
    if sched.get("playlist_id"):
        token_path = CREDS_DIR / "token.json"
        import json as _json_check
        needs_reauth = False
        if token_path.exists():
            try:
                _t = _json_check.loads(token_path.read_text())
                _scopes = _t.get("scopes", [])
                if "https://www.googleapis.com/auth/youtube" not in _scopes:
                    needs_reauth = True
            except Exception:
                pass
        if needs_reauth:
            st.warning(
                "⚠️ 再生リストへの追加には追加の権限が必要です。"
                "STEP4の認証画面でトークンをリセットして再ログインしてください。",
            )

    # ── 関連動画リンク設定 ──────────────────────────────────────
    st.markdown("")
    st.markdown("### 🔗 関連動画リンク")

    # 保存済みURLを復元
    _rv_saved = sched.get("related_video_urls") or [""]
    _rv_count_key = "s4_related_count"
    if _rv_count_key not in st.session_state:
        st.session_state[_rv_count_key] = len(_rv_saved)

    _rv_urls = []
    for _ri in range(st.session_state[_rv_count_key]):
        _rv_col_a, _rv_col_b = st.columns([10, 1])
        with _rv_col_a:
            _default_rv = _rv_saved[_ri] if _ri < len(_rv_saved) else ""
            _rv_val = st.text_input(
                f"関連動画 {_ri+1}",
                value=_default_rv,
                placeholder="https://youtu.be/xxxxxxxx  または  https://www.youtube.com/watch?v=xxxx",
                key=f"s4_related_{_ri}",
                label_visibility="collapsed" if _ri > 0 else "visible",
            )
            _rv_urls.append(_rv_val.strip())
        with _rv_col_b:
            if _ri == 0:
                st.markdown('<div style="height:28px"></div>', unsafe_allow_html=True)
            if st.session_state[_rv_count_key] > 1:
                if st.button("✕", key=f"s4_del_related_{_ri}", help="削除"):
                    _rv_saved_new = [u for j, u in enumerate(_rv_saved) if j != _ri]
                    sched["related_video_urls"] = _rv_saved_new
                    st.session_state[_rv_count_key] = max(1, st.session_state[_rv_count_key] - 1)
                    st.rerun()

    if st.button("＋ 関連動画を追加", key="s4_add_related"):
        st.session_state[_rv_count_key] += 1
        st.rerun()

    sched["related_video_urls"] = [u for u in _rv_urls if u]

    # 利用方法トグル
    _has_urls = bool(sched["related_video_urls"])
    _rv_opt_col1, _rv_opt_col2 = st.columns(2)
    with _rv_opt_col1:
        _add_to_desc = st.toggle(
            "📄 説明欄に追記する",
            value=bool(sched.get("related_add_desc", True)),
            key="s4_rv_desc",
            disabled=not _has_urls,
            help="各クリップの説明欄末尾に「▼ 関連動画」として自動追記します",
        )
        sched["related_add_desc"] = _add_to_desc
    with _rv_opt_col2:
        _post_comment = st.toggle(
            "💬 コメントに自動投稿する",
            value=bool(sched.get("post_related_comment", False)),
            key="s4_rv_comment",
            disabled=not _has_urls,
            help="アップロード直後に関連動画リンクをコメントとして投稿します",
        )
        sched["post_related_comment"] = _post_comment
        if _post_comment:
            st.caption("⚠️ ピン留めはYouTube Studioで手動操作が必要です（API非対応）")

    # プレビュー
    st.markdown("")
    st.markdown("### 📅 投稿スケジュール プレビュー")

    enabled_clips = [c for c in s.clips if c.get("enabled", True)]
    try:
        _dt_list = sched.get("daily_times") or ["09:00", "15:00", "21:00"]
        _start   = datetime.strptime(sched["start_date"], "%Y-%m-%d").date()
        for i, clip in enumerate(enabled_clips):
            _day = _start + timedelta(days=i // len(_dt_list))
            _hm  = _dt_list[i % len(_dt_list)]
            post_dt = datetime.strptime(f"{_day} {_hm}", "%Y-%m-%d %H:%M")
            title_str = clip["title"][:35] or f"クリップ {clip['index']}"
            st.markdown(f"""
            <div class="sched-row">
              <span class="sched-num">#{i+1}</span>
              <span class="sched-time">{post_dt.strftime('%Y/%m/%d %H:%M')} JST</span>
              <span class="sched-title">{title_str}</span>
            </div>
            """, unsafe_allow_html=True)
    except Exception:
        st.warning("日付を設定してください")

    st.markdown("")
    col_back, col_next = st.columns([1, 3])
    with col_back:
        if st.button("← 戻る", key="back4"):
            s.step = 3
            st.rerun()
    with col_next:
        if st.button("実行画面へ →", type="primary", use_container_width=True):
            s.schedule = sched
            s.step = 5
            st.rerun()


# ══════════════════════════════════════════════════════════
# アップグレード UI（プラン上限到達時）
# ══════════════════════════════════════════════════════════
def _show_upgrade_ui(user_id: str):
    """今月の上限に達したときに表示するアップグレード画面"""
    import streamlit as _st
    import os as _os

    try:
        from core.usage_tracker import get_plan_info
        _pi = get_plan_info(user_id)
    except Exception:
        return

    _current_plan = _pi.get("plan", "trial")

    _st.error(
        f"今月の生成枠（{_pi['limit']} 本）を使い切りました。"
        " プランをアップグレードすると来月まで待たずに続けられます。",
        icon="🚫",
    )

    _app_url = _get_app_url()

    def _checkout_url(plan: str) -> str | None:
        try:
            import stripe as _stripe
            _stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
            price_id = os.environ.get(f"STRIPE_PRICE_{plan.upper()}", "")
            if not _stripe.api_key or not price_id:
                return None
            sess = _stripe.checkout.Session.create(
                payment_method_types=["card"],
                line_items=[{"price": price_id, "quantity": 1}],
                mode="subscription",
                success_url=f"{_app_url}?payment=success",
                cancel_url=f"{_app_url}?payment=canceled",
                client_reference_id=user_id,
                metadata={"price_id": price_id},
            )
            return sess.url
        except Exception:
            return None

    # ── 無料5本プレゼントの説明 ─────────────────────────────────────
    _st.markdown(
        '<div style="background:#fef9c3;border:1px solid #fde047;border-radius:8px;'
        'padding:10px 14px;margin-bottom:16px;font-size:13px;color:#713f12;text-align:center;">'
        '🎁 <b>全プランに無料5本プレゼント付き！</b>　登録時の無料トライアル分がそのまま加算されます。'
        '</div>',
        unsafe_allow_html=True,
    )

    # ── 3プランを横並び ────────────────────────────────────────────
    _plans_to_show = [
        ("basic",  "⭐ ベーシック",     "月 105本",   "100本 + 🎁5本",   "¥50,000 / 月",   "1チャンネル向け",      500),
        ("pro",    "🚀 プロ",           "月 505本",   "500本 + 🎁5本",   "¥200,000 / 月",  "複数チャンネル向け",   400),
        ("agency", "🏢 エージェンシー", "月 1,005本", "1,000本 + 🎁5本", "¥350,000 / 月",  "チャンネルを量産する", 350),
    ]
    _cols = _st.columns(3)
    for (_plan_key, _plan_name, _clips_txt, _bonus_txt, _price_txt, _target_txt, _unit), _col in zip(_plans_to_show, _cols):
        _is_current = (_plan_key == _current_plan)
        with _col:
            _st.markdown(
                f"""<div style="border:2px solid {'#6366f1' if _is_current else '#e2e8f0'};
                border-radius:12px;padding:16px 12px;text-align:center;
                background:{'#f5f3ff' if _is_current else '#fff'};">
                <div style="font-size:1.1rem;font-weight:700;margin-bottom:4px;">{_plan_name}</div>
                <div style="font-size:1.7rem;font-weight:800;color:#6366f1;line-height:1.2;">{_clips_txt}</div>
                <div style="font-size:0.8rem;color:#16a34a;font-weight:600;margin:4px 0 4px;">
                  {_bonus_txt}
                </div>
                <div style="font-size:0.82rem;color:#64748b;margin-bottom:4px;">{_target_txt}</div>
                <div style="font-size:1.05rem;font-weight:700;color:#1e293b;margin-bottom:3px;">{_price_txt}</div>
                <div style="font-size:0.78rem;color:#94a3b8;">1本あたり 約 ¥{_unit:,}</div>
                </div>""",
                unsafe_allow_html=True,
            )
            _st.markdown("")
            if _is_current:
                _st.info("現在のプラン", icon="✅")
            else:
                _url = _checkout_url(_plan_key)
                if _url:
                    _st.link_button(
                        f"{_plan_name} に変更",
                        _url,
                        use_container_width=True,
                        type="primary" if _plan_key == "pro" else "secondary",
                    )
                else:
                    _st.caption("設定準備中")


# ══════════════════════════════════════════════════════════
# STEP 5 — 実行
# ══════════════════════════════════════════════════════════
def step5():
    # ── Streamlit推奨パターン: パイプラインをメインフローで実行 ──────────
    # ボタンのcallback内でst.status()等を呼ぶとRerunException(BaseException)が
    # 発生してエラーが捕捉できないため、フラグ経由でメインフローに移して実行する
    if s.get("_pipeline_pending"):
        print("[STEP5] _pipeline_pending 検知 → パイプライン開始", flush=True)
        s["_pipeline_pending"] = False
        s["_pipeline_ran"]     = None  # リセット
        _ppl_want_dl = s.get("_pipeline_want_dl", True)
        _ppl_clips   = s.get("_pipeline_clips", [])
        _ppl_sched   = s.get("_pipeline_sched", {})
        s["_pipeline_clips"] = []
        s["_pipeline_sched"] = {}
        print(f"[STEP5] want_dl={_ppl_want_dl}, clips数={len(_ppl_clips)}", flush=True)
        try:
            if _ppl_want_dl:
                _generate_pipeline(_ppl_clips, _ppl_sched)
            else:
                _run_pipeline(_ppl_clips, _ppl_sched)
        except BaseException as _e:
            import traceback as _tb
            _ename = type(_e).__name__
            _etb  = _tb.format_exc()
            print(f"[STEP5] 例外キャッチ [{_ename}]: {_e}", flush=True)
            print(_etb, flush=True)
            # RerunException を re-raise すると _pipeline_ran="done" が記録されず
            # ループに入るため、Streamlit 内部例外も含めてすべてキャプチャして続行する
            s["pipeline_error"] = f"[{_ename}] {_e}\n\n{_etb}"
        finally:
            # 同時実行スロットを解放
            _slot_id = s.pop("_job_slot_id", None)
            if _slot_id and _is_multi_user_mode():
                try:
                    from core.job_queue import release_slot
                    release_slot(_slot_id, success=not bool(s.get("pipeline_error")))
                except Exception:
                    pass
        s["_pipeline_ran"] = "done"  # 完走マーク
        print("[STEP5] パイプライン完了 → st.rerun()", flush=True)
        st.rerun()
        return

    render_stepbar(5)
    render_video_banner()

    # ── 使用量メーター ─────────────────────────────────────────────────
    if _is_multi_user_mode():
        _uid_s5 = s.get("user_id", "")
        if _uid_s5:
            try:
                from core.usage_tracker import get_plan_info
                _pi = get_plan_info(_uid_s5)
                if _pi.get("is_test"):
                    st.info("🔧 テストユーザー：使用制限なし", icon=None)
                else:
                    _rem  = _pi["remaining"]
                    _used = _pi["used"]
                    _lim  = _pi["limit"]
                    _bar_val = min(1.0, _used / _lim) if _lim > 0 else 0
                    if _rem == 0:
                        _badge = "🔴"
                        _rem_txt = "**あと 0 本**（上限到達）"
                    elif _rem <= 5:
                        _badge = "🟡"
                        _rem_txt = f"**あと {_rem} 本**"
                    else:
                        _badge = "🟢"
                        _rem_txt = f"**あと {_rem} 本**"
                    st.progress(
                        _bar_val,
                        text=f"{_badge} {_rem_txt}　（{_used} / {_lim} 本使用）｜ {_pi['label']}",
                    )
                    if _rem == 0:
                        _show_upgrade_ui(_uid_s5)
                        st.stop()
            except Exception:
                pass

    # ──── DEBUGパネル（確認後に削除） ────────────────────────────────────
    with st.expander("🐛 デバッグ情報（確認したら削除）", expanded=True):
        st.json({
            "_pipeline_pending": s.get("_pipeline_pending"),
            "_pipeline_ran":     s.get("_pipeline_ran"),
            "pipeline_error":    s.get("pipeline_error"),
            "generated_clips":   len(s.get("generated_clips", [])),
            "_pipeline_clips":   len(s.get("_pipeline_clips", [])),
        })
    # ────────────────────────────────────────────────────────────────────

    # ── ページ上部ナビゲーション ──
    _tnc5, _ = st.columns([1, 3])
    with _tnc5:
        if st.button("← 戻る", key="back5_top", disabled=s.running):
            s.step = 3 if s.get("_download_only_mode") else 4
            st.rerun()
    st.markdown("<hr style='margin:6px 0 14px;border:none;border-top:1px solid #f1f5f9;'>",
                unsafe_allow_html=True)

    # YouTube OAuth コールバック後のメッセージ表示
    if st.session_state.pop("_oauth_success", False):
        st.success("✅ YouTubeチャンネルを接続しました！")
    _err = st.session_state.pop("_oauth_error", None)
    if _err:
        st.error(f"YouTube認証エラー: {_err}")

    st.markdown("""
    <div style="padding:28px 40px 0;margin-left:-40px;margin-right:-40px;">
      <div style="font-size:20px;font-weight:800;color:#1e293b;margin-bottom:4px;">
        🚀 実行
      </div>
    </div>
    """, unsafe_allow_html=True)

    # ── 認証状態チェック ──
    _dl_only_mode = s.get("_download_only_mode", False)
    secret_ok  = (CREDS_DIR / "client_secret.json").exists()
    multi_mode = _is_multi_user_mode()
    token_ok   = False  # ダウンロードのみモードはFalseのまま; 通常モードは以下で上書き

    if _dl_only_mode:
        pass  # YouTube認証不要（ダウンロードのみモード）
    elif multi_mode:
        # ─── マルチユーザーモード: ユーザーごと YouTube 接続 ───────────
        yt_token   = s.get("yt_token")
        ch_name    = s.get("yt_channel_name", "")
        ch_thumb   = s.get("yt_channel_thumbnail", "")
        token_ok   = False

        if not secret_ok:
            # client_secret.json が Secrets に未設定（管理者向けエラー）
            st.error(
                "⚙️ YouTube API の設定が完了していません。"
                "管理者に連絡してください。"
            )
        else:
            # ── 接続状態カード ──────────────────────────────────
            if yt_token:
                from core.uploader import check_token_valid
                if check_token_valid(yt_token):
                    token_ok = True
                    # チャンネル情報カード
                    thumb_html = (
                        f'<img src="{ch_thumb}" width="36" height="36" '
                        f'style="border-radius:50%;object-fit:cover;margin-right:10px;vertical-align:middle;">'
                        if ch_thumb else
                        '<span style="font-size:28px;margin-right:10px;">📺</span>'
                    )
                    ch_display = ch_name if ch_name else "YouTubeチャンネル"
                    st.markdown(
                        f'<div style="display:flex;align-items:center;background:#f0fdf4;'
                        f'border:1px solid #86efac;border-radius:12px;padding:14px 18px;margin-bottom:12px;">'
                        f'{thumb_html}'
                        f'<div>'
                        f'<div style="font-weight:700;color:#166534;font-size:14px;">✅ 接続中</div>'
                        f'<div style="color:#15803d;font-size:13px;">{ch_display}</div>'
                        f'</div></div>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.warning("⚠️ 認証トークンが期限切れです。再接続してください。")

            # ── 接続 / 再接続ボタン ─────────────────────────────
            # 承認状態を確認（既存トークン保持者は自動承認扱い）
            _yt_approved  = bool(yt_token)
            _yt_req_email = ""
            if not _yt_approved and s.get("user_id"):
                from core.db import get_subscription as _get_sub_yt
                _yt_sub       = _get_sub_yt(s["user_id"])
                _yt_approved  = bool(_yt_sub.get("youtube_approved"))
                _yt_req_email = _yt_sub.get("youtube_request_email") or ""

            if not token_ok and not _yt_approved:
                # ── 未承認: 申請フォーム ────────────────────────────
                if _yt_req_email:
                    st.markdown(
                        '<div style="background:#f0fdf4;border:1px solid #86efac;border-radius:12px;'
                        'padding:14px 18px;margin-bottom:12px;">'
                        '<div style="font-weight:700;color:#166534;font-size:14px;">📩 申請受付済み</div>'
                        '<div style="color:#15803d;font-size:13px;margin-top:4px;">'
                        '担当者がアクセスを設定します。承認後にメールでお知らせします。</div>'
                        '</div>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        '<div style="background:#fefce8;border:1px solid #fde68a;border-radius:12px;'
                        'padding:14px 18px;margin-bottom:12px;">'
                        '<div style="font-weight:700;color:#92400e;font-size:14px;">📺 YouTubeチャンネルを接続する</div>'
                        '<div style="color:#78716c;font-size:12px;margin-top:6px;">'
                        'セキュリティのため、接続前に申請が必要です。'
                        'YouTubeへの接続に使用するGoogleアカウントのメールアドレスを入力してください。</div>'
                        '</div>',
                        unsafe_allow_html=True,
                    )
                    _req_input = st.text_input(
                        "Googleアカウントのメールアドレス",
                        placeholder="example@gmail.com",
                        key="_yt_req_email_input",
                    )
                    if st.button(
                        "📩 接続を申請する",
                        type="primary",
                        disabled=not (_req_input or "").strip(),
                        use_container_width=True,
                        key="_yt_req_submit",
                    ):
                        try:
                            from core.db import submit_youtube_request as _submit_req
                            _submit_req(s["user_id"], _req_input.strip())
                            st.success("✅ 申請しました！担当者が確認後、メールでお知らせします。")
                            st.rerun()
                        except Exception as _e:
                            st.error(f"申請エラー: {_e}")
            else:
                if not token_ok:
                    st.markdown(
                        '<div style="font-size:13px;color:#64748b;margin-bottom:10px;">'
                        '📺 自分の YouTube チャンネルを接続して動画を自動アップロードしましょう</div>',
                        unsafe_allow_html=True,
                    )

                col_conn, col_disc = st.columns([3, 1])
                with col_conn:
                    # ── 認証URL生成済みなら「クリックして認証」リンクを表示 ──
                    _pending_url = s.get("_yt_oauth_url")
                    if _pending_url:
                        import html as _html
                        import streamlit.components.v1 as _comp
                        _escaped_url = _html.escape(_pending_url, quote=True)
                        _comp.html(f"""
<a id="yt-oauth-btn" href="{_escaped_url}" data-url="{_escaped_url}"
   onclick="
     var url=this.getAttribute('data-url');
     try{{
       var a=window.top.document.createElement('a');
       a.href=url; a.target='_self';
       window.top.document.body.appendChild(a);
       a.click();
       setTimeout(function(){{try{{window.top.document.body.removeChild(a);}}catch(e){{}}}},500);
     }}catch(e){{
       window.open(url,'_blank');
     }}
     return false;"
   style="display:block;background:#7c3aed;color:#fff;
          padding:14px;border-radius:8px;font-weight:700;
          text-decoration:none;text-align:center;font-size:15px;
          cursor:pointer;font-family:sans-serif;box-sizing:border-box;width:100%;">
  &#9654;&#65039; クリックして Google 認証を完了する
</a>
""", height=60)
                        if st.button("↩ キャンセル", use_container_width=True, key="_yt_cancel"):
                            s.pop("_yt_oauth_url", None)
                            st.rerun()
                    else:
                        # ── 通常: 接続ボタン → URL生成 → リンク表示へ ──
                        btn_lbl = "🔄 YouTubeを再接続する" if token_ok else "▶️ YouTubeチャンネルを接続する"
                        if st.button(btn_lbl,
                                     type="secondary" if token_ok else "primary",
                                     use_container_width=True):
                            try:
                                import secrets as _sec
                                from core.uploader import get_auth_url as _gau
                                _user_id       = s.get("user_id", "anon")
                                _code_verifier = _sec.token_urlsafe(96)
                                _state         = _make_oauth_state(_user_id, _code_verifier)
                                _auth_url, _   = _gau(_get_app_url(), state=_state, code_verifier=_code_verifier)
                                s["_yt_oauth_url"] = _auth_url
                                st.rerun()
                            except Exception as e:
                                st.error(f"認証URL生成エラー: {e}")
                with col_disc:
                    if token_ok and st.button("🗑 接続解除", use_container_width=True):
                        if s.get("user_id"):
                            from core.db import delete_youtube_token
                            delete_youtube_token(s["user_id"])
                        s.pop("yt_token", None)
                        s.pop("yt_channel_name", None)
                        s.pop("yt_channel_id", None)
                        s.pop("yt_channel_thumbnail", None)
                        st.rerun()

    else:
        # ─── シングルユーザーモード: ファイルベース（既存） ────────────
        from core.uploader import check_auth as _check_auth
        token_file = (CREDS_DIR / "token.json").exists()
        token_ok   = _check_auth()
        scope_warn = token_file and not token_ok

        with st.expander("🔑 YouTube 認証", expanded=not (secret_ok and token_ok)):
            c1, c2 = st.columns(2)
            c1.metric("client_secret.json", "✅ 設定済み" if secret_ok else "❌ 未設定")
            _tok_label = "✅ 取得済み" if token_ok else ("⚠️ 再認証が必要" if scope_warn else "❌ 未取得")
            c2.metric("認証トークン", _tok_label)
            if scope_warn:
                st.warning("⚠️ 認証スコープが不足しています。「YouTubeにログイン」から再認証してください。")

            if not secret_ok:
                st.markdown("""
<div style="background:#fefce8;border:1px solid #fde68a;border-radius:12px;padding:16px 20px;margin:12px 0;">
<div style="font-weight:700;font-size:14px;color:#92400e;margin-bottom:8px;">📋 Google Cloud Console で取得した情報を入力してください</div>
<div style="font-size:12px;color:#78716c;">
  <a href="https://console.cloud.google.com/apis/library/youtube.googleapis.com" target="_blank"
     style="color:#1d4ed8;font-weight:600;">① YouTube Data API v3 を有効化</a>
  　→
  <a href="https://console.cloud.google.com/apis/credentials/oauthclient" target="_blank"
     style="color:#1d4ed8;font-weight:600;">② OAuth クライアントID（デスクトップアプリ）を作成</a>
  　→　③ 下欄に貼り付け
</div>
</div>
""", unsafe_allow_html=True)
                inp_id  = st.text_input("クライアント ID", key="oauth_client_id",
                                        placeholder="xxxxxxxxxx-xxxx.apps.googleusercontent.com")
                inp_sec = st.text_input("クライアント シークレット", type="password",
                                        key="oauth_client_secret", placeholder="GOCSPX-...")
                if st.button("💾 保存して認証へ進む", type="primary",
                             disabled=not (inp_id.strip() and inp_sec.strip())):
                    _secret_data = {
                        "installed": {
                            "client_id":     inp_id.strip(),
                            "client_secret": inp_sec.strip(),
                            "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
                            "token_uri":     "https://oauth2.googleapis.com/token",
                            "auth_provider_x509_cert_url":
                                "https://www.googleapis.com/oauth2/v1/certs",
                            "redirect_uris": ["http://localhost"],
                        }
                    }
                    CREDS_DIR.mkdir(exist_ok=True)
                    (CREDS_DIR / "client_secret.json").write_text(
                        json.dumps(_secret_data, indent=2), encoding="utf-8"
                    )
                    st.success("✅ 保存しました")
                    st.rerun()
                st.markdown('<div style="text-align:center;color:#9ca3af;font-size:12px;margin:8px 0;">または</div>',
                            unsafe_allow_html=True)
                uf = st.file_uploader("client_secret.json をアップロード", type="json",
                                      label_visibility="collapsed")
                if uf:
                    CREDS_DIR.mkdir(exist_ok=True)
                    (CREDS_DIR / "client_secret.json").write_bytes(uf.read())
                    st.success("✅ 保存しました")
                    st.rerun()

            if secret_ok and not token_ok:
                btn_label = "🔑 YouTubeに再ログイン（ブラウザが開きます）" if scope_warn else "🔑 YouTubeにログイン（ブラウザが開きます）"
                if st.button(btn_label, type="primary"):
                    if scope_warn:
                        (CREDS_DIR / "token.json").unlink(missing_ok=True)
                    with st.spinner("認証中..."):
                        try:
                            from core.uploader import get_youtube_service
                            get_youtube_service()
                            st.success("✅ 認証完了！")
                            st.rerun()
                        except Exception as e:
                            st.error(f"認証エラー: {e}")

            if token_ok:
                if st.button("🔄 トークンをリセット"):
                    (CREDS_DIR / "token.json").unlink(missing_ok=True)
                    st.rerun()

    # ── 実行サマリー ──
    st.markdown("")
    enabled_clips = [c for c in s.clips if c.get("enabled", True)]
    sched = s.schedule

    if _dl_only_mode:
        col1, col2 = st.columns(2)
        col1.metric("処理本数", f"{len(enabled_clips)} 本")
        col2.metric("元動画",   (s.video_info or {}).get("title", "—")[:20])
    else:
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("処理本数",   f"{len(enabled_clips)} 本")
        col2.metric("元動画",     (s.video_info or {}).get("title", "—")[:20])
        try:
            _dt_list = sched.get("daily_times") or ["09:00", "15:00", "21:00"]
            _start   = datetime.strptime(sched["start_date"], "%Y-%m-%d").date()
            _n       = len(enabled_clips)
            _first_dt = datetime.strptime(f"{_start} {_dt_list[0]}", "%Y-%m-%d %H:%M")
            _last_day = _start + timedelta(days=(_n - 1) // len(_dt_list))
            _last_hm  = _dt_list[(_n - 1) % len(_dt_list)]
            _last_dt  = datetime.strptime(f"{_last_day} {_last_hm}", "%Y-%m-%d %H:%M")
            col3.metric("初回投稿", _first_dt.strftime("%m/%d %H:%M"))
            col4.metric("最終投稿", _last_dt.strftime("%m/%d %H:%M"))
        except Exception:
            pass

    st.markdown("")

    # ── フェーズ判定 ──
    _in_phase_b = bool(s.get("generated_clips"))

    if not _in_phase_b:
        # ═══ Phase A: 実行前確認 ════════════════════════════════════

        if _dl_only_mode:
            # ── ダウンロードのみモード ────────────────────────────────────
            if s.get("pipeline_error"):
                _err_full = s["pipeline_error"]
                _err_head = _err_full.split("\n")[0]
                st.error(f"❌ {_err_head}")
                if "\n" in _err_full:
                    with st.expander("🔍 詳細エラー（クリックで展開）", expanded=False):
                        st.code(_err_full, language="")

            if not enabled_clips:
                st.warning("クリップを1本以上選択してください")

            _dl_col_back, _dl_col_run = st.columns([1, 3])
            with _dl_col_back:
                if st.button("← クリップ確認へ", key="back5_dlonly", disabled=s.running):
                    s["_download_only_mode"] = False
                    s.step = 3
                    st.rerun()
            with _dl_col_run:
                if st.button(
                    f"⬇️ {len(enabled_clips)} 本のクリップを生成",
                    type="primary", use_container_width=True,
                    disabled=(not enabled_clips or s.get("_pipeline_pending", False)),
                    key="btn_generate_dlonly",
                ):
                    print(f"[BTN] DLのみ: clips={len(enabled_clips)}", flush=True)
                    if _is_multi_user_mode():
                        from core.usage_tracker import check_can_generate
                        from core.job_queue import acquire_slot
                        _u_ok, _u_err = check_can_generate(s.get("user_id", ""), len(enabled_clips))
                        if not _u_ok:
                            s["pipeline_error"] = _u_err
                            st.rerun()
                        _slot = acquire_slot(s.get("user_id", ""))
                        if _slot is None:
                            s["pipeline_error"] = "ただいまサーバーが混雑しています。少し待ってから再度お試しください。"
                            st.rerun()
                        s["_job_slot_id"] = _slot
                    s["_pipeline_pending"] = True
                    s["_pipeline_ran"]     = None
                    s["_pipeline_want_dl"] = True
                    s["_pipeline_clips"]   = enabled_clips
                    s["_pipeline_sched"]   = dict(sched)
                    s["pipeline_error"]    = None
                    st.rerun()
            st.stop()

        # ── 通常モード ────────────────────────────────────────────────
        # cookies 期限切れ警告
        _prev_err = s.get("pipeline_error") or ""
        if "cookies が期限切れ" in _prev_err or "cookies を再エクスポート" in _prev_err:
            if _is_admin():
                st.warning(
                    "🍪 **cookies が期限切れです。** 管理パネルの「YouTube Cookies 管理」から更新してください。",
                    icon="⚠️",
                )
            else:
                st.warning("🍪 **cookies が期限切れです。** 管理者に連絡してください。", icon="⚠️")

        _want_dl  = st.checkbox(
            "📥 生成後にスマホ・PCへダウンロードする",
            key="want_download",
        )

        if _want_dl:
            import streamlit.components.v1 as _comp_hint
            _comp_hint.html("""
<div style="background:#f0fdf4;border:1px solid #86efac;border-radius:10px;
            padding:12px 16px;margin:4px 0 8px;font-family:sans-serif;">
  <div style="font-weight:700;color:#166534;font-size:13px;margin-bottom:6px;">
    📥 チェックON：生成 → ダウンロード → アップロードの順で進みます
  </div>
  <div style="font-size:12px;color:#14532d;line-height:2.0;">
    クリップが生成されたあと、ダウンロード画面が表示されます。<br>
    各クリップを端末に保存してから、YouTubeへアップロードするか選べます。<br>
    <b>💡 保存先フォルダを選びたい場合</b>（ブラウザ設定を変えるとダウンロードのたびに保存先を選べます）：<br>
    　📱 iPhone / iPad（Safari）：設定アプリ → Safari → ダウンロード → 好きな場所を選択<br>
    　🤖 Android（Chrome）：Chromeメニュー → 設定 → ダウンロード → ダウンロード先を変更<br>
    　💻 PC（Chrome）：Chrome設定 → ダウンロード →「ダウンロード前に保存場所を確認する」をオン
  </div>
</div>
""", height=230)
        else:
            import streamlit.components.v1 as _comp_hint
            _comp_hint.html("""
<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;
            padding:12px 16px;margin:4px 0 8px;font-family:sans-serif;">
  <div style="font-weight:700;color:#475569;font-size:13px;margin-bottom:6px;">
    ☁️ チェックOFF：生成 → 即YouTube予約投稿（ダウンロードなし）
  </div>
  <div style="font-size:12px;color:#64748b;line-height:1.7;">
    クリップは生成後そのままYouTubeにアップロードされます。<br>
    端末への保存は行われません。
  </div>
</div>
""", height=90)

        # ダウンロードしない場合はYouTube認証も必要
        all_ready = secret_ok and token_ok and len(enabled_clips) > 0
        gen_ready = len(enabled_clips) > 0

        if s.get("pipeline_error"):
            _err_full = s["pipeline_error"]
            _err_head = _err_full.split("\n")[0]
            st.error(f"❌ {_err_head}")
            if "\n" in _err_full:
                with st.expander("🔍 詳細エラー（クリックで展開）", expanded=False):
                    st.code(_err_full, language="")

        if not gen_ready:
            st.warning("クリップを1本以上選択してください")
        elif not _want_dl and not all_ready:
            st.warning("YouTube認証を完了してから実行してください")

        _can_run = gen_ready and (_want_dl or all_ready)
        _btn_label = (
            f"▶️  {len(enabled_clips)} 本のクリップを生成"
            if _want_dl
            else f"▶️  {len(enabled_clips)} 本のショートを作成・予約投稿"
        )

        col_back, col_run = st.columns([1, 3])
        with col_back:
            if st.button("← 戻る", key="back5", disabled=s.running):
                s.step = 4
                st.rerun()
        with col_run:
            if st.button(
                _btn_label,
                type="primary", use_container_width=True,
                disabled=(not _can_run or s.get("_pipeline_pending", False)),
                key="btn_generate",
            ):
                # フラグを立てて即rerun → パイプラインはstep5()先頭のメインフローで実行
                print(f"[BTN] クリック: want_dl={_want_dl}, clips={len(enabled_clips)}", flush=True)
                if _is_multi_user_mode():
                    from core.usage_tracker import check_can_generate
                    from core.job_queue import acquire_slot
                    _u_ok, _u_err = check_can_generate(s.get("user_id", ""), len(enabled_clips))
                    if not _u_ok:
                        s["pipeline_error"] = _u_err
                        st.rerun()
                    _slot = acquire_slot(s.get("user_id", ""))
                    if _slot is None:
                        s["pipeline_error"] = "ただいまサーバーが混雑しています。少し待ってから再度お試しください。"
                        st.rerun()
                    s["_job_slot_id"] = _slot
                s["_pipeline_pending"] = True
                s["_pipeline_ran"]     = None
                s["_pipeline_want_dl"] = _want_dl
                s["_pipeline_clips"]   = enabled_clips
                s["_pipeline_sched"]   = dict(sched)
                s["pipeline_error"]    = None
                st.rerun()

    else:
        # ═══ Phase B: ダウンロード確認画面 ════════════════════════
        all_ready = secret_ok and token_ok

        st.markdown("### 📥 クリップが生成されました")

        import streamlit.components.v1 as _comp_dl
        _comp_dl.html("""
<div style="background:#f0f9ff;border:1px solid #bae6fd;border-radius:10px;
            padding:12px 16px;margin-bottom:4px;font-family:sans-serif;">
  <div style="font-weight:700;color:#0369a1;font-size:13px;margin-bottom:6px;">
    💡 保存先フォルダを選びたい場合
  </div>
  <div style="font-size:12px;color:#0c4a6e;line-height:1.9;">
    ブラウザの設定を変えると、ダウンロードのたびに保存先を選べます。<br>
    <b>📱 iPhone / iPad（Safari）</b>：設定アプリ → Safari → ダウンロード → 好きな場所を選択<br>
    <b>🤖 Android（Chrome）</b>：Chromeメニュー → 設定 → ダウンロード → ダウンロード先を変更<br>
    <b>💻 PC（Chrome）</b>：Chrome設定 → ダウンロード →「ダウンロード前に保存場所を確認する」をオン
  </div>
</div>
""", height=115)

        _generated = s.get("generated_clips", [])
        for _clip in _generated:
            _p = Path(_clip["shorts_path"])
            if _p.exists():
                with open(_p, "rb") as _f:
                    st.download_button(
                        label=f"⬇️ {_clip['num']}本目をダウンロード: {_clip['title'][:30]}",
                        data=_f.read(),
                        file_name=f"short_{_clip['index']:02d}.mp4",
                        mime="video/mp4",
                        key=f"dl_{_clip['index']}",
                        use_container_width=True,
                    )
            else:
                st.warning(f"⚠️ {_clip['num']}本目のファイルが見つかりません: `{_p.name}`")

        st.markdown("")

        if _dl_only_mode:
            # ダウンロードのみ: アップロードボタンなし
            col_done_dl, _ = st.columns([1, 3])
            with col_done_dl:
                if st.button(
                    "✅ 完了",
                    use_container_width=True,
                    key="btn_dlonly_done",
                ):
                    s["_download_only_mode"] = False
                    s["generated_clips"] = []
                    s["raw_path"] = None
                    s.step = 3
                    st.rerun()
        else:
            if not all_ready:
                st.warning("YouTubeにアップロードするには認証を完了してください")

            col_upload, col_skip = st.columns([3, 1])
            with col_upload:
                if st.button(
                    "☁️ YouTubeにアップロード",
                    type="primary", use_container_width=True,
                    disabled=(not all_ready or s.running),
                    key="btn_upload",
                ):
                    s.running = True
                    _upload_pipeline()
                    s.running = False
                    st.rerun()
            with col_skip:
                if st.button(
                    "スキップ",
                    use_container_width=True,
                    disabled=s.running,
                    key="btn_skip",
                ):
                    for _c in s.get("generated_clips", []):
                        try:
                            Path(_c["shorts_path"]).unlink(missing_ok=True)
                        except Exception:
                            pass
                    _rp = s.get("raw_path")
                    if _rp:
                        try:
                            Path(_rp).unlink(missing_ok=True)
                        except Exception:
                            pass
                    s.results = [
                        {
                            "num":         _c["num"],
                            "title":       _c["title"],
                            "video_id":    None,
                            "publish_jst": _c["publish_jst"],
                            "status":      "ダウンロードのみ",
                        }
                        for _c in s.get("generated_clips", [])
                    ]
                    s["generated_clips"] = []
                    s["raw_path"]        = None
                    s["sched_pending"]   = None
                    st.rerun()

    # ── 結果表示 ──
    if s.results:
        st.markdown("")
        st.markdown("### 📊 処理結果")
        ok_count = sum(1 for r in s.results if r.get("video_id"))
        st.metric("成功", f"{ok_count} / {len(s.results)} 本")
        for r in s.results:
            icon = "✅" if r.get("video_id") else "❌"
            link = (
                f"[youtube.com/shorts/{r['video_id']}]"
                f"(https://youtube.com/shorts/{r['video_id']})"
                if r.get("video_id") else "—"
            )
            st.markdown(
                f"{icon} **{r['num']}本目** `{r['publish_jst']} JST`"
                f" — {r['title'][:40]}　{link}"
            )

        if st.button("🔁 最初からやり直す"):
            for k in ["step","video_info","clips","results","running",
                      "generated_clips","raw_path","sched_pending",
                      "pipeline_error","_pipeline_pending","_pipeline_ran",
                      "_pipeline_clips","_pipeline_sched","_pipeline_want_dl",
                      "_download_only_mode","_file_upload_mode"]:
                del st.session_state[k]
            st.rerun()


# ── エンコード中ローディングオーバーレイ HTML ───────────────
def _make_loading_html(clip_num: int, total_clips: int,
                       elapsed: float, remaining,
                       clip_title: str = "") -> str:
    """全画面ローディングオーバーレイ HTML を生成する。
    clip_num   : 現在処理中のクリップ番号（1始まり）
    total_clips: 全クリップ数
    elapsed    : 経過秒
    remaining  : 残り秒の推定値（None = 推定不可）
    clip_title : クリップタイトル
    """
    import math

    # 進捗 %（現在のクリップを0.5本分として計算）
    pct = min(99, int((clip_num - 1) / total_clips * 100))

    # 経過時間の文字列
    if elapsed >= 60:
        elapsed_str = f"{int(elapsed // 60)}分{int(elapsed % 60):02d}秒"
    else:
        elapsed_str = f"{int(elapsed)}秒"

    # 残り時間の文字列
    if remaining is None:
        rem_str = "推定中..."
    elif remaining >= 60:
        rem_str = f"約{int(remaining // 60)}分{int(remaining % 60):02d}秒"
    else:
        rem_str = f"約{int(remaining)}秒"

    # 円形プログレスリング（SVG strokeDashoffset）
    radius        = 52
    circumference = 2 * math.pi * radius
    dash_offset   = circumference * (1 - pct / 100)

    # クリップのドットインジケーター
    dots_html = "".join(
        '<div class="ld-dot ld-dot-done"></div>'   if j < clip_num - 1 else
        '<div class="ld-dot ld-dot-cur"></div>'    if j == clip_num - 1 else
        '<div class="ld-dot ld-dot-pend"></div>'
        for j in range(total_clips)
    )

    short_title = (clip_title[:32] + "…") if len(clip_title) > 32 else clip_title

    return f"""
<style>
@keyframes ld-bounce{{0%,100%{{transform:translateY(0) rotate(-1.5deg)}}50%{{transform:translateY(-14px) rotate(1.5deg)}}}}
@keyframes ld-float {{0%,100%{{transform:translateY(0);opacity:.6}}50%{{transform:translateY(-12px);opacity:1}}}}
@keyframes ld-pulse {{0%,100%{{opacity:.4;transform:scale(.8)}}50%{{opacity:1;transform:scale(1.2)}}}}
@keyframes ld-rotate{{from{{transform:rotate(0deg)}}to{{transform:rotate(360deg)}}}}
@keyframes ld-shimmer{{0%{{background-position:-200% center}}100%{{background-position:200% center}}}}
@keyframes ld-snip  {{0%,100%{{transform:rotate(0deg)}}40%{{transform:rotate(22deg) translate(1px,-1px)}}70%{{transform:rotate(-8deg)}}}}
@keyframes ld-phone {{0%,100%{{transform:rotate(-6deg)}}50%{{transform:rotate(6deg) translateY(-3px)}}}}
@keyframes ld-blink {{0%,88%,100%{{transform:scaleY(1)}}93%{{transform:scaleY(0.08)}}}}
@keyframes ld-star  {{0%,100%{{opacity:.4;transform:scale(.8) rotate(0deg)}}50%{{opacity:1;transform:scale(1.2) rotate(180deg)}}}}
@keyframes ld-act   {{0%,30%{{opacity:1;transform:translateY(0)}}35%,100%{{opacity:0;transform:translateY(-5px)}}}}
@keyframes ld-act2  {{0%,32%{{opacity:0}}33%,63%{{opacity:1;transform:translateY(0)}}68%,100%{{opacity:0;transform:translateY(-5px)}}}}
@keyframes ld-act3  {{0%,65%{{opacity:0}}66%,96%{{opacity:1;transform:translateY(0)}}100%{{opacity:0}}}}
.ld-overlay{{
  position:fixed!important;inset:0!important;
  width:100vw!important;height:100vh!important;
  background:radial-gradient(ellipse at 55% 20%,#3b0a0a 0%,#1a0a0a 40%,#0f172a 100%);
  z-index:999999!important;
  display:flex!important;flex-direction:column!important;
  align-items:center!important;justify-content:center!important;
  font-family:'Segoe UI',system-ui,sans-serif;overflow:hidden;
}}
.ld-glow{{position:absolute;border-radius:50%;filter:blur(60px);pointer-events:none;}}
.ld-chara{{
  animation:ld-bounce 2.2s ease-in-out infinite;
  filter:drop-shadow(0 12px 30px rgba(220,80,60,.55)) drop-shadow(0 2px 10px rgba(255,200,0,.3));
  margin-bottom:12px;
}}
.ld-title{{color:#fef3c7;font-size:1.3em;font-weight:700;margin-bottom:4px;letter-spacing:.4px;text-shadow:0 0 20px rgba(251,191,36,.6);}}
.ld-subtitle{{color:rgba(148,163,184,.8);font-size:.85em;margin-bottom:20px;max-width:320px;text-align:center;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}}
.ld-ring-wrap{{position:relative;width:136px;height:136px;margin-bottom:20px;}}
.ld-ring-wrap svg{{display:block;}}
.ld-ring-bg{{fill:none;stroke:rgba(255,255,255,.08);stroke-width:10;}}
.ld-ring-fg{{fill:none;stroke-width:10;stroke-linecap:round;stroke:url(#ld-rg);transform-origin:68px 68px;transform:rotate(-90deg);transition:stroke-dashoffset .6s ease;}}
.ld-ring-text{{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;color:white;}}
.ld-ring-pct{{font-size:1.9em;font-weight:700;line-height:1;}}
.ld-ring-sub{{font-size:.72em;color:rgba(255,255,255,.6);margin-top:2px;}}
.ld-stat{{text-align:center;background:rgba(255,255,255,.06);border:1px solid rgba(255,200,0,.2);border-radius:14px;padding:10px 28px;backdrop-filter:blur(8px);margin-bottom:18px;}}
.ld-stat-label{{color:rgba(148,163,184,.7);font-size:.68em;margin-bottom:3px;}}
.ld-stat-val{{font-size:1.12em;font-weight:700;background:linear-gradient(90deg,#fbbf24,#f87171,#fbbf24);background-size:200%;-webkit-background-clip:text;-webkit-text-fill-color:transparent;animation:ld-shimmer 2s linear infinite;}}
.ld-dots{{display:flex;gap:7px;align-items:center;margin-bottom:20px;}}
.ld-dot{{width:9px;height:9px;border-radius:50%;}}
.ld-dot-done{{background:#fbbf24;box-shadow:0 0 6px rgba(251,191,36,.7);}}
.ld-dot-cur{{background:#f87171;box-shadow:0 0 10px rgba(248,113,113,.9);animation:ld-pulse 1s ease-in-out infinite;}}
.ld-dot-pend{{background:rgba(255,255,255,.15);}}
.ld-act-wrap{{position:relative;height:22px;width:200px;text-align:center;margin-bottom:8px;}}
.ld-act-lbl{{position:absolute;left:0;right:0;font-size:12px;font-weight:700;border-radius:12px;padding:3px 12px;}}
.ld-cancel-btn{{
  margin-top:4px;padding:8px 28px;
  background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.2);
  border-radius:20px;color:rgba(255,255,255,.7);font-size:.82em;
  cursor:pointer;transition:all .2s;
}}
.ld-cancel-btn:hover{{background:rgba(255,255,255,.16);color:white;border-color:rgba(255,255,255,.4);}}
</style>

<div class="ld-overlay">
  <div class="ld-glow" style="width:500px;height:500px;background:rgba(180,30,10,.18);top:-150px;left:-100px;"></div>
  <div class="ld-glow" style="width:400px;height:400px;background:rgba(251,191,36,.1);bottom:-100px;right:-80px;"></div>
  <span style="position:absolute;top:9%;left:11%;font-size:1.1em;color:#fbbf24;animation:ld-star 2.8s ease-in-out infinite;">✦</span>
  <span style="position:absolute;top:14%;left:78%;font-size:.8em;color:#f87171;animation:ld-star 3.5s ease-in-out infinite .5s;">✦</span>
  <span style="position:absolute;top:22%;left:91%;font-size:1.3em;color:#fbbf24;animation:ld-star 3s ease-in-out infinite 1s;">★</span>
  <span style="position:absolute;top:72%;left:4%;font-size:1em;color:#f87171;animation:ld-star 4s ease-in-out infinite 1.2s;">✦</span>
  <span style="position:absolute;top:82%;left:93%;font-size:.9em;color:#fbbf24;animation:ld-float 3.8s ease-in-out infinite .3s;">✦</span>

  <div class="ld-chara">
    <svg viewBox="0 0 190 210" width="115" height="125" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <radialGradient id="lk" cx="40%" cy="35%"><stop offset="0%" stop-color="#FFE0B2"/><stop offset="100%" stop-color="#FFAB40"/></radialGradient>
        <radialGradient id="lkh" cx="35%" cy="25%"><stop offset="0%" stop-color="#455A64"/><stop offset="100%" stop-color="#1A237E"/></radialGradient>
        <radialGradient id="lkr" cx="35%" cy="20%"><stop offset="0%" stop-color="#EF5350"/><stop offset="100%" stop-color="#B71C1C"/></radialGradient>
        <radialGradient id="lkrl" cx="35%" cy="20%"><stop offset="0%" stop-color="#FF8A80"/><stop offset="100%" stop-color="#EF5350"/></radialGradient>
        <radialGradient id="lkey" cx="35%" cy="30%"><stop offset="0%" stop-color="#283593"/><stop offset="100%" stop-color="#0D47A1"/></radialGradient>
        <filter id="lks"><feDropShadow dx="2" dy="3" stdDeviation="3" flood-color="rgba(0,0,0,0.4)"/></filter>
      </defs>
      <ellipse cx="95" cy="202" rx="45" ry="7" fill="rgba(0,0,0,0.2)"/>
      <path d="M40 130 Q40 200 95 200 Q150 200 150 130 Q140 118 95 116 Q50 118 40 130Z" fill="url(#lkr)"/>
      <path d="M55 125 Q65 115 95 114 Q125 115 135 125 Q120 118 95 117 Q70 118 55 125Z" fill="rgba(255,255,255,0.18)"/>
      <path d="M72 120 Q95 132 118 120 Q110 138 95 140 Q80 138 72 120Z" fill="#FAFAFA"/>
      <line x1="95" y1="138" x2="95" y2="195" stroke="#FF8A80" stroke-width="2" stroke-dasharray="3,3"/>
      <path d="M40 130 Q40 200 60 200 Q50 170 48 140Z" fill="rgba(0,0,0,0.1)"/>
      <path d="M150 130 Q150 200 130 200 Q140 170 142 140Z" fill="rgba(0,0,0,0.1)"/>
      <path d="M55 128 Q28 140 16 162" stroke="url(#lkr)" stroke-width="18" stroke-linecap="round" fill="none"/>
      <path d="M55 128 Q28 140 16 162" stroke="url(#lkrl)" stroke-width="12" stroke-linecap="round" fill="none"/>
      <circle cx="13" cy="166" r="11" fill="url(#lk)" filter="url(#lks)"/>
      <g transform="translate(-4,158)" style="transform-origin:8px 8px;animation:ld-snip 1.8s ease-in-out infinite;">
        <path d="M8,8 L38,-8" stroke="#CFD8DC" stroke-width="4.5" stroke-linecap="round"/>
        <path d="M8,8 L38,-8" stroke="white" stroke-width="1.5" stroke-linecap="round" opacity="0.5"/>
        <path d="M8,8 L40,18" stroke="#CFD8DC" stroke-width="4.5" stroke-linecap="round"/>
        <path d="M8,8 L40,18" stroke="white" stroke-width="1.5" stroke-linecap="round" opacity="0.4"/>
        <circle cx="3" cy="2" r="8" fill="none" stroke="#F44336" stroke-width="3.5"/>
        <circle cx="3" cy="16" r="8" fill="none" stroke="#F44336" stroke-width="3.5"/>
        <circle cx="3" cy="2" r="4" fill="rgba(255,255,255,0.3)"/>
        <circle cx="3" cy="16" r="4" fill="rgba(255,255,255,0.3)"/>
      </g>
      <path d="M135 128 Q162 140 174 162" stroke="url(#lkr)" stroke-width="18" stroke-linecap="round" fill="none"/>
      <path d="M135 128 Q162 140 174 162" stroke="url(#lkrl)" stroke-width="12" stroke-linecap="round" fill="none"/>
      <circle cx="177" cy="166" r="11" fill="url(#lk)" filter="url(#lks)"/>
      <g transform="translate(164,144)" style="transform-origin:14px 22px;animation:ld-phone 2.4s ease-in-out infinite;">
        <rect x="0" y="0" width="28" height="44" rx="5" fill="#212121" stroke="#424242" stroke-width="1.5"/>
        <rect x="2" y="3" width="24" height="36" rx="3" fill="#FAFAFA"/>
        <rect x="4" y="8" width="20" height="16" rx="3" fill="#FF0000"/>
        <polygon points="11,12 11,20 21,16" fill="white"/>
        <text x="14" y="32" text-anchor="middle" font-size="5" fill="#333" font-weight="800" font-family="Arial,sans-serif">YouTube</text>
        <rect x="8" y="39" width="12" height="2" rx="1" fill="#9E9E9E"/>
      </g>
      <circle cx="95" cy="78" r="44" fill="url(#lk)" filter="url(#lks)"/>
      <ellipse cx="78" cy="60" rx="20" ry="25" fill="rgba(255,255,255,0.18)" transform="rotate(-15 78 60)"/>
      <path d="M48 62 Q95 54 142 62 Q138 74 95 75 Q52 74 48 62Z" fill="#263238" filter="url(#lks)"/>
      <path d="M50 63 Q95 56 140 63 Q136 70 95 71 Q54 70 50 63Z" fill="#37474F"/>
      <path d="M60 62 Q62 18 95 14 Q128 18 130 62 Q112 56 95 55 Q78 56 60 62Z" fill="url(#lkh)" filter="url(#lks)"/>
      <ellipse cx="80" cy="30" rx="14" ry="8" fill="rgba(255,255,255,0.13)" transform="rotate(-20 80 30)"/>
      <circle cx="95" cy="40" r="15" fill="#FDD835" stroke="#F9A825" stroke-width="2"/>
      <circle cx="95" cy="40" r="12" fill="#FFEE58"/>
      <circle cx="89" cy="35" r="3" fill="rgba(255,255,255,0.4)"/>
      <text x="95" y="46" text-anchor="middle" font-size="11" font-weight="900" fill="#1A237E" font-family="'Arial Black',sans-serif">AI</text>
      <text x="70" y="34" font-size="9" fill="#FFD700" opacity="0.9">★</text>
      <text x="112" y="28" font-size="7" fill="#FFD700" opacity="0.7">★</text>
      <g style="transform-origin:74px 80px;animation:ld-blink 4s ease-in-out infinite;">
        <ellipse cx="74" cy="80" rx="16" ry="18" fill="white"/>
        <circle cx="76" cy="82" r="12" fill="url(#lkey)"/>
        <circle cx="77" cy="83" r="7" fill="#0D0D2B"/>
        <circle cx="80" cy="77" r="4" fill="white"/>
        <circle cx="72" cy="85" r="2" fill="white" opacity="0.7"/>
      </g>
      <g style="transform-origin:116px 80px;animation:ld-blink 4s ease-in-out infinite;">
        <ellipse cx="116" cy="80" rx="16" ry="18" fill="white"/>
        <circle cx="118" cy="82" r="12" fill="url(#lkey)"/>
        <circle cx="119" cy="83" r="7" fill="#0D0D2B"/>
        <circle cx="122" cy="77" r="4" fill="white"/>
        <circle cx="114" cy="85" r="2" fill="white" opacity="0.7"/>
      </g>
      <text x="64" y="72" font-size="10" fill="#FFD700" style="animation:ld-star 1.5s infinite;">✦</text>
      <text x="122" y="70" font-size="8" fill="#FFD700" style="animation:ld-star 1.5s infinite .3s;">✦</text>
      <ellipse cx="60" cy="94" rx="14" ry="9" fill="#FF8A80" opacity="0.55"/>
      <ellipse cx="130" cy="94" rx="14" ry="9" fill="#FF8A80" opacity="0.55"/>
      <path d="M78 100 Q95 116 112 100" stroke="#C0392B" stroke-width="3.5" fill="none" stroke-linecap="round"/>
      <path d="M81 100 Q95 112 109 100" fill="white"/>
      <ellipse cx="95" cy="94" rx="4" ry="3" fill="#FFAB40" opacity="0.7"/>
    </svg>
  </div>

  <div class="ld-act-wrap">
    <span class="ld-act-lbl" style="color:#FDD835;background:rgba(253,216,53,.15);animation:ld-act 6s ease-in-out infinite;">✏️ 企画中...</span>
    <span class="ld-act-lbl" style="color:#40C4FF;background:rgba(64,196,255,.15);animation:ld-act2 6s ease-in-out infinite;">🎨 デザイン中...</span>
    <span class="ld-act-lbl" style="color:#FF8A80;background:rgba(255,138,128,.15);animation:ld-act3 6s ease-in-out infinite;">✂️ 編集中...</span>
  </div>

  <div class="ld-title">🎬 ショート制作中</div>
  <div class="ld-subtitle">{short_title}</div>

  <div class="ld-ring-wrap">
    <svg width="136" height="136" viewBox="0 0 136 136">
      <defs>
        <linearGradient id="ld-rg" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%"   stop-color="#fbbf24"/>
          <stop offset="50%"  stop-color="#f87171"/>
          <stop offset="100%" stop-color="#fbbf24"/>
        </linearGradient>
      </defs>
      <circle class="ld-ring-bg" cx="68" cy="68" r="{radius}"/>
      <circle class="ld-ring-fg" cx="68" cy="68" r="{radius}"
              stroke-dasharray="{circumference:.2f}"
              stroke-dashoffset="{dash_offset:.2f}"/>
    </svg>
    <div class="ld-ring-text">
      <div class="ld-ring-pct">{pct}%</div>
      <div class="ld-ring-sub">{clip_num}/{total_clips} 本目</div>
    </div>
  </div>

  <div class="ld-stat">
    <div class="ld-stat-label">⏳ 残り時間</div>
    <div class="ld-stat-val">{rem_str}</div>
  </div>

  <div class="ld-dots">{dots_html}</div>

  <button class="ld-cancel-btn" onclick="window.location.reload()">
    ↩ 前の画面に戻る（中断）
  </button>
</div>
"""


# ── パイプライン実行 ──────────────────────────────────────
def _run_pipeline(clips: list, sched: dict):
    from core.downloader import download_video
    from core.processor  import create_shorts
    from core.uploader   import upload_shorts

    video_info   = s.video_info
    _dt_list     = sched.get("daily_times") or ["09:00", "15:00", "21:00"]
    _start_date  = datetime.strptime(sched["start_date"], "%Y-%m-%d").date()
    category     = sched.get("category_id", "22")

    def _clip_jst(i: int) -> datetime:
        _day = _start_date + timedelta(days=i // len(_dt_list))
        _hm  = _dt_list[i % len(_dt_list)]
        return datetime.strptime(f"{_day} {_hm}", "%Y-%m-%d %H:%M")

    # ── マルチユーザー: YouTube トークン取得＆リフレッシュ ──
    _yt_token  = None
    _user_id   = None
    if _is_multi_user_mode():
        _user_id  = s.get("user_id")
        _yt_token = s.get("yt_token")
        if not _yt_token:
            s["pipeline_error"] = "YouTubeチャンネルが接続されていません。認証セクションで接続してください。"
            return
        # 事前にトークンをリフレッシュ（1時間の有効期限対策）
        try:
            from core.uploader import refresh_token_if_needed
            from core.db import save_youtube_token
            _yt_token = refresh_token_if_needed(_yt_token)
            s["yt_token"] = _yt_token
            if _user_id:
                save_youtube_token(_user_id, _yt_token)
        except Exception as _e:
            s["pipeline_error"] = f"YouTubeトークンのリフレッシュに失敗しました。再接続してください。({_e})"
            return

    results = []
    OUTPUT_DIR.mkdir(exist_ok=True)

    # ユーザーIDごとのサブディレクトリ（複数ユーザー混在防止）
    _uid_slug = str(_user_id) if _user_id else "local"
    _user_out = OUTPUT_DIR / _uid_slug
    _user_out.mkdir(parents=True, exist_ok=True)

    _dl_ok   = False
    raw_path = None
    with st.status("処理中...", expanded=True) as status:
        prog = st.progress(0, text="準備中...")

        # ① 元動画を取得（ファイルアップロード済みならダウンロードをスキップ）
        if s.get("_file_upload_mode") and s.get("raw_path"):
            raw_path = Path(s["raw_path"])
            if raw_path.exists():
                st.write(f"✅ アップロード済みファイルを使用: `{raw_path.name}`")
                _dl_ok = True
            else:
                s["pipeline_error"] = "アップロードファイルが見つかりません。もう一度ファイルを選択してください。"
                status.update(label="ファイルエラー", state="error")
        else:
            st.write(f"⬇️ 元動画をダウンロード中: `{video_info['url'][:60]}`")
            import threading as _dl_th
            import time as _dl_time
            _dl_res: list = [None, None]  # [path, error]
            _dl_ev = _dl_th.Event()

            def _dl_worker(_url=video_info["url"], _out=_user_out / "raw",
                           _res=_dl_res, _ev=_dl_ev):
                try:
                    _res[0] = download_video(_url, _out)
                except Exception as _de:
                    _res[1] = _de
                finally:
                    _ev.set()

            _dl_th.Thread(target=_dl_worker, daemon=True).start()
            _dl_ph = st.empty()
            _dl_t0       = _dl_time.time()
            _output_raw  = _user_out / "raw"
            _output_raw.mkdir(parents=True, exist_ok=True)
            _last_size      = 0
            _last_size_time = _dl_t0
            _prev_size      = 0
            _prev_speed_t   = _dl_t0
            _speed_mbps     = 0.0
            _STALL_TIMEOUT  = 300   # 5分間サイズ変化なし = ハング
            _ABS_TIMEOUT    = 7200  # 2時間の絶対上限

            while not _dl_ev.wait(timeout=5.0):
                _now = _dl_time.time()
                _el  = _now - _dl_t0

                # 絶対タイムアウト（2時間）
                if _el > _ABS_TIMEOUT:
                    _dl_res[1] = RuntimeError(
                        "2時間を超えたため中断しました。\n"
                        "動画が非常に長いか、通信環境が極めて不安定です。"
                    )
                    _dl_ev.set()
                    break

                # .part ファイル含む全ファイルサイズを監視
                _cur_size = 0
                if _output_raw.exists():
                    for _f in _output_raw.iterdir():
                        try:
                            _cur_size += _f.stat().st_size
                        except OSError:
                            pass

                # 速度計算（5秒ごと更新）
                _dt = _now - _prev_speed_t
                if _dt >= 5.0:
                    _speed_mbps   = (_cur_size - _prev_size) / _dt / 1_048_576
                    _prev_size    = _cur_size
                    _prev_speed_t = _now

                # 進捗があればタイマーリセット
                if _cur_size > _last_size:
                    _last_size      = _cur_size
                    _last_size_time = _now

                _stall_sec = _now - _last_size_time

                # 5分間停止 = 本当にハング
                if _stall_sec > _STALL_TIMEOUT and _last_size > 0:
                    _dl_res[1] = RuntimeError(
                        "ダウンロードが5分間停止しました。\n"
                        "通信が切断されたか、サーバーが応答していません。\n"
                        "接続を確認してもう一度お試しください。"
                    )
                    _dl_ev.set()
                    break

                # UI 更新
                _size_mb = _cur_size // 1_048_576
                if _stall_sec > 30 and _last_size > 0:
                    # 進捗が止まってきた
                    _detail = f"⚠️ {int(_stall_sec//60)}分{int(_stall_sec%60)}秒間進捗なし... {_size_mb}MB 取得済み"
                    _note   = "📡 通信が不安定かもしれません。そのままお待ちください"
                elif _cur_size > 0 and _speed_mbps > 0.01:
                    # 正常にダウンロード中
                    _detail = f"取得中 {_size_mb}MB 完了（{_speed_mbps:.1f} MB/s）"
                    _note   = _get_dl_context_note(_el, _speed_mbps)
                else:
                    # まだ接続中 or 開始直後
                    _detail = f"YouTube に接続中... {int(_el)}秒"
                    _note   = _get_wait_note(_el)

                _show_stage_html(_dl_ph, _make_analysis_stage_html(
                    "動画のおいしいところ分析中",
                    _detail,
                    note=_note,
                ), height=420)

            _dl_ph.empty()

            if _dl_res[1]:
                err_msg = str(_dl_res[1])
                hint = ""
                if "403" in err_msg or "IP制限" in err_msg:
                    _ck = CREDS_DIR / "cookies.txt"
                    _has_cookies = _ck.exists() and _ck.stat().st_size > 0
                    if _has_cookies:
                        hint = (
                            "\n\n⚠️ cookies は設定済みですが 403 エラーが発生しています。\n"
                            "cookies が期限切れの可能性があります。\n"
                            "管理パネルの「🍪 YouTube Cookies 管理」から cookies を再エクスポートしてください。"
                        )
                    else:
                        hint = (
                            "\n\n💡 cookies が設定されていません。\n"
                            "管理パネルの「🍪 YouTube Cookies 管理」から cookies を設定してください。"
                        )
                elif "タイムアウト" in err_msg:
                    hint = "\n\n⏱ 動画の長さを短くするか、時間をおいて再試行してください。"
                s["pipeline_error"] = (
                    "ご不便をおかけして申し訳ありません🙇\n\n"
                    f"ダウンロードに失敗しました。\n{err_msg}{hint}"
                )
                status.update(label="ダウンロード失敗", state="error")
            else:
                raw_path = _dl_res[0]
                st.write(f"✅ ダウンロード完了: `{raw_path.name}`")
                _dl_ok = True

        # ② 各クリップを処理（ダウンロード成功時のみ）
        import time as _time
        import threading as _threading
        _clip_times_run = []  # 各クリップの実処理秒数（残り時間推定用）

        for i, clip in (enumerate(clips) if _dl_ok else ()):
            pct  = i / len(clips)  # 開始時点の進捗
            title = clip["title"] or f"ショート{clip['index']}"
            hashtags = clip.get("hashtags", "#Shorts")
            _rv_links = sched.get("related_video_urls") or []
            _rv_block = ("\n\n▼ 関連動画\n" + "\n".join(_rv_links)) if (_rv_links and sched.get("related_add_desc", True)) else ""
            description = (clip.get("description","").strip() + "\n\n" + hashtags + _rv_block).strip()
            tags = [t.lstrip("#") for t in hashtags.split() if t.startswith("#")]

            jst_dt = _clip_jst(i)
            utc_dt = (jst_dt - timedelta(hours=9)).replace(tzinfo=timezone.utc)

            if _clip_times_run:
                _avg_sec_r = sum(_clip_times_run) / len(_clip_times_run)
                _rem_sec_r = _avg_sec_r * (len(clips) - i)
                _rem_str_r = f"残り約{int(_rem_sec_r//60)}分{int(_rem_sec_r%60)}秒"
            else:
                _avg_sec_r = None
                _rem_str_r = "推定中..."

            prog.progress(pct, text=f"[{i+1}/{len(clips)}] エンコード中... {_rem_str_r}")

            try:
                # デザイン設定を取得（クリップごとランダムモード対応）
                _rand = st.session_state.get("rand_mode", False)
                _designs = st.session_state.get("clip_designs", {})
                _cidx = clip.get("index", i)
                if _rand and _cidx in _designs:
                    _d = _designs[_cidx]
                    _theme_key   = _d["theme"]
                    _size_key    = _d["size"]
                    _pattern_key = _d["pattern"]
                else:
                    _theme_key   = st.session_state.get("title_theme",   "purple")
                    _size_key    = st.session_state.get("title_size",    "large")
                    _pattern_key = st.session_state.get("title_pattern", "none")

                _bottom_img = clip.get("bottom_image")
                _bottom_path = Path(_bottom_img) if _bottom_img else None

                # 変換（スレッドで実行し残り時間をライブ表示）
                st.write(f"✂️ **{i+1}本目: 切り出し変換中** "
                         f"({int(clip['start'])}s → {int(clip['end'])}s)")
                shorts_path = _user_out / "shorts" / f"short_{clip['index']:02d}.mp4"

                _cs_result_r = [None]
                _cs_done_r   = _threading.Event()
                _cs_kw_r     = dict(
                    input_path=raw_path, output_path=shorts_path,
                    max_duration=int(clip["end"] - clip["start"]),
                    start_sec=int(clip["start"]), title=title,
                    theme_key=_theme_key, size_key=_size_key, pattern_key=_pattern_key,
                    themes=TITLE_THEMES, sizes=TITLE_SIZES or {},
                    bottom_image_path=_bottom_path,
                    catchphrase=clip.get("catchphrase", ""),
                )
                def _cs_worker_r(kw=_cs_kw_r, res=_cs_result_r, done=_cs_done_r):
                    try:
                        create_shorts(**kw)
                    except Exception as _ex:
                        res[0] = _ex
                    finally:
                        done.set()
                _cs_thread_r = _threading.Thread(target=_cs_worker_r, daemon=True)
                _cs_thread_r.start()
                _time_ph_r = st.empty()
                _clip_t0_r = _time.time()
                while not _cs_done_r.wait(timeout=1.0):
                    _el = _time.time() - _clip_t0_r
                    if _avg_sec_r is not None:
                        _tr_r = max(0.0, _avg_sec_r - _el) + _avg_sec_r * (len(clips) - i - 1)
                        _rem_r = _tr_r
                    else:
                        _rem_r = None
                    _show_stage_html(
                        _time_ph_r,
                        _make_loading_html(i + 1, len(clips), _el, _rem_r, title),
                        height=600,
                    )
                _cs_thread_r.join()
                _elapsed_r = _time.time() - _clip_t0_r
                _time_ph_r.empty()
                if _cs_result_r[0] is not None:
                    raise _cs_result_r[0]
                _clip_times_run.append(_elapsed_r)

                # アップロード
                st.write(f"☁️ **{i+1}本目: アップロード中** "
                         f"— 予約: `{jst_dt.strftime('%Y/%m/%d %H:%M')} JST`")
                video_id = upload_shorts(
                    shorts_path, title, description, tags, utc_dt, category,
                    playlist_id=sched.get("playlist_id"),
                    made_for_kids=bool(sched.get("made_for_kids", False)),
                    age_restricted=bool(sched.get("age_restricted", False)),
                    token_json=_yt_token,  # マルチユーザー: per-user token
                )

                # 関連動画コメント投稿
                if sched.get("post_related_comment") and sched.get("related_video_urls"):
                    try:
                        from core.uploader import post_comment as _post_comment
                        _rv_text = "▼ 関連動画\n" + "\n".join(sched["related_video_urls"])
                        _post_comment(video_id, _rv_text, token_json=_yt_token)
                        st.write("💬 関連動画リンクをコメントに投稿しました")
                    except Exception as _ce:
                        st.warning(f"⚠️ コメント投稿に失敗しました: {_ce}")

                results.append({
                    "num": i+1, "title": title, "video_id": video_id,
                    "publish_jst": jst_dt.strftime("%Y/%m/%d %H:%M"), "status": "✅"
                })
                st.write(
                    f"✅ **完了** → "
                    f"[youtube.com/shorts/{video_id}](https://youtube.com/shorts/{video_id})"
                )

                # アップロード完了後にローカルの shorts ファイルを即時削除
                try:
                    shorts_path.unlink(missing_ok=True)
                except Exception:
                    pass

            except Exception as e:
                results.append({
                    "num": i+1, "title": title, "video_id": None,
                    "publish_jst": jst_dt.strftime("%Y/%m/%d %H:%M"), "status": f"❌ {e}"
                })
                st.write(f"❌ **エラー [{i+1}本目]**: {e}")

        if _dl_ok:
            # 全クリップ処理後に raw 動画（元ダウンロードファイル）を削除
            try:
                raw_path.unlink(missing_ok=True)
            except Exception:
                pass

            prog.progress(1.0, text="全処理完了！")
            ok = sum(1 for r in results if r["video_id"])
            status.update(
                label=f"🎉 完了！{ok}/{len(results)} 本の予約投稿が完了しました",
                state="complete",
            )

            # マルチユーザー: 使用量を更新（テストユーザーはスキップ）
            if _is_multi_user_mode() and _user_id and ok > 0:
                try:
                    from core.usage_tracker import increment_usage
                    increment_usage(_user_id, ok)
                except Exception:
                    pass

    s.results = results


# ── 生成パイプライン（ダウンロード＋変換のみ、アップロードなし）──────────
def _generate_pipeline(clips: list, sched: dict):
    print(f"[PIPELINE] _generate_pipeline 開始: clips={len(clips)}", flush=True)
    from core.downloader import download_video
    from core.processor  import create_shorts

    s["pipeline_error"] = None  # 前回エラーをクリア

    video_info   = s.video_info
    _dt_list     = sched.get("daily_times") or ["09:00", "15:00", "21:00"]
    _start_date  = datetime.strptime(sched["start_date"], "%Y-%m-%d").date()

    def _clip_jst(i: int) -> datetime:
        _day = _start_date + timedelta(days=i // len(_dt_list))
        _hm  = _dt_list[i % len(_dt_list)]
        return datetime.strptime(f"{_day} {_hm}", "%Y-%m-%d %H:%M")

    _user_id  = s.get("user_id") if _is_multi_user_mode() else None
    _uid_slug = str(_user_id) if _user_id else "local"
    OUTPUT_DIR.mkdir(exist_ok=True)
    _user_out = OUTPUT_DIR / _uid_slug
    _user_out.mkdir(parents=True, exist_ok=True)

    generated = []
    _dl_ok    = False  # ダウンロード成功フラグ（with内でreturnしないための制御変数）

    with st.status("処理中...", expanded=True) as status:
        prog = st.progress(0, text="準備中...")

        # ① 元動画を取得（ファイルアップロード済みならダウンロードをスキップ）
        if s.get("_file_upload_mode") and s.get("raw_path"):
            raw_path = Path(s["raw_path"])
            if raw_path.exists():
                st.write(f"✅ アップロード済みファイルを使用: `{raw_path.name}`")
                print(f"[PIPELINE] ファイルアップロードモード: {raw_path}", flush=True)
                _dl_ok = True
            else:
                s["pipeline_error"] = "アップロードファイルが見つかりません。もう一度ファイルを選択してください。"
                status.update(label="ファイルエラー", state="error")
        else:
            st.write(f"⬇️ 元動画をダウンロード中: `{video_info['url'][:60]}`")
            try:
                raw_path = download_video(video_info["url"], _user_out / "raw")
                print(f"[PIPELINE] ダウンロード完了: {raw_path}", flush=True)
                st.write(f"✅ ダウンロード完了: `{raw_path.name}`")
                _dl_ok = True
            except Exception as e:
                print(f"[PIPELINE] ダウンロード失敗: {e}", flush=True)
                err_msg = str(e)
                hint = ""
                if "403" in err_msg or "IP制限" in err_msg:
                    _ck = CREDS_DIR / "cookies.txt"
                    if _ck.exists() and _ck.stat().st_size > 0:
                        hint = "\n\n⚠️ cookies は設定済みですが 403 エラーが発生しています。cookies が期限切れの可能性があります。管理パネルの「🍪 YouTube Cookies 管理」から更新してください。"
                    else:
                        hint = "\n\n💡 cookies が設定されていません。管理パネルの「🍪 YouTube Cookies 管理」から cookies を設定してください。"
                s["pipeline_error"] = f"ダウンロード失敗: {err_msg}{hint}"
                status.update(label="ダウンロード失敗", state="error")
                # ← return しない：with ブロックを自然に終了させる

        if _dl_ok:
            s["raw_path"] = str(raw_path)

            import time as _time
            import threading as _threading
            _clip_times = []  # 各クリップの実処理秒数を記録（残り時間推定用）

            for i, clip in enumerate(clips):
                pct   = i / len(clips)   # 開始時点の進捗（完了したクリップ数ベース）
                title = clip["title"] or f"ショート{clip['index']}"
                hashtags    = clip.get("hashtags", "#Shorts")
                _rv_links2  = sched.get("related_video_urls") or []
                _rv_block2  = ("\n\n▼ 関連動画\n" + "\n".join(_rv_links2)) if (_rv_links2 and sched.get("related_add_desc", True)) else ""
                description = (clip.get("description", "").strip() + "\n\n" + hashtags + _rv_block2).strip()
                tags        = [t.lstrip("#") for t in hashtags.split() if t.startswith("#")]

                jst_dt = _clip_jst(i)
                utc_dt = (jst_dt - timedelta(hours=9)).replace(tzinfo=timezone.utc)

                # 残り時間の初期推定
                if _clip_times:
                    _avg_sec = sum(_clip_times) / len(_clip_times)
                    _rem_sec = _avg_sec * (len(clips) - i)
                    _rem_str = f"残り約{int(_rem_sec//60)}分{int(_rem_sec%60)}秒"
                else:
                    _avg_sec = None
                    _rem_str = "推定中..."

                prog.progress(pct, text=f"[{i+1}/{len(clips)}] エンコード中... {_rem_str}")

                print(f"[PIPELINE] クリップ {i+1}/{len(clips)} 変換開始: {title[:40]}", flush=True)
                try:
                    _rand    = st.session_state.get("rand_mode", False)
                    _designs = st.session_state.get("clip_designs", {})
                    _cidx    = clip.get("index", i)
                    if _rand and _cidx in _designs:
                        _d           = _designs[_cidx]
                        _theme_key   = _d["theme"]
                        _size_key    = _d["size"]
                        _pattern_key = _d["pattern"]
                    else:
                        _theme_key   = st.session_state.get("title_theme",   "purple")
                        _size_key    = st.session_state.get("title_size",    "large")
                        _pattern_key = st.session_state.get("title_pattern", "none")

                    _bottom_img  = clip.get("bottom_image")
                    _bottom_path = Path(_bottom_img) if _bottom_img else None

                    st.write(f"✂️ **{i+1}本目: 切り出し変換中** "
                             f"({int(clip['start'])}s → {int(clip['end'])}s)")
                    shorts_path = _user_out / "shorts" / f"short_{clip['index']:02d}.mp4"

                    # ── create_shorts をスレッドで実行し、メインスレッドで1秒ごとに残り時間を表示 ──
                    _cs_result   = [None]   # None=成功, Exception=失敗
                    _cs_done     = _threading.Event()
                    _cs_kwargs   = dict(
                        input_path=raw_path,
                        output_path=shorts_path,
                        max_duration=int(clip["end"] - clip["start"]),
                        start_sec=int(clip["start"]),
                        title=title,
                        theme_key=_theme_key,
                        size_key=_size_key,
                        pattern_key=_pattern_key,
                        themes=TITLE_THEMES,
                        sizes=TITLE_SIZES or {},
                        bottom_image_path=_bottom_path,
                        catchphrase=clip.get("catchphrase", ""),
                    )

                    def _cs_worker(kw=_cs_kwargs, res=_cs_result, done=_cs_done):
                        try:
                            create_shorts(**kw)
                        except Exception as _ex:
                            res[0] = _ex
                        finally:
                            done.set()

                    _cs_thread = _threading.Thread(target=_cs_worker, daemon=True)
                    _cs_thread.start()

                    # 1秒ごとに残り時間を更新
                    _time_ph  = st.empty()
                    _clip_t0  = _time.time()
                    while not _cs_done.wait(timeout=1.0):
                        _elapsed = _time.time() - _clip_t0
                        if _avg_sec is not None:
                            _this_rem = max(0.0, _avg_sec - _elapsed)
                            _rest_rem = _avg_sec * (len(clips) - i - 1)
                            _total    = _this_rem + _rest_rem
                            _rem_arg  = _total
                        else:
                            _rem_arg  = None
                        _show_stage_html(
                            _time_ph,
                            _make_loading_html(i + 1, len(clips), _elapsed, _rem_arg, title),
                            height=600,
                        )
                    _cs_thread.join()
                    _elapsed_final = _time.time() - _clip_t0
                    _time_ph.empty()

                    if _cs_result[0] is not None:
                        raise _cs_result[0]

                    _clip_times.append(_elapsed_final)

                    generated.append({
                        "num":         i + 1,
                        "index":       clip["index"],
                        "title":       title,
                        "shorts_path": str(shorts_path),
                        "description": description,
                        "tags":        tags,
                        "jst_dt":      jst_dt.isoformat(),
                        "utc_dt":      utc_dt.isoformat(),
                        "publish_jst": jst_dt.strftime("%Y/%m/%d %H:%M"),
                    })
                    prog.progress((i+1)/len(clips),
                                  text=f"[{i+1}/{len(clips)}] ✅ 完了 ({int(_elapsed_final)}秒)")
                    st.write(f"✅ **{i+1}本目: 変換完了** ({int(_elapsed_final)}秒)")

                except Exception as e:
                    import traceback as _tb
                    _clip_err = f"[{i+1}本目] {type(e).__name__}: {e}"
                    st.write(f"❌ **エラー [{i+1}本目]**: {e}")
                    print(f"[PIPELINE] クリップ変換エラー: {_clip_err}", flush=True)
                    print(_tb.format_exc(), flush=True)
                    s['_clip_errors'] = s.get('_clip_errors', []) + [_clip_err]

            if not generated:
                try:
                    raw_path.unlink(missing_ok=True)
                except Exception:
                    pass
                s["raw_path"] = None
                _errs = s.get('_clip_errors', [])
                _err_detail = ("\n\n詳細:\n" + "\n".join(_errs[:3])) if _errs else ""
                s["pipeline_error"] = "すべてのクリップの変換に失敗しました" + _err_detail
                status.update(label="変換失敗", state="error")
                # ← return しない：with ブロックを自然に終了させる
            else:
                prog.progress(1.0, text="変換完了！")
                status.update(
                    label=f"✅ {len(generated)} 本の変換完了。ダウンロードまたはアップロードしてください。",
                    state="complete",
                )

    # with ブロックの外：正常完了時のみセッション状態を更新
    if _dl_ok and generated:
        # ダウンロード成功クリップにフラグを付けてカウント（後でアップロードしても二重カウントしない）
        for _c in generated:
            _c["already_counted"] = True
        s["generated_clips"] = generated
        s["sched_pending"]   = dict(sched)
        if _is_multi_user_mode() and _user_id:
            try:
                from core.usage_tracker import increment_usage
                increment_usage(_user_id, len(generated))
            except Exception:
                pass


# ── アップロードパイプライン（生成済みファイルをYouTubeへ投稿）──────────
def _upload_pipeline():
    from core.uploader import upload_shorts

    generated = s.get("generated_clips", [])
    sched     = s.get("sched_pending", {})
    category  = sched.get("category_id", "22")

    _yt_token = None
    _user_id  = None
    if _is_multi_user_mode():
        _user_id  = s.get("user_id")
        _yt_token = s.get("yt_token")
        if not _yt_token:
            st.error("YouTubeチャンネルが接続されていません。認証セクションで接続してください。")
            return
        try:
            from core.uploader import refresh_token_if_needed
            from core.db import save_youtube_token
            _yt_token = refresh_token_if_needed(_yt_token)
            s["yt_token"] = _yt_token
            if _user_id:
                save_youtube_token(_user_id, _yt_token)
        except Exception as _e:
            st.error(f"YouTubeトークンのリフレッシュに失敗しました。再接続してください。({_e})")
            return

    results = []

    with st.status("アップロード中...", expanded=True) as status:
        prog = st.progress(0, text="アップロード準備中...")

        for i, clip in enumerate(generated):
            pct         = (i + 1) / len(generated)
            title       = clip["title"]
            shorts_path = Path(clip["shorts_path"])
            description = clip["description"]
            tags        = clip["tags"]
            publish_jst = clip["publish_jst"]

            try:
                utc_dt = datetime.fromisoformat(clip["utc_dt"])
            except Exception:
                utc_dt = None

            prog.progress(pct, text=f"[{i+1}/{len(generated)}] {title[:40]}")
            st.write(f"☁️ **{i+1}本目: アップロード中** — 予約: `{publish_jst} JST`")

            try:
                video_id = upload_shorts(
                    shorts_path, title, description, tags, utc_dt, category,
                    playlist_id=sched.get("playlist_id"),
                    made_for_kids=bool(sched.get("made_for_kids", False)),
                    age_restricted=bool(sched.get("age_restricted", False)),
                    token_json=_yt_token,
                )

                # 関連動画コメント投稿
                if sched.get("post_related_comment") and sched.get("related_video_urls"):
                    try:
                        from core.uploader import post_comment as _post_comment2
                        _rv_text2 = "▼ 関連動画\n" + "\n".join(sched["related_video_urls"])
                        _post_comment2(video_id, _rv_text2, token_json=_yt_token)
                        st.write("💬 関連動画リンクをコメントに投稿しました")
                    except Exception as _ce2:
                        st.warning(f"⚠️ コメント投稿に失敗しました: {_ce2}")

                results.append({
                    "num":         clip["num"],
                    "title":       title,
                    "video_id":    video_id,
                    "publish_jst": publish_jst,
                    "status":      "✅",
                })
                st.write(
                    f"✅ **完了** → "
                    f"[youtube.com/shorts/{video_id}](https://youtube.com/shorts/{video_id})"
                )
            except Exception as e:
                results.append({
                    "num":         clip["num"],
                    "title":       title,
                    "video_id":    None,
                    "publish_jst": publish_jst,
                    "status":      f"❌ {e}",
                })
                st.write(f"❌ **エラー [{i+1}本目]**: {e}")
            finally:
                try:
                    shorts_path.unlink(missing_ok=True)
                except Exception:
                    pass

        _rp = s.get("raw_path")
        if _rp:
            try:
                Path(_rp).unlink(missing_ok=True)
            except Exception:
                pass

        ok = sum(1 for r in results if r.get("video_id"))
        prog.progress(1.0, text="アップロード完了！")
        status.update(
            label=f"🎉 完了！{ok}/{len(results)} 本の予約投稿が完了しました",
            state="complete",
        )

        # ダウンロード時に未カウントの成功クリップのみカウント（二重カウント防止）
        if _is_multi_user_mode() and _user_id:
            try:
                from core.usage_tracker import increment_usage
                _uncounted = sum(
                    1 for r, c in zip(results, generated)
                    if r.get("video_id") and not c.get("already_counted")
                )
                if _uncounted > 0:
                    increment_usage(_user_id, _uncounted)
            except Exception:
                pass

    s["generated_clips"] = []
    s["raw_path"]        = None
    s["sched_pending"]   = None
    s.results            = results


# ══════════════════════════════════════════════════════════
# ルーティング（全関数定義後に実行）
# ══════════════════════════════════════════════════════════

# ① ログアウト後の Cookie クリア
if s.get("_clearing_cookie"):
    del st.session_state["_clearing_cookie"]
    st.session_state["_cookie_cleared"] = True  # Cookie 復元を一時無効化
    _emit_cookie_clear()
    render_login_page()
    st.stop()

# ② YouTube OAuth コールバック処理 → 未処理なら Supabase PKCE として処理
if "code" in st.query_params:
    if not _handle_oauth_callback():
        _handle_supabase_pkce_callback()

# ③ Supabase メール確認トークン処理
if "sb_access_token" in st.query_params:
    _handle_supabase_confirmation()

# ③' Google OAuth エラー表示
if "sb_auth_error" in st.query_params:
    _err = st.query_params.get("sb_auth_error", "")
    st.query_params.clear()
    st.error(f"⚠️ Googleログインエラー: {_err}")

# ④ マルチユーザー: Cookie からセッション復元 + ログインチェック
if _is_multi_user_mode():
    if not s.get("user_id") and not s.get("_cookie_cleared"):
        # Cookie から refresh_token を読んでセッション復元を試みる
        try:
            import urllib.parse
            _raw_rt = st.context.cookies.get(_COOKIE_NAME, "")
            rt = urllib.parse.unquote(_raw_rt) if _raw_rt else ""
        except Exception:
            rt = ""
        if rt:
            try:
                from core.auth import refresh_session
                result = refresh_session(rt)
            except Exception:
                result = None
            if result:
                s["user_id"]      = result["user_id"]
                s["user_email"]   = result["user_email"]
                s["_supabase_rt"] = result["refresh_token"]
                # YouTube トークンも復元
                try:
                    from core.db import get_youtube_token
                    from core.uploader import get_channel_info
                    yt = get_youtube_token(result["user_id"])
                    if yt:
                        s["yt_token"] = yt
                        ch = get_channel_info(yt)
                        if ch:
                            s["yt_channel_name"]      = ch["title"]
                            s["yt_channel_id"]        = ch["id"]
                            s["yt_channel_thumbnail"] = ch.get("thumbnail", "")
                except Exception:
                    pass
                st.rerun()
            else:
                # トークン期限切れ → Cookie クリア → ログインページへ
                _emit_cookie_clear()
                render_login_page()
                st.stop()
        else:
            render_login_page()
            st.stop()
    elif not s.get("user_id"):
        # _cookie_cleared=True かつ未ログイン → ログインページを表示
        render_login_page()
        st.stop()

# ⑤ 管理者パネル
if st.query_params.get("page") == "admin":
    if _is_admin():
        render_admin_panel()
        st.stop()
    else:
        # 非管理者はメインページへリダイレクト
        st.query_params.clear()
        st.rerun()

# ⑥ メール認証完了メッセージ
if st.session_state.pop("_email_confirmed", False):
    st.success("✅ メールアドレスを確認しました。ようこそ切り抜きくんへ！")

STEPS = {1: step1, 2: step2, 3: step3, 4: step4, 5: step5}
STEPS[s.step]()

# ⑦ Cookie に refresh_token を永続化（毎レンダリング・ログイン中のみ）
if _is_multi_user_mode() and s.get("user_id") and s.get("_supabase_rt"):
    _emit_cookie_writer(s["_supabase_rt"])

st.markdown(
    '<div class="footer">✂️ 切り抜きくん &nbsp;·&nbsp; '
    'Powered by yt-dlp / ffmpeg / YouTube Data API v3</div>',
    unsafe_allow_html=True,
)
